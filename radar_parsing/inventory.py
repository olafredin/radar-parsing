"""Inventory of a raw data folder, and SHA-256 manifests for transferring it."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

from .cor import read_cor_table
from .geo import track_length_m
from .rad import RadHeader
from .rd3 import DTYPES, n_traces_from_size

MANIFEST_NAME = "MANIFEST.sha256"


def _bases(raw_dir: Path) -> list[str]:
    return sorted({p.stem for p in raw_dir.iterdir() if p.suffix.lower() == ".rad"})


def inventory(raw_dir: str | Path, qc_dir: str | Path | None = None) -> pd.DataFrame:
    """One row per recording (per .rad file) with header info, trace counts and GNSS coverage.

    If ``qc_dir`` holds a QC'd .cor for the recording, along-track length and
    time span are computed from it; otherwise from the raw .cor.
    """
    raw_dir = Path(raw_dir)
    qc_dir = Path(qc_dir) if qc_dir else None
    rows = []
    for base in _bases(raw_dir):
        hdr = RadHeader.from_file(raw_dir / f"{base}.rad")
        row: dict = {
            "file_base": base,
            "antenna_mhz": hdr.antenna_mhz,
            "samples": hdr.samples,
            "sampling_frequency_mhz": hdr.sampling_frequency_mhz,
            "time_window_ns": hdr.time_window_ns,
            "trace_interval_s": hdr.trace_interval_s,
            "last_trace_header": hdr.last_trace,
        }
        data = next((raw_dir / f"{base}{e}" for e in DTYPES if (raw_dir / f"{base}{e}").exists()), None)
        row["data_file"] = data.name if data else None
        try:
            row["n_traces"] = n_traces_from_size(data, hdr.samples) if data else None
            row["data_ok"] = data is not None   # present, size consistent with SAMPLES
        except ValueError:
            row["n_traces"], row["data_ok"] = None, False
        # In 2025/26 some headers have a stale LAST TRACE; the .rd3 size and the
        # .cor trace numbers agree with each other, so the header is the odd one out.
        row["header_last_trace_ok"] = row["n_traces"] is not None and row["n_traces"] == hdr.last_trace

        qc_cor = qc_dir / f"{base}.cor" if qc_dir else None
        raw_cor = raw_dir / f"{base}.cor"
        cor_path = qc_cor if qc_cor and qc_cor.exists() else (raw_cor if raw_cor.exists() else None)
        row["gnss_source"] = cor_path.parent.name if cor_path else None
        cor = read_cor_table(cor_path) if cor_path and cor_path.stat().st_size else pd.DataFrame()
        row["gnss_fixes"] = len(cor)
        row["cor_last_trace"] = int(cor["trace_id"].max()) if len(cor) else None
        if len(cor):
            t = pd.to_datetime(cor["date"] + " " + cor["time"], errors="coerce").dropna()
            row["start"] = t.min() if len(t) else None
            row["end"] = t.max() if len(t) else None
            row["length_km"] = round(track_length_m(cor["lat"], cor["lon"]) / 1000, 3)
            # Smooth GNSS excursions (not caught by the spike filter) show up here
            row["elev_min"] = round(float(cor["elev"].min()), 3)
            row["elev_max"] = round(float(cor["elev"].max()), 3)
        rows.append(row)
    return pd.DataFrame(rows)


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            h.update(block)
    return h.hexdigest()


def write_manifest(raw_dir: str | Path, out: str | Path | None = None) -> Path:
    """Write ``sha256sum``-compatible checksums for every file in ``raw_dir``."""
    raw_dir = Path(raw_dir)
    out = Path(out) if out else raw_dir / MANIFEST_NAME
    files = sorted(p for p in raw_dir.iterdir() if p.is_file() and p.name != MANIFEST_NAME)
    with open(out, "w", newline="\n") as fh:
        for p in files:
            fh.write(f"{sha256(p)}  {p.name}\n")
    return out


def check_manifest(raw_dir: str | Path, manifest: str | Path | None = None) -> list[tuple[str, str]]:
    """Verify files against a manifest. Returns [(filename, problem)] — empty means all OK."""
    raw_dir = Path(raw_dir)
    manifest = Path(manifest) if manifest else raw_dir / MANIFEST_NAME
    problems = []
    for line in manifest.read_text().splitlines():
        if not line.strip():
            continue
        digest, name = line.split(maxsplit=1)
        name = name.lstrip("*").strip()
        p = raw_dir / name
        if not p.exists():
            problems.append((name, "missing"))
        elif sha256(p) != digest:
            problems.append((name, "checksum mismatch"))
    return problems
