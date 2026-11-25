"""Crate-safe file names and per-element attachment naming."""
import re

import pytest

from chemotion_crate.model import AttachmentRecord
from chemotion_crate.naming import CrateFileNamer, sanitize_filename


@pytest.mark.parametrize("name,expected", [
    ("spectrum", "spectrum"),
    ("a b/c\\d", "a_b_c_d"),
    ("x.y", "x_y"),
    ("Ä-ok_1", "A-ok_1"),
    ("Benzoesäure", "Benzoesaure"),
    ("Образец 1", "Obrazets_1"),
    ("苯甲酸", "BenJiaSuan"),
    ("..", "unnamed"),
    ("***", "unnamed"),
    ("", "unnamed"),
])
def test_sanitize_filename(name, expected):
    assert sanitize_filename(name) == expected


def test_transliteration_warns_once(monkeypatch, caplog):
    import chemotion_crate.naming as naming
    monkeypatch.setattr(naming, "_warned_transliteration", False)
    with caplog.at_level("WARNING", logger="chemotion_crate.naming"):
        sanitize_filename("plain ascii")
        assert not caplog.records, "ASCII names are not transliterated"
        sanitize_filename("Benzoesäure")
        sanitize_filename("Образец")
    assert len(caplog.records) == 1
    assert "transliterated to ASCII" in caplog.records[0].getMessage()
    assert "Benzoesaure" in caplog.records[0].getMessage()


def _names(*atts):
    namer = CrateFileNamer()
    for att in atts:
        namer.add(att)
    return namer, [name for _, name in namer.files]


def test_extension_is_kept_and_stem_sanitised():
    _, names = _names(AttachmentRecord(filename="my spectrum.jdx", id=1))
    assert names == ["my_spectrum.jdx"]


def test_directory_parts_are_dropped():
    _, names = _names(AttachmentRecord(filename="../../etc/passwd", id=1))
    assert names == ["passwd"]


def test_same_record_twice_is_one_file():
    att = AttachmentRecord(filename="a.txt", identifier="u-1")
    again = AttachmentRecord(filename="a.txt", identifier="u-1")
    _, names = _names(att, again)
    assert names == ["a.txt"]


def test_dedupe_is_by_identity_not_filename():
    namer, names = _names(AttachmentRecord(filename="spectrum.jdx", identifier="u-1"),
                          AttachmentRecord(filename="spectrum.jdx", identifier="u-2"),
                          AttachmentRecord(filename="spectrum.jdx", id=3))
    assert names == ["spectrum.jdx", "spectrum_2.jdx", "spectrum_3.jdx"]


def test_sanitised_collision_gets_a_suffix():
    _, names = _names(AttachmentRecord(filename="a b.txt", id=1),
                      AttachmentRecord(filename="a_b.txt", id=2))
    assert names == ["a_b.txt", "a_b_2.txt"]


def test_suffix_skips_names_already_claimed():
    _, names = _names(AttachmentRecord(filename="a.txt", id=1),
                      AttachmentRecord(filename="a_2.txt", id=2),
                      AttachmentRecord(filename="a.txt", id=3))
    assert names == ["a.txt", "a_2.txt", "a_3.txt"]


def test_same_name_different_records_without_ids_differing_source_names_kept():
    _, names = _names(AttachmentRecord(filename="a b.txt"), AttachmentRecord(filename="a_b.txt"))
    assert names == ["a_b.txt", "a_b_2.txt"]


def test_identical_records_without_any_id_collapse():
    _, names = _names(AttachmentRecord(filename="a.txt"), AttachmentRecord(filename="a.txt"))
    assert names == ["a.txt"]


def test_name_of():
    first = AttachmentRecord(filename="x.csv", identifier="u-1")
    second = AttachmentRecord(filename="x.csv", identifier="u-2")
    namer, _ = _names(first, second)
    assert namer.name_of(first) == "x.csv"
    assert namer.name_of(second) == "x_2.csv"
    assert namer.name_of(AttachmentRecord(filename="x.csv", identifier="other")) is None


@pytest.mark.parametrize("filename", ["scan.raw data", "spec.jdx#1"])
def test_crate_names_are_always_safe(filename):
    _, names = _names(AttachmentRecord(filename=filename, id=1))
    assert re.fullmatch(r"[A-Za-z0-9_\-.]+", names[0]), names[0]
