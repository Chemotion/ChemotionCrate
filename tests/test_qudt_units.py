"""Chemotion unit strings become QUDT IRIs, or nothing: never a guess."""
import logging

import pytest

from chemotion_crate.qudt import _BY_SPELLING, QUDT_UNIT, qudt_unit


@pytest.mark.parametrize("unit,expected", [
    ("g", "GM"), ("mg", "MilliGM"), ("µg", "MicroGM"), ("ug", "MicroGM"), ("kg", "KiloGM"),
    ("mol", "MOL"), ("mmol", "MilliMOL"), ("µmol", "MicroMOL"),
    ("l", "L"), ("L", "L"), ("ml", "MilliL"), ("mL", "MilliL"), ("µl", "MicroL"),
    ("M", "MOL-PER-L"), ("mol/l", "MOL-PER-L"), ("g/mL", "GM-PER-MilliL"), ("g/mol", "GM-PER-MOL"),
    ("°C", "DEG_C"), ("C", "DEG_C"), ("K", "K"), ("kelvin", "K"),
    ("h", "HR"), ("Hour(s)", "HR"), ("s", "SEC"), ("min", "MIN"), ("Minute(s)", "MIN"),
    ("%", "PERCENT"),
])
def test_known_spellings_map(unit, expected):
    assert qudt_unit(unit) == QUDT_UNIT + expected


@pytest.mark.parametrize("unit", ["°F", "F", "f", "deg_F", "deg F", "Fahrenheit"])
def test_fahrenheit_is_deg_f_never_faraday(unit):
    """Bare ``F`` appended to the QUDT namespace resolves to the Faraday, a unit of charge."""
    assert qudt_unit(unit) == QUDT_UNIT + "DEG_F"
    assert qudt_unit(unit) != QUDT_UNIT + "F"


@pytest.mark.parametrize("unit", ["bar", "cm", "angstrom", "atm", "rpm", "ah_g", "nonsense"])
def test_unknown_unit_yields_none(unit):
    assert qudt_unit(unit) is None


def test_unknown_unit_is_logged(caplog):
    with caplog.at_level(logging.INFO, logger="chemotion_crate.qudt"):
        assert qudt_unit("furlong") is None
    assert any("furlong" in r.getMessage() for r in caplog.records)


def test_known_unit_is_not_logged(caplog):
    with caplog.at_level(logging.INFO, logger="chemotion_crate.qudt"):
        qudt_unit("mg")
    assert not caplog.records


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_empty_unit_is_unitless(empty):
    assert qudt_unit(empty) == QUDT_UNIT + "UNITLESS"


def test_matching_is_case_and_space_insensitive():
    assert qudt_unit("  Mg ") == qudt_unit("mg")
    assert qudt_unit("HOUR") == qudt_unit("h")


def test_no_mapping_target_is_obviously_malformed():
    for spelling, iri in _BY_SPELLING.items():
        local = iri[len(QUDT_UNIT):]
        assert iri.startswith(QUDT_UNIT)
        assert local and local.isascii(), f"{spelling!r} -> {iri}"
        assert not set(local) & set(" /°"), f"{spelling!r} -> {iri}"


def test_spellings_are_stored_lowercase():
    assert all(s == s.lower() for s in _BY_SPELLING)
