"""Compile QC'd .cor files into one track table, and second-pass track QC."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer

from .config import Config
from .cor import has_data, read_cor_table
from .geo import haversine_m_vec
from .rad import antenna_mhz

log = logging.getLogger(__name__)

TRACK_COLUMNS = [
    "trace_id", "date", "time", "lat", "lon", "elev", "elev_unit",
    "easting", "northing", "file_base", "antenna_mhz",
]


def find_rad(base: str, *dirs: Path) -> Path | None:
    for d in dirs:
        for ext in (".rad", ".RAD"):
            p = d / f"{base}{ext}"
            if p.exists():
                return p
    return None


def compile_tracks(qc_dir: str | Path, raw_dir: str | Path, cfg: Config | None = None) -> pd.DataFrame:
    """Read every QC'd .cor in ``qc_dir`` into one table with projected coordinates.

    The antenna frequency is taken from the matching .rad in ``raw_dir``
    (falling back to ``qc_dir``).
    """
    cfg = cfg or Config()
    qc_dir, raw_dir = Path(qc_dir), Path(raw_dir)
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{cfg.epsg}", always_xy=True)

    cor_files = sorted(p for p in qc_dir.iterdir() if p.is_file() and p.suffix.lower() == ".cor")
    tracks = []
    for cor_path in cor_files:
        if not has_data(cor_path):
            log.info("skip %s: no data", cor_path.name)
            continue
        df = read_cor_table(cor_path)
        if df.empty:
            log.warning("skip %s: no parseable lines", cor_path.name)
            continue
        east, north = transformer.transform(df["lon"].to_numpy(), df["lat"].to_numpy())
        df["easting"] = east
        df["northing"] = north
        df["file_base"] = cor_path.stem
        rad = find_rad(cor_path.stem, raw_dir, qc_dir)
        df["antenna_mhz"] = antenna_mhz(rad) if rad else None
        tracks.append(df[TRACK_COLUMNS])

    if not tracks:
        return pd.DataFrame(columns=TRACK_COLUMNS)
    return pd.concat(tracks, ignore_index=True)


# ---------------------------------------------------------------------------
# Second-pass QC on the compiled table
# ---------------------------------------------------------------------------

def _qc_one_track(g: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    g = g.sort_values("trace_id").copy()
    b = cfg.bbox
    in_bbox = g["lat"].between(b.lat_min, b.lat_max) & g["lon"].between(b.lon_min, b.lon_max)
    has_coords = g[["lat", "lon", "easting", "northing"]].notna().all(axis=1)
    has_time = g["dt"].notna()

    lat = g["lat"].to_numpy(dtype=float)
    lon = g["lon"].to_numpy(dtype=float)
    t = g["dt"].to_numpy(dtype="datetime64[ns]")
    speed = np.full(len(g), np.nan)
    if len(g) > 1:
        dist = haversine_m_vec(lat[:-1], lon[:-1], lat[1:], lon[1:])
        dts = (t[1:] - t[:-1]) / np.timedelta64(1, "s")
        with np.errstate(divide="ignore", invalid="ignore"):
            sp = np.where(dts > 0, dist / dts, np.nan)
        speed[1:] = sp

    teleport = np.nan_to_num(speed, nan=0.0) > cfg.track_qc.max_speed_mps
    g["qc_in_bbox"] = in_bbox
    g["qc_speed_m_s"] = speed
    g["qc_bad"] = ~(in_bbox & has_coords & has_time) | teleport
    return g


def _interpolate_bad(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("trace_id").copy()
    for c in ["lat", "lon", "easting", "northing", "elev"]:
        g[c] = g[c].astype(float)
        g.loc[g["qc_bad"], c] = np.nan
        g[c] = g[c].interpolate(method="linear", limit_direction="both")
    g["qc_was_interpolated"] = g["qc_bad"]
    g["qc_bad"] = False
    return g


def track_qc(df: pd.DataFrame, cfg: Config | None = None) -> tuple[pd.DataFrame, dict]:
    """Flag points outside the bbox, without time/coords, or implying speed >
    ``track_qc.max_speed_mps`` (consecutive points within each file), then
    interpolate or drop them per ``track_qc.action``.
    """
    cfg = cfg or Config()
    required = {"trace_id", "date", "time", "lat", "lon", "easting", "northing", "file_base", "antenna_mhz"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Track table is missing columns: {sorted(missing)}")

    df = df.assign(dt=pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str), errors="coerce"))
    if df.empty:  # no recording with GNSS yet
        out = df.assign(qc_in_bbox=pd.Series(dtype=bool), qc_speed_m_s=pd.Series(dtype=float),
                        qc_bad=pd.Series(dtype=bool), qc_action=pd.Series(dtype=object))
        return out, {"n_total": 0, "n_bad": 0, "n_out": 0}
    groups = [_qc_one_track(g, cfg) for _, g in df.groupby("file_base", sort=False)]
    qc = pd.concat(groups, ignore_index=True)
    stats = {"n_total": len(qc), "n_bad": int(qc["qc_bad"].sum())}

    if cfg.track_qc.action == "drop":
        out = qc.loc[~qc["qc_bad"]].copy()
        out["qc_action"] = "dropped_bad_points"
    else:
        out = pd.concat([_interpolate_bad(g) for _, g in qc.groupby("file_base", sort=False)], ignore_index=True)
        out["qc_action"] = "interpolated_bad_points"
    stats["n_out"] = len(out)
    return out, stats


# ---------------------------------------------------------------------------
# Navigation per radar trace
# ---------------------------------------------------------------------------

def nav_for_traces(cor: pd.DataFrame, n_traces: int, extrapolate: bool = True) -> pd.DataFrame:
    """Interpolate GNSS fixes (by trace number) onto every radar trace 1..n_traces.

    Without extrapolation, traces before the first / after the last fix get NaN.
    """
    cor = cor.sort_values("trace_id").drop_duplicates("trace_id")
    cor = cor[np.isfinite(cor["lat"]) & np.isfinite(cor["lon"])]
    traces = np.arange(1, n_traces + 1)
    x = cor["trace_id"].to_numpy(dtype=float)
    out = {"trace": traces}
    for col in ("lat", "lon", "elev"):
        y = cor[col].to_numpy(dtype=float)
        v = np.interp(traces, x, y) if len(x) else np.full(n_traces, np.nan)
        if not extrapolate and len(x):
            v[(traces < x.min()) | (traces > x.max())] = np.nan
        out[col] = v
    return pd.DataFrame(out)
