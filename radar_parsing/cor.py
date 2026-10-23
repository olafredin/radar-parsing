"""Reading, repairing and writing Malå ``.cor`` GNSS position files.

A .cor file is tab-separated, one line per GNSS fix, no header::

    0 trace  1 date  2 time  3 lat  4 N/S  5 lon  6 E/W  7 elevation  8 unit ("M")  9 quality

Coordinates are unsigned with hemisphere letters. The Malå logger used in
2025/26 occasionally garbles characters or shifts fields; see
``docs/mala_file_formats.md`` for the catalogue of glitches handled here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .geo import to_signed

N_COLS = 10
UNKNOWN_QUALITY = "."

# Repair tags returned by normalize_fields()
REPAIR_NONE = ""
REPAIR_UNIT = "unit"            # "38.940  9  0.6"   -> unit restored to M
REPAIR_FUSED = "fused"          # "38.944M  9  0.6"  -> elevation 38.944
REPAIR_SHIFTED = "shifted"      # "M  9  38.975"     -> elevation 38.975, quality unknown
REPAIR_ELEV_BAD = "elev_bad"    # elevation unrecoverable, interpolated later


def looks_like_float(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


def decimals(s: str) -> int:
    """Number of decimals in a numeric string, e.g. '71.80606811550' -> 11."""
    return len(s.split(".", 1)[1]) if "." in s else 0


@dataclass
class CorLine:
    fields: list[str]
    repair: str = REPAIR_NONE


def normalize_fields(line: str) -> CorLine | None:
    """Coerce one raw .cor line into the 10-column layout.

    Returns None for blank or hopelessly short lines. Latitude/longitude are
    not touched here; they are validated by the QC step.
    """
    if not line.strip():
        return None
    fields = line.split("\t")
    if len(fields) < 7:
        return None
    if len(fields) > N_COLS:  # extra columns: fold the tail into the last field
        fields = fields[:9] + ["".join(fields[9:])]
    if len(fields) < N_COLS:
        return None

    elev = fields[7].strip()
    unit = fields[8].strip().upper()
    last = fields[9].strip()

    if looks_like_float(elev):
        if unit != "M":
            fields[8] = "M"
            return CorLine(fields, REPAIR_UNIT)
        return CorLine(fields)

    # Elevation fused with its unit, e.g. "38.944M": strip the M, keep quality.
    if elev[-1:].upper() == "M" and looks_like_float(elev[:-1]):
        fields[7] = elev[:-1]
        fields[8] = "M"
        return CorLine(fields, REPAIR_FUSED)

    # Elevation shifted into the last column, e.g. "M  9  38.975".
    if looks_like_float(last):
        fields[7] = last
        fields[8] = "M"
        fields[9] = UNKNOWN_QUALITY
        return CorLine(fields, REPAIR_SHIFTED)

    return CorLine(fields, REPAIR_ELEV_BAD)


def read_lines(path: str | Path) -> list[str]:
    return Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()


def write_lines(path: str | Path, lines: list[str]) -> None:
    """Write .cor lines with CRLF endings, as the Malå software does."""
    with open(path, "w", encoding="ascii", errors="replace", newline="\r\n") as fh:
        fh.write("\n".join(lines) + "\n")


def has_data(path: str | Path) -> bool:
    """True if the file has at least one tab-separated line with >= 8 fields."""
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if line and "\t" in line and len(line.split("\t")) >= 8:
                    return True
    except OSError:
        return False
    return False


def _float_or_nan(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return float("nan")


def read_cor_table(path: str | Path) -> pd.DataFrame:
    """Parse a (QC'd) .cor file into a table with signed coordinates.

    Lines that cannot be parsed (trace, lat, lon or elevation not numeric)
    are skipped. Columns: trace_id, date, time, lat, lon, elev, elev_unit, quality.
    """
    rows = []
    for line in read_lines(path):
        parts = line.strip().split("\t")
        if len(parts) < 9:
            continue
        try:
            trace_id = int(parts[0])
            lat = float(parts[3])
            lon = float(parts[5])
            elev = float(parts[7])
        except ValueError:
            continue
        lat_hemi = parts[4].upper()
        lon_hemi = parts[6].upper()
        if lat_hemi in ("S", "N"):
            lat = to_signed(lat, lat_hemi, "S")
        if lon_hemi in ("W", "E"):
            lon = to_signed(lon, lon_hemi, "W")
        rows.append(
            {
                "trace_id": trace_id,
                "date": parts[1],
                "time": parts[2],
                "lat": lat,
                "lon": lon,
                "elev": elev,
                "elev_unit": parts[8].upper(),
                "quality": _float_or_nan(parts[9]) if len(parts) > 9 else np.nan,
            }
        )
    cols = ["trace_id", "date", "time", "lat", "lon", "elev", "elev_unit", "quality"]
    return pd.DataFrame(rows, columns=cols)
