"""Supabase client factory.

The client is created lazily so the app can show a helpful setup screen
instead of crashing at import time when credentials are missing.

SECURITY: the key used here is a *server-side* key and must never be sent to
the browser or committed. SmartAttend uses its own username/password (teachers)
and face (students) authentication, so Row Level Security cannot identify the
end user; access control is enforced in this server-side code instead. Keep RLS
ENABLED on every table with no public policies (see supabase/schema.sql) so the
public anon key cannot read or write anything.
"""
from __future__ import annotations

import logging

import streamlit as st

from src.settings import get_setting

log = logging.getLogger("smartattend.db")


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


_client_override = None  # used by tests only


def set_client_for_testing(client) -> None:
    global _client_override
    _client_override = client


def classify_key(key: str | None) -> str:
    """'secret' (server-side), 'public' (anon/publishable) or 'unknown'. Never logs the key."""
    import base64
    import json

    if not key:
        return "unknown"
    if key.startswith("sb_secret_"):
        return "secret"
    if key.startswith("sb_publishable_"):
        return "public"
    parts = key.split(".")
    if len(parts) == 3:
        try:
            payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            return {"service_role": "secret", "anon": "public"}.get(payload.get("role"), "unknown")
        except Exception:  # noqa: BLE001
            return "unknown"
    return "unknown"


@st.cache_resource(show_spinner=False)
def _create_client():
    url = get_setting("SUPABASE_URL")
    key = get_setting("SUPABASE_KEY")
    if not url or not key:
        raise ConfigError(
            "SUPABASE_URL and SUPABASE_KEY are not configured. "
            "Copy .env.example to .env (or .streamlit/secrets.toml.example to "
            ".streamlit/secrets.toml) and fill in your project values."
        )
    return build_client(url, key)


def build_client(url: str, key: str):
    from supabase import create_client

    kind = classify_key(key)
    if kind == "public":
        # Fail closed: with a public key the app would either be denied everything (RLS on) or would
        # expose every table to anyone holding that key (RLS off). Neither is acceptable.
        raise ConfigError(
            "SUPABASE_KEY is a public (anon/publishable) key. SmartAttend needs the server-side SECRET key, "
            "kept only in server secrets, with Row Level Security enabled (see docs/SECURITY.md)."
        )
    log.info("Supabase key type: %s", kind)
    return create_client(url, key)


def get_supabase():
    if _client_override is not None:
        return _client_override
    return _create_client()
