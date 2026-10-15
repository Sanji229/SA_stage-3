import io

import pytest
from PIL import Image

from src import validation as v
from src.database import db
from src.services import marks as m
from tests.helpers import fake_face, fake_voice


@pytest.mark.parametrize("name", ["José Álvarez", "O'Neil", "Anne-Marie", "Zoë", "Li", "Dr. Rao", "李雷", "  Ana   María  "])
def test_real_names_are_accepted(name):
    assert v.clean_person_name(name)


@pytest.mark.parametrize("bad", ["", " ", "A", "x" * 61, "<script>alert(1)</script>", "Robert'); DROP TABLE students;--", "Ann\x00e", "Ann\u202eBob",
                                 "R2D2", "名前😀", "=cmd|'/c calc'!A1", "-Dash", None, 123])
def test_bad_names_are_rejected(bad):
    with pytest.raises(v.ValidationError):
        v.clean_person_name(bad)


def test_subject_fields():
    assert v.clean_subject_fields(" cs 101 ", "  Intro   to CS ", " A ") == ("CS101", "Intro to CS", "A")
    for args in (("", "Name", "A"), ("C", "Name", "A"), ("CS-101!", "Name", "A"), ("CS101", "<b>x</b>", "A"), ("CS101", "N", "A"),
                 ("CS101", "Name", ""), ("CS101", "Name", "x" * 21), ("CS101", "Name\x07", "A"), ("A" * 21, "Name", "A")):
        with pytest.raises(v.ValidationError):
            v.clean_subject_fields(*args)


def png(size=(8, 8), mode="RGB", fmt="PNG"):
    b = io.BytesIO(); Image.new(mode, size).save(b, fmt); return b.getvalue()


def test_image_validation_by_content_not_by_name():
    assert v.check_image_bytes(png()).size == (8, 8)
    assert v.check_image_bytes(png(fmt="JPEG"))
    for raw, why in ((b"", "empty"), (b"GIF89a....", "not a valid image"), (b"<html>not an image</html>", "not a valid image"),
                     (png()[:40], "not a valid image"), (png(fmt="GIF"), "JPEG and PNG"), (png(fmt="BMP"), "JPEG and PNG"),
                     (b"\xff\xd8\xff" + b"\x00" * 50, "not a valid image")):
        with pytest.raises(v.ValidationError, match=why):
            v.check_image_bytes(raw)
    with pytest.raises(v.ValidationError, match="larger than"):
        v.check_image_bytes(png(), max_bytes=10)
    with pytest.raises(v.ValidationError, match="resolution"):
        v.check_image_bytes(png((7000, 7000), "L"))                              # decompression-bomb style: tiny file, huge canvas


def test_audio_validation():
    assert v.check_audio_bytes(b"abc")
    for raw, why in ((b"", "empty"), (b"x" * 11, "larger than")):
        with pytest.raises(v.ValidationError, match=why):
            v.check_audio_bytes(raw, max_bytes=10)


def test_reason_rules():
    assert v.clean_reason("  late   arrival ") == "late arrival"
    for bad in ("", "  ", "ab", "x" * 501, "bad\x00reason", None):
        with pytest.raises(v.ValidationError):
            v.clean_reason(bad)


# ------------------------------------------------- server-side enforcement (UI bypassed)
def test_create_student_enforces_everything_server_side(fake_db):
    bad_faces = [None, [], [0.0] * 128, [1.0] * 127, [float("nan")] * 128, [float("inf")] * 128, ["a"] * 128, "x" * 128, {"a": 1}, [True] * 128 + [1]]
    for face in bad_faces:
        with pytest.raises(db.InvalidInput):
            db.create_student("Valid Name", face)
    for voice in ([1.0] * 255, [float("nan")] * 256, [0.0] * 256, "voice"):
        with pytest.raises(db.InvalidInput):
            db.create_student("Valid Name", fake_face(1), voice)
    with pytest.raises(db.InvalidInput):
        db.create_student("<b>x</b>", fake_face(1))
    assert fake_db.tables["students"] == []                                      # no half-created profile from any failure
    row = db.create_student("Valid Name", fake_face(1), fake_voice(1))[0]
    assert row["name"] == "Valid Name"


def test_a_failed_voice_enrolment_never_leaves_a_complete_looking_profile(fake_db):
    with pytest.raises(db.InvalidInput):
        db.create_student("Valid Name", fake_face(2), [1.0] * 10)
    assert fake_db.tables["students"] == []


def test_subject_creation_validated_server_side(fake_db):
    db.create_teacher("t1", "password-1", "Tee One")
    t = db.teacher_login("t1", "password-1")["teacher_id"]
    for args in (("", "Name", "A"), ("CS 1!", "Name", "A"), ("CS101", "<script>", "A"), ("CS101", "Name", "")):
        with pytest.raises(db.InvalidInput):
            db.create_subject(*args, t)
    assert fake_db.tables["subjects"] == []
    assert db.get_subject_by_code("not a code!!") is None and db.get_subject_by_code("%") is None and db.get_subject_by_code("") is None


def test_teacher_registration_validation():
    for args, why in ((("ab", "Name", "longenough1", "longenough1"), "3-32"), (("good_user", "Name", "short", "short"), "at least 8"),
                      (("good_user", "Name", "longenough1", "different11"), "do not match"), (("good user", "Name", "longenough1", "longenough1"), "3-32"),
                      (("good_user", "", "longenough1", "longenough1"), "required"), (("bad/user", "Name", "longenough1", "longenough1"), "3-32"),
                      (("good_user", "Name", "x" * 73, "x" * 73), "too long")):
        assert why in db.validate_registration(*args)
    assert db.validate_registration("good_user", "Name", "longenough1", "longenough1") is None


# ------------------------------------------------------------------------ marks CSV abuse
ENR = {1: "Asha Rao", 2: "Ben Ortiz"}


@pytest.mark.parametrize("raw", [
    b"student_id,marks\n1,5\x002\n", b"student_id,marks\n1,\"unclosed\n", b"student_id,marks\n1\n2,3,4,5\n", b"\x00\x01\x02",
    b"student_id,marks\n" + b"1,1e999\n", b"student_id,marks\n1,\xe2\x80\xae5\n", ("student_id,marks\n1," + "9" * 5000 + "\n").encode(),
    b"student_id,marks\n=1+1,5\n", b"student_id,marks\n1,=SUM(A1)\n", b"student_id;marks\n1;5\n", b"student_id,student_id,marks\n1,2,3\n",
])
def test_hostile_csv_never_crashes_and_never_yields_rows_it_should_not(raw):
    p = m.parse_marks_csv(raw, 20, ENR)                                          # must return, not raise
    assert not p.can_commit or all(1 <= int(r["student_id"]) <= 2 and 0 <= r["obtained"] <= 20 for r in p.rows)


def test_csv_with_formula_in_name_is_neutralised_on_export():
    assert m.marks_to_csv([{"student_id": 1, "name": "=1+1", "obtained": 5}], 10).splitlines()[1].startswith("1,'=1+1")
    assert m.template_csv({1: "+cmd"}).splitlines()[1].startswith("1,'+cmd")


def test_marks_import_rejects_bad_values_at_the_database_layer(fake_db):
    db.create_teacher("t1", "password-1", "Tee One")
    t = db.teacher_login("t1", "password-1")["teacher_id"]
    sub = db.create_subject("S1", "Subject", "A", t)[0]["subject_id"]
    stu = db.create_student("Ann Lee", fake_face(3))[0]["student_id"]
    db.enroll_student_to_subject(stu, sub)
    a = db.create_assessment(t, sub, "Mid", 20)["assessment_id"]
    for bad in (-1, 21, "abc", float("nan"), float("inf"), None, "", "1e400", True and "20.001"):
        with pytest.raises(db.DatabaseError):
            db.import_marks(t, a, [{"student_id": stu, "obtained": bad}])
    for bad_id in (0, 99, -5):
        with pytest.raises(db.DatabaseError):
            db.import_marks(t, a, [{"student_id": bad_id, "obtained": 5}])
    assert fake_db.tables["marks"] == []
    with pytest.raises(db.DatabaseError):
        db.create_assessment(t, sub, "   ", 20)
    for mx in (0, -5, 1e9, float("nan")):
        with pytest.raises(db.DatabaseError):
            db.create_assessment(t, sub, "Other", mx)
