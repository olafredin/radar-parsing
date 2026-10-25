"""QC of raw .cor files: reject bad GNSS fixes and interpolate across them.

For each raw .cor file the steps are:

1. Repair garbled lines (see :func:`radar_parsing.cor.normalize_fields`).
2. Flag a fix as invalid if lat/lon are unparseable, exactly zero (receiver
   dropout), outside the survey bounding box, or carry the wrong hemisphere
   letter.
3. Timestamps that are unparseable, or disagree with the trace clock
   (trace number x .rad TIME INTERVAL), are set aside.
4. "Teleport" test: walking forward from the last good fix, flag points
   implying an impossible speed. Elapsed time is the larger of the trace-clock
   and timestamp differences (the latter catches recording pauses). Without
   any usable time, an impossible step length is used instead. A run of
   ``relock_after`` mutually consistent rejected fixes is re-accepted, so a
   bad anchor cannot lock out the rest of the file.
5. Linearly interpolate lat/lon across all flagged fixes (in line order),
   holding the first/last good value at the ends. Recording pauses longer
   than ``pause_s`` (found from the trace clock) split the file into
   stretches, and interpolation never crosses a pause: the sledge may have
   moved while trace numbering stood still.
6. Elevation: unrecoverable values, and spikes deviating more than
   ``elev_max_dev_m`` from a running median, are interpolated the same way.
7. Write the corrected file with the original column layout and precision.
   Trace numbers, dates and times are written unchanged.

Files with no valid fix at all (test recordings, no GNSS) are skipped.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
import pandas as pd

from .config import BBox, Config, CorQCConfig
from .cor import (
    REPAIR_ELEV_BAD,
    REPAIR_FUSED,
    REPAIR_SHIFTED,
    REPAIR_UNIT,
    decimals,
    looks_like_float,
    normalize_fields,
    read_lines,
    write_lines,
)
from .geo import haversine_m, to_signed, to_unsigned
from .rad import RadHeader


@dataclass
class CorQCResult:
    file: str
    status: str               # "written", "empty", "no_valid_fix"
    n_lines: int = 0
    n_invalid_fix: int = 0    # unparseable / zero / outside bbox / wrong hemisphere
    n_outside_bbox: int = 0   # parseable fixes outside the bbox or with the wrong hemisphere
    max_outside_run: int = 0  # longest run of those; long runs mean the bbox is wrong
    n_time_garbled: int = 0   # timestamps unparseable or off the trace clock (not used)
    n_teleport: int = 0
    n_interpolated: int = 0   # n_invalid_fix + n_teleport
    n_pauses: int = 0         # recording pauses (no interpolation across them)
    n_unit_repaired: int = 0
    n_elev_fused: int = 0
    n_elev_shifted: int = 0
    n_elev_spikes: int = 0
    n_elev_interpolated: int = 0   # unrecoverable + spikes
    # One FixFlags per written line (not part of the per-file report)
    flags: list["FixFlags"] = field(default_factory=list, repr=False)

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("flags")
        return d


@dataclass
class FixFlags:
    """What fix-cor did to one GNSS fix. Empty strings mean 'untouched'."""

    trace_id: Optional[int]
    position: str      # "", "invalid" (unparseable/zero/bbox/hemisphere), "teleport"
    elevation: str     # "", "repaired" (un-fused/un-shifted), "unparseable", "spike"
    time_ignored: bool  # timestamp unparseable or off the trace clock


def interp_fill(values: Sequence[Optional[float]], valid: Sequence[bool]) -> list[float]:
    """Linear interpolation across invalid entries (by index).

    Leading/trailing invalid runs take the first/last valid value.
    """
    n = len(values)
    idx = [i for i, ok in enumerate(valid) if ok and values[i] is not None]
    if not idx:
        raise ValueError("No valid values to interpolate.")
    out: list[Optional[float]] = [None] * n
    for i in idx:
        out[i] = float(values[i])  # type: ignore[arg-type]
    for i in range(idx[0]):
        out[i] = out[idx[0]]
    for a, b in zip(idx, idx[1:]):
        gap = b - a
        va, vb = out[a], out[b]
        for k in range(1, gap):
            t = k / gap
            out[a + k] = (1 - t) * va + t * vb  # type: ignore[operator]
    for i in range(idx[-1] + 1, n):
        out[i] = out[idx[-1]]
    return [float(x) for x in out]  # type: ignore[arg-type]


def parse_datetime(date_str: str, time_str: str) -> Optional[datetime]:
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def teleport_flags(
    lat: Sequence[Optional[float]],
    lon: Sequence[Optional[float]],
    valid: Sequence[bool],
    times: Sequence[Optional[datetime]],
    cfg: CorQCConfig,
    traces: Sequence[Optional[int]] | None = None,
    trace_interval_s: float | None = None,
) -> list[bool]:
    """Return a new validity list with teleport jumps marked invalid.

    Each valid fix is compared with the last fix that survived (the anchor).
    The elapsed time is the larger of

    * trace-number difference x ``trace_interval_s`` (robust to garbled
      timestamps, but blind to recording pauses), and
    * the timestamp difference (sees pauses, but timestamps are often garbled).

    The test is speed > ``max_speed_mps``; if neither time is usable, step
    length > ``max_step_m``. If ``relock_after`` consecutive rejected fixes are
    consistent with each other, the anchor was wrong or the track really
    jumped: they are accepted and become the new anchor.
    """
    out = list(valid)
    if not cfg.teleport_qc:
        return out

    def elapsed(j: int, i: int) -> Optional[float]:
        cands = []
        if traces is not None and trace_interval_s and traces[i] is not None and traces[j] is not None:
            cands.append((traces[i] - traces[j]) * trace_interval_s)  # type: ignore[operator]
        if times[i] is not None and times[j] is not None:
            cands.append((times[i] - times[j]).total_seconds())  # type: ignore[operator]
        cands = [c for c in cands if c >= cfg.min_dt_s]
        return max(cands) if cands else None

    def plausible(j: int, i: int) -> bool:
        d = haversine_m(lat[j], lon[j], lat[i], lon[i])  # type: ignore[arg-type]
        dt = elapsed(j, i)
        return d / dt <= cfg.max_speed_mps if dt is not None else d <= cfg.max_step_m

    prev: Optional[int] = None
    run: list[int] = []  # consecutive rejected fixes that agree with each other
    for i in range(len(out)):
        if not out[i] or lat[i] is None or lon[i] is None:
            continue
        if prev is None:
            prev = i
            continue
        if plausible(prev, i):
            prev, run = i, []
            continue
        out[i] = False
        if cfg.relock_after > 0:
            run = run + [i] if run and plausible(run[-1], i) else [i]
            if len(run) >= cfg.relock_after:
                for k in run:
                    out[k] = True
                prev, run = i, []
    return out


def garbled_times(
    times: Sequence[Optional[datetime]],
    traces: Sequence[Optional[int]],
    trace_interval_s: float,
    tol_s: float,
    window: int = 11,
) -> list[bool]:
    """Flag timestamps that disagree with the trace clock.

    offset = time - trace * trace_interval is constant while recording and
    steps up at a pause; a garbled timestamp is an isolated outlier. Compared
    with a centred running median (which keeps steps sharp).
    """
    idx = [i for i, (t, k) in enumerate(zip(times, traces)) if t is not None and k is not None]
    flags = [False] * len(times)
    if len(idx) < 3:
        return flags
    t0 = times[idx[0]]
    off = pd.Series([(times[i] - t0).total_seconds() - traces[i] * trace_interval_s for i in idx])  # type: ignore[operator]
    med = off.rolling(window, center=True, min_periods=1).median()
    for i, bad in zip(idx, ((off - med).abs() > tol_s).to_numpy()):
        flags[i] = bool(bad)
    return flags


def pause_breaks(
    times: Sequence[Optional[datetime]],
    traces: Sequence[Optional[int]],
    trace_interval_s: float,
    pause_s: float,
) -> list[int]:
    """Line indices where a recording pause starts a new stretch.

    Trace numbering stops during a pause while the GNSS clock runs on, so the
    offset (time - trace x trace_interval) jumps by the pause length. Call
    this after :func:`garbled_times` has removed bad timestamps.
    """
    idx = [i for i, (t, k) in enumerate(zip(times, traces)) if t is not None and k is not None]
    if len(idx) < 2 or pause_s <= 0:
        return []
    t0 = times[idx[0]]
    off = [(times[i] - t0).total_seconds() - traces[i] * trace_interval_s for i in idx]  # type: ignore[operator]
    return [b for a, b, oa, ob in zip(idx, idx[1:], off, off[1:]) if ob - oa > pause_s]


def interp_fill_stretches(
    values: Sequence[Optional[float]], valid: Sequence[bool], breaks: Sequence[int]
) -> list[float]:
    """:func:`interp_fill` within each stretch between ``breaks``, never across them.

    Rejected values at the edge of a stretch are held at the nearest good value
    in that stretch. A stretch without any good value falls back to
    interpolation over the whole file.
    """
    n = len(values)
    if not breaks:
        return interp_fill(values, valid)
    whole = interp_fill(values, valid)
    out = list(whole)
    edges = [0, *sorted(breaks), n]
    for a, b in zip(edges, edges[1:]):
        if any(valid[a:b]):
            out[a:b] = interp_fill(values[a:b], valid[a:b])
    return out


def elevation_flags(elev: Sequence[Optional[float]], ok: Sequence[bool], cfg: CorQCConfig) -> list[bool]:
    """Mark elevations deviating from a centred running median as invalid."""
    out = list(ok)
    if not cfg.elev_qc or cfg.elev_max_dev_m <= 0 or not any(ok):
        return out
    s = pd.Series([e if g else np.nan for e, g in zip(elev, ok)], dtype=float)
    med = s.rolling(max(int(cfg.elev_window), 1), center=True, min_periods=1).median()
    spike = ((s - med).abs() > cfg.elev_max_dev_m).to_numpy()
    return [g and not sp for g, sp in zip(out, spike)]


def qc_cor_file(
    cor_path: str | Path,
    out_path: str | Path,
    cfg: Config | None = None,
    trace_interval_s: float | None = None,
) -> CorQCResult:
    """QC one raw .cor file and write the corrected copy to ``out_path``.

    ``trace_interval_s`` (seconds between traces, from the .rad header) lets the
    teleport test time fixes by trace number; without it only timestamps are used.
    """
    cfg = cfg or Config()
    if not cfg.cor_qc.use_trace_time:
        trace_interval_s = None
    bbox: BBox = cfg.bbox
    qc: CorQCConfig = cfg.cor_qc
    cor_path = Path(cor_path)
    res = CorQCResult(file=cor_path.name, status="empty")

    rows: list[list[str]] = []
    lat_s: list[Optional[float]] = []
    lon_s: list[Optional[float]] = []
    valid: list[bool] = []
    times: list[Optional[datetime]] = []
    traces: list[Optional[int]] = []
    elev_ok: list[bool] = []
    repairs: list[str] = []
    lat_dec: Optional[int] = None
    lon_dec: Optional[int] = None
    outside_run = 0

    for raw in read_lines(cor_path):
        norm = normalize_fields(raw)
        if norm is None:
            continue
        f = norm.fields
        rows.append(f)
        res.n_unit_repaired += norm.repair == REPAIR_UNIT
        res.n_elev_fused += norm.repair == REPAIR_FUSED
        res.n_elev_shifted += norm.repair == REPAIR_SHIFTED
        repairs.append(norm.repair)
        elev_ok.append(norm.repair != REPAIR_ELEV_BAD and looks_like_float(f[7].strip()))
        times.append(parse_datetime(f[1].strip(), f[2].strip()))
        traces.append(int(f[0]) if f[0].strip().isdigit() else None)

        lat_str, ns = f[3].strip(), f[4].strip().upper()
        lon_str, ew = f[5].strip(), f[6].strip().upper()
        try:
            lat_u, lon_u = float(lat_str), float(lon_str)
        except ValueError:
            lat_u = lon_u = 0.0
        if lat_u == 0.0 or lon_u == 0.0:  # unparseable, or receiver dropout (0/0)
            lat_s.append(None)
            lon_s.append(None)
            valid.append(False)
            continue
        la = to_signed(lat_u, ns, "S")
        lo = to_signed(lon_u, ew, "W")
        ok = bbox.contains(la, lo) and ns == qc.expected_ns and ew == qc.expected_ew
        # Garbled fixes outside the bbox come in short runs (<= 15 in 2025/26);
        # a long run is a track that really left the bbox.
        outside_run = 0 if ok else outside_run + 1
        res.n_outside_bbox += not ok
        res.max_outside_run = max(res.max_outside_run, outside_run)
        lat_s.append(la if ok else None)
        lon_s.append(lo if ok else None)
        valid.append(ok)
        if ok:
            lat_dec = decimals(lat_str) if lat_dec is None else lat_dec
            lon_dec = decimals(lon_str) if lon_dec is None else lon_dec

    res.n_lines = len(rows)
    if not rows:
        return res
    if not any(valid):
        res.status = "no_valid_fix"
        return res

    res.n_invalid_fix = valid.count(False)
    res.n_time_garbled = sum(t is None for t in times)
    if trace_interval_s:
        for i, bad in enumerate(garbled_times(times, traces, trace_interval_s, qc.time_tol_s)):
            if bad:
                times[i] = None
                res.n_time_garbled += 1
    valid2 = teleport_flags(lat_s, lon_s, valid, times, qc, traces, trace_interval_s)
    res.n_teleport = sum(a and not b for a, b in zip(valid, valid2))
    res.n_interpolated = valid2.count(False)

    breaks = pause_breaks(times, traces, trace_interval_s, qc.pause_s) if trace_interval_s else []
    res.n_pauses = len(breaks)
    lat_f = interp_fill_stretches(lat_s, valid2, breaks)
    lon_f = interp_fill_stretches(lon_s, valid2, breaks)
    lat_dec = 8 if lat_dec is None else lat_dec
    lon_dec = 8 if lon_dec is None else lon_dec

    elev_vals = [float(f[7]) if ok else None for f, ok in zip(rows, elev_ok)]
    elev_good = elevation_flags(elev_vals, elev_ok, qc)
    res.n_elev_spikes = sum(a and not b for a, b in zip(elev_ok, elev_good))
    if not all(elev_good) and any(elev_good):
        elev_f = interp_fill_stretches(elev_vals, elev_good, breaks)
        for f, ok, e in zip(rows, elev_good, elev_f):
            if not ok:
                f[7] = f"{e:.3f}"
                f[8] = "M"
        res.n_elev_interpolated = elev_good.count(False)

    out_lines = []
    for f, la, lo in zip(rows, lat_f, lon_f):
        lat_u, ns = to_unsigned(la, "S", "N")
        lon_u, ew = to_unsigned(lo, "W", "E")
        f[3], f[4] = f"{lat_u:.{lat_dec}f}", ns
        f[5], f[6] = f"{lon_u:.{lon_dec}f}", ew
        out_lines.append("\t".join(f))
    write_lines(out_path, out_lines)
    res.status = "written"

    for i in range(len(rows)):
        pos = "" if valid2[i] else ("teleport" if valid[i] else "invalid")
        if not elev_ok[i]:
            el = "unparseable"
        elif not elev_good[i]:
            el = "spike"
        elif repairs[i] in (REPAIR_FUSED, REPAIR_SHIFTED):
            el = "repaired"
        else:
            el = ""
        res.flags.append(FixFlags(traces[i], pos, el, times[i] is None))
    return res


def flags_table(results: Sequence[CorQCResult]) -> pd.DataFrame:
    """All per-fix flags from a fix-cor run as one table (one row per GNSS fix)."""
    rows = [
        {"file_base": Path(r.file).stem, "trace_id": f.trace_id, "position": f.position,
         "elevation": f.elevation, "time_ignored": f.time_ignored}
        for r in results for f in r.flags
    ]
    return pd.DataFrame(rows, columns=["file_base", "trace_id", "position", "elevation", "time_ignored"])


def qc_directory(raw_dir: str | Path, out_dir: str | Path, cfg: Config | None = None) -> list[CorQCResult]:
    """Run :func:`qc_cor_file` on every .cor file in ``raw_dir``.

    A QC'd copy left over from an earlier run is removed when the file is now
    skipped, so later steps do not pick up stale positions.
    """
    raw_dir, out_dir = Path(raw_dir), Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cor_files = sorted(p for p in raw_dir.iterdir() if p.is_file() and p.suffix.lower() == ".cor")
    results = []
    for p in cor_files:
        rad = p.with_suffix(".rad")
        dt = RadHeader.from_file(rad).trace_interval_s if rad.exists() else None
        res = qc_cor_file(p, out_dir / p.name, cfg, trace_interval_s=dt)
        if res.status != "written":
            (out_dir / p.name).unlink(missing_ok=True)
        results.append(res)
    return results
