from pathlib import Path
import numpy as np

def read_rad_simple(rad_path: str | Path) -> dict:
    meta = {}
    for line in Path(rad_path).read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith(("...", "#", ";", "//")):
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            k = k.strip().upper()
            v = v.strip()
            # try numeric
            try:
                meta[k] = float(v) if ("." in v or "e" in v.lower()) else int(v)
            except ValueError:
                meta[k] = v
    return meta

rad = read_rad_simple("/mnt/data/DAT_0013_A1.rad")
n_samples = int(rad["SAMPLES"])

rd3_path = Path("/mnt/data/DAT_0013_A1.rd3")
n_traces = rd3_path.stat().st_size // (n_samples * 2)

data = np.memmap(rd3_path, dtype=np.dtype("<i2"), mode="r", shape=(n_traces, n_samples))
print(n_traces, n_samples, data.dtype)   # 6884 2024 int16