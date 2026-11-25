"""ElementRecord -> chem-dcat-ap mapping (offline, from hand-built records)."""
import isodate
import pytest

from chemotion_crate.model import ElementRecord, MoleculeRecord
from chemotion_crate.nodes import (
    MOLECULE_SCHEME,
    chemotion_duration_to_iso8601,
    has_dcat_mapping,
    reaction_to_dcat,
    sample_to_dcat,
    to_dcat,
)

UNIT = "http://qudt.org/vocab/unit/"
KIND = "http://qudt.org/vocab/quantitykind/"


def _sample(**kw):
    defaults = {"kind": "sample", "id": 7, "short_label": "S-7",
                "url": "https://eln.example.org/api/v1/samples/7"}
    return ElementRecord(**{**defaults, **kw})


def _reaction(**kw):
    defaults = {"kind": "reaction", "id": 5, "name": "Oxidation",
                "url": "https://eln.example.org/api/v1/reactions/5"}
    return ElementRecord(**{**defaults, **kw})


# -- duration strings -------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("3.5 Hour(s)", "PT3H30M"),
    ("3 Day(s)", "P3D"),
    ("90 Minute(s)", "PT1H30M"),
    ("45 Second(s)", "PT45S"),
    ("2 Week(s)", "P14D"),
    ("1.5 h", "PT1H30M"),
    ("10 min", "PT10M"),
])
def test_duration_strings(text, expected):
    assert chemotion_duration_to_iso8601(text) == expected


def test_zero_duration_is_a_valid_zero():
    iso = chemotion_duration_to_iso8601("0 Hour(s)")
    assert iso is not None
    assert isodate.parse_duration(iso).total_seconds() == 0


@pytest.mark.parametrize("text", [None, "", "garbage", "Hour(s)", "5 Fortnights", "-3 Hour(s)"])
def test_unreadable_duration_is_none(text):
    assert chemotion_duration_to_iso8601(text) is None


# -- samples ----------------------------------------------------------------------------------

@pytest.mark.parametrize("role,is_solvent,cls", [
    ("starting_materials", False, "StartingMaterial"),
    ("reactants", False, "Reagent"),
    ("products", False, "ChemicalProduct"),
    ("solvents", False, "DissolvingSubstance"),
    (None, False, "SubstanceSample"),
    (None, True, "DissolvingSubstance"),
    ("unknown_role", False, "SubstanceSample"),
])
def test_sample_role_selects_class(role, is_solvent, cls):
    assert type(sample_to_dcat(_sample(role=role, is_solvent=is_solvent))).__name__ == cls


def test_sample_identity_and_title():
    node = sample_to_dcat(_sample(description="desc"))
    assert node.id == "https://eln.example.org/api/v1/samples/7"
    assert node.title == "S-7"
    assert node.description == "desc"
    assert node.rdf_type.id == "CHEBI:59999"
    # the ELN's database id is not a Chemotion Repository PID, so none is claimed
    assert node.other_identifier is None


def test_sample_without_url_gets_a_urn():
    assert sample_to_dcat(_sample(url=None)).id == "urn:chemotion:sample:7"


def test_molecule_entity_fields():
    mol = MoleculeRecord(id=11, cano_smiles="CCO", inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                         inchistring="InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3", sum_formular="C2H6O",
                         iupac_name="ethanol", molecular_weight=46.07,
                         cas=["64-17-5", "", "64-17-6"], names=["alcohol", ""])
    node = sample_to_dcat(_sample(molecule=mol))
    (entity,) = node.composed_of
    assert entity.id == f"{MOLECULE_SCHEME}11"
    assert entity.smiles[0].value == "CCO"
    assert entity.inchikey[0].value == "LFQSCWFLJHTTHZ-UHFFFAOYSA-N"
    assert entity.inchi[0].value.startswith("InChI=1S/")
    assert entity.molecular_formula[0].value == "C2H6O"
    assert entity.iupac_name[0].value == "ethanol"
    assert entity.has_molar_mass[0].value == pytest.approx(46.07)
    assert entity.has_molar_mass[0].unit == UNIT + "GM-PER-MOL"
    cas = [i.notation for i in entity.other_identifier if i.title == "CAS Registry Number"]
    assert cas == ["64-17-5", "64-17-6"]  # one Identifier per CAS, blanks dropped
    assert [i.notation for i in entity.other_identifier if i.title == "Synonym"] == ["alcohol"]


def test_molecule_without_id_is_minted_under_the_sample():
    node = sample_to_dcat(_sample(molecule=MoleculeRecord(cano_smiles="C")))
    assert node.composed_of[0].id == "https://eln.example.org/api/v1/samples/7#EvaluatedCompound"


def test_molecule_formula_and_mass_fall_back_to_sample_properties():
    node = sample_to_dcat(_sample(molecule=MoleculeRecord(id=1),
                                  properties={"sum_formula": "H2O", "molecular_mass": "18.02"}))
    entity = node.composed_of[0]
    assert entity.molecular_formula[0].value == "H2O"
    assert entity.has_molar_mass[0].value == pytest.approx(18.02)
    assert entity.smiles is None and entity.other_identifier is None


def test_no_molecule_no_composed_of():
    assert sample_to_dcat(_sample()).composed_of is None


def test_amounts_in_mg_ml_mmol():
    node = sample_to_dcat(_sample(mass_g=1.5, volume_l=0.002, amount_mol=0.01, density=0.79))
    assert node.has_mass[0].value == pytest.approx(1500.0)
    assert node.has_mass[0].unit == UNIT + "MilliGM"
    assert node.has_mass[0].has_quantity_type == KIND + "Mass"
    assert node.has_volume[0].value == pytest.approx(2.0)
    assert node.has_volume[0].unit == UNIT + "MilliL"
    assert node.has_amount[0].value == pytest.approx(10.0)
    assert node.has_amount[0].unit == UNIT + "MilliMOL"
    assert node.has_density[0].unit == UNIT + "GM-PER-MilliL"


def test_zero_or_missing_amounts_are_omitted():
    node = sample_to_dcat(_sample(mass_g=0.0, volume_l=None, amount_mol=-1.0))
    assert node.has_mass is None and node.has_volume is None and node.has_amount is None


@pytest.mark.parametrize("role", ["starting_materials", "reactants"])
def test_equivalent_is_unitless_on_starting_material_and_reagent(role):
    node = sample_to_dcat(_sample(role=role, equivalent=1.0))
    (eq,) = node.has_molar_equivalent
    assert eq.value == 1.0
    assert eq.unit == UNIT + "UNITLESS"


@pytest.mark.parametrize("role", ["products", "solvents", None])
def test_equivalent_only_on_starting_material_and_reagent(role):
    node = sample_to_dcat(_sample(role=role, equivalent=1.0))
    assert getattr(node, "has_molar_equivalent", None) is None


def test_known_molarity_unit():
    node = sample_to_dcat(_sample(molarity_value=0.5, molarity_unit="M"))
    (conc,) = node.has_concentration
    assert conc.value == 0.5
    assert conc.unit == UNIT + "MOL-PER-L"
    assert conc.title == "Molarity [M]"


def test_unknown_molarity_unit_omits_unit():
    node = sample_to_dcat(_sample(molarity_value=0.5, molarity_unit="furlongs"))
    (conc,) = node.has_concentration
    assert conc.value == 0.5
    assert conc.unit is None
    assert "furlongs" in conc.title


# -- reactions --------------------------------------------------------------------------------

def test_reaction_materials_by_role():
    rec = _reaction(materials={
        "starting_materials": [_sample(id=1, role="starting_materials", equivalent=1.0)],
        "reactants": [_sample(id=2, role="reactants")],
        "products": [_sample(id=3, role="products")],
        "solvents": [_sample(id=4, role="solvents")],
    })
    node = reaction_to_dcat(rec)
    assert type(node).__name__ == "ChemicalReaction"
    assert type(node.used_starting_material[0]).__name__ == "StartingMaterial"
    assert type(node.used_reactant[0]).__name__ == "Reagent"
    assert type(node.generated_product[0]).__name__ == "ChemicalProduct"
    assert type(node.used_solvent[0]).__name__ == "DissolvingSubstance"


def test_reaction_title_and_fallbacks():
    assert reaction_to_dcat(_reaction()).title == ["Oxidation"]
    bare = reaction_to_dcat(_reaction(name=None, url=None))
    assert bare.title == ["Reaction-5"]
    assert bare.id == "urn:chemotion:reaction:5"
    assert bare.used_starting_material is None and bare.has_duration is None


def test_duration_from_timestamps_wins_over_display_string():
    node = reaction_to_dcat(_reaction(timestamp_start="2026-01-01T10:00:00",
                                      timestamp_stop="2026-01-01T12:15:00",
                                      duration="99 Day(s)"))
    assert node.has_duration == "PT2H15M"


def test_duration_from_display_string_when_no_timestamps():
    assert reaction_to_dcat(_reaction(duration="3.5 Hour(s)")).has_duration == "PT3H30M"


@pytest.mark.parametrize("start,stop", [
    ("2026-01-01T12:00:00", "2026-01-01T10:00:00"),  # negative
    ("not a time", "2026-01-01T10:00:00"),
    ("2026-01-01T10:00:00", None),
])
def test_odd_timestamps_fall_back_to_display_string(start, stop):
    node = reaction_to_dcat(_reaction(timestamp_start=start, timestamp_stop=stop,
                                      duration="3 Day(s)"))
    assert node.has_duration == "P3D"


def test_garbage_duration_is_omitted():
    assert reaction_to_dcat(_reaction(duration="garbage")).has_duration is None


def test_temperature_user_text():
    node = reaction_to_dcat(_reaction(temperature={"valueUnit": "°C", "userText": " 25 "}))
    (t,) = node.has_temperature
    assert t.value == 25.0
    assert t.unit == UNIT + "DEG_C"
    assert t.title == "Temperature"


def test_temperature_steps_win_over_user_text():
    node = reaction_to_dcat(_reaction(temperature={
        "valueUnit": "K", "userText": "300",
        "data": [{"time": "00:00:00", "value": "280"}, {"time": "00:10:00", "value": ""},
                 "junk", {"time": "00:20:00", "value": "290"}]}))
    assert [t.value for t in node.has_temperature] == [280.0, 290.0]
    assert all(t.unit == UNIT + "K" for t in node.has_temperature)


def test_temperature_defaults_to_celsius_and_fahrenheit_is_deg_f():
    assert reaction_to_dcat(_reaction(temperature={"userText": "5"})).has_temperature[0].unit == \
        UNIT + "DEG_C"
    assert reaction_to_dcat(_reaction(temperature={"valueUnit": "°F", "userText": "77"})
                            ).has_temperature[0].unit == UNIT + "DEG_F"


def test_non_numeric_temperature_is_dropped():
    assert reaction_to_dcat(_reaction(temperature={"userText": "room temp"})).has_temperature is None
    assert reaction_to_dcat(_reaction(temperature={})).has_temperature is None


def test_yield_and_status():
    node = reaction_to_dcat(_reaction(yield_percent=85.0, status="Successful"))
    (y,) = node.has_yield
    assert y.value == 85.0 and y.unit == UNIT + "PERCENT"
    (status,) = node.has_qualitative_attribute
    assert status.value == "Successful"
    assert status.rdf_type.id == "SIO:001326"


def test_no_yield_no_status():
    node = reaction_to_dcat(_reaction())
    assert node.has_yield is None and node.has_qualitative_attribute is None


def test_rinchi_identifiers():
    node = reaction_to_dcat(_reaction(properties={
        "rinchi_long_key": " Long-Key ", "rinchi_short_key": "Short-Key",
        "rinchi_web_key": "", "rinchi_string": "RInChI=1.00.1S/x"}))
    ids = {i.title: i.notation for i in node.other_identifier}
    assert ids == {"RInChI": "Long-Key", "RInChI short key": "Short-Key"}


def test_no_rinchi_no_identifiers():
    assert reaction_to_dcat(_reaction()).other_identifier is None


# -- dispatch ---------------------------------------------------------------------------------

def test_dispatch():
    assert has_dcat_mapping(_sample()) and has_dcat_mapping(_reaction())
    assert not has_dcat_mapping(ElementRecord(kind="wellplate", id=1))
    assert type(to_dcat(_reaction())).__name__ == "ChemicalReaction"
    with pytest.raises(KeyError):
        to_dcat(ElementRecord(kind="wellplate", id=1))
