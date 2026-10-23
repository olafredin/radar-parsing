import numpy as np
import pytest

from radar_parsing.rad import RadHeader, read_rad
from radar_parsing.rd3 import Bundle, n_traces_from_size, read_traces, time_axis_ns


def test_rad_header(raw_dir):
    meta = read_rad(raw_dir / "DAT_0001_A1.rad")
    assert meta["SAMPLES"] == 64
    assert meta["TIME INTERVAL"] == 0.25
    hdr = RadHeader(meta)
    assert hdr.antenna_mhz == "250"
    assert hdr.sample_interval_ns == pytest.approx(1000 / 6532.4995)
    assert hdr.time_window_ns == pytest.approx(64 * hdr.sample_interval_ns, rel=1e-6)
    assert hdr.trace_interval_s == 0.25
    assert hdr.last_trace == 120


def test_real_header_consistency():
    # Values from a 2025/26 file: TIMEWINDOW == SAMPLES * 1000 / FREQUENCY
    hdr = RadHeader({"SAMPLES": 2024, "FREQUENCY": 6532.499512, "TIMEWINDOW": 309.835458})
    assert hdr.samples * hdr.sample_interval_ns == pytest.approx(hdr.time_window_ns, rel=1e-6)


def test_read_traces(raw_dir):
    b = Bundle.find("DAT_0001_A1", raw_dir)
    a = read_traces(b.data_path, 64)
    assert a.shape == (120, 64) and a.dtype == np.dtype("<i2")
    t = time_axis_ns(RadHeader.from_file(b.rad_path))
    assert t.shape == (64,) and t[0] == 0


def test_size_mismatch(raw_dir):
    with pytest.raises(ValueError):
        n_traces_from_size(raw_dir / "DAT_0001_A1.rd3", 63)


def test_rd7(tmp_path):
    p = tmp_path / "X.rd7"
    np.arange(40, dtype="<i4").tofile(p)
    assert read_traces(p, 8).shape == (5, 8)


def test_missing_bundle(tmp_path):
    with pytest.raises(FileNotFoundError):
        Bundle.find("nope", tmp_path)
