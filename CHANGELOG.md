# Changelog

## 0.2.0 — 2026-10-23

Refactor of the 2025/26 field scripts into a package with a command-line interface.

### Changed
- The scripts in `scripts/` are replaced by the `radar_parsing` package and one command:

  | Old script | New command |
  |---|---|
  | `Mala_fix_cor.py`, `Mala_fix_cor2.py` | `radar-parsing fix-cor` |
  | `Mala_tracks.py` | `radar-parsing tracks` |
  | `Mala_posQC.py` | `radar-parsing track-qc` |
  | `Parse_rd3.py` | `radar-parsing plot`, `radar-parsing inventory` |

- QC thresholds moved from code into a TOML config (`configs/rlis_2025-26.toml`); defaults unchanged.
- Raw data is read from `data_raw/` only; outputs go to `output/` (QC'd `.cor` in `output/cor_qc/`).
  `.rd3` files no longer need to be copied next to the QC'd `.cor` files.
- QC'd `.cor` files are written with CRLF line endings on every OS, as Malå does.
- Survey data removed from the repository and its history; see README for data transfer.

### Fixed
- **Teleport test locked up on garbled timestamps.** When the last good fix had a garbled time,
  the test fell back to a 40 m step limit against that fix forever, rejecting the rest of the
  file and replacing it with a straight line. Five recordings were affected: DAT_0032, 0037,
  0051, 0067 and 0104, with 63–96 % of fixes rejected and the track off by up to 9 km
  (DAT_0051). Elapsed time now also comes from trace numbers × `TIME INTERVAL`. Timestamps
  that disagree with that clock are ignored, and a run of 10 consistent rejected fixes resets
  the anchor. Rejected fixes over all files: 11 715 → 392. Total track length: 305.6 → 338.7 km.
- Elevation fused with its unit (`38.944M`) was replaced by the quality value (0.6 m); now 38.944.
- Shifted lines (`M 9 38.975`) put the elevation into the quality column; quality is now `.`.
- Garbled elevations (dropped digits, e.g. `8.962` or `38957.0`) were passed through to the
  track table; they are now detected against a running median and interpolated.
- Lines whose elevation could not be parsed were silently dropped from the track table;
  they are now kept with an interpolated elevation.
- Radargram time axis: `TIME INTERVAL` (seconds between traces) was used as a sample interval;
  the axis now uses the sampling `FREQUENCY` and is labelled in ns.

### Added
- `inventory`: per-recording table (header values, trace count vs `LAST TRACE`, GNSS coverage,
  track length).
- `manifest`: SHA-256 checksums for transferring the raw data.
- `.rd7` (32-bit) support, `plot --save`, per-file QC report.
- Test suite on synthetic data.

Regression against the 0.1 outputs (all 103 QC'd files, 121 421 fixes): positions are
identical in 91 files. The 12 files that differ are the five above plus seven where 1–62 fixes
moved by 3–226 m. The largest is DAT_0086, where 0.1 had shifted ~60 good fixes by ~200 m
after a garbled timestamp. Elevations differ only where the fixes above apply. The track table
gains 97 rows that 0.1 dropped.

## 0.1.0 — 2025-12 / 2026-01

Field scripts written during the 2025/26 season.
