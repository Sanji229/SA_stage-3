"""Embedding validation and duplicate detection (pure numpy; no dlib, no database)."""
from __future__ import annotations

import math
from typing import Iterable, Sequence

import numpy as np

FACE_DIM = 128
VOICE_DIM = 256


def is_valid_embedding(vec, dim: int) -> bool:
    if not isinstance(vec, (list, tuple, np.ndarray)) or len(vec) != dim:
        return False
    try:
        return all(isinstance(x, (int, float, np.floating, np.integer)) and math.isfinite(float(x)) for x in vec)
    except (TypeError, ValueError):
        return False


def clean_embedding(vec, dim: int, label: str) -> list[float]:
    """Return a plain list of floats or raise ValueError (never store a malformed embedding)."""
    if not is_valid_embedding(vec, dim):
        raise ValueError(f"{label} embedding must be {dim} finite numbers.")
    arr = np.asarray(vec, dtype=np.float64)
    if not np.any(arr):
        raise ValueError(f"{label} embedding is empty (all zeros).")
    return [float(x) for x in arr]


def find_face_conflicts(embedding: Sequence[float], rows: Iterable[dict], threshold: float, exclude_id: int | None = None) -> list[tuple[int, float]]:
    """Existing students whose stored face is within `threshold` of `embedding`, closest first.

    Malformed stored rows are skipped (they can never match), never raised on.
    """
    probe = np.asarray(embedding, dtype=np.float64)
    out = []
    for r in rows:
        emb = r.get("face_embedding")
        if r.get("student_id") == exclude_id or not is_valid_embedding(emb, FACE_DIM):
            continue
        d = float(np.linalg.norm(np.asarray(emb, dtype=np.float64) - probe))
        if d <= threshold:
            out.append((int(r["student_id"]), d))
    return sorted(out, key=lambda t: t[1])
