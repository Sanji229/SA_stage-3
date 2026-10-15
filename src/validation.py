"""Input validation shared by the UI and the server-side data layer.

Every function returns a cleaned value or raises ValidationError with a message that is safe to
show to users. The data layer (src/database/db.py) calls these again before any write, so skipping
the UI never skips validation.
"""
from __future__ import annotations

import io
import re
import unicodedata

from PIL import Image, UnidentifiedImageError


class ValidationError(ValueError):
    """User-correctable input problem (message is safe to display)."""


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_AUDIO_BYTES = 25 * 1024 * 1024
ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG"}

_NAME_ALLOWED = re.compile(r"^[^\W\d_](?:[^\W\d_]|[ .'\u2019-])*$", re.UNICODE)
_CODE_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{1,19}$")


def _no_controls(text: str, label: str) -> None:
    if any(unicodedata.category(c).startswith("C") for c in text):
        raise ValidationError(f"{label} contains invalid characters.")


def clean_person_name(name, label: str = "Name") -> str:
    text = " ".join(str(name or "").split())
    if len(text) < 2:
        raise ValidationError(f"{label} must be at least 2 characters.")
    if len(text) > 60:
        raise ValidationError(f"{label} must be at most 60 characters.")
    _no_controls(text, label)
    if not _NAME_ALLOWED.match(text):
        raise ValidationError(f"{label} may contain letters, spaces, apostrophes, hyphens and dots only.")
    return text


def clean_subject_fields(code, name, section) -> tuple[str, str, str]:
    code = re.sub(r"\s+", "", str(code or "")).upper()
    if not _CODE_RE.match(code):
        raise ValidationError("Subject code must be 2-20 characters: letters, numbers, dash or underscore.")
    name = " ".join(str(name or "").split())
    section = " ".join(str(section or "").split())
    if not (2 <= len(name) <= 100):
        raise ValidationError("Subject name must be 2-100 characters.")
    if not (1 <= len(section) <= 20):
        raise ValidationError("Section must be 1-20 characters.")
    for label, v in (("Subject name", name), ("Section", section)):
        _no_controls(v, label)
        if re.search(r"[<>]", v):
            raise ValidationError(f"{label} may not contain < or >.")
    return code, name, section


def clean_reason(reason, minimum: int = 3, maximum: int = 500) -> str:
    text = " ".join(str(reason or "").split())
    if len(text) < minimum:
        raise ValidationError("Please give a reason for the correction.")
    if len(text) > maximum:
        raise ValidationError(f"The reason is too long (max {maximum} characters).")
    _no_controls(text, "Reason")
    return text


def check_image_bytes(raw: bytes, max_bytes: int = MAX_IMAGE_BYTES) -> Image.Image:
    """Validate an uploaded image by CONTENT (not file name) and return a decoded copy."""
    if not raw:
        raise ValidationError("The image file is empty.")
    if len(raw) > max_bytes:
        raise ValidationError(f"The image is larger than {max_bytes // (1024 * 1024)} MB.")
    try:
        probe = Image.open(io.BytesIO(raw))
        fmt = probe.format
        w, h = probe.size
        if fmt not in ALLOWED_IMAGE_FORMATS:
            raise ValidationError("Only JPEG and PNG images are supported.")
        if w * h > MAX_IMAGE_PIXELS:
            raise ValidationError("The image resolution is too large.")
        probe.verify()
        img = Image.open(io.BytesIO(raw))
        img.load()
        return img
    except ValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, SyntaxError):
        raise ValidationError("That file is not a valid image.") from None


def check_audio_bytes(raw: bytes, max_bytes: int = MAX_AUDIO_BYTES) -> bytes:
    if not raw:
        raise ValidationError("The audio recording is empty.")
    if len(raw) > max_bytes:
        raise ValidationError(f"The recording is larger than {max_bytes // (1024 * 1024)} MB.")
    return raw
