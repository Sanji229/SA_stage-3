"""Browser recorder that can stop and submit its audio from one Save action."""
from pathlib import Path

import streamlit.components.v1 as components

_voice_capture = components.declare_component(
    "smartattend_voice_capture",
    path=str(Path(__file__).with_name("voice_capture_web")),
)


def voice_capture(key: str, action_label: str = "Create account"):
    return _voice_capture(action_label=action_label, key=key, default=None, height=300)
