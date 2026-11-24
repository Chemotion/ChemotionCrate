"""Export against a live Chemotion ELN. Skipped unless CHEMOTION_URL/USER/PASSWORD are set.

Run with the seeded docker stack of the chemotion-api repo (``just stack-reset`` there). The
restricted-share test also needs the seed's second user (``CHEMOTION_SECOND_USER``/``_PASSWORD``)::

    CHEMOTION_URL=http://127.0.0.1:3579 CHEMOTION_USER=API CHEMOTION_PASSWORD='…' \\
        uv run pytest -m integration tests/test_integration_live.py
"""
from __future__ import annotations

import hashlib
import os
import warnings

import pytest

from chemotion_crate import Author, AuthorAffiliation, export, read

pytestmark = pytest.mark.integration

AUTHORS = [Author(name="Live Test", orcid="0000-0002-1825-0097",
                  affiliations=[AuthorAffiliation(name="KIT", ror="https://ror.org/04t3en479")],
                  email="no-reply@eln.invalid")]


@pytest.fixture(scope="module")
def instance():
    url, user, password = (os.environ.get(k) for k in
                           ("CHEMOTION_URL", "CHEMOTION_USER", "CHEMOTION_PASSWORD"))
    if not (url and user and password):
        pytest.skip("CHEMOTION_URL/USER/PASSWORD not set")
    from chemotion_api import Instance
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # password login is deprecated in the client
        return Instance(url).test_connection().login(user, password)


def test_export_root_child_collections_and_read_back(instance, tmp_path):
    root = instance.get_root_collection()
    exported = 0
    for col in root.children[:5]:
        out = export(instance, col, tmp_path / f"{col.id}.eln", authors=AUTHORS,
                     ontology_lookup=False)
        with read(out) as crate:
            assert crate.is_chemotion_crate
            assert crate.source_eln and crate.source_eln.get("version", "").startswith("v")
            assert crate.root.name == col.label
            for f in crate.files:
                data = f.read()
                assert hashlib.sha256(data).hexdigest() == f.node["sha256"]
                assert f.content_size == len(data)
        exported += 1
    assert exported


def test_withheld_host_leaves_no_trace(instance, tmp_path):
    col = instance.get_root_collection().children[0]
    out = export(instance, col, tmp_path / "w.eln", authors=AUTHORS, ontology_lookup=False,
                 disclose_host=False)
    host = instance.host_url.split("://", 1)[-1].strip("/")
    with read(out) as crate:
        import json
        assert host not in json.dumps(crate.metadata)
        assert crate.source_eln["@id"] == "https://withheld.chemotion.invalid"


# -- what a crate must get right on a real server ----------------------------------------------
# Each test makes its own data through the client, so it does not depend on what the seed holds.

def _fresh_collection(instance, prefix, *children):
    import uuid
    root = instance.get_root_collection()
    label = f"{prefix}_{uuid.uuid4().hex[:6]}"
    root.add_collection([label])
    for child in children:
        root.add_collection([label, child])
    root.save()
    return instance.get_root_collection(reload=True).get_collection(label), label


def _metadata(path):
    import json
    with read(path) as crate:
        return crate.metadata, json.dumps(crate.metadata)


def test_an_element_in_two_collections_is_stored_once_with_both_memberships(instance, tmp_path):
    parent, _ = _fresh_collection(instance, "CRATE_two_cols", "A", "B")
    col_a, col_b = parent.get_collection("A"), parent.get_collection("B")
    sample = col_a.new_sample_smiles("CCO")
    sample.save()
    col_a.assign_elements(col_b, sample)

    out = export(instance, parent, tmp_path / "two.eln", authors=AUTHORS, ontology_lookup=False)
    with read(out) as crate:
        samples = [e for e in crate.elements if e.kind == "sample" and e.source_id == sample.id]
        assert len(samples) == 1, "stored once"
        member_of = samples[0].node["isPartOf"]
        assert isinstance(member_of, list) and len(member_of) == 2
        assert {c.name for c in crate.collections} >= {"A", "B"}


def test_reaction_type_and_analysis_technique_become_defined_terms(instance, tmp_path):
    col, _ = _fresh_collection(instance, "CRATE_terms")
    sample = col.new_sample_smiles("CCO")
    sample.analyses.add_analyses("1H NMR")["kind"] = (
        "CHMO:0000595 | proton nuclear magnetic resonance spectroscopy (1H NMR)")
    sample.save()
    reaction = col.new_reaction()
    reaction.properties["rxno"] = "RXNO:0000024 | Michael addition"
    reaction.save()

    out = export(instance, col, tmp_path / "terms.eln", authors=AUTHORS, ontology_lookup=False)
    metadata, text = _metadata(out)
    terms = {n["@id"] for n in metadata["@graph"] if n.get("@type") == "DefinedTerm"}
    assert "CHMO:0000595" in terms, "the analysis kind reached the crate as a term"
    assert "RXNO:0000024" in terms, "the reaction type reached the crate as a term"


def test_an_export_from_a_restricted_share_keeps_the_element_and_never_writes_the_mask(
        instance, tmp_path):
    """Release blocker O1, live: a recipient below full detail level exports what was shared."""
    user, password = (os.environ.get(k) for k in
                      ("CHEMOTION_SECOND_USER", "CHEMOTION_SECOND_PASSWORD"))
    if not (user and password):
        pytest.skip("CHEMOTION_SECOND_USER/PASSWORD not set (the seed's second user)")
    from chemotion_api import Instance
    from chemotion_api.collection import CollectionSharePermission

    col, label = _fresh_collection(instance, "CRATE_restricted")
    sample = col.new_sample_smiles("CCO")
    sample.properties["description"] = "confidential note"
    sample.properties["target_amount"]["value"] = 1.25
    sample.save()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        recipient = Instance(os.environ["CHEMOTION_URL"]).test_connection().login(user, password)
    col.share(CollectionSharePermission.ReadElements, recipient.get_user(), detail_level=0)
    shared = {c.label: c for c in recipient.get_root_collection().sync_root.children}[label]
    out = export(recipient, shared, tmp_path / "restricted.eln", authors=AUTHORS,
                 ontology_lookup=False)
    metadata, text = _metadata(out)
    assert '"***"' not in text and "confidential note" not in text, "the mask is never a value"
    with read(out) as crate:
        (element,) = [e for e in crate.elements if e.kind == "sample"]
        assert element.source_id == sample.id, "the restricted element is kept"
    withheld = [n for n in metadata["@graph"] if n.get("propertyID") == "chemotion:withheld"]
    assert withheld and "description" in withheld[0]["value"]
    manifest = next(n for n in metadata["@graph"] if n["@id"] == "#export-manifest")
    assert "shared below full detail level" in manifest["description"]


def test_cell_lines_screens_sbmm_samples_and_vessels_are_exported(instance, tmp_path):
    col, _ = _fresh_collection(instance, "CRATE_kinds")
    line = col.new_cell_line("HeLa", source="ATCC")
    line.properties["amount"] = 1000000
    line.properties["unit"] = "cells"
    line.properties["passage"] = 3
    line.save()
    wellplate = col.new_wellplate()
    wellplate.save()
    screen = col.new_screen()
    screen.properties["name"] = "Kinase panel"
    screen.properties["collaborator"] = "Lab B"
    screen.wellplate_ids.append(wellplate.id)
    screen.save()
    try:
        sbmm = col.new_sbmm_sample(uniprot="P69905")  # the ELN asks UniProt for the protein
        sbmm.save()
    except Exception as exc:  # no internet on the test stack: the other two kinds still count
        sbmm = None
        warnings.warn(f"SBMM sample not created ({exc}); it is not checked", stacklevel=2)

    import uuid
    template = instance.new_vessel_template(f"CRATE flask {uuid.uuid4().hex[:6]}", vessel_type="flask",
                                            material_type="glass", volume_amount=100,
                                            volume_unit="ml").save()
    vessel = col.new_vessel(template, name="Flask A", weight_amount=52.1, weight_unit="g")

    out = export(instance, col, tmp_path / "kinds.eln", authors=AUTHORS, ontology_lookup=False)
    metadata, text = _metadata(out)
    assert "This crate is incomplete" not in text, "no kind is declared as not retrieved"
    with read(out) as crate:
        kinds = {e.kind: e for e in crate.elements}
        assert kinds["cell_line"].source_id == line.id
        assert "HeLa" in text and "cells" in text
        screen_node = kinds["screen"].node
        mentioned = screen_node["mentions"] if isinstance(screen_node["mentions"], list) \
            else [screen_node["mentions"]]
        assert {"@id": kinds["wellplate"].id} in mentioned, "the screen mentions its wellplate"
        (vessel_element,) = [e for e in crate.elements if e.kind == "vessel"]
        assert vessel_element.name == "Flask A" and vessel_element.source_id == vessel.id
        assert "flask" in text and "52.1" in text, "the vessel and its template reach the crate"
        if sbmm is not None:
            assert kinds["sequence_based_macromolecule_sample"].source_id == sbmm.id
            assert "P69905" in text


def test_derived_amounts_follow_the_eln(instance):
    """A liquid by volume without a density has no mass; feedstock and gas amounts use the ELN's
    formulas (R = 0.0821, vessel volume in litres). chemotion-api 401.1.1 got all three wrong."""
    from chemotion_api.elements.utils import GasType

    from chemotion_crate.extract import extract

    R, T, VESSEL_L, PPM = 0.0821, 294, 0.2, 200
    col, _ = _fresh_collection(instance, "CRATE_amounts")
    liquid = col.new_sample_smiles("CCCCCCO")
    liquid.properties["target_amount"] = {"value": 0.005, "unit": "l"}  # density and molarity unset
    liquid.save()
    reaction = col.new_reaction()
    reaction.reactants.append(col.new_sample_smiles("CC(C)O"))
    reaction.starting_materials.append(col.new_sample_smiles("[H][H]"))
    reaction.products.append(col.new_sample_smiles("C"))
    reaction.save()
    reaction.gas_schema = True
    reaction.properties["vessel_size"] = {"amount": VESSEL_L * 1000, "unit": "ml"}
    reaction.reactants[0].set_ref(True)
    reaction.reactants[0].set_amount(1, "g")
    reaction.starting_materials[0].gas_type = GasType.FEEDSTOCK
    reaction.products[0].gas_type = GasType.GAS
    reaction.starting_materials[0].set_amount(0.1, "mol")
    gas_phase = reaction.products[0].properties.get("gas_phase_data")
    gas_phase.ppm, gas_phase.temperature_in_k = PPM, T
    reaction.save()

    model = extract(instance, col)
    assert not model.skipped
    (liquid_rec,) = [e for e in model.elements if e.kind == "sample" and e.id == liquid.id]
    assert (liquid_rec.mass_g, liquid_rec.volume_l) == (None, 0.005)
    (rx,) = [e for e in model.elements if e.kind == "reaction"]
    (feedstock,) = rx.materials["starting_materials"]
    assert feedstock.amount_mol == pytest.approx(0.1)
    assert feedstock.volume_l == pytest.approx(0.1 * R * T)  # V = n·R·T/p, p = 1 atm
    (gas,) = rx.materials["products"]
    moles = PPM * VESSEL_L / (R * T * 1e6)
    assert gas.amount_mol == pytest.approx(moles)
    assert gas.volume_l == pytest.approx(moles * R * T)


def _login():
    from chemotion_api import Instance
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return Instance(os.environ["CHEMOTION_URL"]).test_connection().login(
            os.environ["CHEMOTION_USER"], os.environ["CHEMOTION_PASSWORD"])


def test_select_multi_and_datetime_range_values_reach_the_crate(instance, tmp_path):
    """select-multi and datetime-range values (read from the raw fields) and the template
    release the element and its segment were saved with."""
    import uuid

    from chemotion_api.labImotion.items.options import FieldType, SaveActionType

    from chemotion_crate.extract import extract

    suffix = uuid.uuid4().hex[:6]
    name = "crt" + "".join(chr(97 + int(c, 16)) for c in suffix[:4])  # letters only
    options = {"solv": {"options": [{"key": "h2o", "label": "Water"},
                                    {"key": "etoh", "label": "Ethanol"}]}}
    gm = instance.generic_manager()
    klass = gm.create_new_element(f"CRATE multi {suffix}", "bug", name=name)
    klass.properties.select_options = options
    layer = klass.properties.add_new_layer("Run")
    layer.add_field(FieldType.SELECT_MULTI, "Solvents").option_layers = "solv"
    layer.add_field(FieldType.DATETIME_RANGE, "Time")
    klass.save(SaveActionType.MINOR)
    gm.create_new_segment(f"CRATE seg {suffix}", "", gm.get_element(name))
    segment = gm.get_segment(f"CRATE seg {suffix}")
    segment.properties.select_options = options
    seg_layer = segment.properties.add_new_layer("Work")
    seg_layer.add_field(FieldType.SELECT_MULTI, "Solvents").option_layers = "solv"
    seg_layer.add_field(FieldType.DATETIME_RANGE, "Time")
    seg_layer.add_field(FieldType.DATETIME_RANGE, "Empty")
    segment.save(SaveActionType.MINOR)
    segment.is_active = True

    fresh = _login()  # knows the new element class and segment
    col, _ = _fresh_collection(fresh, "CRATE_generic")
    elem = col.new_generic(name)
    elem.name = "multi"
    elem.save()

    # Set through the client: since chemotion-api 402.0.0 it reads and writes both field types,
    # so an edit of the raw JSON would be overwritten on save.
    elem.properties["Run"]["Solvents"] = ["h2o", "etoh"]
    elem.properties["Run"]["Time"] = {"timeStart": "24/11/2026 10:00:00",
                                      "timeStop": "24/11/2026 12:30:00",
                                      "duration": {"value": "2.5", "unit": "h"}}
    work = elem.segments.get(f"CRATE seg {suffix}")["Work"]
    work["Solvents"] = ["etoh"]
    work["Time"] = {"timeStop": "24/11/2026 12:30:00", "duration": {"value": "90", "unit": "min"}}
    elem.save()

    model = extract(_login(), col)
    assert not model.skipped
    (rec,) = [e for e in model.elements if e.kind == name]
    values = {pv["name"]: pv for pv in rec.property_values}
    seg = f"CRATE seg {suffix}"
    assert values["Run.Solvents"]["value"] == "Water, Ethanol"
    assert values["Run.Time.timeStart"]["value"] == "24/11/2026 10:00:00"
    assert values["Run.Time.timeStop"]["value"] == "24/11/2026 12:30:00"
    assert values["Run.Time.duration"] == {"name": "Run.Time.duration", "value": "2.5", "unitText": "h"}
    assert values[f"{seg} / Work / Solvents"]["value"] == "Ethanol"
    assert values[f"{seg} / Work / Time.duration"]["unitText"] == "min"
    assert f"{seg} / Work / Time.timeStart" not in values and not any("Empty" in n for n in values)
    assert not any("durationCalc" in n for n in values)
    assert values["template label"]["value"] == f"CRATE multi {suffix}"
    assert values["template version"]["value"] == gm.get_element(name).version
    assert values["template uuid"]["value"] == gm.get_element(name).uuid
    assert values[f"{seg} / template uuid"]["value"] == gm.get_segment(seg).uuid

    out = export(fresh, col, tmp_path / "generic.eln", authors=AUTHORS, ontology_lookup=False)
    _, text = _metadata(out)
    assert "Water, Ethanol" in text and "24/11/2026 10:00:00" in text
