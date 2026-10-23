"""Write Malå files: .rad headers, .rd3/.rd7 traces, and ReflexW-ready bundles.

ReflexW imports Malå data with the "MALA RD3" import format; with the trace
headers set to RAMAC-GPS it reads the ``.cor`` with the same base name and
interpolates positions between fixes. The functions here produce exactly that
file set, with the QC'd ``.cor`` and a header that matches the data:

* :func:`export_bundle` - one recording, unchanged traces, QC'd positions.
* :func:`merge_bundles` - consecutive recordings joined into one profile.
* :func:`write_bundle`  - your own (processed) array written as a new recording.

Nothing here overwrites an existing file unless ``overwrite=True``.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .cor import has_data, read_cor_table, read_lines, write_lines
from .geo import haversine_m
from .rad import RadHeader
from .rd3 import DTYPES, Bundle, n_traces_from_size, read_traces

CHUNK_TRACES = 4096


# ---------------------------------------------------------------------------
# .rad header, formatting preserved
# ---------------------------------------------------------------------------

@dataclass
class RadFile:
    """A .rad header as ordered (key, separator, value) entries.

    Keeping the separator (``":"`` or ``": "``) and the value text means an
    unmodified header is written back byte for byte.
    """

    entries: list[tuple[str, str, str]]
    newline: str = "\r\n"

    @classmethod
    def read(cls, path: str | Path) -> "RadFile":
        text = Path(path).read_bytes().decode("ascii", errors="replace")
        newline = "\r\n" if "\r\n" in text else "\n"
        entries = []
        for line in text.splitlines():
            if ":" not in line:  # blank or odd line: keep verbatim
                entries.append((line, "", ""))
                continue
            key, _, rest = line.partition(":")
            value = rest.lstrip(" ")
            entries.append((key, ":" + " " * (len(rest) - len(value)), value))
        return cls(entries, newline)

    def get(self, key: str) -> str | None:
        key = key.upper()
        return next((v for k, _, v in self.entries if k.strip().upper() == key), None)

    def set(self, key: str, value: Any) -> None:
        if isinstance(value, bool):
            text = str(int(value))
        elif isinstance(value, (int, np.integer)):
            text = str(int(value))
        elif isinstance(value, (float, np.floating)):
            text = f"{float(value):.6f}"
        else:
            text = str(value)
        for i, (k, sep, _) in enumerate(self.entries):
            if k.strip().upper() == key.upper():
                self.entries[i] = (k, sep, text)
                return
        self.entries.append((key.upper(), ":", text))

    def header(self) -> RadHeader:
        from .rad import read_rad_text

        return RadHeader(read_rad_text(self.text()))

    def text(self) -> str:
        return "".join(f"{k}{sep}{v}{self.newline}" for k, sep, v in self.entries)

    def write(self, path: str | Path, overwrite: bool = False) -> Path:
        path = Path(path)
        _check_free(path, overwrite)
        path.write_bytes(self.text().encode("ascii", errors="replace"))
        return path


def _fix_trace_count(rad: RadFile, n_traces: int) -> bool:
    """Set LAST TRACE (and STOP POSITION for time-triggered data) to ``n_traces``.

    Returns True if the header changed. A header that already has the right
    LAST TRACE is left untouched (Malå's own STOP POSITION is more precise
    than n_traces x TIME INTERVAL).
    """
    if rad.get("LAST TRACE") == str(n_traces):
        return False
    before = rad.text()
    rad.set("LAST TRACE", n_traces)
    hdr = rad.header()
    if hdr.trace_interval_s and rad.get("STOP POSITION") is not None:
        rad.set("STOP POSITION", n_traces * hdr.trace_interval_s)
    return rad.text() != before


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------

def _to_int(a: np.ndarray, dtype: np.dtype, fit: str, peak: float | None = None) -> np.ndarray:
    info = np.iinfo(dtype)
    if np.issubdtype(a.dtype, np.integer) and (a.size == 0 or (a.min() >= info.min and a.max() <= info.max)):
        return a.astype(dtype, copy=False)
    a = np.nan_to_num(np.asarray(a, dtype=np.float64))
    if fit == "scale":
        if peak:
            a = a * (info.max / peak)
    a = np.rint(a)
    if fit == "clip":
        a = np.clip(a, info.min, info.max)
    elif a.size and (a.min() < info.min or a.max() > info.max):
        raise ValueError(
            f"Values {a.min():.0f}..{a.max():.0f} do not fit {np.dtype(dtype).name}; "
            "use fit='clip' or fit='scale'"
        )
    return a.astype(dtype)


def write_traces(path: str | Path, data: np.ndarray, fit: str = "error", overwrite: bool = False) -> Path:
    """Write an array of shape (n_traces, n_samples) as .rd3 (int16) or .rd7 (int32).

    ``fit`` controls float or out-of-range input: ``"error"`` (default),
    ``"clip"`` to the integer range, or ``"scale"`` so the peak absolute
    value maps to the integer maximum (relative amplitudes are kept, absolute
    ones are not).
    """
    path = Path(path)
    if path.suffix.lower() not in DTYPES:
        raise ValueError(f"Extension must be one of {sorted(DTYPES)}")
    if fit not in {"error", "clip", "scale"}:
        raise ValueError("fit must be 'error', 'clip' or 'scale'")
    data = np.asarray(data)
    if data.ndim != 2:
        raise ValueError("data must be 2-D: (n_traces, n_samples)")
    _check_free(path, overwrite)
    dtype = np.dtype(DTYPES[path.suffix.lower()])
    peak = float(np.nanmax(np.abs(data))) if fit == "scale" and data.size else None
    with open(path, "wb") as fh:
        for i in range(0, data.shape[0], CHUNK_TRACES):
            fh.write(_to_int(data[i:i + CHUNK_TRACES], dtype, fit, peak).tobytes())
    return path


def _check_free(path: Path, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} exists (use overwrite=True / --overwrite)")


# ---------------------------------------------------------------------------
# Bundles
# ---------------------------------------------------------------------------

def write_bundle(
    out_dir: str | Path,
    base: str,
    data: np.ndarray,
    template_rad: str | Path | RadFile,
    cor_lines: Sequence[str] | None = None,
    fit: str = "error",
    ext: str = ".rd3",
    overwrite: bool = False,
) -> dict[str, Path]:
    """Write ``data`` (n_traces, n_samples) as a new Malå recording.

    The header is copied from ``template_rad`` with SAMPLES, LAST TRACE,
    STOP POSITION and TIMEWINDOW updated to match the data. ``cor_lines``
    (e.g. from a QC'd .cor) are written as ``base.cor`` if given.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rad = template_rad if isinstance(template_rad, RadFile) else RadFile.read(template_rad)
    rad = RadFile(list(rad.entries), rad.newline)
    n_traces, n_samples = np.asarray(data).shape
    if str(n_samples) != rad.get("SAMPLES"):
        rad.set("SAMPLES", n_samples)
        dt = rad.header().sample_interval_ns
        if dt:
            rad.set("TIMEWINDOW", n_samples * dt)
    _fix_trace_count(rad, n_traces)

    paths = {"rd3": out_dir / f"{base}{ext}", "rad": out_dir / f"{base}.rad"}
    if cor_lines is not None:
        paths["cor"] = out_dir / f"{base}.cor"
    for p in paths.values():
        _check_free(p, overwrite)
    write_traces(paths["rd3"], data, fit=fit, overwrite=overwrite)
    rad.write(paths["rad"], overwrite=overwrite)
    if cor_lines is not None:
        write_lines(paths["cor"], list(cor_lines))
    return paths


def _cor_source(base: str, raw_dir: Path, qc_dir: Path | None) -> tuple[Path | None, str]:
    if qc_dir is not None and (qc_dir / f"{base}.cor").exists():
        return qc_dir / f"{base}.cor", "qc"
    raw = raw_dir / f"{base}.cor"
    if raw.exists() and has_data(raw):
        return raw, "raw (not QC'd)"
    return None, "none"


@dataclass
class ExportResult:
    base: str
    n_traces: int
    header_fixed: bool
    cor: str
    files: dict[str, Path] = field(default_factory=dict)


def export_bundle(
    base: str, raw_dir: str | Path, qc_dir: str | Path | None, out_dir: str | Path, overwrite: bool = False
) -> ExportResult:
    """Copy one recording to ``out_dir`` with the QC'd .cor and a corrected header."""
    raw_dir, out_dir = Path(raw_dir), Path(out_dir)
    qc_dir = Path(qc_dir) if qc_dir else None
    out_dir.mkdir(parents=True, exist_ok=True)
    b = Bundle.find(base, raw_dir)
    rad = RadFile.read(b.rad_path)
    n_traces = n_traces_from_size(b.data_path, rad.header().samples)
    fixed = _fix_trace_count(rad, n_traces)

    files = {"rd3": out_dir / b.data_path.name, "rad": out_dir / f"{base}.rad"}
    cor_path, cor_kind = _cor_source(base, raw_dir, qc_dir)
    if cor_path:
        files["cor"] = out_dir / f"{base}.cor"
    mrk = raw_dir / f"{base}.mrk"
    if mrk.exists():
        files["mrk"] = out_dir / mrk.name
    for p in files.values():
        _check_free(p, overwrite)

    shutil.copyfile(b.data_path, files["rd3"])
    rad.write(files["rad"], overwrite=True)
    if cor_path:
        shutil.copyfile(cor_path, files["cor"])
    if "mrk" in files:
        shutil.copyfile(mrk, files["mrk"])
    return ExportResult(base, n_traces, fixed, cor_kind, files)


@dataclass
class Junction:
    after: str
    before: str
    gap_s: float | None
    gap_m: float | None


@dataclass
class MergeResult:
    name: str
    n_traces: int
    parts: list[dict]
    junctions: list[Junction]
    files: dict[str, Path]


def default_merge_name(bases: Sequence[str]) -> str:
    """DAT_0032_A1 + DAT_0033_A1 -> DAT_0032_0033_A1."""
    pat = re.compile(r"^(.*?)(\d+)(_[A-Za-z0-9]+)?$")
    m0, m1 = pat.match(bases[0]), pat.match(bases[-1])
    if m0 and m1 and m0.group(1) == m1.group(1) and m0.group(3) == m1.group(3):
        return f"{m0.group(1)}{m0.group(2)}_{m1.group(2)}{m0.group(3) or ''}"
    return "merged"


def merge_bundles(
    bases: Sequence[str],
    raw_dir: str | Path,
    qc_dir: str | Path | None,
    out_dir: str | Path,
    name: str | None = None,
    overwrite: bool = False,
) -> MergeResult:
    """Join consecutive recordings into one profile, in the order given.

    All parts must share SAMPLES, sampling FREQUENCY, TIME INTERVAL, antenna
    and data type. Traces are appended end to end (no gap traces); .cor trace
    numbers are offset accordingly, using the QC'd .cor where available.
    """
    if len(bases) < 2:
        raise ValueError("Give at least two recordings to merge")
    raw_dir, out_dir = Path(raw_dir), Path(out_dir)
    qc_dir = Path(qc_dir) if qc_dir else None
    name = name or default_merge_name(bases)
    bundles = [Bundle.find(b, raw_dir) for b in bases]
    headers = [RadHeader.from_file(b.rad_path) for b in bundles]

    ref, h0 = bundles[0], headers[0]
    for b, h in zip(bundles[1:], headers[1:]):
        problems = []
        if b.data_path.suffix.lower() != ref.data_path.suffix.lower():
            problems.append("data type (.rd3/.rd7)")
        if h.samples != h0.samples:
            problems.append(f"SAMPLES {h.samples} vs {h0.samples}")
        if abs((h.sampling_frequency_mhz or 0) - (h0.sampling_frequency_mhz or 0)) > 1e-3:
            problems.append(f"FREQUENCY {h.sampling_frequency_mhz} vs {h0.sampling_frequency_mhz}")
        if abs((h.trace_interval_s or 0) - (h0.trace_interval_s or 0)) > 1e-4:
            problems.append(f"TIME INTERVAL {h.trace_interval_s} vs {h0.trace_interval_s}")
        if h.antenna_mhz != h0.antenna_mhz:
            problems.append(f"antenna {h.antenna_mhz} vs {h0.antenna_mhz} MHz")
        if problems:
            raise ValueError(f"Cannot merge {b.base} with {ref.base}: " + "; ".join(problems))

    out_dir.mkdir(parents=True, exist_ok=True)
    files = {"rd3": out_dir / f"{name}{ref.data_path.suffix.lower()}", "rad": out_dir / f"{name}.rad",
             "cor": out_dir / f"{name}.cor"}
    mrk = raw_dir / f"{bases[0]}.mrk"
    if mrk.exists():
        files["mrk"] = out_dir / f"{name}.mrk"
    for p in files.values():
        _check_free(p, overwrite)

    parts, cor_out, offset = [], [], 0
    ends: list[tuple] = []  # (first_fix, last_fix) per part, as (lat, lon, datetime)
    with open(files["rd3"], "wb") as fh:
        for b, h in zip(bundles, headers):
            traces = read_traces(b.data_path, h.samples)
            for i in range(0, traces.shape[0], CHUNK_TRACES):
                fh.write(np.ascontiguousarray(traces[i:i + CHUNK_TRACES]).tobytes())
            cor_path, cor_kind = _cor_source(b.base, raw_dir, qc_dir)
            if cor_path:
                for line in read_lines(cor_path):
                    f = line.split("\t")
                    if line.strip() and f[0].strip().isdigit():
                        f[0] = str(int(f[0]) + offset)
                        cor_out.append("\t".join(f))
                ends.append(_first_last_fix(cor_path))
            else:
                ends.append((None, None))
            parts.append({"base": b.base, "n_traces": traces.shape[0], "first_trace": offset + 1, "cor": cor_kind})
            offset += traces.shape[0]

    rad = RadFile.read(ref.rad_path)
    _fix_trace_count(rad, offset)
    rad.write(files["rad"], overwrite=True)
    write_lines(files["cor"], cor_out)
    if "mrk" in files:
        shutil.copyfile(mrk, files["mrk"])

    junctions = []
    for (b1, (_, last)), (b2, (first, _)) in zip(zip(bases, ends), zip(bases[1:], ends[1:])):
        gap_s = gap_m = None
        if last and first:
            gap_m = haversine_m(last[0], last[1], first[0], first[1])
            if last[2] is not None and first[2] is not None:
                gap_s = (first[2] - last[2]).total_seconds()
        junctions.append(Junction(b1, b2, gap_s, gap_m))
    return MergeResult(name, offset, parts, junctions, files)


def _first_last_fix(cor_path: Path):
    import pandas as pd

    df = read_cor_table(cor_path)
    if df.empty:
        return (None, None)
    t = pd.to_datetime(df["date"] + " " + df["time"], errors="coerce", format="%Y-%m-%d %H:%M:%S")

    def fix(i):
        ts = t.iloc[i]
        return (df["lat"].iloc[i], df["lon"].iloc[i], None if pd.isna(ts) else ts.to_pydatetime())

    return (fix(0), fix(-1))
