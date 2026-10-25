# Malå file formats (as seen in the 2025/26 RLIS data)

Each recording `DAT_xxxx_A1` is a set of files with the same base name.

| File | Content |
|---|---|
| `.rad` | Header, plain text `KEY:VALUE` |
| `.rd3` | Traces, binary signed 16-bit little-endian (`.rd7`: 32-bit) |
| `.cor` | GNSS fixes, tab-separated text, CRLF line endings |
| `.mrk` | Markers; in 2025/26 only `HEADER VERSION:100` |
| `.em`  | Empty in 2025/26 |

## .rad header

Fields used by this package (example from `DAT_0008_A1.rad`):

| Key | Example | Meaning |
|---|---|---|
| `SAMPLES` | 2024 | Samples per trace |
| `FREQUENCY` | 6532.499512 | **Sampling** frequency in MHz (not the antenna frequency). Sample interval = 1000 / FREQUENCY ns |
| `TIMEWINDOW` | 309.835458 | Time window in ns, = SAMPLES × 1000 / FREQUENCY (holds for all 118 files) |
| `TIME INTERVAL` | 0.250000 | Seconds **between traces** (time-triggered, `TIME FLAG:1`), not within a trace |
| `LAST TRACE` | 843 | Number of traces; must match the `.rd3` size |
| `ANTENNAS` | `250 MHz shielded=1` | Nominal antenna frequency |
| `STACKS` | 4 | Stacks per trace |

The old `Parse_rd3.py` took `TIME INTERVAL` as a sample interval. It is the trace interval;
the time axis now comes from `FREQUENCY`.

## .rd3 traces

No header and no trace headers: `LAST TRACE` × `SAMPLES` int16 values, stored trace by trace
(little-endian, matching Rickard Pettersson's `malaread3.m`: `fread(fid, [samples inf], 'int16')`).
The trace count follows from the file size: `size / (2 × SAMPLES)`.

## .cor GNSS fixes

One line per fix, no header. Fix interval is about 1 s (every ~4th trace):

```
0 trace  1 date        2 time    3 lat           4   5 lon           6   7 elev   8   9 quality
1        2025-12-13    15:20:39  71.92496373583  S   11.04844133633  W   38.916   M   0.6
```

Trace numbers are 1-based and refer to the `.rd3` traces. Coordinates are unsigned with
hemisphere letters; elevation in metres. The last column is a GNSS quality value
(0.6–0.7 throughout, probably DOP).

### Glitch catalogue

Every line in the 2025/26 files has 10 tab-separated fields, but individual characters are
often lost or swapped. Trace numbers are never affected: they are integers and strictly
increasing in every file. Counts are from `fix-cor` over all 121 421 lines in the 103 files
with GNSS data.

| Pattern | Lines | Example | Handling |
|---|---|---|---|
| Unit garbled | 97 | `W 38.940 , 0.6` / `W 38.940 9 0.6` | Unit restored to `M` |
| Elevation fused with unit | 80 | `W 38.944M 9 0.6` | Elevation 38.944 (old scripts wrote **0.6**) |
| Elevation shifted to last column | 295 | `W M 9 38.975` | Elevation 38.975, quality `.` (old scripts copied the elevation into the quality column) |
| Elevation unrecoverable | 97 | `W M 9 0.68.965` | Interpolated |
| Elevation with dropped/extra digits | 277 | `W 8.962 M 0.6` (38.962), `38957.0` | Spike filter → interpolated |
| Invalid position | 654 | `7.82072231550 S` (dropped digit), hemisphere `1` instead of `W`, lat/lon 0 | Interpolated |
| Position jump inside the bbox (mostly garbled digits) | 392 | longitude `12.11964065` between fixes at `12.1293…` | Teleport test → interpolated |
| Garbled timestamp | 461 | `15:22:5.`, `32:21:0.`, `10:24:10` between 10:24:50 and 10:24:52 | Ignored; trace clock used |
| Empty file | 15 files | GNSS off / test recordings | Skipped |

`output/cor_qc_report.csv` lists the counts per file after each `fix-cor` run.

### Degraded GNSS solutions

The glitches above are character-level and isolated. A different problem is a degraded GNSS
solution: smooth excursions that no spike filter can catch, and that should probably not be "fixed".
Example: DAT_0051 on 18 Dec 2025, ~10:11–10:16, where the elevation drifts from +4 m to
−425 m and back up to +268 m while the quality value rises from 0.6–0.7 to 2.5–8.5. Check
`elev_min` and `elev_max` in `output/inventory.csv`, and treat positions in such windows with
caution too. The quality column itself is garbled too often (`06` for 0.6) to be a reliable
flag on its own.

### Pauses

Trace numbering stops while recording is paused, but the GNSS clock runs on. For example,
DAT_0012 has 23 880 traces (5 970 s at 0.25 s) but spans 8 144 s of GNSS time. In 2025/26 the
sledge often moved during pauses (up to 1.3 km), probably because the Malå units ran out of battery, so `fix-cor` never interpolates across a pause, and the GIS export splits transects there. This is also why the teleport
test uses the larger of the trace-clock and timestamp differences.

### Stale LAST TRACE

In six recordings (DAT_0047, 0048, 0057, 0067, 0083, 0093) `LAST TRACE` in the .rad is smaller
than the number of traces in the .rd3 (e.g. 17 vs 1 099). The .cor trace numbers agree with
the .rd3 size, so the header is wrong; the code uses the .rd3 size. `inventory` flags these in
`header_last_trace_ok`.
