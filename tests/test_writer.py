import numpy as np
import pytest

from conftest import straight_track, write_bundle as make_bundle
from radar_parsing.cli import main
from radar_parsing.cor import read_cor_table
from radar_parsing.qc import qc_directory
from radar_parsing.rad import RadHeader
from radar_parsing.rd3 import read_traces
from radar_parsing.writer import (
    RadFile,
    default_merge_name,
    export_bundle,
    merge_bundles,
    write_bundle,
    write_traces,
)


def test_rad_roundtrip_is_byte_identical(tmp_path):
    p = tmp_path / "x.rad"
    text = "SAMPLES:2024\r\nTIME INTERVAL: 0.250001\r\nANTENNAS:500 MHz shielded=2\r\nLAST TRACE:616\r\n"
    p.write_bytes(text.encode())
    rad = RadFile.read(p)
    assert rad.text() == text
    rad.set("LAST TRACE", 700)
    rad.set("TIME INTERVAL", 0.5)
    assert rad.text() == text.replace("616", "700").replace(" 0.250001", " 0.500000")


def test_write_traces_fit(tmp_path):
    a = np.array([[0.4, -1.6, 40000.0]])
    with pytest.raises(ValueError):
        write_traces(tmp_path / "a.rd3", a)
    write_traces(tmp_path / "b.rd3", a, fit="clip")
    assert read_traces(tmp_path / "b.rd3", 3).tolist() == [[0, -2, 32767]]
    write_traces(tmp_path / "c.rd3", a, fit="scale")
    assert read_traces(tmp_path / "c.rd3", 3)[0, 2] == 32767
    with pytest.raises(FileExistsError):
        write_traces(tmp_path / "c.rd3", a, fit="scale")


def test_write_bundle_updates_header(raw_dir, tmp_path):
    data = read_traces(raw_dir / "DAT_0001_A1.rd3", 64)[:50, :32] * 2
    paths = write_bundle(tmp_path / "out", "PROC", data, raw_dir / "DAT_0001_A1.rad")
    hdr = RadHeader.from_file(paths["rad"])
    assert (hdr.samples, hdr.last_trace) == (32, 50)
    assert hdr.time_window_ns == pytest.approx(32 * hdr.sample_interval_ns, rel=1e-6)
    assert np.array_equal(read_traces(paths["rd3"], 32), data)


def test_export_fixes_stale_last_trace(raw_dir, tmp_path):
    qc = tmp_path / "qc"
    qc_directory(raw_dir, qc)
    rad = raw_dir / "DAT_0001_A1.rad"
    rad.write_text(rad.read_text().replace("LAST TRACE:120", "LAST TRACE:17"))
    r = export_bundle("DAT_0001_A1", raw_dir, qc, tmp_path / "rw")
    assert r.header_fixed and r.cor == "qc"
    assert RadHeader.from_file(r.files["rad"]).last_trace == 120
    assert r.files["rd3"].read_bytes() == (raw_dir / "DAT_0001_A1.rd3").read_bytes()
    assert r.files["cor"].read_bytes() == (qc / "DAT_0001_A1.cor").read_bytes()
    with pytest.raises(FileExistsError):
        export_bundle("DAT_0001_A1", raw_dir, qc, tmp_path / "rw")


def test_export_leaves_correct_header_untouched(raw_dir, tmp_path):
    rad = raw_dir / "DAT_0001_A1.rad"
    rad.write_text(rad.read_text() + "STOP POSITION:30.000123\n")  # Malå's own, more precise value
    r = export_bundle("DAT_0001_A1", raw_dir, None, tmp_path / "rw")
    assert not r.header_fixed
    assert r.files["rad"].read_bytes() == rad.read_bytes()


def test_merge(tmp_path):
    raw = tmp_path / "raw"
    make_bundle(raw, "DAT_0010_A1", straight_track(30))
    # second recording starts 40 s later, ~45 m further along
    make_bundle(raw, "DAT_0011_A1", straight_track(30, lat0=-71.9 + 70e-5, lon0=-11.0 + 70e-5, start_s=70))
    r = merge_bundles(["DAT_0010_A1", "DAT_0011_A1"], raw, None, tmp_path / "rw")
    assert r.name == "DAT_0010_0011_A1" and r.n_traces == 240
    a = read_traces(r.files["rd3"], 64)
    assert np.array_equal(a[:120], read_traces(raw / "DAT_0010_A1.rd3", 64))
    assert np.array_equal(a[120:], read_traces(raw / "DAT_0011_A1.rd3", 64))
    assert RadHeader.from_file(r.files["rad"]).last_trace == 240
    cor = read_cor_table(r.files["cor"])
    assert cor["trace_id"].is_monotonic_increasing
    assert cor["trace_id"].iloc[30] == 121       # first fix of part 2, renumbered
    j = r.junctions[0]
    assert j.gap_s == pytest.approx(41) and j.gap_m == pytest.approx(55, abs=10)


def test_merge_refuses_mismatch(tmp_path):
    raw = tmp_path / "raw"
    make_bundle(raw, "DAT_0010_A1", straight_track(5))
    make_bundle(raw, "DAT_0011_A1", straight_track(5), samples=32)
    with pytest.raises(ValueError, match="SAMPLES"):
        merge_bundles(["DAT_0010_A1", "DAT_0011_A1"], raw, None, tmp_path / "rw")


def test_default_merge_name():
    assert default_merge_name(["DAT_0032_A1", "DAT_0033_A1", "DAT_0034_A1"]) == "DAT_0032_0034_A1"
    assert default_merge_name(["a", "b"]) == "merged"


def test_cli_export_and_merge(raw_dir, tmp_path, capsys):
    out = tmp_path / "output"
    args = ["--raw-dir", str(raw_dir), "--out-dir", str(out)]
    main(["fix-cor", *args])
    main(["export", "DAT_0001_A1", *args])
    assert (out / "reflexw" / "DAT_0001_A1.cor").exists()
    # DAT_0001 (250 MHz) + DAT_0003 (250 MHz, no GNSS): allowed
    main(["merge", "DAT_0001_A1", "DAT_0003_A1", "--name", "M", *args])
    text = capsys.readouterr().out
    assert "M: 240 traces" in text and ".cor none" in text
    assert (out / "reflexw" / "M.rd3").stat().st_size == 240 * 64 * 2
    # DAT_0002 is 500 MHz: refused with a clear message
    with pytest.raises(SystemExit, match="antenna 500 vs 250 MHz"):
        main(["merge", "DAT_0001_A1", "DAT_0002_A1", *args])
