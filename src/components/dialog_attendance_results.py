import streamlit as st
import pandas as pd

from src.database.db import DatabaseError, save_attendance_session
from src.services.attendance import apply_review_edits


def show_attendance_result(df, logs):
    """Editable review table. Nothing is saved until the teacher confirms; edits are recorded as manual."""
    st.write("Review attendance before confirming. Tick **Present** for anyone who attended.")
    timestamp = logs[0]["timestamp"] if logs else "x"
    headers = st.columns([2.0, 0.8, 0.9, 1.5, 2.2])
    for col, label in zip(headers, ("Name", "Mark present", "Status", "Evidence", "Review")):
        col.markdown(f"**{label}**")

    edited_rows = []
    for _, row in df.iterrows():
        cells = st.columns([2.0, 0.8, 0.9, 1.5, 2.2], vertical_alignment="center")
        cells[0].write(row["Name"])
        is_present = cells[1].checkbox(
            "Present",
            value=bool(row["Present"]),
            key=f"review_present_{timestamp}_{int(row['ID'])}",
            label_visibility="collapsed",
        )
        status_color = "#16803c" if is_present else "#c62828"
        status_text = "Present" if is_present else "Absent"
        cells[2].markdown(f"<span style='color:{status_color};font-weight:600'>{status_text}</span>", unsafe_allow_html=True)
        cells[3].write(row["Evidence"])
        cells[4].write(row["Review"])
        edited_rows.append({"ID": int(row["ID"]), "Present": is_present})

    edited = df[["ID"]].merge(pd.DataFrame(edited_rows), on="ID", how="left")
    final = apply_review_edits(logs, edited)
    n_present = sum(1 for l in final if l["is_present"])
    n_manual = sum(1 for l in final if l.get("source") == "manual")
    st.caption(f"{n_present} present · {len(final) - n_present} absent · {n_manual} manually changed")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("Discard", width="stretch", key="att_discard"):
            st.session_state.voice_attendance_results = None
            st.session_state.attendance_images = []
            st.rerun()
    with col2:
        if st.button("Confirm & save", width="stretch", type="primary", key="att_confirm"):
            try:
                written = save_attendance_session(final, teacher_id=st.session_state.teacher_data["teacher_id"])
            except DatabaseError as exc:
                st.error(str(exc))
                return
            st.session_state.attendance_images = []
            st.session_state.voice_attendance_results = None
            st.session_state.flash = (
                "Attendance saved." if written else "This session was already saved, so nothing new was added."
            )
            st.rerun()


@st.dialog("Attendance report", width="large")
def attendance_result_dialog(df, logs, summary: str = ""):
    if summary:
        st.caption(summary)
    show_attendance_result(df, logs)
