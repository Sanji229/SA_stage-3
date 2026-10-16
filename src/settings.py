"""Central place for reading configuration.

Order of precedence: environment variable -> Streamlit secrets -> default.
Nothing in this module ever logs or returns a value to the UI.
"""
from __future__ import annotations

import math
import os
from typing import Optional

import streamlit as st
from src.config_errors import SettingsError


def get_setting(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value:
        return value
    try:
        secret = st.secrets.get(name)  # raises if no secrets file exists
    except Exception:
        secret = None
    return str(secret) if secret else default


def _float_setting(name: str, default: str, minimum: float, maximum: float) -> float:
    raw = get_setting(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be a number between {minimum:g} and {maximum:g}.") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise SettingsError(f"{name} must be a finite number between {minimum:g} and {maximum:g}.")
    return value


def _int_setting(name: str, default: str, minimum: int, maximum: int) -> int:
    raw = get_setting(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise SettingsError(f"{name} must be an integer between {minimum} and {maximum}.") from None
    if not minimum <= value <= maximum:
        raise SettingsError(f"{name} must be an integer between {minimum} and {maximum}.")
    return value


# Tunable recognition thresholds (documented in README).
FACE_MATCH_THRESHOLD = _float_setting("FACE_MATCH_THRESHOLD", "0.45", 0.0, 1.0)
VOICE_MATCH_THRESHOLD = _float_setting("VOICE_MATCH_THRESHOLD", "0.55", 0.0, 1.0)
# Ambiguity margin: if the 2nd-best candidate is also within the threshold and closer than
# this to the best, the face is sent to teacher review instead of auto-assigned.
# PROVISIONAL default: not yet tuned on real labelled data (see evaluation/README).
FACE_MATCH_MARGIN = _float_setting("FACE_MATCH_MARGIN", "0.05", 0.0, 2.0)
# dlib re-sampling jitter for embeddings (0 = fastest; higher = slightly more stable, slower).
FACE_NUM_JITTERS = _int_setting("FACE_NUM_JITTERS", "0", 0, 100)
# Faces whose bounding box is smaller than this (pixels) are flagged as low quality.
FACE_MIN_SIZE_PX = _int_setting("FACE_MIN_SIZE_PX", "40", 1, 2000)
# Voice: 2nd-best speaker within this similarity of the best (and above threshold) => teacher review.
# PROVISIONAL, not tuned on labelled recordings.
VOICE_MATCH_MARGIN = _float_setting("VOICE_MATCH_MARGIN", "0.05", 0.0, 2.0)

# ---- account security
# One-time secret that lets the FIRST administrator be created. Remove it from your secrets after setup.
ADMIN_SETUP_TOKEN = get_setting("ADMIN_SETUP_TOKEN")
LOGIN_MAX_FAILURES = _int_setting("LOGIN_MAX_FAILURES", "5", 1, 100)
LOGIN_WINDOW_MINUTES = _int_setting("LOGIN_WINDOW_MINUTES", "15", 1, 1440)
STUDENT_REGISTER_PER_HOUR = _int_setting("STUDENT_REGISTER_PER_HOUR", "30", 1, 10000)
SESSION_IDLE_TIMEOUT_MINUTES = _int_setting("SESSION_IDLE_TIMEOUT_MINUTES", "30", 1, 1440)
INVITE_MAX_DAYS = 14
