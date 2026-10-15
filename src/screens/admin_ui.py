"""Administrator tools. Visibility here is a convenience only: every db.* call re-checks the role."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from src.database import db
from src.database.db import DatabaseError
from src.ui.base_layout import empty_state, page_header


def admin_tab(admin_id: int) -> None:
    page_header("Administration", "Invite teachers and manage access. Invitation codes stay valid until used or revoked.")

    st.subheader("Invite a teacher")
    with st.form("create_invite"):
        c1, c2 = st.columns([2, 1])
        note = c1.text_input(
            "Recipient name (optional)", max_chars=60, placeholder="e.g. Dr. Rao",
            help="Use letters, spaces, apostrophes, hyphens or dots; numbers are not allowed.",
        )
        role = c2.selectbox("Role", ["teacher", "admin"])
        if st.form_submit_button("Create invitation", type="primary"):
            try:
                token = db.create_invitation(admin_id, role, note=note)
            except DatabaseError as exc:
                st.error(str(exc))
            else:
                st.session_state.new_invite_token = token
    token = st.session_state.get("new_invite_token")
    if token:
        st.success("Invitation created. Copy it now: it is shown only once and cannot be recovered.")
        st.code(token, language="text")
        if st.button("I have copied it"):
            st.session_state.pop("new_invite_token", None)
            st.rerun()

    try:
        invites = db.list_invitations(admin_id)
        teachers = db.list_teachers(admin_id)
    except DatabaseError as exc:
        st.error(str(exc))
        return

    st.subheader("Invitations")
    if invites:
        st.dataframe(pd.DataFrame(invites)[["note", "role", "status", "created_at"]], hide_index=True, width="stretch")
        open_ones = {i["invite_id"]: i for i in invites if i["status"] == "open"}
        if open_ones:
            pick = st.selectbox("Revoke an open invitation", list(open_ones), format_func=lambda i: f"{open_ones[i]['note'] or 'no note'} · {open_ones[i]['role']}")
            if st.button("Revoke"):
                try:
                    db.revoke_invitation(admin_id, pick)
                    st.session_state.flash = "Invitation revoked."
                    st.rerun()
                except DatabaseError as exc:
                    st.error(str(exc))
    else:
        empty_state("No invitations yet.")

    st.subheader("Teacher accounts")
    st.dataframe(pd.DataFrame(teachers)[["username", "name", "role", "status"]], hide_index=True, width="stretch")
    others = {t["teacher_id"]: t for t in teachers if t["teacher_id"] != admin_id}
    if others:
        who = st.selectbox("Account", list(others), format_func=lambda i: f"{others[i]['username']} ({others[i]['status']})")
        new = "active" if others[who]["status"] == "disabled" else "disabled"
        if st.button("Enable account" if new == "active" else "Disable account"):
            try:
                db.set_teacher_status(admin_id, who, new)
            except DatabaseError as exc:
                st.error(str(exc))
            else:
                st.session_state.flash = f"{others[who]['username']} is now {new}."
                st.rerun()
