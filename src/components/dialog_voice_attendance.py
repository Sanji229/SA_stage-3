import streamlit as st

from src.components.dialog_attendance_results import show_attendance_result
from src.database.db import DatabaseError, get_enrolled_students
from src.pipelines.voice_pipeline import VoiceError, process_bulk_audio_detailed
from src.settings import VOICE_MATCH_THRESHOLD
from src.services.attendance import build_review_session, new_session_timestamp


@st.dialog("Voice attendance", width="large")
def voice_attendance_dialog(subject_id):
    st.write("Record students one at a time. Each student should speak alone for at least 5 seconds, with a short pause between speakers. Overlapping voices cannot be identified reliably.")
    audio = st.audio_input("Record classroom audio")

    if st.button("Analyze audio", width="stretch", type="primary"):
        st.session_state.voice_attendance_results = None
        if audio is None:
            st.warning("Record some audio first.")
            return
        try:
            with st.spinner("Processing audio…"):
                enrolled = get_enrolled_students(st.session_state.teacher_data["teacher_id"], subject_id)
                if not enrolled:
                    st.warning("No students are enrolled in this subject.")
                    return
                candidates = {s["student_id"]: s["voice_embedding"] for s in enrolled if s.get("voice_embedding")}
                if not candidates:
                    st.error("None of the enrolled students has a voice profile.")
                    return
                result = process_bulk_audio_detailed(audio.getvalue(), candidates)
        except (VoiceError, DatabaseError) as exc:
            st.error(str(exc))
            return
        except ImportError:
            st.error("Voice recognition is not installed on this server (see README: resemblyzer).")
            return
        if result.segments == 0:
            st.info("No speech segment was long and clear enough to identify. Re-record each student speaking alone for at least 5 seconds.")
            return
        if not result.matched and not result.ambiguous:
            best = max(result.best_scores, default=0.0)
            st.info(
                f"No voice was confidently identified, so attendance was not created. "
                f"The best profile similarity was {best:.2f}; this app requires {VOICE_MATCH_THRESHOLD:.2f}. "
                "Have each student replace their voice profile with at least 5 seconds of clear speech, "
                "then record students one at a time with a pause between speakers. You can also use classroom photos."
            )
            return
        if result.ambiguous:
            st.info(
                f"{len(result.ambiguous)} speaking turn(s) were too similar to multiple voice profiles. "
                "Check that each student recorded their own voice profile; those rows are marked for review."
            )
        for issue in (result.quality.issues if result.quality else []):
            st.warning(issue)
        matched_scores = {int(sid): score for sid, score in result.matched.items()}
        matched_ids = set(matched_scores)
        present = {sid: f"Voice {score:.2f}" for sid, score in matched_scores.items()}
        notes = {int(s["student_id"]): "No voice profile: mark manually if present" for s in enrolled if not s.get("voice_embedding")}
        ambiguous_ids = {int(sid) for group in result.ambiguous for sid in group}
        for sid in ambiguous_ids:
            present.setdefault(sid, "Possible voice match; teacher review")
        for student in enrolled:
            sid = int(student["student_id"])
            if sid not in matched_ids and sid not in ambiguous_ids and student.get("voice_embedding"):
                notes[sid] = "Not identified; verify attendance"
        for group in result.ambiguous:  # similar-sounding speakers: teacher decides
            for sid in group:
                notes[int(sid)] = "Please verify attendance"
        st.session_state.voice_attendance_results = build_review_session(
            enrolled, present, subject_id, new_session_timestamp(), scores=matched_scores, review_notes=notes, method="voice"
        )

    if st.session_state.get("voice_attendance_results"):
        st.divider()
        df, logs = st.session_state.voice_attendance_results
        show_attendance_result(df, logs)
