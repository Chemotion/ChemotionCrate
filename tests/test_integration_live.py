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
