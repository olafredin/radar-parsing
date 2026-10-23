"""Small geodesy helpers."""

from __future__ import annotations

import math

import numpy as np

EARTH_RADIUS_M = 6371000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two points given in degrees."""
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def haversine_m_vec(lat1, lon1, lat2, lon2) -> np.ndarray:
    """Vectorised :func:`haversine_m` (NaN in, NaN out)."""
    lat1, lon1, lat2, lon2 = (np.radians(np.asarray(x, dtype=float)) for x in (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_M * np.arcsin(np.sqrt(a))


def to_signed(value: float, hemi: str, neg_hemi: str) -> float:
    """Unsigned magnitude + hemisphere letter -> signed degrees."""
    return -abs(value) if (hemi or "").strip().upper() == neg_hemi else abs(value)


def to_unsigned(value_signed: float, neg_hemi: str, pos_hemi: str) -> tuple[float, str]:
    """Signed degrees -> (unsigned magnitude, hemisphere letter)."""
    return (abs(value_signed), neg_hemi) if value_signed < 0 else (abs(value_signed), pos_hemi)


def track_length_m(lat: np.ndarray, lon: np.ndarray) -> float:
    """Cumulative along-track length in metres (NaNs ignored)."""
    lat = np.asarray(lat, dtype=float)
    lon = np.asarray(lon, dtype=float)
    ok = np.isfinite(lat) & np.isfinite(lon)
    lat, lon = lat[ok], lon[ok]
    if lat.size < 2:
        return 0.0
    return float(np.nansum(haversine_m_vec(lat[:-1], lon[:-1], lat[1:], lon[1:])))
