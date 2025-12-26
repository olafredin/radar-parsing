from __future__ import annotations

from pathlib import Path
from typing import List, Tuple, Optional

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
    out = [None] * n

    valid_idx = [i for i, ok in enumerate(valid) if ok and values[i] is not None]
    if not valid_idx:
        raise ValueError("No valid values to interpolate.")

    # seed known points
    for i in valid_idx:
        out[i] = float(values[i])

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
        for k in range(1, gap):
            t = k / gap
            out[a + k] = (1 - t) * va + t * vb

    # fill trailing
    last = valid_idx[-1]
    for i in range(last + 1, n):
        out[i] = out[last]

    return [float(x) for x in out]


def qc_one_cor_file(cor_path: Path, out_path: Path) -> bool:
    lines = cor_path.read_text(errors="ignore").splitlines()
    if not lines:
        return False

    parsed: List[List[str]] = []
    lat_signed: List[Optional[float]] = []
    lon_signed: List[Optional[float]] = []
    valid: List[bool] = []

    # infer formatting precision from the first plausible numeric strings we see
    lat_dec = None
    lon_dec = None

    for line in lines:
        if not line.strip():
            continue
        fields = line.split("\t")
        # Expected (from your sample):
        # 0 idx, 1 date, 2 time, 3 lat, 4 S/N, 5 lon, 6 W/E, 7 elev, 8 M, 9 something
        if len(fields) < 7:
            continue

        parsed.append(fields)

        lat_str = fields[3].strip() if len(fields) > 3 else ""
        ns = fields[4].strip().upper() if len(fields) > 4 else ""
        lon_str = fields[5].strip() if len(fields) > 5 else ""
        ew = fields[6].strip().upper() if len(fields) > 6 else ""

        # try parse numbers
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

        # also insist the hemisphere matches expectation (you can relax this if needed)
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

    # If there are no valid fixes at all: ignore file (your test runs)
    if not any(valid):
        return False

    # decide decimals (fallback if never inferred)
    lat_dec = lat_dec if lat_dec is not None else 8
    lon_dec = lon_dec if lon_dec is not None else 8

    # interpolate across invalids
    lat_filled = _interp_fill(lat_signed, valid)
    lon_filled = _interp_fill(lon_signed, valid)

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


if __name__ == "__main__":
    main()
