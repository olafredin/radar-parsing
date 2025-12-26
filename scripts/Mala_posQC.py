from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd


# -----------------------------
# Settings you will likely edit
# -----------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]   # scripts/.. (project root)
INPUT_CSV    = PROJECT_ROOT / "output" / "mala_tracks_compiled.csv"
OUTPUT_CSV   = PROJECT_ROOT / "output" / "mala_tracks_compiled_qc.csv"

# Your expected geographic envelope (degrees)
LAT_MIN, LAT_MAX = -73.0, -70.0
LON_MIN, LON_MAX = -13.0, -10.0

# Speed threshold for "teleporting" points.
# Snowmobile speeds are typically far below this; adjust if needed.
MAX_SPEED_M_S = 10.0  # 10 m/s = 36 km/h

# Choose action: "drop" or "interpolate"
ACTION = "interpolate"
# ACTION = "drop"


# -----------------------------
# Helpers
# -----------------------------
def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in meters between two lat/lon points (degrees)."""
    R = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)

    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def add_time_column(df: pd.DataFrame) -> pd.DataFrame:
    # Combine date + time safely (your CSV has separate columns)
    dt = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str), errors="coerce")
    return df.assign(dt=dt)


def qc_one_track(g: pd.DataFrame) -> pd.DataFrame:
    """
    QC within one file_base (one transect).
    Flags points as bad if outside bbox, missing time, missing coords, or implausible jump speed.
    """
    g = g.sort_values("trace_id").copy()

    # Basic validity
    in_bbox = (
        g["lat"].between(LAT_MIN, LAT_MAX)
        & g["lon"].between(LON_MIN, LON_MAX)
    )

    has_coords = g[["lat", "lon", "easting", "northing"]].notna().all(axis=1)
    has_time = g["dt"].notna()

    bad = ~(in_bbox & has_coords & has_time)

    # Jump/speed test (only for points that look otherwise valid)
    # Compute distance and dt to previous valid-looking point
    lat = g["lat"].to_numpy()
    lon = g["lon"].to_numpy()
    dt  = g["dt"].to_numpy()

    dist = np.full(len(g), np.nan, dtype=float)
    dts  = np.full(len(g), np.nan, dtype=float)
    speed = np.full(len(g), np.nan, dtype=float)

    for i in range(1, len(g)):
        if pd.isna(lat[i-1]) or pd.isna(lon[i-1]) or pd.isna(lat[i]) or pd.isna(lon[i]):
            continue
        if pd.isna(dt[i-1]) or pd.isna(dt[i]):
            continue

        dist[i] = haversine_m(lat[i-1], lon[i-1], lat[i], lon[i])
        dts[i] = (pd.Timestamp(dt[i]) - pd.Timestamp(dt[i-1])).total_seconds()
        if dts[i] and dts[i] > 0:
            speed[i] = dist[i] / dts[i]

    # Flag "teleport" points
    teleport = (speed > MAX_SPEED_M_S)
    bad = bad | teleport

    g["qc_in_bbox"] = in_bbox
    g["qc_speed_m_s"] = speed
    g["qc_bad"] = bad

    return g


def interpolate_bad_points(g: pd.DataFrame) -> pd.DataFrame:
    """
    Replace bad points with linear interpolation (within file_base).
    Keeps endpoints as NaN if no valid neighbours exist.
    """
    g = g.sort_values("trace_id").copy()

    cols_interp = ["lat", "lon", "easting", "northing", "elev"]
    for c in cols_interp:
        # bad -> NaN
        g.loc[g["qc_bad"], c] = np.nan
        # interpolate along trace order
        g[c] = g[c].interpolate(method="linear", limit_direction="both")

    g["qc_was_interpolated"] = g["qc_bad"]
    g["qc_bad"] = False  # after interpolation, treat as usable
    return g


def main() -> None:
    if not INPUT_CSV.exists():
        raise FileNotFoundError(f"Could not find input CSV: {INPUT_CSV}")

    df = pd.read_csv(INPUT_CSV)
    df = add_time_column(df)

    required = {"trace_id", "date", "time", "lat", "lon", "easting", "northing", "file_base", "antenna_mhz"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Input CSV is missing required columns: {sorted(missing)}")

    # Run QC per transect
    df_qc = df.groupby("file_base", group_keys=False).apply(qc_one_track)

    # Summary before action
    n_total = len(df_qc)
    n_bad = int(df_qc["qc_bad"].sum())
    print(f"Total points: {n_total:,}")
    print(f"Flagged bad:  {n_bad:,} ({n_bad / max(n_total, 1) * 100:.2f}%)")

    if ACTION.lower() == "drop":
        df_out = df_qc.loc[~df_qc["qc_bad"]].copy()
        df_out["qc_action"] = "dropped_bad_points"
        print(f"Output points after drop: {len(df_out):,}")

    elif ACTION.lower() == "interpolate":
        df_out = df_qc.groupby("file_base", group_keys=False).apply(interpolate_bad_points)
        df_out["qc_action"] = "interpolated_bad_points"
        # Keep a note of how many were interpolated
        n_interp = int(df_out.get("qc_was_interpolated", pd.Series(False)).sum())
        print(f"Interpolated points: {n_interp:,}")
        print(f"Output points after interpolation: {len(df_out):,}")

    else:
        raise ValueError("ACTION must be either 'drop' or 'interpolate'")

    # For GIS, you may want to keep only what you need:
    # (But I recommend keeping QC columns for transparency.)
    df_out.to_csv(OUTPUT_CSV, index=False)
    print(f"Wrote: {OUTPUT_CSV}")


if __name__ == "__main__":
    main()
