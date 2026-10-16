from __future__ import annotations

import hashlib
import logging

import streamlit as st

from src.components.dialog_enroll import enroll_dialog
from src.components.footer import footer_dashboard
from src.components.header import header_dashboard
from src.components.subject_card import subject_card
from src.components.voice_capture import voice_capture
from src.database.db import (
    DatabaseError,
    register_student,
    reserve_student_registration,
    get_student,
    get_student_attendance,
    get_student_subjects,
    delete_student_profile,
    update_student_voice_embedding,
    update_student_face_embedding,
    unenroll_student_to_subject,
)
from src.pipelines.face_pipeline import analyze_photo, clear_gallery_cache, detect_faces
from src.pipelines.voice_pipeline import VoiceError, get_voice_embedding
from src.screens.marks_ui import student_marks_section
from src.services.attendance import DEFAULT_MIN_ATTENDANCE, attendance_rate, low_attendance_subjects, student_stats
from src.settings import FACE_MIN_SIZE_PX
from src.validation import ValidationError, check_audio_bytes, check_image_bytes, decode_audio_base64
from src.ui.base_layout import empty_state, page_header, show_flash, style_base_layout

log = logging.getLogger("smartattend.student")


def _start_session(student: dict) -> None:
    st.session_state.is_logged_in = True
    st.session_state.user_role = "student"
    st.session_state.student_data = {"student_id": student["student_id"], "name": student["name"]}


def _logout() -> None:
    st.session_state.pop("student_data", None)
    st.session_state.is_logged_in = False
    st.session_state.user_role = None


def _save_student_voice_profile(student_id: int, capture: dict) -> None:
    try:
        with st.spinner("Processing your voice sample…"):
            audio_bytes = decode_audio_base64(capture.get("audio_base64", ""))
            embedding = get_voice_embedding(check_audio_bytes(audio_bytes))
            update_student_voice_embedding(student_id, embedding)
    except ValidationError as exc:
        st.error(str(exc))
    except (ValidationError, VoiceError, DatabaseError) as exc:
        st.error(str(exc))
    except ImportError:
        st.error("Voice recognition is not installed on this server.")
    else:
        st.success("Voice profile saved. You can record a new sample here to replace it.")


def _save_student_face_profile(student_id: int, photo) -> None:
    try:
        with st.spinner("Updating your face profile…"):
            normalized = check_image_bytes(photo.getvalue())
            faces = detect_faces(normalized)
            if len(faces) != 1:
                st.warning("Exactly one clear face is needed. Please take another photo.")
                return
            bbox, embedding = faces[0]
            if min(bbox[2] - bbox[0], bbox[3] - bbox[1]) < FACE_MIN_SIZE_PX:
                st.warning(f"Move closer so your face is at least {FACE_MIN_SIZE_PX} pixels wide and tall.")
                return
            update_student_face_embedding(student_id, embedding.tolist())
    except (ValidationError, DatabaseError) as exc:
        st.error(str(exc))
    except ImportError:
        st.error("Face recognition is not installed on this server.")
    except Exception:  # noqa: BLE001
        log.exception("Face profile update failed")
        st.error("Could not update your face profile. Please try again or ask your teacher for help.")
    else:
        clear_gallery_cache()
        st.success("Face profile updated. Your photo was processed but is not stored.")


def student_dashboard():
    student = st.session_state.student_data
    right = header_dashboard()
    with right:
        c1, c2 = st.columns([3, 1], vertical_alignment="center")
        c1.markdown(f"**{student['name']}**")
        if c2.button("Log out", key="student_logout"):
            _logout()
            st.rerun()
    show_flash()

    top, action = st.columns([3, 1], vertical_alignment="bottom")
    with top:
        page_header("Your subjects", "Subjects you are enrolled in and your attendance so far.")
    with action:
        if st.button("Enroll in subject", type="primary", width="stretch"):
            enroll_dialog()

    try:
        with st.spinner("Loading your subjects…"):
            subjects = get_student_subjects(student["student_id"])
            logs = get_student_attendance(student["student_id"])
    except DatabaseError as exc:
        st.error(str(exc))
        return

    stats_map = student_stats(logs)
    total = sum(s["total"] for s in stats_map.values())
    attended = sum(s["attended"] for s in stats_map.values())
    m1, m2, m3 = st.columns(3)
    m1.metric("Enrolled subjects", len(subjects))
    m2.metric("Classes recorded", total)
    rate = attendance_rate(attended, total)
    m3.metric("Overall attendance", "–" if rate is None else f"{rate}%")
    st.write("")

    with st.expander("Set up voice attendance"):
        st.write("Read the prompt below in your normal speaking voice while alone. Aim for at least 5 seconds; you can replace this sample anytime.")
        st.info('“Hello, my name is [your name]. I am present today. I enjoy learning new things and sharing ideas with my classmates.”')
        voice_sample = voice_capture(key="student_voice_profile_capture", action_label="Save voice profile")
        if isinstance(voice_sample, dict) and voice_sample.get("submitted"):
            request_id = voice_sample.get("request_id")
            if request_id != st.session_state.get("student_voice_profile_request"):
                st.session_state.student_voice_profile_request = request_id
                _save_student_voice_profile(student["student_id"], voice_sample)

    with st.expander("Update face sign-in profile"):
        st.write("Use this if your appearance has changed or face sign-in has become unreliable. Your photo is processed into a template and is not stored.")
        face_photo = st.camera_input("Take a new face profile photo", key="student_face_profile_camera")
        if face_photo:
            face_digest = hashlib.sha256(face_photo.getvalue()).hexdigest()
            if st.session_state.get("student_face_profile_request") != face_digest:
                st.session_state.student_face_profile_request = face_digest
                _save_student_face_profile(student["student_id"], face_photo)

    if not subjects:
        empty_state("You are not enrolled in any subject yet. Use <b>Enroll in subject</b> and enter the code from your teacher.")
        footer_dashboard()
        return

    low = set(low_attendance_subjects(stats_map, DEFAULT_MIN_ATTENDANCE))
    if low:
        names = [(n.get("subjects") or {}).get("name", "a subject") for n in subjects if n["subject_id"] in low]
        st.warning(f"Low attendance: you are below {DEFAULT_MIN_ATTENDANCE:g}% in " + ", ".join(names) + ".")

    cols = st.columns(2)
    for i, node in enumerate(subjects):
        sub = node.get("subjects") or {}
        sid = node["subject_id"]
        s = stats_map.get(sid, {"total": 0, "attended": 0})
        pct = attendance_rate(s["attended"], s["total"])

        def unenroll_ui(sub=sub, sid=sid):
            with st.popover("Unenroll", icon=":material/logout:"):
                st.write(f"Leave **{sub.get('name', 'this subject')}**? Your past attendance records are kept.")
                if st.button("Yes, unenroll", key=f"unenroll_{sid}", type="primary"):
                    try:
                        unenroll_student_to_subject(student["student_id"], sid)
                    except DatabaseError as exc:
                        st.error(str(exc))
                        return
                    st.session_state.flash = f"Unenrolled from {sub.get('name', 'subject')}."
                    st.rerun()

        with cols[i % 2]:
            subject_card(
                sub.get("name", "Unknown"),
                sub.get("subject_code", "?"),
                sub.get("section", "-"),
                stats=[("📅", "classes", s["total"]), ("✅", "attended", s["attended"])]
                + ([("📈", "%", pct)] if pct is not None else []),
                footer_callback=unenroll_ui,
                progress=pct,
            )
    st.divider()
    student_marks_section(student["student_id"])
    with st.expander("Privacy and account deletion"):
        st.write("Deleting your profile removes your face and voice templates and enrollment. Attendance and marks remain for institutional records, labeled **Former student**.")
        confirm_delete = st.checkbox("I understand that this action cannot be undone.", key="confirm_student_profile_deletion")
        if st.button("Delete my profile", type="secondary", disabled=not confirm_delete, key="delete_student_profile"):
            try:
                delete_student_profile(student["student_id"])
            except DatabaseError as exc:
                st.error(str(exc))
            else:
                clear_gallery_cache()
                _logout()
                st.session_state.profile_deleted_notice = True
                st.rerun()
    footer_dashboard()


def _registration_form(photo) -> None:
    with st.container(border=True):
        st.subheader("Create your profile")
        st.caption("Your face and voice are converted to numbers for matching. The photo and raw recording are not stored.")
        name = st.text_input("Your name", key="new_student_name", placeholder="e.g. Hamza Rizvi", max_chars=60,
                             help="Use letters, spaces, apostrophes, hyphens or dots; numbers are not allowed.")
        st.markdown("**Voice sample required** for voice attendance. Read this prompt naturally while alone for at least 5 seconds:")
        prompt_name = name.strip() or "[your name]"
        prompt = f"My name is {prompt_name}. I am present. Present, ma’am. Yes, ma’am."
        st.info(f'“{prompt}”')
        consent = st.checkbox(
            "I consent to SmartAttend processing and storing face and voice biometric templates for attendance. I understand I can delete my profile later; attendance and marks will be retained anonymously.",
            key="student_biometric_consent",
        )
        capture = voice_capture(key="student_registration_voice_capture")

        if isinstance(capture, dict) and capture.get("submitted"):
            request_id = capture.get("request_id")
            if request_id == st.session_state.get("student_registration_voice_request"):
                return
            st.session_state.student_registration_voice_request = request_id
            if not consent:
                st.warning("Please provide biometric consent before creating a profile.")
                return
            if not name.strip():
                st.warning("Please enter your name before creating the account.")
                return
            try:
                audio_bytes = decode_audio_base64(capture.get("audio_base64", ""))
            except ValidationError as exc:
                st.error(str(exc))
                return
            with st.spinner("Creating your profile…"):
                try:
                    reserve_student_registration()
                    normalized_photo = check_image_bytes(photo.getvalue())
                    faces = detect_faces(normalized_photo)
                    if len(faces) != 1:
                        st.error("Exactly one clear face is needed to register. Please retake the photo.")
                        return
                    bbox, face_embedding = faces[0]
                    if min(bbox[2] - bbox[0], bbox[3] - bbox[1]) < FACE_MIN_SIZE_PX:
                        st.error(f"Move closer so your face is at least {FACE_MIN_SIZE_PX} pixels wide and tall.")
                        return
                    # A recording that cannot be used must stop registration: no half-finished profile.
                    voice_emb = get_voice_embedding(check_audio_bytes(audio_bytes))
                    rows = register_student(name, face_embedding.tolist(), voice_emb, _reserved=True, biometric_consent=True)
                except ValidationError as exc:
                    st.error(str(exc))
                    return
                except VoiceError as exc:
                    st.error(f"Voice enrolment failed: {exc} Please record the prompt again.")
                    return
                except ImportError:
                    st.error("Voice enrolment is not available on this server, so the profile cannot be created right now.")
                    return
                except DatabaseError as exc:
                    st.error(str(exc))
                    return
            clear_gallery_cache()
            _start_session(rows[0])
            st.session_state.flash = f"Profile created. Welcome, {rows[0]['name']}!"
            st.rerun()


def student_screen():
    style_base_layout()
    if st.session_state.pop("profile_deleted_notice", False):
        st.success("Your profile and biometric templates were deleted. Attendance and marks remain anonymized as Former student.")
    if "student_data" in st.session_state:
        student_dashboard()
        return

    right = header_dashboard()
    with right:
        if st.button("Back to home", key="student_back"):
            st.session_state.login_type = None
            st.query_params.clear()
            st.rerun()

    page_header("Student sign in", "Look at the camera to sign in with FaceID. New here? You will be asked to register.")
    photo = st.camera_input("Position your face in the centre of the frame")
    if photo:
        try:
            with st.spinner("Scanning…"):
                result = analyze_photo(photo)
                student = get_student(next(iter(result.matched))) if len(result.matched) == 1 and result.num_faces == 1 else None
        except DatabaseError as exc:
            st.error(str(exc))
            return
        except Exception:  # noqa: BLE001
            log.exception("Face scan failed")
            st.error("Could not process the photo. Please try again.")
            return

        if result.num_faces == 0:
            st.warning("No face found. Face the camera in good light and try again.")
        elif result.num_faces > 1:
            st.warning("More than one face found. Only one person should be in the frame.")
        elif result.faces and result.faces[0].status in ("ambiguous", "duplicate"):
            st.warning("Your face could not be matched confidently (it is very similar to another profile). "
                       "Please ask your teacher to verify you; registering again would create a duplicate profile.")
        elif student:
            _start_session(student)
            st.session_state.flash = f"Welcome back, {student['name']}!"
            st.rerun()
        else:
            photo_digest = hashlib.sha256(photo.getvalue()).hexdigest()
            if st.session_state.get("registration_photo_digest") != photo_digest:
                st.session_state.registration_photo_digest = photo_digest
                st.session_state.new_student_name = ""
            st.info("Face not recognised. If you are new, register below.")
            _registration_form(photo)
    footer_dashboard()
