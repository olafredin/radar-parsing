import numpy as np
import pandas as pd
import pytest

gpd = pytest.importorskip("geopandas")
pyogrio = pytest.importorskip("pyogrio")

from conftest import cor_line, straight_track, write_bundle as make_bundle, write_crlf  # noqa: E402
from radar_parsing.cli import main  # noqa: E402
from radar_parsing.config import Config  # noqa: E402
from radar_parsing.gis import build_layers, segments, write_gpkg, write_shapefiles  # noqa: E402
from radar_parsing.qc import flags_table, qc_directory  # noqa: E402


@pytest.fixture
def qc(raw_dir, tmp_path):
    results = qc_directory(raw_dir, tmp_path / "qc")
    return tmp_path / "qc", flags_table(results)


def test_flags_table(qc):
    _, flags = qc
    f = flags[flags["file_base"] == "DAT_0002_A1"]
    assert len(f) == 30
    assert (f["position"] == "invalid").sum() == 3
    assert (f["position"] == "teleport").sum() == 1
    assert (f["elevation"] == "repaired").sum() == 2
    assert (f["elevation"] == "spike").sum() == 1
    assert (flags[flags["file_base"] == "DAT_0001_A1"][["position", "elevation"]] == "").all().all()


def test_layers(raw_dir, qc):
    qc_dir, flags = qc
    cfg = Config()
    cfg.gis.tick_every = 50
    layers = build_layers(qc_dir, raw_dir, cfg, flags=flags)
    assert set(layers) == {"transects", "fixes", "trace_ticks"}
    for gdf in layers.values():
        assert gdf.crs.to_epsg() == 3031
        assert all(len(c) <= 10 for c in gdf.columns), list(gdf.columns)
    tr = layers["transects"].set_index("file_base")
    assert list(tr.index) == ["DAT_0001_A1", "DAT_0002_A1"]
    assert tr.loc["DAT_0002_A1", "n_pos_int"] == 4 and tr.loc["DAT_0002_A1", "n_elev_int"] == 1
    assert tr.loc["DAT_0001_A1", "antenna"] == 250 and tr.loc["DAT_0002_A1", "antenna"] == 500
    # projected line length ~ geodesic length (EPSG:3031 is true scale at 71 S)
    assert tr.loc["DAT_0001_A1"].geometry.length / 1000 == pytest.approx(tr.loc["DAT_0001_A1", "length_km"], rel=0.01)
    fx = layers["fixes"]
    assert len(fx) == 60 and set(fx["pos_flag"]) == {"", "invalid", "teleport"}
    ticks = layers["trace_ticks"]
    t1 = ticks[ticks["file_base"] == "DAT_0001_A1"]
    assert list(t1["trace"]) == [1, 50, 100, 117]       # fixes cover traces 1..117
    assert list(t1["kind"]) == ["start", "tick", "tick", "end"]
    assert t1["label"].iloc[0] == "DAT_0001_A1"


def test_split_at_jump(raw_dir, tmp_path):
    qc_dir = tmp_path / "qc"
    qc_dir.mkdir()
    lines = straight_track(20)
    lines += [cor_line(200 + 4 * i, f"15:30:{i:02d}", -71.9 + 0.01 + i * 1e-5, -11.0) for i in range(20)]
    write_crlf(qc_dir / "DAT_0001_A1.cor", lines)
    tr = build_layers(qc_dir, raw_dir)["transects"]
    assert tr.loc[0, "n_segments"] == 2 and tr.geometry[0].geom_type == "MultiLineString"
    lat = np.array([0.0, 1e-5, 2e-5, 1.0, 1.00001])
    assert [len(s) for s in segments(lat, np.zeros(5), 50)] == [3, 2]


def test_write_gpkg_and_shp(raw_dir, qc, tmp_path):
    qc_dir, flags = qc
    layers = build_layers(qc_dir, raw_dir, flags=flags)
    p = write_gpkg(layers, tmp_path / "out" / "radar_tracks.gpkg")
    names = [n for n, _ in pyogrio.list_layers(p)]
    assert names == ["transects", "trace_ticks", "fixes"]
    back = gpd.read_file(p, layer="transects")
    assert back.crs.to_epsg() == 3031 and pd.api.types.is_datetime64_any_dtype(back["t_start"])
    write_gpkg(layers, p)  # replacing an existing file works
    shp = write_shapefiles(layers, tmp_path / "shp")
    assert {s.name for s in shp} == {"transects.shp", "trace_ticks.shp", "fixes.shp"}
    assert list(gpd.read_file(shp[0]).columns[:3]) == ["file_base", "antenna", "date"]


def test_locked_gpkg_gets_timestamped_copy(raw_dir, qc, tmp_path, monkeypatch):
    import pathlib

    qc_dir, flags = qc
    layers = build_layers(qc_dir, raw_dir, flags=flags)
    target = write_gpkg(layers, tmp_path / "radar_tracks.gpkg")
    real_unlink = pathlib.Path.unlink

    def locked(self, *a, **k):
        if self == target:
            raise PermissionError("file in use")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(pathlib.Path, "unlink", locked)
    alt = write_gpkg(layers, target)
    assert alt != target and alt.name.startswith("radar_tracks_") and alt.exists()
    assert [n for n, _ in pyogrio.list_layers(alt)] == ["transects", "trace_ticks", "fixes"]


def test_cli_run_writes_gpkg_with_profiles(raw_dir, tmp_path, capsys):
    out = tmp_path / "output"
    args = ["--raw-dir", str(raw_dir), "--out-dir", str(out)]
    main(["fix-cor", *args])
    main(["export", "DAT_0001_A1", *args])
    main(["gis", *args])
    assert "profiles 1" in capsys.readouterr().out
    assert [n for n, _ in pyogrio.list_layers(out / "radar_tracks.gpkg")][:2] == ["transects", "profiles"]
    main(["run", *args])
    assert (out / "cor_qc_flags.csv").exists() and (out / "radar_tracks.gpkg").exists()
