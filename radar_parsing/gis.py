"""GeoPackage / shapefile export of the radar tracks for QGIS.

Default CRS is EPSG:3031 (Antarctic Polar Stereographic), as used by
Quantarctica. Layers:

``transects``    one (multi)line per recording, split where the track jumps
                 more than ``gis.split_jump_m`` between consecutive fixes
``fixes``        every QC'd GNSS fix, with what fix-cor did to it
``trace_ticks``  a point every ``gis.tick_every`` traces (plus first/last trace),
                 to find radargram trace numbers on the map
``profiles``     recordings in ``output/reflexw/`` (exported and merged), as lines

All field names are at most 10 characters, so the shapefile export has the
same schema. Times are as logged by the GNSS receiver (normally UTC).

Requires the optional dependencies: ``pip install -e ".[gis]"``.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, MultiLineString

from .config import Config
from .cor import read_cor_table
from .geo import gap_length_m, haversine_m_vec, track_length_m
from .inventory import inventory
from .rad import RadHeader, antenna_mhz
from .rd3 import DTYPES, n_traces_from_size
from .tracks import find_rad, nav_for_traces

LAYER_ORDER = ["transects", "profiles", "trace_ticks", "fixes"]


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _cor_with_time(path: Path) -> pd.DataFrame:
    df = read_cor_table(path)
    if df.empty:
        return df
    df = df.sort_values("trace_id").drop_duplicates("trace_id").reset_index(drop=True)
    df["time"] = pd.to_datetime(df["date"] + " " + df["time"], errors="coerce", format="%Y-%m-%d %H:%M:%S")
    return df


def segments(lat: np.ndarray, lon: np.ndarray, max_jump_m: float) -> list[np.ndarray]:
    """Index arrays of runs without a jump > ``max_jump_m`` between consecutive points."""
    n = len(lat)
    if n == 0:
        return []
    if n == 1 or max_jump_m <= 0:
        return [np.arange(n)]
    step = haversine_m_vec(lat[:-1], lon[:-1], lat[1:], lon[1:])
    breaks = np.where(step > max_jump_m)[0] + 1
    return np.split(np.arange(n), breaks)


def _line(xs: np.ndarray, ys: np.ndarray, segs: list[np.ndarray]):
    parts = [LineString(np.column_stack([xs[s], ys[s]])) for s in segs if len(s) >= 2]
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else MultiLineString(parts)


def _project(lon, lat, epsg: int):
    pts = gpd.GeoSeries(gpd.points_from_xy(lon, lat), crs="EPSG:4326").to_crs(epsg=epsg)
    return pts.x.to_numpy(), pts.y.to_numpy(), pts


def build_layers(
    qc_dir: str | Path,
    raw_dir: str | Path,
    cfg: Config | None = None,
    flags: pd.DataFrame | None = None,
    reflexw_dir: str | Path | None = None,
) -> dict[str, gpd.GeoDataFrame]:
    """Build the GIS layers from QC'd .cor files (``qc_dir``) and raw headers (``raw_dir``)."""
    cfg = cfg or Config()
    g = cfg.gis
    qc_dir, raw_dir = Path(qc_dir), Path(raw_dir)
    crs = f"EPSG:{g.epsg}"
    inv = inventory(raw_dir, qc_dir, g.split_jump_m).set_index("file_base")

    fix_frames, transects, ticks = [], [], []
    for cor_path in sorted(p for p in qc_dir.iterdir() if p.suffix.lower() == ".cor"):
        base = cor_path.stem
        cor = _cor_with_time(cor_path)
        if cor.empty:
            continue
        x, y, _ = _project(cor["lon"].to_numpy(), cor["lat"].to_numpy(), g.epsg)
        rad = find_rad(base, raw_dir, qc_dir)
        ant = _to_int(antenna_mhz(rad)) if rad else None

        # --- fixes
        fx = pd.DataFrame({
            "file_base": base, "trace_id": cor["trace_id"], "time": cor["time"],
            "elev": cor["elev"], "quality": cor["quality"], "antenna": ant,
        })
        if flags is not None and len(flags):
            fl = flags[flags["file_base"] == base][["trace_id", "position", "elevation", "time_ignored"]]
            fx = fx.merge(fl, on="trace_id", how="left")
        else:
            fx["position"], fx["elevation"], fx["time_ignored"] = "", "", False
        fx = fx.rename(columns={"position": "pos_flag", "elevation": "elev_flag", "time_ignored": "time_ign"})
        fx["pos_flag"] = fx["pos_flag"].fillna("")
        fx["elev_flag"] = fx["elev_flag"].fillna("")
        fx["time_ign"] = fx["time_ign"].fillna(False).astype(bool)
        fix_frames.append(gpd.GeoDataFrame(fx, geometry=gpd.points_from_xy(x, y), crs=crs))

        # --- transect
        segs = segments(cor["lat"].to_numpy(), cor["lon"].to_numpy(), g.split_jump_m)
        geom = _line(x, y, segs)
        if geom is None:
            continue
        row = inv.loc[base] if base in inv.index else pd.Series(dtype=object)
        t = cor["time"].dropna()
        n_tr = _to_int(row.get("n_traces")) or int(cor["trace_id"].max())
        transects.append({
            "file_base": base,
            "antenna": ant,
            "date": t.min().strftime("%Y-%m-%d") if len(t) else None,
            "t_start": t.min() if len(t) else pd.NaT,
            "t_end": t.max() if len(t) else pd.NaT,
            "dur_min": round((t.max() - t.min()).total_seconds() / 60, 1) if len(t) else None,
            "length_km": round(track_length_m(cor["lat"], cor["lon"], g.split_jump_m) / 1000, 3),
            "gap_km": round(gap_length_m(cor["lat"], cor["lon"], g.split_jump_m) / 1000, 3),
            "n_traces": n_tr,
            "n_fixes": len(cor),
            "samples": _to_int(row.get("samples")),
            "twin_ns": float(row["time_window_ns"]) if "time_window_ns" in row and pd.notna(row["time_window_ns"]) else None,
            "n_pos_int": int((fx["pos_flag"] != "").sum()),
            "n_elev_int": int(fx["elev_flag"].isin(["unparseable", "spike"]).sum()),
            "n_segments": len([s for s in segs if len(s) >= 2]),
            "elev_min": round(float(cor["elev"].min()), 2),
            "elev_max": round(float(cor["elev"].max()), 2),
            "header_ok": bool(row.get("header_last_trace_ok", True)),
            "geometry": geom,
        })

        # --- trace ticks
        # first/last positioned trace, plus every tick_every-th trace in between
        nav = nav_for_traces(cor, n_tr, extrapolate=False)
        first = max(1, int(cor["trace_id"].min()))
        last = min(n_tr, int(cor["trace_id"].max()))
        inner = range(g.tick_every, last, g.tick_every) if g.tick_every > 0 else []
        want = sorted({first, last, *[w for w in inner if w > first]})
        sel = nav.iloc[[w - 1 for w in want]].dropna(subset=["lat", "lon"])
        if len(sel):
            tx, ty, _ = _project(sel["lon"].to_numpy(), sel["lat"].to_numpy(), g.epsg)
            kind = np.where(sel["trace"] == first, "start", np.where(sel["trace"] == last, "end", "tick"))
            label = np.where(kind == "start", base, sel["trace"].astype(str))
            ticks.append(gpd.GeoDataFrame(
                {"file_base": base, "trace": sel["trace"].to_numpy(), "kind": kind, "label": label},
                geometry=gpd.points_from_xy(tx, ty), crs=crs))

    layers: dict[str, gpd.GeoDataFrame] = {}
    if transects:
        layers["transects"] = gpd.GeoDataFrame(transects, geometry="geometry", crs=crs)
    if ticks:
        layers["trace_ticks"] = pd.concat(ticks, ignore_index=True)
    if fix_frames:
        layers["fixes"] = pd.concat(fix_frames, ignore_index=True)
    prof = _profiles(Path(reflexw_dir), cfg) if reflexw_dir and Path(reflexw_dir).is_dir() else None
    if prof is not None and len(prof):
        layers["profiles"] = prof
    return layers


def _profiles(reflexw_dir: Path, cfg: Config) -> gpd.GeoDataFrame | None:
    rows = []
    for rad_path in sorted(reflexw_dir.glob("*.rad")):
        base = rad_path.stem
        cor_path = reflexw_dir / f"{base}.cor"
        data = next((reflexw_dir / f"{base}{e}" for e in DTYPES if (reflexw_dir / f"{base}{e}").exists()), None)
        if not cor_path.exists():
            continue
        cor = _cor_with_time(cor_path)
        if cor.empty:
            continue
        x, y, _ = _project(cor["lon"].to_numpy(), cor["lat"].to_numpy(), cfg.gis.epsg)
        segs = segments(cor["lat"].to_numpy(), cor["lon"].to_numpy(), cfg.gis.split_jump_m)
        geom = _line(x, y, segs)
        if geom is None:
            continue
        hdr = RadHeader.from_file(rad_path)
        n_tr = n_traces_from_size(data, hdr.samples) if data else hdr.last_trace
        rows.append({"name": base, "antenna": _to_int(hdr.antenna_mhz), "n_traces": n_tr,
                     "n_fixes": len(cor),
                     "length_km": round(track_length_m(cor["lat"], cor["lon"], cfg.gis.split_jump_m) / 1000, 3),
                     "gap_km": round(gap_length_m(cor["lat"], cor["lon"], cfg.gis.split_jump_m) / 1000, 3),
                     "n_segments": len([s for s in segs if len(s) >= 2]), "geometry": geom})
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=f"EPSG:{cfg.gis.epsg}") if rows else None


def write_gpkg(layers: dict[str, gpd.GeoDataFrame], path: str | Path) -> Path:
    """Write all layers to one GeoPackage, replacing an existing file.

    A GeoPackage is an SQLite database, and SQLite can fail on network drives,
    synced or mounted folders. So the file is built in the local temp folder
    and copied into place. If the existing file is locked (open in QGIS on
    Windows), a timestamped copy is written next to it instead. The path
    actually written is returned.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / path.name
        for name in [n for n in LAYER_ORDER if n in layers]:
            layers[name].to_file(tmp, layer=name, driver="GPKG", engine="pyogrio")
        try:
            if path.exists():
                path.unlink()
            shutil.copyfile(tmp, path)
            return path
        except PermissionError:
            alt = path.with_name(f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S}{path.suffix}")
            shutil.copyfile(tmp, alt)
            return alt


def write_shapefiles(layers: dict[str, gpd.GeoDataFrame], out_dir: str | Path) -> list[Path]:
    """Write one shapefile per layer (datetimes as ISO text; shapefiles have no datetime type)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in [n for n in LAYER_ORDER if n in layers]:
        gdf = layers[name].copy()
        for c in gdf.columns:
            if pd.api.types.is_datetime64_any_dtype(gdf[c]):
                gdf[c] = gdf[c].dt.strftime("%Y-%m-%d %H:%M:%S")
            elif gdf[c].dtype == bool:
                gdf[c] = gdf[c].astype(int)
        p = out_dir / f"{name}.shp"
        gdf.to_file(p, driver="ESRI Shapefile", engine="pyogrio")
        written.append(p)
    return written
