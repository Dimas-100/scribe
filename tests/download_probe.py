r"""
Network probe: really download a small model (tiny.en, about 75 MB) into a
throwaway cache and watch the progress Scribe would show. Not part of the
safe suite - it uses the internet.

Run from the project root:   venv\Scripts\python tests\download_probe.py
"""

import os
import shutil
import sys
import tempfile

CACHE = tempfile.mkdtemp(prefix="scribe-hf-")
os.environ["HF_HUB_CACHE"] = CACHE                 # before huggingface_hub loads
os.environ["SCRIBE_DATA_DIR"] = tempfile.mkdtemp(prefix="scribe-probe-")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np      # noqa: E402
import model_manager    # noqa: E402

try:
    assert model_manager.find_cached("tiny.en") is None, "the throwaway cache starts empty"
    reports = []
    path = model_manager.download("tiny.en", lambda d, t: reports.append((d, t)))
    assert os.path.isfile(os.path.join(path, "model.bin"))
    assert len(reports) > 5, f"expected steady progress, got {len(reports)} reports"
    dones = [d for d, _t in reports]
    assert dones == sorted(dones), "progress never goes backwards"
    total = reports[-1][1]
    biggest_jump = max(b - a for a, b in zip(dones, dones[1:]))
    assert biggest_jump < 0.25 * total, \
        f"progress should be steady; one jump was {biggest_jump / 1e6:.0f} MB"
    # The raw counter can end a little PAST its total: files whose size isn't
    # known up front add bytes without adding to it (the manager caps it).
    last_done, last_total = reports[-1]
    assert last_done >= last_total > 50_000_000, reports[-1]
    print(f"PASS  downloaded {last_done / 1e6:.0f} MB (expected {last_total / 1e6:.0f}) "
          f"with {len(reports)} progress reports.")
    assert model_manager.find_cached("tiny.en") == path
    print("PASS  the model is found in the cache afterwards (no network needed).")
    model = model_manager.load(path)
    segments, _info = model.transcribe(np.zeros(16000, dtype=np.float32))
    list(segments)
    print("PASS  the downloaded model loads and runs.")
finally:
    shutil.rmtree(CACHE, ignore_errors=True)
print("\nAll download probe checks passed.")
