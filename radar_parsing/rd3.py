"""Reader for Malå binary trace files.

``.rd3`` holds signed 16-bit little-endian integers, ``.rd7`` signed 32-bit,
stored trace by trace (SAMPLES values per trace, no trace headers). The
number of traces follows from the file size.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .rad import RadHeader

DTYPES = {".rd3": "<i2", ".rd7": "<i4"}


@dataclass
class Bundle:
    """One Malå recording: DAT_xxxx_A1.rad + .rd3/.rd7 (+ .cor/.mrk)."""

    base: str
    rad_path: Path
    data_path: Path
    cor_path: Path | None

    @classmethod
    def find(cls, base: str, raw_dir: str | Path) -> "Bundle":
        raw_dir = Path(raw_dir)
        rad = raw_dir / f"{base}.rad"
        if not rad.exists():
            raise FileNotFoundError(f"Missing header: {rad}")
        data = next((raw_dir / f"{base}{ext}" for ext in DTYPES if (raw_dir / f"{base}{ext}").exists()), None)
        if data is None:
            raise FileNotFoundError(f"No .rd3/.rd7 for {base} in {raw_dir}")
        cor = raw_dir / f"{base}.cor"
        return cls(base, rad, data, cor if cor.exists() else None)


def n_traces_from_size(path: str | Path, n_samples: int) -> int:
    path = Path(path)
    itemsize = np.dtype(DTYPES[path.suffix.lower()]).itemsize
    bytes_per_trace = n_samples * itemsize
    size = path.stat().st_size
    if size % bytes_per_trace:
        raise ValueError(
            f"{path.name}: size {size} is not a multiple of {bytes_per_trace} bytes/trace "
            f"(SAMPLES={n_samples}); header and data file do not match."
        )
    return size // bytes_per_trace


def read_traces(path: str | Path, n_samples: int, endian: str = "<") -> np.memmap:
    """Memory-map a .rd3/.rd7 file as an array of shape (n_traces, n_samples)."""
    path = Path(path)
    dtype = np.dtype(DTYPES[path.suffix.lower()]).newbyteorder(endian)
    n_traces = n_traces_from_size(path, n_samples)
    return np.memmap(path, dtype=dtype, mode="r", shape=(n_traces, n_samples))


def time_axis_ns(header: RadHeader) -> np.ndarray | None:
    """Two-way travel time (ns) for each sample, from the sampling frequency."""
    dt = header.sample_interval_ns
    return np.arange(header.samples) * dt if dt else None


# ---------------------------------------------------------------------------
# Minimal processing for quick-look plots
# ---------------------------------------------------------------------------

def remove_trace_mean(a: np.ndarray) -> np.ndarray:
    """Subtract each trace's mean (DC removal; a crude 'dewow')."""
    return a - a.mean(axis=1, keepdims=True)


def time_power_gain(a: np.ndarray, t: np.ndarray | None, power: float = 1.0) -> np.ndarray:
    """Multiply each sample by (t - t0 + 1)**power; power <= 0 disables."""
    if power <= 0:
        return a
    x = np.arange(a.shape[1], dtype=float) if t is None else (t - float(t[0]))
    return a * ((x + 1.0) ** power)[np.newaxis, :]
