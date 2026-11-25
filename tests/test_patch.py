"""Patching ontology IRIs into an exported crate (chemotion_crate.patch). Offline and pure."""
from __future__ import annotations

import json
import os
import stat
import zipfile

import pytest

from chemotion_crate.ontology_map import ELEMENT_TYPE_PROPERTY_ID, ontology_placeholder
from chemotion_crate.patch import patch_eln_ontology, patch_metadata_dict

IRI = "http://purl.obolibrary.org/obo/OBI_0400081"
WELLPLATE = "./wellplates/W_1/"


# --- patch_metadata_dict ---------------------------------------------------------

def test_patch_metadata_injects_iri_and_additional_type():
    meta = {"@graph": [
        {"@id": "./", "@type": "Dataset", "name": "root"},
        {"@id": WELLPLATE, "@type": "Dataset", "name": "W-1",
         "variableMeasured": [ontology_placeholder("wellplate")]},
    ]}
    assert patch_metadata_dict(meta, {"wellplate": IRI}) == 1
    wp = meta["@graph"][1]
    assert wp["variableMeasured"][0]["identifier"] == IRI
    assert wp["@type"] == ["Dataset", IRI]  # structural type kept, ontology type added


def test_patch_metadata_is_idempotent_on_type():
    meta = {"@graph": [{"@id": WELLPLATE, "@type": ["Dataset", IRI],
                        "variableMeasured": [ontology_placeholder("wellplate")]}]}
    assert patch_metadata_dict(meta, {"wellplate": IRI}) == 1
    assert meta["@graph"][0]["@type"] == ["Dataset", IRI]


def test_patch_metadata_skips_unmapped_kinds():
    meta = {"@graph": [{"@id": "./research_plans/RP_1/", "@type": "Dataset",
                        "variableMeasured": [ontology_placeholder("research_plan")]}]}
    assert patch_metadata_dict(meta, {"wellplate": "http://x"}) == 0
    assert meta["@graph"][0]["@type"] == "Dataset"  # untouched


def test_patch_metadata_ignores_an_empty_iri():
    meta = {"@graph": [{"@id": WELLPLATE, "@type": "Dataset",
                        "variableMeasured": [ontology_placeholder("wellplate")]}]}
    assert patch_metadata_dict(meta, {"wellplate": ""}) == 0
    assert "identifier" not in meta["@graph"][0]["variableMeasured"][0]


def test_patch_metadata_ignores_non_placeholder_nodes():
    meta = {"@graph": [{"@id": "./samples/S_1/", "@type": "Dataset",
                        "variableMeasured": [{"@type": "PropertyValue",
                                              "propertyID": "other", "value": "x"}]}]}
    assert patch_metadata_dict(meta, {"x": "http://x"}) == 0


def test_patch_metadata_resolves_flattened_references():
    """RO-Crate wants a flattened graph: `variableMeasured` may hold `{"@id": …}` references.
    Without dereferencing them nothing has a `propertyID` and the patch silently does nothing."""
    pv = {"@id": "#pv-wp", **ontology_placeholder("wellplate")}
    meta = {"@graph": [{"@id": WELLPLATE, "@type": "Dataset",
                        "variableMeasured": [{"@id": "#pv-wp"}]}, pv]}
    assert patch_metadata_dict(meta, {"wellplate": IRI}) == 1
    assert pv["identifier"] == IRI
    assert meta["@graph"][0]["@type"] == ["Dataset", IRI]


def test_patch_metadata_accepts_a_single_variable_measured_object():
    meta = {"@graph": [{"@id": WELLPLATE, "@type": "Dataset",
                        "variableMeasured": ontology_placeholder("wellplate")}]}
    assert patch_metadata_dict(meta, {"wellplate": IRI}) == 1


def test_patch_metadata_tolerates_a_non_object_graph_member():
    metadata = {"@graph": ["a bare string", None,
                           {"@id": "./s/", "variableMeasured": [
                               {"@type": "PropertyValue",
                                "propertyID": ELEMENT_TYPE_PROPERTY_ID, "value": "sample"}]}]}
    assert patch_metadata_dict(
        metadata, {"sample": "http://purl.obolibrary.org/obo/CHMO_0002773"}) == 1


def test_patch_metadata_tolerates_a_non_list_graph(caplog):
    with caplog.at_level("WARNING"):
        assert patch_metadata_dict({"@graph": "nonsense"}, {"sample": "x"}) == 0
    assert "not a list" in caplog.text


def test_patch_metadata_ignores_a_non_string_kind():
    meta = {"@graph": [{"@id": WELLPLATE, "variableMeasured": [
        {"propertyID": ELEMENT_TYPE_PROPERTY_ID, "value": ["wellplate"]}]}]}
    assert patch_metadata_dict(meta, {"wellplate": IRI}) == 0


# --- patch_eln_ontology (zip round-trip) -----------------------------------------

def _demo_eln(tmp_path):
    eln = tmp_path / "demo.eln"
    meta = {"@context": "https://w3id.org/ro/crate/1.2/context", "@graph": [
        {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"}},
        {"@id": "./", "@type": "Dataset", "name": "root"},
        {"@id": WELLPLATE, "@type": "Dataset", "name": "W-1",
         "variableMeasured": [ontology_placeholder("wellplate")]},
    ]}
    with zipfile.ZipFile(eln, "w") as z:
        z.writestr("demo/ro-crate-metadata.json", json.dumps(meta))
        z.writestr("demo/wellplates/W_1/data.txt", b"hi")
    return eln


def _patched_wellplate(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        patched = json.loads(z.read("demo/ro-crate-metadata.json"))
        payload = z.read("demo/wellplates/W_1/data.txt")
    wp = next(n for n in patched["@graph"] if n["@id"] == WELLPLATE)
    return names, wp, payload


def test_patch_eln_roundtrip_preserves_other_entries(tmp_path):
    eln = _demo_eln(tmp_path)
    original = eln.read_bytes()
    out = tmp_path / "patched.eln"
    assert patch_eln_ontology(str(eln), {"wellplate": IRI}, output_path=str(out)) == 1

    names, wp, payload = _patched_wellplate(out)
    assert IRI in wp["@type"] and wp["variableMeasured"][0]["identifier"] == IRI
    assert names == ["demo/ro-crate-metadata.json", "demo/wellplates/W_1/data.txt"]
    assert payload == b"hi"
    assert eln.read_bytes() == original        # input untouched when output_path is given
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demo.eln", "patched.eln"]


def test_patch_eln_in_place(tmp_path):
    eln = _demo_eln(tmp_path)
    os.chmod(eln, 0o644)
    assert patch_eln_ontology(eln, {"wellplate": IRI}) == 1
    _, wp, payload = _patched_wellplate(eln)
    assert IRI in wp["@type"] and payload == b"hi"
    # no temp file left behind, and the input's permissions survive the replace
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demo.eln"]
    assert stat.S_IMODE(eln.stat().st_mode) == 0o644


def test_patch_eln_raises_without_metadata(tmp_path):
    eln = tmp_path / "bad.eln"
    with zipfile.ZipFile(eln, "w") as z:
        z.writestr("demo/data.txt", b"x")
    with pytest.raises(ValueError, match="ro-crate-metadata.json"):
        patch_eln_ontology(str(eln), {"wellplate": "http://x"},
                           output_path=str(tmp_path / "o.eln"))
    assert not (tmp_path / "o.eln").exists()


def test_patch_eln_refuses_non_object_metadata(tmp_path):
    eln = tmp_path / "list.eln"
    with zipfile.ZipFile(eln, "w") as z:
        z.writestr("demo/ro-crate-metadata.json", "[]")
    with pytest.raises(ValueError, match="not an object"):
        patch_eln_ontology(eln, {"wellplate": IRI}, output_path=tmp_path / "o.eln")


def test_patch_eln_patches_the_shallowest_metadata(tmp_path):
    """A nested decoy descriptor must not be the one rewritten, whatever the namelist order."""
    eln = tmp_path / "decoy.eln"
    real = {"@graph": [{"@id": WELLPLATE, "@type": "Dataset",
                        "variableMeasured": [ontology_placeholder("wellplate")]}]}
    with zipfile.ZipFile(eln, "w") as z:
        z.writestr("demo/nested/ro-crate-metadata.json", json.dumps({"@graph": []}))
        z.writestr("demo/evil-ro-crate-metadata.json", json.dumps({"@graph": []}))
        z.writestr("demo/ro-crate-metadata.json", json.dumps(real))
    out = tmp_path / "out.eln"
    assert patch_eln_ontology(eln, {"wellplate": IRI}, output_path=out) == 1
    with zipfile.ZipFile(out) as z:
        assert json.loads(z.read("demo/nested/ro-crate-metadata.json")) == {"@graph": []}
        assert IRI in json.loads(z.read("demo/ro-crate-metadata.json"))["@graph"][0]["@type"]


def test_patch_types_like_the_exporter_and_falls_back_to_generic(tmp_path):
    from chemotion_crate import Author, AuthorAffiliation, export_model
    from chemotion_crate.model import ExportModel
    from chemotion_crate.patch import patch_metadata_dict
    authors = [Author(name="A", orcid="0000-0002-1825-0097", email="no-reply@eln.invalid",
                      affiliations=[AuthorAffiliation(name="KIT", ror="04t3en479")])]
    out = export_model(ExportModel.load("tests/fixtures/mini_model.json"), tmp_path / "p.eln",
                       authors=authors, ontology_lookup=False)
    with zipfile.ZipFile(out) as z:
        meta = json.loads(z.read(next(n for n in z.namelist() if n.endswith("/ro-crate-metadata.json"))))
    patched = patch_metadata_dict(meta, {"wellplate": "http://purl.obolibrary.org/obo/OBI_0000192",
                                         "generic": "http://purl.obolibrary.org/obo/IAO_0000030"})
    graph = {n["@id"]: n for n in meta["@graph"]}
    wellplate = next(n for n in graph.values() if n.get("genre") == "wellplate")
    generic = next(n for n in graph.values() if n.get("genre") == "lab_notebook_page")
    assert patched == 2
    assert "OBI:0000192" in wellplate["@type"] and wellplate["@type"] == sorted(wellplate["@type"])
    assert "IAO:0000030" in generic["@type"], "a LabIMotion type falls back to the generic term"
    assert graph["OBI:0000192"]["@type"] == "DefinedTerm"
    root_mentions = [m["@id"] for m in graph["./"]["mentions"]]
    assert "OBI:0000192" in root_mentions and "IAO:0000030" in root_mentions


def _bare_crate(*extra: dict) -> dict:
    return {"@context": "https://w3id.org/ro/crate/1.2/context", "@graph": [
        {"@id": "./", "@type": "Dataset", "hasPart": {"@id": "#wellplates/W_1"}},
        {"@id": "#wellplates/W_1", "@type": "Dataset", "name": "W",
         "variableMeasured": [ontology_placeholder("wellplate")]},
        *extra]}


def test_patch_mentions_a_single_term_as_one_reference_like_the_exporter():
    meta = _bare_crate()
    iri = "http://purl.obolibrary.org/obo/OBI_0000192"
    assert patch_metadata_dict(meta, {"wellplate": iri}) == 1
    root = next(n for n in meta["@graph"] if n["@id"] == "./")
    assert root["mentions"] == {"@id": iri}, "a single value is not wrapped in a list"


def test_patch_does_not_add_a_node_whose_id_is_already_in_the_graph():
    iri = "http://purl.obolibrary.org/obo/OBI_0000192"
    meta = _bare_crate({"@id": iri, "@type": ["DefinedTerm"], "name": "wellplate"})
    patch_metadata_dict(meta, {"wellplate": iri})
    assert [n["@id"] for n in meta["@graph"]].count(iri) == 1
