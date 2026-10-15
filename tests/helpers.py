import numpy as np


def fake_face(i: int) -> list[float]:
    """Deterministic 128-d unit vector per identity. Different i => distance ~1.4 (far above any match threshold)."""
    v = np.random.default_rng(1000 + i).normal(size=128)
    return (v / np.linalg.norm(v)).tolist()


def fake_voice(i: int) -> list[float]:
    v = np.random.default_rng(5000 + i).normal(size=256)
    return (v / np.linalg.norm(v)).tolist()
