#!/usr/bin/env python3
"""
Parse, explore, and plot a MALÅ RD3 bundle using RAD + COR.

Assumed project layout:
project_root/
  scripts/
    Parse_rd3_bundle.py   <-- this script
  output_cor_qc/
    DAT_0013_A1.rd3
    DAT_0013_A1.rad
    DAT_0013_A1.cor
    (optional) DAT_0013_A1.mrk

Run:
  python scripts/Parse_rd3_bundle.py --base DAT_0013_A1
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Dict, Tuple, Optional

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


# -------------------------
# Paths (Option 1)
# -------------------------
def project_paths() -> Tuple[Path, Path, Path]:
    script_dir = Path(__file__).resolve().parent
    project_dir = script_dir.parent
    data_dir = project_dir / "output_cor_qc"
    return script_dir, project_dir, data_dir


# -------------------------
# RAD parsing
# -------------------------
def read_rad_simple(rad_path: str | Path) -> Dict[str, object]:
    """
    Read MALÅ .rad header into a dict.

    Handles:
      KEY:VALUE
      KEY: VALUE
    """
    meta: Dict[str, object] = {}
    for line in Path(rad_path).read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith(("...", "#", ";", "//")):
            continue
        if ":" not in line:
            continue

        k, v = line.split(":", 1)
        k = k.strip().upper()
        v = v.strip()

        # try numeric
        try:
            if "." in v or "e" in v.lower():
                meta[k] = float(v)
            else:
                meta[k] = int(v)
        except ValueError:
            meta[k] = v

    return meta


def build_time_axis(rad: Dict[str, object], n_samples: int) -> Tuple[Optional[np.ndarray], str]:
    """
    Build per-sample time axis.

    MALÅ headers can be inconsistent in the units of TIME INTERVAL vs TIMEWINDOW.
    The most robust approach for plotting is to use TIMEWINDOW when present:
      t = linspace(0, TIMEWINDOW, n_samples)

    Returns (t, label) where label is the y-axis label.
    """
    # TIMEWINDOW appears in your header as TIMEWINDOW (no space)
    tw_key = "TIMEWINDOW"
    dt_key = "TIME INTERVAL"

    if tw_key in rad:
        tw = float(rad[tw_key])
        t = np.linspace(0.0, tw, n_samples, endpoint=False)
        # Unit is not guaranteed, so keep label conservative.
        # If you decide TW really is ns for your system, change to "Two-way time (ns)".
        return t, "Two-way time (TIMEWINDOW units)"

    if dt_key in rad:
        dt = float(rad[dt_key])
        t = np.arange(n_samples, dtype=float) * dt
        return t, "Two-way time (TIME INTERVAL units)"

    return None, "Sample index"


def rad_sanity_checks(rad: Dict[str, object], n_samples: int) -> None:
    """
    Print a couple of consistency checks to help you decide on units.
    """
    dt = float(rad.get("TIME INTERVAL", np.nan))
    tw = float(rad.get("TIMEWINDOW", np.nan))

    if np.isfinite(dt):
        implied_tw = dt * n_samples
        print(f"RAD check: TIME INTERVAL * SAMPLES = {implied_tw:.6f} (units?)")

    if np.isfinite(tw) and np.isfinite(dt):
        ratio = (tw / (dt * n_samples)) if (dt * n_samples) != 0 else np.nan
        print(f"RAD check: TIMEWINDOW / (TIME INTERVAL*SAMPLES) = {ratio:.6f} (should be ~1 if consistent)")


# -------------------------
# RD3 reading
# -------------------------
def guess_endianness(rd3_path: str | Path, n_samples: int) -> str:
    """
    Compare a first-trace statistic under little vs big endian.
    Prefer the interpretation with a more plausible distribution.
    """
    rd3_path = Path(rd3_path)
    raw = np.fromfile(rd3_path, dtype=np.uint8, count=n_samples * 2)
    if raw.size < n_samples * 2:
        return "<"

    le = raw.view("<i2")
    be = raw.view(">i2")

    # Use robust spread metric
    le_span = float(np.nanpercentile(le, 99) - np.nanpercentile(le, 1))
    be_span = float(np.nanpercentile(be, 99) - np.nanpercentile(be, 1))

    return "<" if le_span <= be_span else ">"


def read_rd3_memmap(rd3_path: str | Path, n_samples: int, endian: str = "<") -> Tuple[np.memmap, int]:
    rd3_path = Path(rd3_path)
    bytes_per_trace = n_samples * 2
    file_size = rd3_path.stat().st_size

    if file_size % bytes_per_trace != 0:
        raise ValueError(
            f"RD3 size ({file_size}) not divisible by bytes/trace ({bytes_per_trace}). "
            f"Check SAMPLES and endianness."
        )

    n_traces = file_size // bytes_per_trace
    dtype = np.dtype(endian + "i2")
    data = np.memmap(rd3_path, dtype=dtype, mode="r", shape=(n_traces, n_samples))
    return data, int(n_traces)


# -------------------------
# COR parsing + nav interpolation
# -------------------------
def _to_float_or_nan(x: str) -> float:
    try:
        return float(x)
    except Exception:
        return float("nan")


def read_cor_qc(cor_path: str | Path) -> pd.DataFrame:
    """
    Read a QC-corrected MALÅ .cor file (tab-delimited).
    Tolerates qual='.' by setting NaN.
    """
    rows = []
    for line in Path(cor_path).read_text(encoding="utf-8", errors="ignore").splitlines():
        parts = line.strip().split("\t")
        if len(parts) < 10:
            continue

        trace = int(parts[0])
        date = parts[1]
        time = parts[2]

        lat = _to_float_or_nan(parts[3])
        lat_hemi = parts[4].upper()

        lon = _to_float_or_nan(parts[5])
        lon_hemi = parts[6].upper()

        elev = _to_float_or_nan(parts[7])
        qual = _to_float_or_nan(parts[9])

        if lat_hemi == "S":
            lat = -lat
        if lon_hemi == "W":
            lon = -lon

        rows.append((trace, f"{date} {time}", lat, lon, elev, qual))

    df = pd.DataFrame(rows, columns=["trace", "datetime", "lat", "lon", "elev_m", "qual"])
    df = df.sort_values("trace").drop_duplicates("trace").reset_index(drop=True)
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

    df = df[np.isfinite(df["lat"]) & np.isfinite(df["lon"])].reset_index(drop=True)
    return df


def interpolate_nav_to_traces(
    cor: pd.DataFrame,
    n_traces: int,
    allow_extrapolation: bool = True
) -> pd.DataFrame:
    trace_all = np.arange(1, n_traces + 1, dtype=int)

    cor_trace = cor["trace"].to_numpy(dtype=float)
    cor_lat = cor["lat"].to_numpy(dtype=float)
    cor_lon = cor["lon"].to_numpy(dtype=float)
    cor_elev = cor["elev_m"].to_numpy(dtype=float)

    if allow_extrapolation:
        lat_i = np.interp(trace_all, cor_trace, cor_lat)
        lon_i = np.interp(trace_all, cor_trace, cor_lon)
        elev_i = np.interp(trace_all, cor_trace, cor_elev)
    else:
        valid = (trace_all >= cor_trace.min()) & (trace_all <= cor_trace.max())
        lat_i = np.full(trace_all.shape, np.nan, dtype=float)
        lon_i = np.full(trace_all.shape, np.nan, dtype=float)
        elev_i = np.full(trace_all.shape, np.nan, dtype=float)
        lat_i[valid] = np.interp(trace_all[valid], cor_trace, cor_lat)
        lon_i[valid] = np.interp(trace_all[valid], cor_trace, cor_lon)
        elev_i[valid] = np.interp(trace_all[valid], cor_trace, cor_elev)

    return pd.DataFrame({"trace": trace_all, "lat": lat_i, "lon": lon_i, "elev_m": elev_i})


# -------------------------
# Simple processing
# -------------------------
def dewow_mean_per_trace(A: np.ndarray) -> np.ndarray:
    return A - A.mean(axis=1, keepdims=True)


def gain_time_power(A: np.ndarray, t: Optional[np.ndarray], power: float = 1.0) -> np.ndarray:
    if power <= 0:
        return A
    if t is None:
        g = (np.arange(A.shape[1], dtype=float) + 1.0) ** power
    else:
        g = (t - float(t[0]) + 1.0) ** power
    return A * g[np.newaxis, :]


# -------------------------
# Plotting
# -------------------------
def plot_radargram(A: np.ndarray, t: Optional[np.ndarray], ylabel: str, title: str, max_traces: int = 2000) -> None:
    n_traces, _ = A.shape

    if n_traces > max_traces:
        idx = np.linspace(0, n_traces - 1, max_traces).astype(int)
        A_plot = A[idx, :]
        xlabel = f"Trace (downsampled {max_traces}/{n_traces})"
    else:
        A_plot = A
        xlabel = "Trace"

    plt.figure()
    if t is None:
        plt.imshow(A_plot.T, aspect="auto", origin="upper")
    else:
        extent = [0, A_plot.shape[0], float(t[-1]), float(t[0])]
        plt.imshow(A_plot.T, aspect="auto", origin="upper", extent=extent)
    plt.xlabel(xlabel)
    plt.ylabel(ylabel if t is not None else "Sample")
    plt.title(title)
    plt.tight_layout()


def plot_track(nav: pd.DataFrame, title: str) -> None:
    plt.figure()
    plt.plot(nav["lon"].to_numpy(), nav["lat"].to_numpy())
    plt.xlabel("Longitude (deg)")
    plt.ylabel("Latitude (deg)")
    plt.title(title)
    plt.axis("equal")
    plt.tight_layout()


def print_summary(base: str, rad: Dict[str, object], n_traces: int, n_samples: int, cor: pd.DataFrame, endian: str) -> None:
    print("\n--- Bundle summary ---")
    print(f"Base: {base}")
    print(f"SAMPLES: {n_samples}")
    print(f"RD3 traces: {n_traces}")
    print(f"Endianness: {'little' if endian=='<' else 'big'} ({endian})")
    print(f"RAD TIME INTERVAL: {rad.get('TIME INTERVAL', 'N/A')}")
    print(f"RAD TIMEWINDOW: {rad.get('TIMEWINDOW', 'N/A')}")
    print(f"RAD FREQUENCY: {rad.get('FREQUENCY', 'N/A')}")
    print(f"RAD ANTENNAS: {rad.get('ANTENNAS', 'N/A')}")
    print(f"COR rows: {len(cor)} | qual NaNs: {cor['qual'].isna().sum()}")
    print(f"COR trace range: {cor['trace'].min()} .. {cor['trace'].max()}")
    if "LAST TRACE" in rad:
        print(f"RAD LAST TRACE: {rad.get('LAST TRACE')} (header)")
    rad_sanity_checks(rad, n_samples)


# -------------------------
# Main
# -------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Parse and plot MALÅ RD3 using RAD + COR.")
    parser.add_argument("--base", required=True, help="Base name, e.g. DAT_0013_A1")
    parser.add_argument("--data-dir", default=None, help="Override data directory (default: project_root/output_cor_qc)")
    parser.add_argument("--no-extrapolation", action="store_true", help="Do not extrapolate nav outside COR trace range.")
    parser.add_argument("--gain", type=float, default=0.0, help="Time-power gain exponent (0 disables).")
    parser.add_argument("--max-traces-plot", type=int, default=2000, help="Max traces to show in radargram plot.")
    args = parser.parse_args()

    _, _, default_data_dir = project_paths()
    data_dir = Path(args.data_dir) if args.data_dir else default_data_dir

    base = args.base
    rad_path = data_dir / f"{base}.rad"
    rd3_path = data_dir / f"{base}.rd3"
    cor_path = data_dir / f"{base}.cor"

    for p in (rad_path, rd3_path, cor_path):
        if not p.exists():
            raise FileNotFoundError(f"Missing required file: {p}")

    rad = read_rad_simple(rad_path)
    if "SAMPLES" not in rad:
        raise ValueError(f"RAD header missing SAMPLES: {rad_path}")
    n_samples = int(rad["SAMPLES"])

    endian = guess_endianness(rd3_path, n_samples=n_samples)
    data_mm, n_traces = read_rd3_memmap(rd3_path, n_samples=n_samples, endian=endian)

    # Sanity check against RAD LAST TRACE if present
    if "LAST TRACE" in rad:
        last_trace = int(rad["LAST TRACE"])
        if last_trace != n_traces:
            print(f"Warning: RAD LAST TRACE={last_trace} but RD3 implies {n_traces} traces.")

    # Read COR and interpolate nav
    cor = read_cor_qc(cor_path)
    nav = interpolate_nav_to_traces(cor, n_traces=n_traces, allow_extrapolation=not args.no_extrapolation)

    # Time axis (robust: prefer TIMEWINDOW)
    t, t_label = build_time_axis(rad, n_samples=n_samples)

    # Bring RD3 into float for processing
    A = np.array(data_mm, dtype=np.float32)
    A = dewow_mean_per_trace(A)
    A = gain_time_power(A, t=t, power=args.gain)

    print_summary(base, rad, n_traces, n_samples, cor, endian)

    plot_radargram(
        A,
        t=t,
        ylabel=t_label,
        title=f"{base} radargram (dewow{' + gain' if args.gain>0 else ''})",
        max_traces=args.max_traces_plot,
    )
    plot_track(nav, title=f"{base} navigation track (interpolated to traces)")

    plt.show()


if __name__ == "__main__":
    main()
