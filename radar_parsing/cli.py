"""Command-line interface: ``radar-parsing <command>`` or ``python -m radar_parsing <command>``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

from . import __version__
from .config import Config, load_config

log = logging.getLogger("radar_parsing")

# fix-cor warns when a file has this many consecutive fixes outside the bbox.
# Garbled fixes came in runs of at most 15 in 2025/26.
OUTSIDE_RUN_WARN = 30


class Paths:
    def __init__(self, args: argparse.Namespace):
        self.raw = Path(args.raw_dir)
        self.out = Path(args.out_dir)
        self.cor_qc = self.out / "cor_qc"
        self.cor_qc_report = self.out / "cor_qc_report.csv"
        self.cor_qc_flags = self.out / "cor_qc_flags.csv"
        self.gpkg = self.out / "radar_tracks.gpkg"
        self.shp_dir = self.out / "gis_shp"
        self.tracks = self.out / "mala_tracks_compiled.csv"
        self.tracks_qc = self.out / "mala_tracks_compiled_qc.csv"
        self.inventory = self.out / "inventory.csv"
        self.figures = self.out / "figures"
        self.reflexw = self.out / "reflexw"

    def require_raw(self) -> None:
        if not self.raw.is_dir():
            sys.exit(f"Raw data folder not found: {self.raw.resolve()}  (use --raw-dir)")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_fix_cor(p: Paths, cfg: Config, args) -> None:
    from .qc import flags_table, qc_directory

    p.require_raw()
    results = qc_directory(p.raw, p.cor_qc, cfg)
    report = pd.DataFrame([r.as_dict() for r in results])
    p.out.mkdir(parents=True, exist_ok=True)
    report.to_csv(p.cor_qc_report, index=False)
    flags_table(results).to_csv(p.cor_qc_flags, index=False)
    written = report[report["status"] == "written"] if len(report) else report
    print(f"fix-cor: wrote {len(written)} of {len(report)} .cor files to {p.cor_qc}")
    if len(report):
        skipped = report[report["status"] != "written"]
        print(f"  skipped {len(skipped)} (empty or no valid GNSS fix)")
        print(f"  fixes interpolated: {int(written['n_interpolated'].sum()):,} "
              f"(of which teleport jumps: {int(written['n_teleport'].sum()):,})")
        print(f"  elevation: un-fused {int(written['n_elev_fused'].sum())}, "
              f"un-shifted {int(written['n_elev_shifted'].sum())}, "
              f"spikes {int(written['n_elev_spikes'].sum())}, "
              f"interpolated {int(written['n_elev_interpolated'].sum())}")
    tq = cfg.cor_qc
    print(f"  teleport QC {'on' if tq.teleport_qc else 'off'} (max {tq.max_speed_mps} m/s, step {tq.max_step_m} m)")
    print(f"  per-file report: {p.cor_qc_report}")
    if len(report):
        outside = report[(report["max_outside_run"] >= OUTSIDE_RUN_WARN)
                         | ((report["status"] == "no_valid_fix") & (report["n_outside_bbox"] > 0))]
        if len(outside):
            b = cfg.bbox
            print(f"  WARNING: {len(outside)} file(s) have long stretches outside the bbox "
                  f"(lat {b.lat_min}..{b.lat_max}, lon {b.lon_min}..{b.lon_max}) or not in the "
                  f"{tq.expected_ns}/{tq.expected_ew} hemispheres. Those positions were replaced by "
                  f"interpolation or the file was skipped. Check [bbox] and [cor_qc] expected_ns/expected_ew "
                  f"in the config (--config):")
            for _, r in outside.iterrows():
                print(f"    {r['file']}: {r['n_outside_bbox']} of {r['n_lines']} fixes outside ({r['status']})")


def cmd_tracks(p: Paths, cfg: Config, args) -> None:
    from .tracks import compile_tracks

    if not p.cor_qc.is_dir():
        sys.exit(f"No QC'd .cor files in {p.cor_qc}; run 'fix-cor' first.")
    df = compile_tracks(p.cor_qc, p.raw, cfg)
    df.to_csv(p.tracks, index=False)
    print(f"tracks: {len(df):,} points from {df['file_base'].nunique() if len(df) else 0} files "
          f"(EPSG:{cfg.epsg}) -> {p.tracks}")


def cmd_track_qc(p: Paths, cfg: Config, args) -> None:
    from .tracks import track_qc

    if args.action:
        cfg.track_qc.action = args.action
    if not p.tracks.exists():
        sys.exit(f"{p.tracks} not found; run 'tracks' first.")
    out, stats = track_qc(pd.read_csv(p.tracks), cfg)
    out.to_csv(p.tracks_qc, index=False)
    pct = stats["n_bad"] / max(stats["n_total"], 1) * 100
    print(f"track-qc: flagged {stats['n_bad']:,} of {stats['n_total']:,} points ({pct:.2f}%), "
          f"action={cfg.track_qc.action} -> {p.tracks_qc}")


def cmd_inventory(p: Paths, cfg: Config, args) -> None:
    from .inventory import inventory

    p.require_raw()
    df = inventory(p.raw, p.cor_qc if p.cor_qc.is_dir() else None)
    p.out.mkdir(parents=True, exist_ok=True)
    df.to_csv(p.inventory, index=False)
    gnss = int((df["gnss_fixes"] > 0).sum()) if len(df) else 0
    km = df["length_km"].sum() if "length_km" in df else 0
    gap = df["gap_km"].sum() if "gap_km" in df else 0
    print(f"inventory: {len(df)} recordings, {gnss} with GNSS, {km:.1f} km recorded "
          f"(+ {gap:.1f} km moved during pauses) -> {p.inventory}")
    if len(df):
        bad = df.loc[~df["data_ok"], "file_base"].tolist()
        stale = df.loc[df["data_ok"] & ~df["header_last_trace_ok"], "file_base"].tolist()
        if bad:
            print(f"  WARNING: .rd3/.rd7 missing, .rad without SAMPLES, or .rd3/.rd7 size "
                  f"inconsistent with SAMPLES (half-copied?): {', '.join(bad)}")
        if stale:
            print(f"  note: .rad LAST TRACE differs from the .rd3 size in {len(stale)} recording(s) "
                  f"({', '.join(stale)}); the .rd3 size is used")


def cmd_manifest(p: Paths, cfg: Config, args) -> None:
    from .inventory import check_manifest, write_manifest

    p.require_raw()
    if args.check:
        problems = check_manifest(p.raw, args.file)
        if problems:
            for name, what in problems:
                print(f"  {what}: {name}")
            sys.exit(f"manifest: {len(problems)} problem(s)")
        print("manifest: all files match")
    else:
        out = write_manifest(p.raw, args.file)
        print(f"manifest: wrote {out}")


def cmd_plot(p: Paths, cfg: Config, args) -> None:
    from .plot import plot_bundle

    p.require_raw()
    written = plot_bundle(
        args.base,
        p.raw,
        qc_dir=p.cor_qc,
        max_traces=args.max_traces,
        gain=args.gain,
        extrapolate=not args.no_extrapolation,
        save_dir=p.figures if args.save else None,
    )
    for w in written:
        print(f"plot: wrote {w}")


def _qc_dir_or_warn(p: Paths) -> Path | None:
    if p.cor_qc.is_dir():
        return p.cor_qc
    print(f"  WARNING: no QC'd .cor files in {p.cor_qc}; raw .cor files are used. Run 'fix-cor' first.")
    return None


def cmd_export(p: Paths, cfg: Config, args) -> None:
    from .writer import export_bundle

    p.require_raw()
    qc = _qc_dir_or_warn(p)
    bases = args.bases
    if args.all:
        bases = sorted(f.stem for f in p.raw.iterdir() if f.suffix.lower() in (".rd3", ".rd7"))
    if not bases:
        sys.exit("export: give recording names or --all")
    for base in bases:
        r = export_bundle(base, p.raw, qc, p.reflexw, overwrite=args.overwrite)
        note = "  (LAST TRACE corrected)" if r.header_fixed else ""
        print(f"export: {base}: {r.n_traces} traces, .cor {r.cor}{note}")
    print(f"  -> {p.reflexw}  (ReflexW: import format MALA RD3, trace headers RAMAC-GPS)")


def cmd_merge(p: Paths, cfg: Config, args) -> None:
    from .writer import merge_bundles

    p.require_raw()
    qc = _qc_dir_or_warn(p)
    try:
        r = merge_bundles(args.bases, p.raw, qc, p.reflexw, name=args.name, overwrite=args.overwrite)
    except (ValueError, FileExistsError) as e:
        sys.exit(f"merge: {e}")
    print(f"merge: {r.name}: {r.n_traces} traces from {len(r.parts)} recordings -> {p.reflexw}")
    for part in r.parts:
        print(f"  {part['base']}: traces {part['first_trace']}-{part['first_trace'] + part['n_traces'] - 1}, .cor {part['cor']}")
    for j in r.junctions:
        gap_s = f"{j.gap_s:.0f} s" if j.gap_s is not None else "? s"
        gap_m = f"{j.gap_m:.0f} m" if j.gap_m is not None else "? m"
        flag = "  <-- check" if (j.gap_m or 0) > 50 or (j.gap_s is not None and j.gap_s < 0) else ""
        print(f"  joint {j.after} | {j.before}: {gap_s}, {gap_m} between last and first GNSS fix{flag}")


GIS_HINT = 'GIS export needs the optional packages: pip install -e ".[gis]"'


def cmd_gis(p: Paths, cfg: Config, args) -> None:
    try:
        from .gis import build_layers, write_gpkg, write_shapefiles
    except ImportError:
        sys.exit(f"gis: {GIS_HINT}")
    p.require_raw()
    if not p.cor_qc.is_dir():
        sys.exit(f"No QC'd .cor files in {p.cor_qc}; run 'fix-cor' first.")
    if getattr(args, "tick_every", None):
        cfg.gis.tick_every = args.tick_every
    flags = pd.read_csv(p.cor_qc_flags, keep_default_na=False) if p.cor_qc_flags.exists() else None
    if flags is not None and len(flags):
        flags["time_ignored"] = flags["time_ignored"].astype(str).str.lower() == "true"
    layers = build_layers(p.cor_qc, p.raw, cfg, flags=flags, reflexw_dir=p.reflexw)
    if not layers:
        print("gis: no recording with GNSS fixes; nothing written")
        return
    if getattr(args, "format", "gpkg") == "shp":
        out = write_shapefiles(layers, p.shp_dir)
        where = p.shp_dir
    else:
        where = write_gpkg(layers, p.gpkg)
    counts = ", ".join(f"{k} {len(v):,}" for k, v in layers.items())
    print(f"gis: EPSG:{cfg.gis.epsg}; {counts} -> {where}")
    if where != p.gpkg and where != p.shp_dir:
        print(f"  note: {p.gpkg.name} is locked (open in QGIS?); wrote {where.name} instead")


def cmd_run(p: Paths, cfg: Config, args) -> None:
    cmd_fix_cor(p, cfg, args)
    cmd_tracks(p, cfg, args)
    args.action = None
    cmd_track_qc(p, cfg, args)
    cmd_inventory(p, cfg, args)
    try:
        import geopandas  # noqa: F401
    except ImportError:
        print(f"gis: skipped. {GIS_HINT}")
        return
    cmd_gis(p, cfg, args)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--raw-dir", default="data_raw", help="Folder with raw Malå files (default: data_raw)")
    common.add_argument("--out-dir", default="output", help="Folder for all outputs (default: output)")
    common.add_argument("--config", default=None, help="TOML file with QC parameters (default: built-in RLIS 2025/26 values)")
    common.add_argument("-v", "--verbose", action="store_true")

    ap = argparse.ArgumentParser(prog="radar-parsing", description="Parse and QC Malå GPR data (.rad/.rd3/.cor).")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("run", parents=[common], help="fix-cor + tracks + track-qc + inventory (+ gis if installed)")
    sub.add_parser("fix-cor", parents=[common], help="QC raw .cor files -> OUT/cor_qc/")
    sub.add_parser("tracks", parents=[common], help="Compile QC'd .cor files -> OUT/mala_tracks_compiled.csv")
    s = sub.add_parser("track-qc", parents=[common], help="Second-pass QC -> OUT/mala_tracks_compiled_qc.csv")
    s.add_argument("--action", choices=["interpolate", "drop"], default=None)
    sub.add_parser("inventory", parents=[common], help="Table of all recordings -> OUT/inventory.csv")
    s = sub.add_parser("manifest", parents=[common], help="Write/check SHA-256 checksums of the raw folder")
    s.add_argument("--check", action="store_true", help="Verify instead of write")
    s.add_argument("--file", default=None, help="Manifest path (default: RAW/MANIFEST.sha256)")
    s = sub.add_parser("plot", parents=[common], help="Radargram + track for one recording")
    s.add_argument("base", help="Recording name, e.g. DAT_0013_A1")
    s.add_argument("--gain", type=float, default=0.0, help="Time-power gain exponent (0 = off)")
    s.add_argument("--max-traces", type=int, default=2000, help="Downsample to at most this many traces")
    s.add_argument("--no-extrapolation", action="store_true", help="No positions outside the GNSS trace range")
    s.add_argument("--save", action="store_true", help="Save PNGs to OUT/figures/ instead of showing")
    s = sub.add_parser("gis", parents=[common], help="GeoPackage for QGIS -> OUT/radar_tracks.gpkg (EPSG:3031)")
    s.add_argument("--format", choices=["gpkg", "shp"], default="gpkg", help="shp writes OUT/gis_shp/*.shp")
    s.add_argument("--tick-every", type=int, default=None, help="Trace-number tick spacing (default from config: 500)")
    s = sub.add_parser("export", parents=[common], help="Copy recordings + QC'd .cor to OUT/reflexw/ for ReflexW")
    s.add_argument("bases", nargs="*", help="Recording names, e.g. DAT_0013_A1")
    s.add_argument("--all", action="store_true", help="Export every recording (copies ~2 GB)")
    s.add_argument("--overwrite", action="store_true")
    s = sub.add_parser("merge", parents=[common], help="Join consecutive recordings into one profile in OUT/reflexw/")
    s.add_argument("bases", nargs="+", help="Recording names in profile order")
    s.add_argument("--name", default=None, help="Output name (default e.g. DAT_0032_0033_A1)")
    s.add_argument("--overwrite", action="store_true")
    return ap


COMMANDS = {
    "run": cmd_run,
    "fix-cor": cmd_fix_cor,
    "tracks": cmd_tracks,
    "track-qc": cmd_track_qc,
    "inventory": cmd_inventory,
    "manifest": cmd_manifest,
    "plot": cmd_plot,
    "gis": cmd_gis,
    "export": cmd_export,
    "merge": cmd_merge,
}


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
        COMMANDS[args.command](Paths(args), cfg, args)
    except (OSError, ValueError) as e:
        # Missing, half-copied or inconsistent files: a message, not a traceback (-v for that)
        if args.verbose:
            raise
        sys.exit(f"{args.command}: {e}")


if __name__ == "__main__":
    main()
