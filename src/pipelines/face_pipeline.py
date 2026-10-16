"""Face pipeline: DETECTION, EMBEDDING and RECOGNITION are separate steps.

* detect_faces()   -> where are faces (and an embedding for each)
* match_encodings()-> whose face is it, *with an explicit outcome per face*

A detected face is never assumed to be a specific student. Each face gets one status:
  matched    nearest enrolled student is within the threshold and clearly better than the rest
  ambiguous  two candidates are too close to call (or a student from another class is closer)
  duplicate  more than one face claims the same student in this photo
  unknown    no enrolled student is within the threshold
Only `matched` faces count as present automatically; everything else is for teacher review.

Neither detection nor recognition proves the student is physically present (a photo of a
photo can match). See README "Limitations".
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np
import streamlit as st
from PIL import Image, ImageOps

from src.database import db
from src.services import biometrics
from src.settings import FACE_MATCH_MARGIN, FACE_MATCH_THRESHOLD, FACE_MIN_SIZE_PX, FACE_NUM_JITTERS
from src.validation import MAX_IMAGE_SIDE

log = logging.getLogger("smartattend.face")
MATCHED, AMBIGUOUS, DUPLICATE, UNKNOWN = "matched", "ambiguous", "duplicate", "unknown"


@dataclass
class FaceMatch:
    index: int
    bbox: tuple[int, int, int, int] | None = None  # left, top, right, bottom in processed image
    status: str = UNKNOWN
    student_id: int | None = None
    distance: float | None = None
    second_distance: float | None = None
    candidates: list[int] = field(default_factory=list)  # students a teacher should consider
    flags: list[str] = field(default_factory=list)       # e.g. small_face, other_class_closer

    @property
    def margin(self) -> float | None:
        return None if self.second_distance is None or self.distance is None else self.second_distance - self.distance


@dataclass
class PhotoResult:
    num_faces: int = 0
    faces: list[FaceMatch] = field(default_factory=list)

    @property
    def matched(self) -> dict[int, float]:
        """student_id -> distance for faces that are safe to auto-accept (back-compat view)."""
        return {f.student_id: f.distance for f in self.faces if f.status == MATCHED}

    @property
    def needs_review(self) -> list[FaceMatch]:
        return [f for f in self.faces if f.status in (AMBIGUOUS, DUPLICATE)]

    @property
    def unmatched(self) -> int:
        return self.num_faces - len(self.matched)

    def summary(self) -> dict[str, int]:
        out = {"faces": self.num_faces, MATCHED: 0, AMBIGUOUS: 0, DUPLICATE: 0, UNKNOWN: 0}
        for f in self.faces:
            out[f.status] += 1
        return out


def to_rgb_array(image) -> np.ndarray:
    """Any PIL image / file-like -> contiguous RGB uint8 array (dlib requires this).

    Handles RGBA PNGs, palette/greyscale images and EXIF-rotated phone photos.
    """
    if not isinstance(image, Image.Image):
        image = Image.open(image)
    image = ImageOps.exif_transpose(image).convert("RGB")
    if max(image.size) > MAX_IMAGE_SIDE:
        image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    return np.ascontiguousarray(np.asarray(image, dtype=np.uint8))


@st.cache_resource(show_spinner="Loading face models…")
def load_dlib_models():
    import dlib  # imported lazily so unit tests that do not need dlib stay fast
    import face_recognition_models

    detector = dlib.get_frontal_face_detector()
    shape_predictor = dlib.shape_predictor(face_recognition_models.pose_predictor_model_location())
    recognizer = dlib.face_recognition_model_v1(face_recognition_models.face_recognition_model_location())
    return detector, shape_predictor, recognizer


def detect_faces(image) -> list[tuple[tuple[int, int, int, int], np.ndarray]]:
    """Detect faces in an uploaded photo, retrying quarter turns if needed.

    Returned boxes always use the normalized input image's coordinate system.
    """
    detector, sp, facerec = load_dlib_models()
    img = to_rgb_array(image)
    height, width = img.shape[:2]
    for turns in range(4):
        oriented = np.ascontiguousarray(np.rot90(img, turns)) if turns else img
        rects = detector(oriented, 1)
        if not rects:
            continue

        out = []
        for rect in rects:
            shape = sp(oriented, rect)
            enc = np.array(facerec.compute_face_descriptor(oriented, shape, FACE_NUM_JITTERS))
            left, top, right, bottom = rect.left(), rect.top(), rect.right(), rect.bottom()
            if turns == 1:  # np.rot90: counter-clockwise
                bbox = (width - bottom, left, width - top, right)
            elif turns == 2:
                bbox = (width - right, height - bottom, width - left, height - top)
            elif turns == 3:
                bbox = (top, height - right, bottom, height - left)
            else:
                bbox = (left, top, right, bottom)
            out.append((bbox, enc))
        return out
    return []


def get_face_embeddings(image) -> list[np.ndarray]:
    return [enc for _, enc in detect_faces(image)]


# ----------------------------------------------------------------------- matching
def build_gallery(rows: list[dict]) -> tuple[list[int], np.ndarray]:
    """ids[i] always describes matrix[i]. Malformed stored embeddings are skipped (and logged),
    never allowed to crash matching for everyone or shift the id<->row mapping."""
    ids, vecs = [], []
    for r in rows:
        emb = r.get("face_embedding")
        if not biometrics.is_valid_embedding(emb, biometrics.FACE_DIM):
            if emb:
                log.warning("Skipping malformed face embedding for student %s", r.get("student_id"))
            continue
        ids.append(int(r["student_id"]))
        vecs.append(emb)
    if not ids:
        return [], np.empty((0, biometrics.FACE_DIM))
    return ids, np.asarray(vecs, dtype=np.float64)


def match_encodings(
    encodings: list[np.ndarray],
    ids: list[int],
    matrix: np.ndarray,
    threshold: float = FACE_MATCH_THRESHOLD,
    allowed_ids: set[int] | None = None,
    margin: float = FACE_MATCH_MARGIN,
    bboxes: list | None = None,
) -> PhotoResult:
    """Classify every detected face; never force an unknown face onto the nearest student.

    `allowed_ids` limits who can be auto-matched (students enrolled in the subject). The
    *whole* gallery is still consulted: if someone outside the class is a closer match, the
    face is marked ambiguous instead of being given to the nearest in-class student.
    """
    if not math.isfinite(threshold) or not 0.0 <= threshold <= 1.0:
        raise ValueError("Face match threshold must be finite and between 0 and 1.")
    if not math.isfinite(margin) or not 0.0 <= margin <= 2.0:
        raise ValueError("Face match margin must be finite and between 0 and 2.")
    result = PhotoResult(num_faces=len(encodings))
    ids_arr = np.array(ids)
    for i, enc in enumerate(encodings):
        face = FaceMatch(index=i, bbox=None if bboxes is None else bboxes[i])
        if face.bbox and min(face.bbox[2] - face.bbox[0], face.bbox[3] - face.bbox[1]) < FACE_MIN_SIZE_PX:
            face.flags.append("small_face")
        result.faces.append(face)
        if not len(ids):
            continue

        dists = np.linalg.norm(matrix - enc, axis=1)
        in_class = np.ones(len(ids), dtype=bool) if allowed_ids is None else np.isin(ids_arr, list(allowed_ids))
        if not in_class.any():
            continue
        cand_idx = np.flatnonzero(in_class)
        order = cand_idx[np.argsort(dists[cand_idx])]
        best = order[0]
        face.distance = float(dists[best])
        face.second_distance = float(dists[order[1]]) if len(order) > 1 else None
        log.debug("face %d: best=%s d=%.4f second=%s", i, ids[best], face.distance, face.second_distance)

        if face.distance > threshold:
            continue  # unknown

        close = [int(ids_arr[j]) for j in order if dists[j] <= threshold and dists[j] - face.distance < margin]
        outsider = [int(ids_arr[j]) for j in np.flatnonzero(~in_class) if dists[j] <= threshold and dists[j] < face.distance + margin]
        if len(close) > 1:
            face.status, face.candidates = AMBIGUOUS, close
        elif outsider:
            face.status, face.candidates = AMBIGUOUS, [int(ids_arr[best])]
            face.flags.append("other_class_closer")
        elif "small_face" in face.flags:
            # Low-resolution crops can be deceptively close to a stored embedding.
            # Keep the candidate visible to the teacher, but do not auto-mark attendance.
            face.status, face.candidates = AMBIGUOUS, [int(ids_arr[best])]
        else:
            face.status, face.student_id, face.candidates = MATCHED, int(ids_arr[best]), [int(ids_arr[best])]

    _resolve_duplicates(result, margin)
    return result


def _resolve_duplicates(result: PhotoResult, margin: float) -> None:
    """A student can appear only once in a photo: extra claimants go to review."""
    claims: dict[int, list[FaceMatch]] = {}
    for f in result.faces:
        if f.status == MATCHED:
            claims.setdefault(f.student_id, []).append(f)
    for sid, faces in claims.items():
        if len(faces) < 2:
            continue
        faces.sort(key=lambda f: f.distance)
        for f in faces[1:]:
            f.status = DUPLICATE
            f.flags.append("duplicate_claim")
        if faces[1].distance - faces[0].distance < margin:  # cannot tell which face is right
            faces[0].status = DUPLICATE
            faces[0].flags.append("duplicate_claim")


@st.cache_data(ttl=300, show_spinner=False)
def _load_gallery(version: str) -> tuple[list[int], np.ndarray]:
    """Heavy part (all embeddings), cached per gallery VERSION. A new enrolment changes the version,
    so every process fetches fresh data immediately; unchanged reruns hit the cache. TTL is only a
    backstop. There is nothing to 'train': matching is a distance search over stored embeddings."""
    log.info("Loading face gallery (version %s)", version)
    return build_gallery(db.get_face_gallery())


def current_gallery() -> tuple[str, list[int], np.ndarray]:
    version = db.get_gallery_version()  # one tiny query per scan
    ids, matrix = _load_gallery(version)
    return version, ids, matrix


def clear_gallery_cache() -> None:
    """Optional: drop this process's cache (the version key already handles other processes)."""
    _load_gallery.clear()


def analyze_photo(image, allowed_ids: set[int] | None = None) -> PhotoResult:
    found = detect_faces(image)
    _, ids, matrix = current_gallery()
    return match_encodings([e for _, e in found], ids, matrix, allowed_ids=allowed_ids, bboxes=[b for b, _ in found])


# ------------------------------------------------------------- enrolment data quality
def confusable_pairs(rows: list[dict], threshold: float = FACE_MATCH_THRESHOLD) -> list[tuple[int, int, float]]:
    """Pairs of DIFFERENT students whose stored faces are within the match threshold.

    Such pairs cannot be told apart reliably (duplicate enrolment, twins, or a bad photo),
    so attendance for both should always be reviewed.
    """
    ids, mat = build_gallery(rows)
    if len(ids) < 2:
        return []
    d = np.linalg.norm(mat[:, None, :] - mat[None, :, :], axis=2)
    ii, jj = np.where(np.triu(d <= threshold, k=1))
    return sorted(((ids[i], ids[j], float(d[i, j])) for i, j in zip(ii, jj)), key=lambda t: t[2])
