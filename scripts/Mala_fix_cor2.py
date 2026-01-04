from __future__ import annotations

from pathlib import Path
from typing import List, Tuple, Optional
from datetime import datetime
import math

# ---- paths (adjust only BASE_DIR if needed) ----
BASE_DIR = Path(__file__).resolve().parents[1]   # scripts/..  (project root)
RAW_DIR  = BASE_DIR / "data_raw"
OUT_DIR  = BASE_DIR / "output_cor_qc"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ---- QC bounds (SIGNED internally) ----
# lat between -70 and -73, lon between -10 and -13
LAT_MIN, LAT_MAX = -73.0, -70.0
LON_MIN, LON_MAX = -13.0, -10.0

EXPECTED_NS = "S"
EXPECTED_EW = "W"

# ---- Teleport jump QC (optional) ----
USE_TELEPORT_QC = True  # <-- set True to enable teleport detection

# If timestamps parse cleanly, we use speed (m/s). Otherwise we use step distance (m).
MAX_SPEED_MPS = 6.0     # skidoo + radar maximum speed
MAX_STEP_M    = 40.0    # fallback if time is missing/garbled
MIN_DT_S      = 0.05     # ignore near-zero dt to avoid divide-by-zero/clock jitter


def _decimals_from_str(s: str) -> int:
    """Infer decimals from a numeric string like '71.80606811550'."""
    if "." not in s:
        return 0
    return len(s.split(".", 1)[1])


def _to_signed(value: float, hemi: str, neg_hemi: str) -> float:
    """Convert unsigned value + hemisphere letter into signed float."""
    hemi = (hemi or "").strip().upper()
    if hemi == neg_hemi:
        return -abs(value)
    return abs(value)


def _to_unsigned_and_hemi(value_signed: float, neg_hemi: str, pos_hemi: str) -> Tuple[float, str]:
    """Convert signed float into unsigned magnitude + hemisphere letter."""
    if value_signed < 0:
        return abs(value_signed), neg_hemi
    return abs(value_signed), pos_hemi


def _is_valid_pos(lat_s: float, lon_s: float) -> bool:
    return (LAT_MIN <= lat_s <= LAT_MAX) and (LON_MIN <= lon_s <= LON_MAX)


def _interp_fill(values: List[Optional[float]], valid: List[bool]) -> List[float]:
    """
    Linear interpolation across invalid runs.
    - invalid leading segment -> filled with first valid
    - invalid trailing segment -> filled with last valid
    """
    n = len(values)
    out: List[Optional[float]] = [None] * n

    valid_idx = [i for i, ok in enumerate(valid) if ok and values[i] is not None]
    if not valid_idx:
        raise ValueError("No valid values to interpolate.")

    # seed known points
    for i in valid_idx:
        out[i] = float(values[i])  # type: ignore[arg-type]

    # fill leading
    first = valid_idx[0]
    for i in range(0, first):
        out[i] = out[first]

    # fill between valid anchors
    for a, b in zip(valid_idx, valid_idx[1:]):
        va, vb = out[a], out[b]
        gap = b - a
        if gap <= 1:
            continue
        assert va is not None and vb is not None
        for k in range(1, gap):
            t = k / gap
            out[a + k] = (1 - t) * va + t * vb

    # fill trailing
    last = valid_idx[-1]
    for i in range(last + 1, n):
        out[i] = out[last]

    return [float(x) for x in out]  # type: ignore[arg-type]


def _parse_dt(date_str: str, time_str: str) -> Optional[datetime]:
    """Parse 'YYYY-MM-DD' + 'HH:MM:SS' or similar. Returns None if it fails."""
    try:
        # Malå typically uses these formats; keep it strict-ish.
        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters."""
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = phi2 - phi1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _looks_like_float(token: str) -> bool:
    try:
        float(token)
        return True
    except Exception:
        return False


def _normalize_mala_fields(line: str) -> Optional[List[str]]:
    """
    Try to wrangle a messy Malå .cor line into the expected 10-column format:
    0 idx, 1 date, 2 time, 3 lat, 4 S/N, 5 lon, 6 W/E, 7 elev, 8 M, 9 last

    If there are extra columns (field shifting), we join extras into the last field.
    If elevation got mangled such that field 7 is not float, we try a light repair.
    """
    if not line.strip():
        return None

    fields = line.split("\t")
    if len(fields) < 7:
        return None

    # If too many fields: join tail into the last column
    if len(fields) > 10:
        fields = fields[:9] + ["".join(fields[9:])]

    # If too few fields, give up (rare, but safer)
    if len(fields) < 10:
        return None

    # Light repair for elevation/units/last when shifting happens:
    # Expect fields[8] == "M" and fields[7] is a float.
    elev = fields[7].strip()
    unit = fields[8].strip().upper()
    last = fields[9].strip()

    # Case: unit is wrong (e.g., "9" or ","), but elevation is fine -> just force unit to "M"
    if unit != "M" and _looks_like_float(elev):
        fields[8] = "M"
        # keep fields[9] as-is

    # Case: elevation is not a float, but the next field is "M" or contains "M" etc.
    if not _looks_like_float(elev):
        # Sometimes elevation gets split like: fields[7]="M", fields[8]="9", fields[9]="31.855"
        # If fields[9] looks float, use that as elevation and reset unit.
        if _looks_like_float(fields[9].strip()):
            fields[7] = fields[9].strip()
            fields[8] = "M"
            fields[9] = last  # keep last (even if odd), we do not rely on it for QC
        else:
            # Another failure mode: last might be fused like "0.51.721"
            # We leave elevation alone; QC operates on lat/lon and we will still write a line,
            # but we do not want to crash on this.
            pass

    return fields


def _apply_teleport_qc(
    lat_s: List[Optional[float]],
    lon_s: List[Optional[float]],
    valid: List[bool],
    dts: List[Optional[datetime]],
) -> List[bool]:
    """
    Mark teleport-jump points invalid and return a new valid[] list.

    Logic:
    - Walk forward through traces.
    - Compare current valid fix with previous valid fix.
    - If distance is too large (speed-based when dt available, else step-based), mark current invalid.
    """
    if not USE_TELEPORT_QC:
        return valid

    n = len(valid)
    out = valid[:]

    prev_i: Optional[int] = None
    for i in range(n):
        if not out[i] or lat_s[i] is None or lon_s[i] is None:
            continue

        if prev_i is None:
            prev_i = i
            continue

        # compute distance between prev valid and current
        d = _haversine_m(lat_s[prev_i], lon_s[prev_i], lat_s[i], lon_s[i])  # type: ignore[arg-type]

        # compute dt if possible
        dt_ok = False
        if dts[prev_i] is not None and dts[i] is not None:
            dt_s = (dts[i] - dts[prev_i]).total_seconds()
            if dt_s >= MIN_DT_S:
                speed = d / dt_s
                if speed > MAX_SPEED_MPS:
                    out[i] = False
                else:
                    dt_ok = True

        # fallback: step threshold if time is missing/garbled or dt too small
        if not dt_ok:
            if d > MAX_STEP_M:
                out[i] = False

        # update prev_i only if current survived (otherwise we keep the last good anchor)
        if out[i]:
            prev_i = i

    return out


def qc_one_cor_file(cor_path: Path, out_path: Path) -> bool:
    raw_lines = cor_path.read_text(errors="ignore").splitlines()
    if not raw_lines:
        return False

    parsed: List[List[str]] = []
    lat_signed: List[Optional[float]] = []
    lon_signed: List[Optional[float]] = []
    valid: List[bool] = []
    dts: List[Optional[datetime]] = []

    # infer formatting precision from the first plausible numeric strings we see
    lat_dec: Optional[int] = None
    lon_dec: Optional[int] = None

    for line in raw_lines:
        fields = _normalize_mala_fields(line)
        if fields is None:
            continue

        parsed.append(fields)

        # Expected indices:
        # 0 idx, 1 date, 2 time, 3 lat, 4 S/N, 5 lon, 6 W/E, 7 elev, 8 M, 9 last
        date_str = fields[1].strip()
        time_str = fields[2].strip()
        dts.append(_parse_dt(date_str, time_str))

        lat_str = fields[3].strip()
        ns = fields[4].strip().upper()
        lon_str = fields[5].strip()
        ew = fields[6].strip().upper()

        # parse numbers
        try:
            lat_u = float(lat_str)
            lon_u = float(lon_str)
        except ValueError:
            lat_signed.append(None)
            lon_signed.append(None)
            valid.append(False)
            continue

        # treat 0/0 as invalid (common dropout)
        if lat_u == 0.0 or lon_u == 0.0:
            lat_signed.append(None)
            lon_signed.append(None)
            valid.append(False)
            continue

        # convert to signed (internal)
        lat_s = _to_signed(lat_u, ns, neg_hemi="S")
        lon_s = _to_signed(lon_u, ew, neg_hemi="W")

        ok = _is_valid_pos(lat_s, lon_s)

        # insist hemisphere matches expectation
        if ns != EXPECTED_NS or ew != EXPECTED_EW:
            ok = False

        lat_signed.append(lat_s if ok else None)
        lon_signed.append(lon_s if ok else None)
        valid.append(ok)

        if ok:
            if lat_dec is None:
                lat_dec = _decimals_from_str(lat_str)
            if lon_dec is None:
                lon_dec = _decimals_from_str(lon_str)

    if not parsed:
        return False

    # If there are no valid fixes at all: ignore file (test runs / no GNSS)
    if not any(valid):
        return False

    # Optional teleport QC: mark “jump” points invalid
    valid2 = _apply_teleport_qc(lat_signed, lon_signed, valid, dts)

    # decide decimals (fallback if never inferred)
    lat_dec = lat_dec if lat_dec is not None else 8
    lon_dec = lon_dec if lon_dec is not None else 8

    # interpolate across invalids (after teleport QC)
    lat_filled = _interp_fill(lat_signed, valid2)
    lon_filled = _interp_fill(lon_signed, valid2)

    # write corrected file, preserving tab structure
    out_lines: List[str] = []
    for fields, lat_s, lon_s in zip(parsed, lat_filled, lon_filled):
        lat_u, ns = _to_unsigned_and_hemi(lat_s, neg_hemi="S", pos_hemi="N")
        lon_u, ew = _to_unsigned_and_hemi(lon_s, neg_hemi="W", pos_hemi="E")

        fields[3] = f"{lat_u:.{lat_dec}f}"
        fields[4] = ns
        fields[5] = f"{lon_u:.{lon_dec}f}"
        fields[6] = ew

        out_lines.append("\t".join(fields))

    out_path.write_text("\n".join(out_lines) + "\n")
    return True


def main() -> None:
    cor_files = sorted(RAW_DIR.glob("*.cor"))
    if not cor_files:
        print(f"No .cor files found in: {RAW_DIR}")
        return

    written = 0
    skipped = 0

    for cor_path in cor_files:
        out_path = OUT_DIR / cor_path.name
        ok = qc_one_cor_file(cor_path, out_path)
        if ok:
            written += 1
        else:
            skipped += 1

    print(f"Done. Wrote {written} corrected .cor files to: {OUT_DIR}")
    print(f"Skipped {skipped} files (empty/no valid GNSS).")

    if USE_TELEPORT_QC:
        print(f"Teleport QC was enabled (MAX_SPEED_MPS={MAX_SPEED_MPS}, MAX_STEP_M={MAX_STEP_M}).")
    else:
        print("Teleport QC was disabled.")


if __name__ == "__main__":
    main()
