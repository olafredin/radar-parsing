"""Processing parameters.

All QC thresholds are here so that a new field season or survey area only
needs a new TOML file (see ``configs/``), not code changes. The defaults
reproduce the settings used for the 2025/26 Riiser-Larsen Ice Shelf season.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 only
    import tomli as tomllib


@dataclass
class BBox:
    """Plausible survey envelope in signed decimal degrees (south/west negative)."""

    lat_min: float = -73.0
    lat_max: float = -70.0
    lon_min: float = -13.0
    lon_max: float = -10.0

    def contains(self, lat: float, lon: float) -> bool:
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max


@dataclass
class CorQCConfig:
    """Settings for ``fix-cor`` (per-file QC of the raw Malå .cor GNSS logs)."""

    expected_ns: str = "S"
    expected_ew: str = "W"
    teleport_qc: bool = True
    max_speed_mps: float = 6.0   # snowmobile + radar sledge, upper bound
    max_step_m: float = 40.0     # used when timestamps are missing or garbled
    min_dt_s: float = 0.05       # ignore near-zero dt (clock jitter)
    relock_after: int = 10       # accept N consecutive self-consistent rejected fixes (0 = never)
    use_trace_time: bool = True  # time from trace numbers x .rad TIME INTERVAL as well as timestamps
    time_tol_s: float = 2.0      # timestamps further than this off the trace clock are ignored
    pause_s: float = 10.0        # never interpolate across a recording pause longer than this
    # Elevation spikes (garbled digits, e.g. 8.962 instead of 38.962):
    # flag |elev - running median| > elev_max_dev_m over elev_window fixes.
    elev_qc: bool = True
    elev_max_dev_m: float = 2.0
    elev_window: int = 11


@dataclass
class TrackQCConfig:
    """Settings for ``track-qc`` (second-pass QC on the compiled track table)."""

    max_speed_mps: float = 10.0
    action: str = "interpolate"  # "interpolate" or "drop"


@dataclass
class GisConfig:
    """Settings for ``gis`` (GeoPackage/shapefile export for QGIS)."""

    epsg: int = 3031            # Antarctic Polar Stereographic, as in Quantarctica
    tick_every: int = 500       # trace-number ticks along each transect
    split_jump_m: float = 50.0  # break transect lines at jumps larger than this


@dataclass
class Config:
    bbox: BBox = field(default_factory=BBox)
    cor_qc: CorQCConfig = field(default_factory=CorQCConfig)
    track_qc: TrackQCConfig = field(default_factory=TrackQCConfig)
    gis: GisConfig = field(default_factory=GisConfig)
    # UTM 29S suits the Neumayer / western RLIS area (~11 deg W). Use 3031 for
    # Antarctic Polar Stereographic if tracks span several UTM zones.
    epsg: int = 32729


def _update_dataclass(obj: Any, values: dict[str, Any], where: str) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"Unknown config key '{where}{key}'")
        current = getattr(obj, key)
        if is_dataclass(current):
            if not isinstance(value, dict):
                raise ValueError(f"Config key '{where}{key}' must be a table")
            _update_dataclass(current, value, f"{where}{key}.")
        else:
            setattr(obj, key, type(current)(value))


def load_config(path: str | Path | None = None) -> Config:
    """Return the default config, optionally overridden by a TOML file."""
    cfg = Config()
    if path is None:
        return cfg
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    _update_dataclass(cfg, data, "")
    if cfg.track_qc.action not in {"interpolate", "drop"}:
        raise ValueError("track_qc.action must be 'interpolate' or 'drop'")
    return cfg
