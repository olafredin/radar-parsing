from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

# pip install pyproj
from pyproj import Transformer


# ---------- Paths (robust: relative to script location) ----------
SCRIPT_DIR  = Path(__file__).resolve().parent          # .../scripts
PROJECT_DIR = SCRIPT_DIR.parent                         # project root
DATA_DIR    = PROJECT_DIR / "data_raw"
OUT_DIR     = PROJECT_DIR / "output"
OUT_DIR.mkdir(exist_ok=True)

OUT_CSV = OUT_DIR / "mala_tracks_compiled.csv"


# ---------- Projection ----------
# Neumayer-area tracks (~11–12°W) -> UTM zone 29S (WGS84)
transformer = Transformer.from_crs("EPSG:4326", "EPSG:32729", always_xy=True)

# If you'd rather use Antarctic Polar Stereographic:
# transformer = Transformer.from_crs("EPSG:4326", "EPSG:3031", always_xy=True)


def parse_antennas_from_rad(rad_path: Path) -> str | None:
    """
    Extract '250' or '500' from a line like:
      ANTENNAS:500 MHz shielded=2
    """
    if not rad_path.exists():
        return None

    text = rad_path.read_text(errors="ignore")
    m = re.search(r"^ANTENNAS:\s*([0-9]+)\s*MHz", text, flags=re.MULTILINE)
    if not m:
        return None

    val = m.group(1)
    return val if val in {"250", "500"} else val


def cor_has_data(cor_path: Path) -> bool:
    """
    Quick check: at least one non-empty line with multiple tab-separated fields.
    (Your .cor example has no header, just data lines.)
    """
    try:
        with cor_path.open("r", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Expecting tab-separated numeric-ish data
                if "\t" in line and len(line.split("\t")) >= 8:
                    return True
        return False
    except OSError:
        return False


def read_cor(cor_path: Path) -> pd.DataFrame:
    """
    Parse Malå .cor lines like:
      trace_id  YYYY-MM-DD  HH:MM:SS  lat  S/N  lon  W/E  elev  M  quality
    """
    rows = []
    with cor_path.open("r", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            parts = line.split("\t")
            if len(parts) < 9:
                continue

            # Defensive parsing
            try:
                trace_id = int(parts[0])
                date = parts[1]
                time = parts[2]

                lat = float(parts[3])
                lat_hemi = parts[4].upper()

                lon = float(parts[5])
                lon_hemi = parts[6].upper()

                elev = float(parts[7])
                elev_unit = parts[8].upper()
            except Exception:
                continue

            if lat_hemi == "S":
                lat = -abs(lat)
            elif lat_hemi == "N":
                lat = abs(lat)

            if lon_hemi == "W":
                lon = -abs(lon)
            elif lon_hemi == "E":
                lon = abs(lon)

            rows.append(
                {
                    "trace_id": trace_id,
                    "date": date,
                    "time": time,
                    "lat": lat,
                    "lon": lon,
                    "elev": elev,
                    "elev_unit": elev_unit,
                }
            )

    return pd.DataFrame(rows)


def main() -> None:
    if not DATA_DIR.exists():
        raise FileNotFoundError(f"DATA_DIR does not exist: {DATA_DIR}")

    # Case-insensitive scan for .cor
    cor_files = [p for p in DATA_DIR.iterdir() if p.is_file() and p.suffix.lower() == ".cor"]

    print(f"DATA_DIR: {DATA_DIR}")
    print(f"Found {len(cor_files)} .cor files (case-insensitive).")

    all_tracks = []
    skipped_empty = 0
    skipped_parse = 0

    for cor_path in cor_files:
        if not cor_has_data(cor_path):
            skipped_empty += 1
            continue

        rad_path = cor_path.with_suffix(".rad")
        # In case the RAD is uppercase/lowercase mismatch, try both:
        if not rad_path.exists():
            rad_path = cor_path.with_suffix(".RAD")

        antenna_mhz = parse_antennas_from_rad(rad_path)

        df = read_cor(cor_path)
        if df.empty:
            skipped_parse += 1
            continue

        # Project to easting/northing
        east, north = transformer.transform(df["lon"].to_numpy(), df["lat"].to_numpy())
        df["easting"] = east
        df["northing"] = north

        df["file_base"] = cor_path.stem
        df["antenna_mhz"] = antenna_mhz

        all_tracks.append(df)

    if not all_tracks:
        print(f"Skipped empty/no-data .cor: {skipped_empty}")
        print(f"Skipped unparseable .cor: {skipped_parse}")
        print("No valid radar tracks found.")
        return

    out = pd.concat(all_tracks, ignore_index=True)
    out.to_csv(OUT_CSV, index=False)

    print(f"Skipped empty/no-data .cor: {skipped_empty}")
    print(f"Skipped unparseable .cor: {skipped_parse}")
    print(f"Wrote {len(out):,} points to: {OUT_CSV}")


if __name__ == "__main__":
    main()
