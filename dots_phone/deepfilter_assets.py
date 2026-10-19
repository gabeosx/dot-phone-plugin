"""Immutable upstream identities; no model bytes or third-party code in Git."""

import hashlib
from pathlib import Path

VERSION = "0.5.6"
COMMIT = "978576aa8400552a4ce9730838c635aa30db5e61"
MODEL_NAME = "DeepFilterNet3_onnx.tar.gz"
MODEL_SHA256 = "c94d91f70911001c946e0fabb4aa9adc37045f45a03b56008cb0c8244cb63616"
MODEL_BYTES = 7_983_136
MODEL_URL = f"https://raw.githubusercontent.com/Rikorose/DeepFilterNet/{COMMIT}/models/{MODEL_NAME}"
SAMPLE_RATE = 48000
HOP_SAMPLES = 480
MODEL_DELAY_MS = 30
# Output is paced to input packet sizes. This covers the causal resampler/hop
# remainder even when carrier packets are smaller than one 10 ms model hop.
ALIGNMENT_MS = 20
TOTAL_DELAY_MS = MODEL_DELAY_MS + ALIGNMENT_MS


def verify_model(path: Path | None):
    if path is None or not path.is_file() or path.stat().st_size != MODEL_BYTES:
        raise RuntimeError("DeepFilterNet3 model missing or incorrect size; run deepfilter_setup")
    with path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != MODEL_SHA256:
        raise RuntimeError("DeepFilterNet3 model checksum mismatch")
