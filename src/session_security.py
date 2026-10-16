"""Small session protections shared by the Streamlit entry point."""
from __future__ import annotations

import time

import streamlit as st

from src.settings import SESSION_IDLE_TIMEOUT_MINUTES


def expire_idle_session() -> bool:
    """Clear an authenticated session after inactivity; return True if it expired."""
    state = st.session_state
    authenticated = "teacher_data" in state or "student_data" in state
    if not authenticated:
        state.pop("_auth_last_activity", None)
        return False

    now = time.time()
    last = state.get("_auth_last_activity")
    if isinstance(last, (int, float)) and now - last >= SESSION_IDLE_TIMEOUT_MINUTES * 60:
        state.pop("teacher_data", None)
        state.pop("student_data", None)
        state.is_logged_in = False
        state.user_role = None
        state.login_type = None
        state.teacher_login_type = "login"
        state.pop("_auth_last_activity", None)
        return True

    state["_auth_last_activity"] = now
    return False
