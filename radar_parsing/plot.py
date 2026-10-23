"""Quick-look plots: radargram and GNSS track for one recording."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from .cor import read_cor_table
from .rad import RadHeader
from .rd3 import Bundle, read_traces, remove_trace_mean, time_axis_ns, time_power_gain
from .tracks import nav_for_traces


def load_for_plot(base: str, raw_dir: Path, qc_dir: Path | None, max_traces: int, gain: float, extrapolate: bool):
    """Return (header, traces-to-plot, time axis, trace indices, nav, n_traces, cor source)."""
    bundle = Bundle.find(base, raw_dir)
    hdr = RadHeader.from_file(bundle.rad_path)
    data = read_traces(bundle.data_path, hdr.samples)
    n_traces = data.shape[0]

    idx = np.linspace(0, n_traces - 1, min(max_traces, n_traces)).astype(int)
    a = np.asarray(data[idx, :], dtype=np.float32)
    t = time_axis_ns(hdr)
    a = time_power_gain(remove_trace_mean(a), t, gain)

    cor_path = None
    if qc_dir is not None and (qc_dir / f"{base}.cor").exists():
        cor_path = qc_dir / f"{base}.cor"
    elif bundle.cor_path is not None:
        cor_path = bundle.cor_path
    nav = None
    if cor_path is not None:
        cor = read_cor_table(cor_path)
        if not cor.empty:
            nav = nav_for_traces(cor, n_traces, extrapolate=extrapolate)
    return hdr, a, t, idx, nav, n_traces, cor_path


def plot_bundle(
    base: str,
    raw_dir: Path,
    qc_dir: Path | None = None,
    max_traces: int = 2000,
    gain: float = 0.0,
    extrapolate: bool = True,
    save_dir: Path | None = None,
    clip_percentile: float = 99.0,
) -> list[Path]:
    """Plot the radargram (and track, if a .cor exists). Show, or save PNGs to ``save_dir``."""
    import matplotlib

    if save_dir is not None:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    hdr, a, t, idx, nav, n_traces, cor_path = load_for_plot(base, raw_dir, qc_dir, max_traces, gain, extrapolate)

    vmax = float(np.nanpercentile(np.abs(a), clip_percentile)) or None
    fig1, ax = plt.subplots(figsize=(11, 5))
    t_end = float(t[-1]) if t is not None else hdr.samples
    ax.imshow(a.T, aspect="auto", cmap="gray", vmin=-vmax if vmax else None, vmax=vmax,
              extent=[idx[0] + 1, idx[-1] + 1, t_end, 0.0], interpolation="nearest")
    ax.set_xlabel("Trace number" + (f" (showing {len(idx)} of {n_traces})" if len(idx) < n_traces else ""))
    ax.set_ylabel("Two-way travel time (ns)" if t is not None else "Sample")
    ant = hdr.antenna_mhz
    ax.set_title(f"{base}  {ant + ' MHz' if ant else ''}  mean removed{'' if gain <= 0 else f', t^{gain:g} gain'}")
    fig1.tight_layout()
    figs = [(fig1, f"{base}_radargram.png")]

    if nav is not None:
        fig2, ax2 = plt.subplots(figsize=(6, 6))
        ax2.plot(nav["lon"], nav["lat"], lw=1)
        ax2.plot(nav["lon"].iloc[0], nav["lat"].iloc[0], "o", label="start")
        ax2.set_xlabel("Longitude (deg)")
        ax2.set_ylabel("Latitude (deg)")
        ax2.set_title(f"{base} track ({cor_path.parent.name}/{cor_path.name})")
        ax2.set_aspect(1 / np.cos(np.radians(np.nanmean(nav["lat"]))))
        ax2.legend()
        fig2.tight_layout()
        figs.append((fig2, f"{base}_track.png"))

    written: list[Path] = []
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
        for fig, name in figs:
            fig.savefig(save_dir / name, dpi=150)
            written.append(save_dir / name)
            plt.close(fig)
    else:
        plt.show()
    return written
