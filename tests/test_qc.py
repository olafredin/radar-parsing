import pytest

from radar_parsing.config import Config
from radar_parsing.cor import read_cor_table
from radar_parsing.qc import interp_fill, qc_cor_file, qc_directory


def test_interp_fill():
    vals = [None, 1.0, None, None, 4.0, None]
    ok = [False, True, False, False, True, False]
    assert interp_fill(vals, ok) == [1.0, 1.0, 2.0, 3.0, 4.0, 4.0]
    with pytest.raises(ValueError):
        interp_fill([None], [False])


def test_clean_file_roundtrips_byte_identical(raw_dir, tmp_path):
    out = tmp_path / "q.cor"
    res = qc_cor_file(raw_dir / "DAT_0001_A1.cor", out)
    assert res.status == "written" and res.n_interpolated == 0
    assert out.read_bytes() == (raw_dir / "DAT_0001_A1.cor").read_bytes()


def test_glitches_are_repaired(raw_dir, tmp_path):
    out = tmp_path / "q.cor"
    res = qc_cor_file(raw_dir / "DAT_0002_A1.cor", out)
    assert res.status == "written"
    assert res.n_invalid_fix == 3          # zero, digit loss, wrong hemisphere
    assert res.n_teleport == 1             # the ~1 km jump
    assert res.n_elev_fused == 1 and res.n_elev_shifted == 1 and res.n_unit_repaired == 1
    assert res.n_elev_spikes == 1          # 8.919 instead of 38.919

    df = read_cor_table(out)
    assert len(df) == 30
    assert df["lat"].between(-71.91, -71.89).all()
    assert df["lon"].between(-11.01, -10.99).all()
    assert df["elev"].between(38.8, 39.0).all()
    assert (df["elev_unit"] == "M").all()
    # positions are a straight line again: check the interpolated teleport point
    assert df.loc[12, "lat"] == pytest.approx(-71.9 + 12e-5, abs=1e-9)
    assert out.read_bytes().count(b"\r\n") == 30


def test_bbox_from_config_rejects_everything(raw_dir, tmp_path):
    cfg = Config()
    cfg.bbox.lat_min, cfg.bbox.lat_max = -80.0, -79.0
    res = qc_cor_file(raw_dir / "DAT_0001_A1.cor", tmp_path / "q.cor", cfg)
    assert res.status == "no_valid_fix"
    assert not (tmp_path / "q.cor").exists()


def test_teleport_qc_can_be_disabled(raw_dir, tmp_path):
    cfg = Config()
    cfg.cor_qc.teleport_qc = False
    res = qc_cor_file(raw_dir / "DAT_0002_A1.cor", tmp_path / "q.cor", cfg)
    assert res.n_teleport == 0


def _anchor_lock_file(tmp_path):
    """Straight track at ~1.4 m/s; one fix with a garbled time is followed by a
    30 s recording gap (~43 m), then 40 more fixes. The old algorithm fell back
    to the 40 m step test against the garbled-time anchor and rejected every
    later fix."""
    from conftest import cor_line, write_crlf

    lines, trace, s = [], 1, 0
    for i in range(50):
        if i == 10:
            trace += 120         # 30 s gap at 0.25 s/trace
            s += 30
        t = "32:21:0." if i == 9 else f"15:{20 + s // 60:02d}:{s % 60:02d}"
        lines.append(cor_line(trace, t, -71.9 + s * 1.3e-5, -11.0))
        trace += 4
        s += 1
    p = tmp_path / "lock.cor"
    write_crlf(p, lines)
    return p


def test_garbled_anchor_time_does_not_lock(tmp_path):
    p = _anchor_lock_file(tmp_path)
    res = qc_cor_file(p, tmp_path / "out.cor", trace_interval_s=0.25)
    assert res.n_teleport == 0


def test_relock_without_trace_time(tmp_path):
    p = _anchor_lock_file(tmp_path)
    cfg = Config()
    cfg.cor_qc.relock_after = 0
    locked = qc_cor_file(p, tmp_path / "a.cor", cfg)     # the old behaviour
    assert locked.n_teleport > 30
    relocked = qc_cor_file(p, tmp_path / "b.cor")        # default relock_after = 10
    assert relocked.n_teleport == 0                      # the run of 10 is re-accepted


def test_garbled_timestamp_cannot_admit_garbled_position(tmp_path):
    """Real case from DAT_0086: a fix with a timestamp 60 s too early, followed
    by a fix with a garbled position 220 m off. With the timestamp trusted, the
    jump looks like 5 m/s and the bad fix becomes the anchor."""
    from conftest import straight_track, write_crlf

    lines = straight_track(40, step_deg=2e-5)
    f = lines[19].split("\t"); f[2] = "15:19:19"; lines[19] = "\t".join(f)   # 60 s early
    f = lines[20].split("\t"); f[3] = f"{float(f[3]) + 0.002:.11f}"; lines[20] = "\t".join(f)
    p = tmp_path / "x.cor"
    write_crlf(p, lines)
    res = qc_cor_file(p, tmp_path / "out.cor", trace_interval_s=0.25)
    assert res.n_time_garbled == 1
    assert res.n_teleport == 1
    df = read_cor_table(tmp_path / "out.cor")
    assert df.loc[20, "lat"] == pytest.approx(-71.9 + 20 * 2e-5, abs=1e-9)


def test_no_interpolation_across_recording_pause(tmp_path):
    """Real case from DAT_0114: GNSS degrades just before a 5-min recording pause
    during which the sledge moves ~650 m. The rejected fixes before the pause must
    stay near the last good fix, not be spread along a line towards the far side."""
    from conftest import cor_line, write_crlf
    from radar_parsing.geo import haversine_m

    lines, trace = [], 1
    for i in range(20):                       # before the pause, ~1.4 m/s
        lat = -71.9 + i * 1.3e-5 + (0.002 if i >= 16 else 0.0)   # 16-19 garbled, 220 m off
        lines.append(cor_line(trace, f"15:20:{i:02d}", lat, -11.0))
        trace += 4
    for i in range(15):                       # after a 300 s pause, 670 m further north
        lines.append(cor_line(trace, f"15:25:{20 + i:02d}", -71.9 + 0.006 + i * 1.3e-5, -11.0))
        trace += 4
    p = tmp_path / "pause.cor"
    write_crlf(p, lines)

    res = qc_cor_file(p, tmp_path / "out.cor", trace_interval_s=0.25)
    assert res.n_pauses == 1 and res.n_teleport == 4
    df = read_cor_table(tmp_path / "out.cor")
    anchor = df.loc[15]
    for i in range(16, 20):
        assert haversine_m(anchor.lat, anchor.lon, df.loc[i, "lat"], df.loc[i, "lon"]) < 1.0

    cfg = Config()
    cfg.cor_qc.pause_s = 0                    # old behaviour: interpolate across the pause
    qc_cor_file(p, tmp_path / "old.cor", cfg, trace_interval_s=0.25)
    old = read_cor_table(tmp_path / "old.cor")
    assert haversine_m(anchor.lat, anchor.lon, old.loc[19, "lat"], old.loc[19, "lon"]) > 400


def test_directory(raw_dir, tmp_path):
    results = {r.file: r.status for r in qc_directory(raw_dir, tmp_path / "qc")}
    assert results == {
        "DAT_0001_A1.cor": "written",
        "DAT_0002_A1.cor": "written",
        "DAT_0003_A1.cor": "empty",
        "DAT_0004_A1.cor": "no_valid_fix",
    }


def test_track_outside_bbox_is_counted(raw_dir, tmp_path):
    cfg = Config()
    cfg.bbox.lat_max = -71.9 + 15e-5   # the track leaves the bbox after 16 of 30 fixes
    res = qc_cor_file(raw_dir / "DAT_0001_A1.cor", tmp_path / "q.cor", cfg)
    assert res.n_outside_bbox == 14 and res.max_outside_run == 14
    res = qc_cor_file(raw_dir / "DAT_0002_A1.cor", tmp_path / "q2.cor")
    assert res.n_outside_bbox == 2 and res.max_outside_run == 1   # digit loss, wrong hemisphere


def test_stale_qc_copy_is_removed(raw_dir, tmp_path):
    qc = tmp_path / "qc"
    qc_directory(raw_dir, qc)
    assert (qc / "DAT_0001_A1.cor").exists()
    (raw_dir / "DAT_0001_A1.cor").write_bytes(b"")
    qc_directory(raw_dir, qc)
    assert not (qc / "DAT_0001_A1.cor").exists()
