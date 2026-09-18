"""Optional semantic-embedding backend for memory search.

sentence-transformers (+ torch) is a large, optional dependency. It is
imported lazily, exactly like wake_word.py does for openWakeWord: if it
is not installed, `available()` just returns False and MemoryManager
falls back to fuzzy/keyword search only, no crash either way.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("jarvis.memory.embeddings")

MODEL_NAME = "all-MiniLM-L6-v2"

_lock = threading.Lock()
_model = None
_unavailable = False


def _load():
    global _model, _unavailable
    if _model is not None or _unavailable:
        return _model
    with _lock:
        if _model is not None or _unavailable:
            return _model
        try:
            from sentence_transformers import SentenceTransformer
        except Exception as exc:  # pragma: no cover - depends on install
            logger.warning(
                "sentence-transformers not available (%s) - semantic memory "
                "search disabled, using fuzzy/keyword search only.", exc,
            )
            _unavailable = True
            return None
        try:
            _model = SentenceTransformer(MODEL_NAME)
        except Exception:  # pragma: no cover - depends on network/disk
            logger.exception("failed to load embedding model %s", MODEL_NAME)
            _unavailable = True
            return None
        return _model


def available() -> bool:
    return _load() is not None


def embed(text: str):
    """Returns a list[float] embedding, or None if unavailable."""
    model = _load()
    if model is None:
        return None
    return model.encode(text, normalize_embeddings=True).tolist()


def to_bytes(vector) -> bytes:
    import numpy as np

    return np.asarray(vector, dtype="float32").tobytes()


def from_bytes(blob) -> list:
    import numpy as np

    return np.frombuffer(blob, dtype="float32").tolist()


def cosine(a, b) -> float:
    import numpy as np

    va = np.asarray(a, dtype="float32")
    vb = np.asarray(b, dtype="float32")
    denom = (float(np.linalg.norm(va)) * float(np.linalg.norm(vb))) or 1.0
    return float(np.dot(va, vb) / denom)
