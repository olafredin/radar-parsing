# radar-parsing

Tools to parse, quality-control and quick-look plot **Malå ground-penetrating radar (GPR)**
data: `.rad` headers, `.rd3`/`.rd7` traces and `.cor` GNSS positions.

Written for the 2025/26 snowmobile-towed GPR survey on the western Riiser-Larsen Ice Shelf
(near Neumayer III; 13–23 December 2025; 250 and 500 MHz shielded antennas, time-triggered
traces every 0.25 s). It works for any Malå export with the same file set: survey-specific
settings live in a config file.

> **Data is not in this repository.** Raw files and pipeline outputs are git-ignored. Get the
> data from Ola Fredin (shared by FTP) and put it in `data_raw/` (see [Data layout](#data-layout)).

---

## Install

Python ≥ 3.10. From the repository folder:

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate      macOS/Linux:  source .venv/bin/activate
pip install -e ".[gis,dev]"
```

This installs the `radar-parsing` command. `python -m radar_parsing …` does the same thing.
The `gis` extra (geopandas, pyogrio, shapely) is only needed for the GeoPackage export;
leave it out for a lighter install.

With conda: `conda create -n radar python=3.12 numpy pandas pyproj matplotlib geopandas pyogrio pytest`,
then `pip install -e . --no-deps`.

> **Windows, Python 3.13.0:** plot windows fail with *"Tcl wasn't installed properly"* inside a
> venv ([cpython #125235](https://github.com/python/cpython/issues/125235)). Upgrade Python
> and recreate the venv, or use `plot … --save`.

## Data layout

```
radar-parsing/
├── data_raw/                  ← raw Malå export, one set per recording (not in git)
│   ├── DAT_0012_A1.rad        header (text)
│   ├── DAT_0012_A1.rd3        traces (binary, int16)
│   ├── DAT_0012_A1.cor        GNSS fixes (text, tab-separated)
│   ├── DAT_0012_A1.mrk        markers
│   ├── DAT_0012_A1.em         (empty in 2025/26)
│   └── MANIFEST.sha256        checksums, see "Transferring data"
└── output/                    ← everything the pipeline writes (not in git)
    ├── cor_qc/                QC'd .cor files, same format as the originals
    ├── cor_qc_report.csv      what was fixed in each file
    ├── cor_qc_flags.csv       what was fixed in each GNSS fix
    ├── radar_tracks.gpkg      map layers for QGIS (EPSG:3031)
    ├── mala_tracks_compiled.csv
    ├── mala_tracks_compiled_qc.csv
    ├── inventory.csv          one row per recording
    ├── figures/               PNGs from `plot --save`
    └── reflexw/               recordings ready for ReflexW (`export`, `merge`)
```

Other folders can be used with `--raw-dir` and `--out-dir` on any command.

## Quick start

```bash
radar-parsing run                    # fix-cor → tracks → track-qc → inventory → gis
radar-parsing plot DAT_0013_A1       # radargram + track in a window
radar-parsing plot DAT_0013_A1 --gain 1 --save   # PNGs to output/figures/
```

## Commands

| Command | Reads | Writes | What it does |
|---|---|---|---|
| `fix-cor` | `data_raw/*.cor` | `output/cor_qc/*.cor`, `cor_qc_report.csv`, `cor_qc_flags.csv` | Repairs garbled lines, rejects bad GNSS fixes and interpolates across them (positions and elevation). Output keeps the Malå format, so it can replace the original `.cor` in other GPR software. |
| `tracks` | `output/cor_qc/*.cor`, `data_raw/*.rad` | `output/mala_tracks_compiled.csv` | One table of all fixes: signed lat/lon, elevation, easting/northing (EPSG from config), recording name, antenna frequency. Ready for QGIS. |
| `track-qc` | `mala_tracks_compiled.csv` | `mala_tracks_compiled_qc.csv` | Optional second pass: flags points outside the bbox, without time, or implying > 10 m/s between consecutive fixes; interpolates (default) or drops them (`--action drop`). Adds `qc_*` columns. |
| `inventory` | `data_raw/`, `output/cor_qc/` | `output/inventory.csv` | Per recording: antenna, samples, time window, trace count (checked against `LAST TRACE`), number of GNSS fixes, start/end time, recorded length (`length_km`) and distance moved during recording pauses (`gap_km`), elevation range. |
| `plot BASE` | `data_raw/BASE.*`, `output/cor_qc/BASE.cor` | screen or `output/figures/` | Quick-look radargram (trace mean removed, optional t^p gain, two-way time in ns) and the GNSS track interpolated to every trace. |
| `manifest` | `data_raw/` | `data_raw/MANIFEST.sha256` | SHA-256 checksums of every raw file; `manifest --check` verifies a received copy. |
| `export BASE… \| --all` | `data_raw/`, `output/cor_qc/` | `output/reflexw/` | Copies recordings for ReflexW with the **QC'd** `.cor` and a header whose `LAST TRACE` matches the data. Traces are unchanged. |
| `merge BASE BASE…` | `data_raw/`, `output/cor_qc/` | `output/reflexw/` | Joins consecutive recordings into one profile (e.g. `DAT_0032_0033_A1`). Renumbers the `.cor` traces and reports the time and distance gap at each joint. Recordings with different antenna, samples or sampling frequency are refused. |
| `gis` | `output/cor_qc/`, `cor_qc_flags.csv`, `output/reflexw/` | `output/radar_tracks.gpkg` | Map layers for QGIS in EPSG:3031 (Quantarctica): transects, fixes with QC flags, trace-number ticks, ReflexW profiles. `--format shp` writes shapefiles to `output/gis_shp/`. See [Map in QGIS](#map-in-qgis). |
| `run` | | | `fix-cor`, `tracks`, `track-qc`, `inventory` and (if installed) `gis` in sequence. |

`radar-parsing <command> --help` lists all options.

## How the .cor QC works

The 2025/26 Malå GNSS log has systematic glitches: dropped or swapped characters in
coordinates, elevations and times, fields shifted by one column, and zero fixes.
`fix-cor` handles each `.cor` file as follows:

1. **Line repair.** The unit restored to `M` when garbled (`38.940  ,  0.6`). Fused elevation split (`38.944M  9  0.6` → 38.944). Shifted elevation moved back (`M  9  38.975` → 38.975; quality unknown, written as `.`).
2. **Invalid fixes.** A fix is invalid if lat/lon can't be parsed, is 0, falls outside the bounding box, or has the wrong hemisphere letter. Example: `7.82072231550 S` with a dropped digit.
3. **Timestamp check.** Trace numbers are reliable and the radar fires every `TIME INTERVAL` (0.25 s), so *time − trace × 0.25 s* is constant while recording and steps up at a pause. Timestamps that are unparseable (`32:21:0.`) or off this trace clock by more than `time_tol_s` (2 s) are ignored.
4. **Teleport test.** Each fix is compared with the last good one (the anchor). Elapsed time is the larger of the trace-clock and timestamp differences; the timestamp covers recording pauses. Faster than `max_speed_mps` (6 m/s; 99.5 % of real fix-to-fix speeds are below 4.8 m/s) is rejected. Without any usable time, a step over `max_step_m` (40 m) is rejected. If `relock_after` (10) consecutive rejected fixes agree with each other, they are accepted and become the anchor, so one bad anchor cannot reject the rest of the file.
5. **Elevation spikes.** Elevations more than `elev_max_dev_m` (2 m) from an 11-fix running median are rejected, e.g. `8.962` for 38.962 or `38957.0`.
6. **Interpolation.** Rejected positions and elevations are linearly interpolated in line order; values at the ends are held. Interpolation never crosses a recording pause longer than `pause_s` (10 s; found from the trace clock), because the sledge may have moved while trace numbering stood still. Rejected fixes next to a pause are held at the nearest good fix on their own side. Coordinates keep their original number of decimals; trace numbers, dates and times are not changed.
7. Files without a single valid fix (test recordings, GNSS off) are skipped and listed as `no_valid_fix` or `empty` in the report.

On the 2025/26 data (121 421 fixes in 103 files), 1 046 positions (0.9 %) and 374 elevations
(0.3 %) are interpolated.

The QC targets isolated glitches. Periods where the GNSS solution itself degraded (smooth
elevation drifts of hundreds of metres, as in DAT_0051) are left as they are. Look for them in
the `elev_min` and `elev_max` columns of `inventory.csv`.

All thresholds are in [`configs/rlis_2025-26.toml`](configs/rlis_2025-26.toml), which also holds
the built-in defaults. File-format details and the full glitch catalogue are in
[`docs/mala_file_formats.md`](docs/mala_file_formats.md).

## Map in QGIS

`radar-parsing run` (or `radar-parsing gis` after `fix-cor`) writes `output/radar_tracks.gpkg`.
Drag it into QGIS; it is in **EPSG:3031**, the projection of
[Quantarctica](https://www.npolar.no/quantarctica/), so it overlays the Quantarctica base
layers directly. Four layers:

| Layer | Geometry | What it is for |
|---|---|---|
| `transects` | line per recording | Coverage map. Label with `file_base`. The line is split (`n_segments` > 1) where consecutive fixes are more than 50 m apart, so a move during a recording pause doesn't show as a fake straight segment. Attributes: antenna, date, `t_start`/`t_end`, `dur_min`, `length_km` (recorded length), `gap_km` (moved during pauses, not recorded), `n_traces`, `n_fixes`, `samples`, `twin_ns` (time window), `n_pos_int`/`n_elev_int` (fixes interpolated by the QC), `elev_min`/`elev_max`, `header_ok`. |
| `profiles` | line per file in `output/reflexw/` | What you open in ReflexW, i.e. exported and merged profiles. |
| `trace_ticks` | point every 500 traces | Finds radargram trace numbers on the map: label with `label` (the recording name at `kind` = `start`, the trace number elsewhere). |
| `fixes` | point per GNSS fix | QC check. Style by `pos_flag` (`invalid`, `teleport`) and `elev_flag` (`repaired`, `unparseable`, `spike`) to see what `fix-cor` changed. Also `trace_id`, `time`, `elev`, `quality`, `time_ign`. |

`radar-parsing gis --format shp` writes the same layers as shapefiles to `output/gis_shp/`
(the format Quantarctica's own [dataset guidelines](https://media.npolar.no/download/quantarctica/Quantarctica3/Making%20Quantarctica-friendly%20datasets.pdf)
use, should the tracks ever go there). Tick spacing, the split distance and the EPSG code are in the `[gis]` section of the config;
`--tick-every` overrides the spacing for one run. Times are as logged by the GNSS receiver
(normally UTC).

**In the field:** after each day, copy the new Malå files into `data_raw/` and run
`radar-parsing run`; reopen the GeoPackage in QGIS (or refresh the layers) to see the day's
coverage and any GNSS problems before planning the next lines.

## Into ReflexW

```bash
radar-parsing run                                  # QC first
radar-parsing export DAT_0013_A1 DAT_0014_A1       # single recordings
radar-parsing merge DAT_0032_A1 DAT_0033_A1        # one profile from consecutive files
```

In ReflexW, import `output/reflexw/` with the import format **MALA RD3** and the trace
headers set to **RAMAC-GPS**. ReflexW then reads the `.cor` with the same name into the trace
headers and interpolates between fixes ([Sandmeier: GPS coordinates in REFLEXW](https://www.sandmeier-geo.de/Download/gpscoordinates.pdf)).
The `.cor` keeps the Malå format (lat/lon in degrees); switch on ReflexW's UTM conversion if
you want metres.

Check the joint report from `merge`. Recordings are joined end to end without gap traces, so a
long pause or a big jump between files shows up as a step in the profile.

### Writing your own processed data

```python
from radar_parsing.rad import RadHeader
from radar_parsing.rd3 import read_traces
from radar_parsing.writer import write_bundle
from radar_parsing.cor import read_lines

hdr = RadHeader.from_file("data_raw/DAT_0013_A1.rad")
a = read_traces("data_raw/DAT_0013_A1.rd3", hdr.samples).astype(float)   # (traces, samples)
a = a - a.mean(axis=1, keepdims=True)                                     # your processing
write_bundle("output/reflexw", "DAT_0013_proc", a, "data_raw/DAT_0013_A1.rad",
             cor_lines=read_lines("output/cor_qc/DAT_0013_A1.cor"), fit="clip")
```

`.rd3` stores 16-bit integers, so floats are rounded. `fit="clip"` cuts values outside
±32767, `fit="scale"` rescales the peak to 32767 (relative amplitudes kept, absolute lost),
and the default `fit="error"` refuses data that doesn't fit. Use the extension `.rd7` for
32-bit output (`ext=".rd7"`). The header is copied from the template, with SAMPLES,
TIMEWINDOW, LAST TRACE and STOP POSITION updated. Existing files are never overwritten
unless you pass `overwrite=True` (or `--overwrite` on the command line).

## Starting a new season

1. Copy the Malå export (all files per recording) into `data_raw/` (or a new folder, used with `--raw-dir`).
2. Copy `configs/rlis_2025-26.toml` to a new file. Set the **bounding box** for the new survey area; fixes outside it are discarded. Check `epsg`: use UTM 29S only between 6° W and 12° W, otherwise the right UTM zone or 3031.
3. Run `radar-parsing run --config configs/<new>.toml` and read `output/cor_qc_report.csv` and `output/inventory.csv`. Many interpolated fixes, or `data_ok = False`, points to a problem.
4. Spot-check a few recordings with `plot … --save`.

## Transferring data

The raw data is ~2 GB and goes by FTP, not git. Before sending:

```bash
radar-parsing manifest             # writes data_raw/MANIFEST.sha256
```

The receiver checks the copy with `radar-parsing manifest --check` (or `sha256sum -c MANIFEST.sha256`
on Linux/macOS).

## Development

```bash
pytest
```

The tests build small synthetic Malå files on the fly; no survey data is needed.

```
radar_parsing/
├── cli.py        command-line interface
├── config.py     QC parameters (+ TOML loading)
├── rad.py        .rad header reader
├── rd3.py        .rd3/.rd7 reader, time axis, simple processing
├── cor.py        .cor reading, line repair, writing
├── qc.py         fix-cor QC
├── tracks.py     compile tracks, track-qc, per-trace navigation
├── inventory.py  inventory and checksums
├── writer.py     .rad/.rd3 writing, export and merge for ReflexW
├── gis.py        GeoPackage/shapefile layers for QGIS
├── plot.py       quick-look plots
└── geo.py        distances, hemisphere handling
```

Version 0.1 was a set of scripts in `scripts/`; see [CHANGELOG.md](CHANGELOG.md) for what changed.

## Related tools

Rickard Pettersson (Uppsala University) has MATLAB readers and writers for Malå files
(`malaread3`, `malawrite3`, …). They are not part of this repository; ask him for them.
This package's reader and writer were written independently from the file format; they
cover what `malaread3`/`malawrite3`/`uimalaread` do. For his `wgs2ps`/`ps2wgs`
polar-stereographic conversions, set `epsg = 3031` in the config.

## License

MIT, see [LICENSE](LICENSE).
