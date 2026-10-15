"""Gallery lifecycle: there is no classifier to train. Matching is a distance search over stored
embeddings, cached per gallery VERSION. These tests prove new enrolments are seen immediately,
reruns do not reload, and bad data cannot break or mislabel recognition. They do NOT measure accuracy."""
import numpy as np
import pytest

from src.database import db
from src.pipelines import face_pipeline as fp
from src.services import biometrics
from tests.helpers import fake_face


@pytest.fixture()
def gallery_calls(fake_db, monkeypatch):
    calls = {"n": 0}
    real = db.get_face_gallery

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(db, "get_face_gallery", counting)
    return calls


def enroll(name, i):
    return db.create_student(name, fake_face(i))[0]["student_id"]


def classify(vec, allowed=None):
    _, ids, mat = fp.current_gallery()
    return fp.match_encodings([np.array(vec)], ids, mat, allowed_ids=allowed).faces[0]


def test_known_unknown_and_new_students(fake_db):
    a, b = enroll("Asha Rao", 1), enroll("Ben Ortiz", 2)
    assert classify(fake_face(1)).student_id == a and classify(fake_face(2)).student_id == b
    stranger = classify(fake_face(77))
    assert stranger.status == fp.UNKNOWN and stranger.student_id is None
    c = enroll("Chen Wei", 3)                                                   # newly enrolled: recognised immediately
    assert classify(fake_face(3)).student_id == c and classify(fake_face(1)).student_id == a


def test_ambiguous_match_when_two_profiles_are_nearly_identical(fake_db):
    a = enroll("Asha Rao", 1)
    twin = (np.array(fake_face(1)) + np.random.default_rng(0).normal(scale=0.002, size=128)).tolist()
    fake_db.table("students").insert({"name": "Twin Row", "face_embedding": twin, "voice_embedding": None}).execute()  # bypasses the guard, as bad legacy data would
    f = classify(fake_face(1))
    assert f.status == fp.AMBIGUOUS and f.student_id is None and len(f.candidates) == 2 and a in f.candidates
    assert fp.confusable_pairs(db.get_face_gallery())                           # and the data-quality check reports it


def test_reruns_do_not_reload_and_new_enrolment_reloads_exactly_once(gallery_calls):
    enroll("Asha Rao", 1)
    base = gallery_calls["n"]                                                   # create_student's own duplicate checks also read
    for _ in range(25):                                                         # Streamlit reruns / repeated scans
        fp.current_gallery()
    assert gallery_calls["n"] == base + 1
    enroll("Ben Ortiz", 2)
    after_enrol = gallery_calls["n"]
    for _ in range(25):
        fp.current_gallery()
    assert gallery_calls["n"] == after_enrol + 1


def test_reenrolment_and_deletion_change_the_version(fake_db):
    a = enroll("Asha Rao", 1)
    v1 = db.get_gallery_version()
    assert db.get_gallery_version() == v1                                       # stable while nothing changes
    row = fake_db.tables["students"][0]
    row["face_embedding"] = fake_face(9)
    row["biometric_updated_at"] = "2026-06-01T00:00:00+00:00"                   # what the DB trigger does
    v2 = db.get_gallery_version()
    assert v2 != v1 and classify(fake_face(9)).student_id == a and classify(fake_face(1)).status == fp.UNKNOWN
    fake_db.tables["students"].clear()
    assert db.get_gallery_version() not in (v1, v2) and fp.current_gallery()[1] == []


def test_version_still_detects_additions_on_an_unmigrated_database(fake_db):
    fake_db.missing_columns["students"] = {"biometric_updated_at"}
    enroll("Asha Rao", 1)
    v1 = db.get_gallery_version()
    enroll("Ben Ortiz", 2)
    assert db.get_gallery_version() != v1 and len(fp.current_gallery()[1]) == 2


def test_cache_survives_a_failed_refresh_and_is_not_poisoned(fake_db, monkeypatch):
    a = enroll("Asha Rao", 1)
    v, ids, _ = fp.current_gallery()
    real = db.get_face_gallery
    monkeypatch.setattr(db, "get_face_gallery", lambda: (_ for _ in ()).throw(db.DatabaseError("db down")))
    assert fp.current_gallery()[1] == ids                                       # same version: cache hit, DB not needed
    enroll_ok = fake_db.table("students").insert({"name": "Late Joiner", "face_embedding": fake_face(5), "voice_embedding": None}).execute()
    with pytest.raises(db.DatabaseError):                                       # new version but DB failing: error, nothing stale/half-built cached
        fp.current_gallery()
    monkeypatch.setattr(db, "get_face_gallery", real)
    assert len(fp.current_gallery()[1]) == 2 and a in fp.current_gallery()[1]


def test_ids_always_line_up_with_rows_even_with_corrupt_embeddings():
    rows = [
        {"student_id": 10, "face_embedding": fake_face(1)},
        {"student_id": 11, "face_embedding": None},
        {"student_id": 12, "face_embedding": [0.1] * 64},                       # wrong length
        {"student_id": 13, "face_embedding": [float("nan")] * 128},
        {"student_id": 14, "face_embedding": fake_face(2)},
        {"student_id": 15, "face_embedding": "garbage"},
    ]
    ids, mat = fp.build_gallery(rows)
    assert ids == [10, 14] and mat.shape == (2, 128)
    assert np.allclose(mat[0], fake_face(1)) and np.allclose(mat[1], fake_face(2))
    assert fp.match_encodings([np.array(fake_face(2))], ids, mat).faces[0].student_id == 14
    assert fp.build_gallery([])[0] == [] and fp.build_gallery([{"student_id": 1, "face_embedding": [1] * 3}])[0] == []


def test_duplicate_registration_is_blocked_including_near_copies(fake_db):
    enroll("Asha Rao", 1)
    with pytest.raises(db.DuplicateError, match="already registered"):
        db.create_student("Someone Else", fake_face(1))                         # exact same face
    near = (np.array(fake_face(1)) + np.random.default_rng(1).normal(scale=0.01, size=128)).tolist()
    with pytest.raises(db.DuplicateError):
        db.create_student("Someone Else", near)                                 # slightly different photo of the same face
    assert len(fake_db.tables["students"]) == 1
    db.create_student("Ben Ortiz", fake_face(2))                                # a different person is fine
    assert len(fake_db.tables["students"]) == 2


def test_concurrent_registration_race_keeps_only_the_older_profile(fake_db, monkeypatch):
    real = db.get_face_gallery
    state = {"first": True}

    def racing():
        if state["first"]:                                                      # our pre-check sees an empty gallery...
            state["first"] = False
            fake_db.table("students").insert({"name": "Older Request", "face_embedding": fake_face(1), "voice_embedding": None}).execute()  # ...then the other request lands
            return []
        return real()

    monkeypatch.setattr(db, "get_face_gallery", racing)
    with pytest.raises(db.DuplicateError):
        db.create_student("Newer Request", fake_face(1))
    assert [s["name"] for s in fake_db.tables["students"]] == ["Older Request"]


def test_student_session_and_lookups_never_carry_biometrics(fake_db):
    sid = db.create_student("Asha Rao", fake_face(1), [0.1] * 256)[0]["student_id"]
    assert set(db.get_student(sid)) == {"student_id", "name"}
    assert not biometrics.is_valid_embedding(db.get_student(sid).get("face_embedding"), 128)
