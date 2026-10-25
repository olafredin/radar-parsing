import pandas as pd
import pytest

from radar_parsing.cli import main
from radar_parsing.config import load_config
from radar_parsing.cor import read_cor_table
from radar_parsing.inventory import check_manifest, write_manifest
from radar_parsing.qc import qc_directory
from radar_parsing.tracks import TRACK_COLUMNS, compile_tracks, nav_for_traces, track_qc


def test_compile_tracks(raw_dir, tmp_path):
    qc = tmp_path / "qc"
    qc_directory(raw_dir, qc)
    df = compile_tracks(qc, raw_dir)
    assert list(df.columns) == TRACK_COLUMNS
    assert set(df["file_base"]) == {"DAT_0001_A1", "DAT_0002_A1"}
    assert set(df["antenna_mhz"]) == {"250", "500"}
    # UTM 29S northing for ~71.9 S is ~2.02e6 m
    assert df["northing"].between(1.9e6, 2.1e6).all()


def test_track_qc_flags_jump(raw_dir, tmp_path):
    qc = tmp_path / "qc"
    qc_directory(raw_dir, qc)
    df = compile_tracks(qc, raw_dir)
    df.loc[3, "lat"] += 0.01   # 1 km in 1 s
    out, stats = track_qc(df)
    assert stats["n_bad"] >= 1
    assert out["qc_was_interpolated"].sum() == stats["n_bad"]
    cfg = load_config()
    cfg.track_qc.action = "drop"
    out2, stats2 = track_qc(df, cfg)
    assert len(out2) == len(df) - stats2["n_bad"]


def test_nav_for_traces(raw_dir):
    cor = read_cor_table(raw_dir / "DAT_0001_A1.cor")  # fixes at traces 1, 5, ..., 117
    nav = nav_for_traces(cor, 120)
    assert len(nav) == 120 and nav["lat"].notna().all()
    nav2 = nav_for_traces(cor, 120, extrapolate=False)
    assert nav2["lat"].isna().sum() == 3


def test_config_file(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("epsg = 3031\n[bbox]\nlat_min = -75.0\n[cor_qc]\nmax_speed_mps = 8\n")
    cfg = load_config(p)
    assert cfg.epsg == 3031 and cfg.bbox.lat_min == -75.0 and cfg.cor_qc.max_speed_mps == 8.0
    p.write_text("[bbox]\nlatmin = 1\n")
    with pytest.raises(ValueError):
        load_config(p)


def test_example_config_matches_defaults():
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "configs" / "rlis_2025-26.toml"
    assert load_config(example) == load_config()


def test_cli_run_end_to_end(raw_dir, tmp_path, capsys):
    out = tmp_path / "output"
    main(["run", "--raw-dir", str(raw_dir), "--out-dir", str(out)])
    for name in ["cor_qc_report.csv", "mala_tracks_compiled.csv", "mala_tracks_compiled_qc.csv", "inventory.csv"]:
        assert (out / name).exists(), name
    inv = pd.read_csv(out / "inventory.csv")
    assert len(inv) == 4 and inv["data_ok"].all() and inv["header_last_trace_ok"].all()
    main(["plot", "DAT_0002_A1", "--raw-dir", str(raw_dir), "--out-dir", str(out), "--save"])
    assert (out / "figures" / "DAT_0002_A1_radargram.png").exists()
    assert (out / "figures" / "DAT_0002_A1_track.png").exists()


def test_manifest(raw_dir):
    write_manifest(raw_dir)
    assert check_manifest(raw_dir) == []
    (raw_dir / "DAT_0001_A1.mrk").write_text("changed")
    assert check_manifest(raw_dir) == [("DAT_0001_A1.mrk", "checksum mismatch")]


def test_cli_warns_when_track_leaves_bbox(raw_dir, tmp_path, capsys):
    from conftest import straight_track, write_bundle

    write_bundle(raw_dir, "DAT_0005_A1", straight_track(40))
    cfg = tmp_path / "c.toml"
    cfg.write_text("[bbox]\nlat_max = -71.899955\n")  # fixes 5.. are outside
    main(["fix-cor", "--raw-dir", str(raw_dir), "--out-dir", str(tmp_path / "o"), "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "WARNING" in out and "DAT_0005_A1.cor: 35 of 40 fixes outside (written)" in out


def test_cli_run_without_gnss_and_with_broken_header(tmp_path, capsys):
    from conftest import write_bundle

    raw = tmp_path / "raw"
    write_bundle(raw, "DAT_0001_A1", [])           # first day: no GNSS yet
    write_bundle(raw, "DAT_0002_A1", [])
    (raw / "DAT_0002_A1.rad").write_text("")       # recording cut off
    main(["run", "--raw-dir", str(raw), "--out-dir", str(tmp_path / "o")])
    out = capsys.readouterr().out
    assert "track-qc: flagged 0 of 0" in out and "DAT_0002_A1" in out.split("WARNING")[1]
    inv = pd.read_csv(tmp_path / "o" / "inventory.csv")
    assert inv["data_ok"].tolist() == [True, False]


def test_cli_missing_recording_is_a_message(raw_dir, tmp_path):
    with pytest.raises(SystemExit, match="plot: Missing header"):
        main(["plot", "DAT_9999_A1", "--raw-dir", str(raw_dir), "--out-dir", str(tmp_path), "--save"])
