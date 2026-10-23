"""Reader for Malå ``.rad`` header files (plain ``KEY:VALUE`` text)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ANTENNA_RE = re.compile(r"^\s*([0-9]+)\s*MHz", flags=re.IGNORECASE)


def read_rad(path: str | Path) -> dict[str, Any]:
    """Read a .rad header into a dict with upper-case keys.

    Numeric values are converted to int/float where possible; everything else
    is kept as a string (e.g. ``ANTENNAS: '250 MHz shielded=1'``).
    """
    return read_rad_text(Path(path).read_text(encoding="utf-8", errors="ignore"))


def read_rad_text(text: str) -> dict[str, Any]:
    """Parse .rad header text (see :func:`read_rad`)."""
    meta: dict[str, Any] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or ":" not in line or line.startswith(("#", ";", "//")):
            continue
        key, value = line.split(":", 1)
        key, value = key.strip().upper(), value.strip()
        try:
            meta[key] = float(value) if ("." in value or "e" in value.lower()) else int(value)
        except ValueError:
            meta[key] = value
    return meta


@dataclass(frozen=True)
class RadHeader:
    """The handful of .rad fields the pipeline uses, with units made explicit."""

    raw: dict[str, Any]

    @classmethod
    def from_file(cls, path: str | Path) -> "RadHeader":
        return cls(read_rad(path))

    @property
    def samples(self) -> int:
        if "SAMPLES" not in self.raw:
            raise ValueError("RAD header has no SAMPLES field")
        return int(self.raw["SAMPLES"])

    @property
    def sampling_frequency_mhz(self) -> float | None:
        """``FREQUENCY`` is the *sampling* frequency in MHz, not the antenna frequency."""
        f = self.raw.get("FREQUENCY")
        return float(f) if isinstance(f, (int, float)) and f > 0 else None

    @property
    def sample_interval_ns(self) -> float | None:
        f = self.sampling_frequency_mhz
        return 1000.0 / f if f else None

    @property
    def time_window_ns(self) -> float | None:
        """``TIMEWINDOW`` in ns; equals SAMPLES * 1000 / FREQUENCY."""
        tw = self.raw.get("TIMEWINDOW")
        if isinstance(tw, (int, float)):
            return float(tw)
        dt = self.sample_interval_ns
        return self.samples * dt if dt else None

    @property
    def trace_interval_s(self) -> float | None:
        """``TIME INTERVAL``: seconds between traces when TIME FLAG=1 (time-triggered).

        Note: this is *not* the sample interval within a trace.
        """
        if self.raw.get("TIME FLAG") != 1:  # distance- or manually triggered
            return None
        v = self.raw.get("TIME INTERVAL")
        return float(v) if isinstance(v, (int, float)) and v > 0 else None

    @property
    def last_trace(self) -> int | None:
        v = self.raw.get("LAST TRACE")
        return int(v) if isinstance(v, (int, float)) else None

    @property
    def antenna_mhz(self) -> str | None:
        """Nominal antenna frequency, e.g. ``'250'`` from ``ANTENNAS:250 MHz shielded=1``."""
        return antenna_mhz_from_value(self.raw.get("ANTENNAS"))


def antenna_mhz_from_value(value: Any) -> str | None:
    if value is None:
        return None
    m = _ANTENNA_RE.match(str(value))
    return m.group(1) if m else None


def antenna_mhz(rad_path: str | Path) -> str | None:
    """Antenna frequency from a .rad file, or None if the file/field is missing."""
    rad_path = Path(rad_path)
    if not rad_path.exists():
        return None
    return RadHeader.from_file(rad_path).antenna_mhz
