"""Synthetic Malå files for tests (no real survey data in the repository)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

RAD_TEMPLATE = """SAMPLES:{samples}
FREQUENCY:{freq:.6f}
FREQUENCY STEPS:6
TIME FLAG:1
TIME INTERVAL: 0.250000
DISTANCE INTERVAL: 0.000000
OPERATOR:_
ANTENNAS:{antenna} MHz shielded=1
ANTENNA SEPARATION: 0.310000
COMMENT:=128
TIMEWINDOW:{tw:.6f}
STACKS:4
LAST TRACE:{last}
"""


def cor_line(trace, t, lat, lon, elev=38.9, ns="S", ew="W", unit="M", q="0.6", date="2025-12-13"):
    return "\t".join(
        [str(trace), date, t, f"{abs(lat):.11f}", ns, f"{abs(lon):.11f}", ew, f"{elev:.3f}", unit, q]
    )


def straight_track(n=30, lat0=-71.9, lon0=-11.0, step_deg=1e-5, start_s=0):
    """n fixes, one per second, ~1.1 m apart heading north-east."""
    lines = []
    for i in range(n):
        s = start_s + i
        t = f"15:{20 + s // 60:02d}:{s % 60:02d}"
        lines.append(cor_line(1 + 4 * i, t, lat0 + i * step_deg, lon0 + i * step_deg, elev=38.9 + 0.001 * i))
    return lines


def write_crlf(path: Path, lines: list[str]) -> None:
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode("ascii"))


def write_bundle(raw: Path, base: str, cor_lines: list[str], samples=64, n_traces=120, antenna=250, freq=6532.4995):
    raw.mkdir(parents=True, exist_ok=True)
    (raw / f"{base}.rad").write_text(
        RAD_TEMPLATE.format(samples=samples, freq=freq, antenna=antenna, tw=samples * 1000 / freq, last=n_traces)
    )
    rng = np.random.default_rng(0)
    data = (rng.normal(0, 1000, size=(n_traces, samples)) + 500).astype("<i2")
    data.tofile(raw / f"{base}.rd3")
    write_crlf(raw / f"{base}.cor", cor_lines)
    (raw / f"{base}.mrk").write_text("HEADER VERSION:100\n")


@pytest.fixture
def raw_dir(tmp_path: Path) -> Path:
    raw = tmp_path / "data_raw"
    good = straight_track(30)
    write_bundle(raw, "DAT_0001_A1", good)
    # Glitchy file: dropout, out-of-bbox digit loss, wrong hemisphere, teleport,
    # fused / shifted / garbled elevations.
    bad = straight_track(30)
    f = bad[5].split("\t"); f[3] = "0.00000000000"; f[5] = "0.00000000000"; bad[5] = "\t".join(f)
    f = bad[8].split("\t"); f[3] = "7.89990000000"; bad[8] = "\t".join(f)
    f = bad[10].split("\t"); f[6] = "1"; bad[10] = "\t".join(f)
    f = bad[12].split("\t"); f[3] = f"{71.9 - 0.01:.11f}"; bad[12] = "\t".join(f)  # ~1 km jump
    f = bad[15].split("\t"); f[7] = f[7] + "M"; f[8] = "9"; bad[15] = "\t".join(f)
    f = bad[17].split("\t"); f[9] = f[7]; f[7] = "M"; f[8] = "9"; bad[17] = "\t".join(f)
    f = bad[19].split("\t"); f[7] = "8.919"; bad[19] = "\t".join(f)  # dropped digit
    f = bad[21].split("\t"); f[8] = ","; bad[21] = "\t".join(f)
    write_bundle(raw, "DAT_0002_A1", bad, antenna=500)
    # Empty .cor (no GNSS) and a test recording with only zero fixes
    write_bundle(raw, "DAT_0003_A1", [])
    write_bundle(raw, "DAT_0004_A1", [cor_line(1, "15:20:00", 0.0, 0.0)])
    return raw
