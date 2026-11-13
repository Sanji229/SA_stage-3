
"""Browser recorder that captures and submits student voice samples."""
from pathlib import Path

import streamlit.components.v1 as components


_COMPONENT_DIR = Path(__file__).resolve().parent / "voice_capture_web"

_voice_capture = components.declare_component(
    "smartattend_voice_capture",
    path=str(_COMPONENT_DIR),
)


def voice_capture(
    key: str,
    action_label: str = "Create account",
):
    """Render the browser voice recorder and return submitted audio data."""
    return _voice_capture(
        action_label=action_label,
        key=key,
        default=None,
    )
