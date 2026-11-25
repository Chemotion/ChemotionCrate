"""The exported crate's structure, against TheELNFileFormat's checker and the layout in ``crate.py``.

Rules mirrored from TheELNFileFormat ``tests/checks/param_metadata_json.py`` (suggested keys,
no empty values, keywords as a string, contentSize as digits, author as references, sdPublisher
to an Organization) and ``tests/checks/versions.py`` (the ``purl.archive.org`` profile on the
root's ``conformsTo``). Everything runs on the two session crates from ``conftest``; tests that
need a different export write their own into ``tmp_path``.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

import pytest

from chemotion_crate import Author, AuthorAffiliation, export_model
from chemotion_crate.graph import ELN_SPEC_PROFILE, RO_CRATE_PROFILE, base_context
from chemotion_crate.manifest import MANIFEST_ID
from chemotion_crate.ontology_map import ELEMENT_TYPE_PROPERTY_ID
from chemotion_crate.producer import WITHHELD_HOST

from .conftest import (
    FIXTURE_AUTHORS,
    FIXTURE_ORCID,
    FIXTURE_ROR,
    REAL_HOST,
    ExportedCrate,
    export_fixture,
    load_fixture_model,
    open_crate,
)

SAMPLE_1 = "#samples/MT-1_1"
SAMPLE_2 = "#samples/MT-2_2"
SHARED_SAMPLE = "./samples/MT-3_3/"
REACTION = "./reactions/MT-R1_5/"
WELLPLATE = "#wellplates/MT-WP1_8"
RESEARCH_PLAN = "#research_plans/Plan_9"
DEVICE = "#device_descriptions/MT-D1_10"
GENERIC = "#generics/lab_notebook_page/Page_3_3"
ELEMENTS = [SAMPLE_1, SAMPLE_2, SHARED_SAMPLE, REACTION, WELLPLATE, RESEARCH_PLAN, DEVICE, GENERIC]
NOTES = f"{SHARED_SAMPLE}notes.txt"
SPECTRUM = f"{REACTION}product_1H.jdx"
FILES = {NOTES: "att-notes-uuid", SPECTRUM: "att-1-uuid"}
COLLECTION_1 = "#collection-1"
COLLECTION_2 = "#collection-2"

#: Keys whose values legitimately change from one export to the next.
VOLATILE = {"ro-crate-metadata.json": {"dateCreated"}, "#ro-crate_created": {"endTime"},
            "./": {"datePublished"}}


# -- helpers --------------------------------------------------------------------------------

def as_list(value: Any) -> list:
    return value if isinstance(value, list) else [value]


def ref_ids(value: Any) -> list[str]:
    return [item["@id"] for item in as_list(value)]


def types(node: dict) -> list[str]:
    return as_list(node.get("@type", []))


def walk(value: Any, path: tuple = ()):
    """Yield ``(path, key, value)`` for every dict entry and list item, recursively."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield path, key, item
            yield from walk(item, (*path, key))
    elif isinstance(value, list):
        for item in value:
            yield path, None, item
            yield from walk(item, path)


def is_absolute_iri(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme) and bool(parsed.netloc)


def property_values(crate: ExportedCrate, element_id: str) -> list[dict]:
    """The element's ``variableMeasured`` entries, references resolved."""
    out = []
    for entry in as_list(crate.node(element_id).get("variableMeasured", [])):
        if set(entry) == {"@id"}:
            entry = crate.node(entry["@id"])
        out.append(entry)
    return out


def pv_by_property(crate: ExportedCrate, element_id: str) -> dict[str, dict]:
    return {pv["propertyID"]: pv for pv in property_values(crate, element_id) if "propertyID" in pv}


def pv_by_name(crate: ExportedCrate, element_id: str) -> dict[str, Any]:
    return {pv["name"]: pv["value"] for pv in property_values(crate, element_id)}


# -- the root Dataset -----------------------------------------------------------------------

def test_root_dataset_carries_the_required_properties(any_crate: ExportedCrate):
    root = any_crate.node("./")
    assert types(root) == ["Dataset"]
    assert root["name"] == "Mini Test Collection"
    assert root["description"] == "Fixture root"
    date.fromisoformat(root["datePublished"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", root["datePublished"])
    license_ids = ref_ids(root["license"])
    assert license_ids and all(i in any_crate.nodes for i in license_ids)
    # TheELNFileFormat's schema requires a plain string, not a PropertyValue.
    assert root["identifier"] == "1"


def test_root_declares_ro_crate_and_eln_profiles(any_crate: ExportedCrate):
    root = any_crate.node("./")
    declared = ref_ids(root["conformsTo"])
    assert "https://w3id.org/ro/crate/1.2" in declared
    assert "https://purl.archive.org/purl/elnconsortium/eln-spec/1.2+20260923" in declared
    assert set(declared) == {RO_CRATE_PROFILE, ELN_SPEC_PROFILE}
    for profile in declared:
        assert "Profile" in types(any_crate.node(profile))


def test_root_authors_are_persons_with_orcid_and_publisher_is_a_ror_organization(
        any_crate: ExportedCrate):
    root = any_crate.node("./")
    authors = any_crate.deref(root["author"])
    assert [a["@id"] for a in authors] == [FIXTURE_ORCID]
    for person in authors:
        assert "Person" in types(person)
        assert person["@id"].startswith("https://orcid.org/")
        affiliations = any_crate.deref(person["affiliation"])
        assert all("Organization" in types(org) for org in affiliations)
    publisher = any_crate.node(root["publisher"]["@id"])
    assert "Organization" in types(publisher)
    assert publisher["@id"] == FIXTURE_ROR
    assert publisher["@id"].startswith("https://ror.org/")
    assert publisher["name"] == "Example University"


def test_root_has_part_lists_every_dataset_and_file(any_crate: ExportedCrate):
    root = any_crate.node("./")
    expected = {i for i, n in any_crate.nodes.items()
                if i not in ("./", "ro-crate-metadata.json")
                and ({"Dataset", "File"} & set(types(n)))}
    listed = ref_ids(root["hasPart"])
    assert len(listed) == len(set(listed)), "hasPart lists something twice"
    assert set(listed) == expected
    assert set(ELEMENTS) | set(FILES) <= set(listed)


def test_root_mentions_the_collections(any_crate: ExportedCrate):
    mentioned = ref_ids(any_crate.node("./")["mentions"])
    assert {COLLECTION_1, COLLECTION_2} <= set(mentioned)
    for col_id in (COLLECTION_1, COLLECTION_2):
        assert types(any_crate.node(col_id)) == ["Collection"]


def test_metadata_descriptor(any_crate: ExportedCrate):
    descriptor = any_crate.node("ro-crate-metadata.json")
    assert descriptor["about"] == {"@id": "./"}
    assert ref_ids(descriptor["conformsTo"]) == [RO_CRATE_PROFILE]
    datetime.fromisoformat(descriptor["dateCreated"])
    assert descriptor["version"]
    publisher = any_crate.node(descriptor["sdPublisher"]["@id"])
    assert "Organization" in types(publisher)


# -- element Datasets -----------------------------------------------------------------------

def test_every_element_is_a_dataset_with_the_suggested_properties(any_crate: ExportedCrate):
    model = load_fixture_model()
    by_path = {}
    for elem in model.elements:
        plural = {"sample": "samples", "reaction": "reactions", "wellplate": "wellplates",
                  "research_plan": "research_plans",
                  "device_description": "device_descriptions"}.get(elem.kind)
        folder = f"{plural}/" if plural else f"generics/{elem.kind}/"
        by_path[next(p for p in ELEMENTS
                     if p.removeprefix("#").removeprefix("./").startswith(folder)
                     and p.rstrip("/").endswith(f"_{elem.id}"))] = elem
    assert set(by_path) == set(ELEMENTS)

    for path, elem in by_path.items():
        node = any_crate.node(path)
        assert "Dataset" in types(node), path
        assert ref_ids(node["author"]) == [FIXTURE_ORCID], path
        assert node["identifier"] == (elem.short_label or str(elem.id)), path
        assert node["genre"] == elem.kind, path
        assert node["name"], path
        if elem.created_at:
            assert node["dateCreated"] == elem.created_at, path
        else:
            assert "dateCreated" not in node, path
        if elem.updated_at:
            assert node["dateModified"] == elem.updated_at, path
        else:
            assert "dateModified" not in node, path
        expected_text = elem.observation or elem.description
        if expected_text:
            assert node["text"] == expected_text, path
        if "keywords" in node:
            assert isinstance(node["keywords"], str), path


def test_keywords_are_one_comma_separated_string(any_crate: ExportedCrate):
    keywords = any_crate.node(SHARED_SAMPLE)["keywords"]
    assert isinstance(keywords, str)
    # A comma inside a label would split it on reading, so it arrives as a semicolon.
    assert [k.strip() for k in keywords.split(",")] == ["fixture", "shared; comma"]
    for node in any_crate.nodes.values():
        assert not isinstance(node.get("keywords"), list), node["@id"]


def test_sample_nodes_carry_the_substance_and_dataset_types(any_crate: ExportedCrate):
    for sample in (SAMPLE_1, SAMPLE_2, SHARED_SAMPLE):
        node_types = types(any_crate.node(sample))
        assert "CHEBI:59999" in node_types, sample
        assert "Dataset" in node_types, sample
    assert "DefinedTerm" in types(any_crate.node("CHEBI:59999"))


def test_reaction_types_and_measurement_techniques(any_crate: ExportedCrate):
    node = any_crate.node(REACTION)
    assert "RXNO:0000021" in types(node)
    assert not any(t.startswith("http") for t in types(node)), "types must be compact"
    techniques = ref_ids(node["measurementTechnique"])
    assert set(techniques) == {"CHMO:0000595", "CHMO:0000593"}
    for term in [*techniques, "RXNO:0000021"]:
        assert types(any_crate.node(term)) == ["DefinedTerm"]
        assert any_crate.node(term)["name"]


def test_device_carries_its_ontology_term_as_a_type(any_crate: ExportedCrate):
    assert "CHMO:0000613" in types(any_crate.node(DEVICE))
    assert "Dataset" in types(any_crate.node(DEVICE))
    assert any_crate.node("CHMO:0000613")["name"] == "NMR instrument"


@pytest.mark.parametrize("element_id, kind, values", [
    (WELLPLATE, "wellplate", {"well A1": "MT-1"}),
    (RESEARCH_PLAN, "research_plan", {}),
    (DEVICE, "device_description", {"vendor company name": "Bruker", "operation mode": "manual"}),
    (GENERIC, "lab_notebook_page", {"layer.temperature": 25}),
])
def test_unmapped_kinds_carry_the_ontology_placeholder_and_their_values(
        any_crate: ExportedCrate, element_id, kind, values):
    raw_entries = as_list(any_crate.node(element_id)["variableMeasured"])
    # Flattened: the placeholder is a reference to a node, not an embedded object.
    assert all(set(entry) == {"@id"} for entry in raw_entries)
    placeholder = pv_by_property(any_crate, element_id)[ELEMENT_TYPE_PROPERTY_ID]
    assert types(placeholder) == ["PropertyValue"]
    assert placeholder["value"] == kind
    named = pv_by_name(any_crate, element_id)
    for name, value in values.items():
        assert named[name] == value
    if element_id == GENERIC:
        unit = next(pv for pv in property_values(any_crate, element_id)
                    if pv["name"] == "layer.temperature")
        assert unit["unitText"] == "°C"


def test_reaction_variations_travel_as_a_json_string_and_purification_as_a_list(
        any_crate: ExportedCrate):
    by_property = pv_by_property(any_crate, REACTION)
    variations = by_property["chemotion:variations"]
    assert types(variations) == ["PropertyValue"]
    assert isinstance(variations["value"], str)
    assert json.loads(variations["value"]) == [{"id": 1, "temperature": {"value": 25, "unit": "°C"}}]
    assert by_property["chemotion:purification"]["value"] == ["Extraction", "Filtration"]


# -- File entities ----------------------------------------------------------------------------

@pytest.mark.parametrize("file_id", sorted(FILES))
def test_file_nodes_describe_the_payload(any_crate: ExportedCrate, file_id):
    node = any_crate.node(file_id)
    payload = any_crate.payload(file_id)
    assert types(node) == ["File"]
    assert node["name"] == file_id.rsplit("/", 1)[-1]
    assert node["description"]
    assert node["encodingFormat"]
    assert isinstance(node["contentSize"], str) and re.fullmatch(r"\d+", node["contentSize"])
    # The writer restates the size from the bytes; the fixture's recorded filesize is stale.
    assert int(node["contentSize"]) == len(payload)
    assert node["sha256"] == hashlib.sha256(payload).hexdigest()
    assert node["identifier"] == FILES[file_id]


def test_files_are_listed_by_their_element_and_the_root(any_crate: ExportedCrate):
    root_parts = set(ref_ids(any_crate.node("./")["hasPart"]))
    assert NOTES in ref_ids(any_crate.node(SHARED_SAMPLE)["hasPart"])
    assert SPECTRUM in ref_ids(any_crate.node(REACTION)["hasPart"])
    assert {NOTES, SPECTRUM} <= root_parts
    assert any_crate.node(NOTES)["encodingFormat"] == "text/plain"
    assert any_crate.node(SPECTRUM)["encodingFormat"] == "chemical/x-jcamp-dx"


ANALYSIS = "#reactions-MT-R1_5-analysis-31"


def test_spectrum_is_listed_by_its_analysis(any_crate: ExportedCrate):
    analysis = any_crate.node(ANALYSIS)
    assert analysis["@type"] == "Dataset"
    assert analysis["name"] == "1H NMR of product"
    assert analysis["about"] == {"@id": REACTION}
    assert analysis["isPartOf"] == {"@id": REACTION}
    assert SPECTRUM in ref_ids(analysis["hasPart"])
    assert "CHMO:0000595" in ref_ids(analysis["measurementTechnique"])
    assert ANALYSIS in ref_ids(any_crate.node(REACTION)["mentions"])


def test_analysis_is_a_local_dataset_not_a_folder(any_crate: ExportedCrate):
    # A Dataset with a path-like id is a payload folder to RO-Crate; an analysis is not one.
    analysis = any_crate.node(ANALYSIS)
    assert analysis["@id"].startswith("#")
    assert "dct:subject" not in analysis  # RO-Crate requires schema.org ``about`` instead
    assert ANALYSIS not in ref_ids(any_crate.node(REACTION)["hasPart"])


# -- graph-wide invariants --------------------------------------------------------------------

def test_no_empty_values_anywhere(any_crate: ExportedCrate):
    for node in any_crate.metadata["@graph"]:
        for key, value in node.items():
            # The .eln checker's own test: str(value).strip() must not be empty.
            assert str(value).strip(), f"{node['@id']}: {key} is empty"
        for _, key, value in walk(node):
            assert value not in ("", [], {}, None), f"{node['@id']}: empty value under {key}"
            if isinstance(value, str):
                assert value.strip(), f"{node['@id']}: blank string under {key}"


def test_graph_nodes_are_well_formed_and_unique(any_crate: ExportedCrate):
    graph = any_crate.metadata["@graph"]
    ids = [n["@id"] for n in graph]
    assert len(ids) == len(set(ids)), "duplicate @id"
    for node in graph:
        assert isinstance(node["@id"], str)
        node_type = node["@type"]
        assert isinstance(node_type, str) or (
            isinstance(node_type, list) and node_type and all(isinstance(t, str) for t in node_type))
        if "hasPart" in node:
            assert isinstance(node["hasPart"], list), f"{node['@id']}: hasPart must be a list"


def test_every_key_is_defined_by_the_context(any_crate: ExportedCrate):
    base = base_context()
    extra = any_crate.metadata["@context"][1]
    assert any_crate.metadata["@context"][0] == "https://w3id.org/ro/crate/1.2/context"
    prefixes = {k for k, v in {**base, **extra}.items()
                if (isinstance(v, str) and v.endswith(("/", "#", ":", "_")))
                or (isinstance(v, dict) and v.get("@prefix"))}
    for node in any_crate.metadata["@graph"]:
        for _, key, _value in walk(node):
            if key is None or key in ("@id", "@type"):
                continue
            if ":" in key:
                assert key in extra or key.split(":", 1)[0] in prefixes, \
                    f"{node['@id']}: {key} uses an undeclared prefix"
            else:
                assert key in base or key in extra, f"{node['@id']}: bare key {key!r} undefined"
        for node_type in types(node):
            if ":" not in node_type:
                assert node_type in base or node_type in extra, \
                    f"{node['@id']}: bare type {node_type!r} undefined"


def test_every_reference_resolves(any_crate: ExportedCrate):
    for node in any_crate.metadata["@graph"]:
        for _, key, value in walk(node):
            if isinstance(value, dict) and set(value) == {"@id"}:
                target = value["@id"]
                assert target in any_crate.nodes or is_absolute_iri(target), \
                    f"{node['@id']}: {key} -> {target!r} dangles"


def test_no_blank_node_ids(any_crate: ExportedCrate):
    assert "_:" not in any_crate.members[any_crate.metadata_member].decode("utf-8")
    for node in any_crate.metadata["@graph"]:
        for _, key, value in walk(node):
            if key == "@id":
                assert not value.startswith("_:")


def test_api_urls_appear_only_in_molecule_prose(disclosed_crate: ExportedCrate):
    for node in disclosed_crate.metadata["@graph"]:
        assert "/api/v1/" not in node["@id"]
        for path, key, value in walk(node):
            if key is not None:
                assert "/api/v1/" not in key
            if isinstance(value, str) and "/api/v1/" in value:
                container = path[-1] if path else key
                assert node["@id"].startswith("#molecule_") and container == "dct:description", \
                    f"{node['@id']}: API URL under {key or container}: {value}"
    # And the prose really is where it lives in this fixture, so the exception is exercised.
    assert "/api/v1/" in json.dumps(disclosed_crate.node("#molecule_11")["dct:description"])


def test_disclosed_source_names_the_real_host(disclosed_crate: ExportedCrate):
    source = disclosed_crate.node(disclosed_crate.node("./")["isBasedOn"]["@id"])
    assert source["@id"] == REAL_HOST
    assert types(source) == ["SoftwareApplication"]
    assert source["version"] == "v4.0.1"


def test_withheld_crate_never_names_the_host(withheld_crate: ExportedCrate):
    for name, data in withheld_crate.members.items():
        assert b"eln.example.org" not in data, name
        assert "eln.example.org" not in name
    source = withheld_crate.node(withheld_crate.node("./")["isBasedOn"]["@id"])
    assert source["@id"] == WITHHELD_HOST == "https://withheld.chemotion.invalid"
    assert types(source) == ["SoftwareApplication"]
    assert source["version"] == "v4.0.1"
    assert source["url"] == WITHHELD_HOST


# -- collections ------------------------------------------------------------------------------

def test_shared_sample_is_stored_once_and_belongs_to_both_collections(any_crate: ExportedCrate):
    assert set(ref_ids(any_crate.node(SHARED_SAMPLE)["isPartOf"])) == {COLLECTION_1, COLLECTION_2}
    for col_id in (COLLECTION_1, COLLECTION_2):
        assert SHARED_SAMPLE in ref_ids(any_crate.node(col_id)["hasPart"])
    assert ref_ids(any_crate.node(COLLECTION_2)["isPartOf"]) == [COLLECTION_1]
    assert sum(1 for n in any_crate.names if n.rstrip("/").endswith("samples/MT-3_3")) == 1


def test_membership_runs_both_ways(any_crate: ExportedCrate):
    for col_id in (COLLECTION_1, COLLECTION_2):
        for member in ref_ids(any_crate.node(col_id)["hasPart"]):
            assert col_id in ref_ids(any_crate.node(member)["isPartOf"])
    for element in ELEMENTS:
        for col_id in ref_ids(any_crate.node(element)["isPartOf"]):
            assert element in ref_ids(any_crate.node(col_id)["hasPart"])


# -- the archive ------------------------------------------------------------------------------

def test_zip_has_one_root_folder_named_after_the_file(any_crate: ExportedCrate):
    roots = {name.split("/", 1)[0] for name in any_crate.names}
    assert roots == {any_crate.path.name}
    assert all("/" in name for name in any_crate.names)


def test_elements_with_files_are_folders_and_the_rest_local_ids(any_crate: ExportedCrate):
    # A folder Dataset SHOULD list its contents (RO-Crate 1.2); an element with no files has
    # nothing to list, so it gets a "#" id and no folder.
    for element in ELEMENTS:
        entry = f"{any_crate.root_folder}/{element.removeprefix('./').removeprefix('#').rstrip('/')}/"
        has_files = element in (SHARED_SAMPLE, REACTION)
        assert element.startswith("./" if has_files else "#"), element
        assert (entry in any_crate.names) == has_files, element
        if has_files:
            assert ref_ids(any_crate.node(element)["hasPart"]), element
        assert element in ref_ids(any_crate.node("./")["hasPart"]), element


def test_the_contact_point_is_made_from_the_author_email(any_crate: ExportedCrate):
    persons = [n for n in any_crate.metadata["@graph"] if "Person" in types(n)]
    assert persons
    for person in persons:
        contact = any_crate.node(person["contactPoint"]["@id"])
        assert contact["@type"] == "ContactPoint"
        assert contact["email"] == person["email"] == "no-reply@eln.invalid"


def test_metadata_is_utf8_json(any_crate: ExportedCrate):
    raw = any_crate.members[any_crate.metadata_member]
    text = raw.decode("utf-8", errors="strict")
    assert isinstance(json.loads(text), dict)
    assert "°C".encode() in raw, "non-ASCII text is written as UTF-8, not escaped"


# -- determinism ------------------------------------------------------------------------------

def _canonical(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((_canonical(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    return value


def _comparable(crate: ExportedCrate) -> dict:
    out = {}
    for node_id, node in crate.nodes.items():
        out[node_id] = _canonical({k: v for k, v in node.items()
                                   if k not in VOLATILE.get(node_id, ())})
    return out


def test_two_exports_produce_the_same_graph(disclosed_crate: ExportedCrate, tmp_path):
    again = open_crate(export_fixture(tmp_path / "disclosed.eln", disclose_host=True))
    first, second = _comparable(disclosed_crate), _comparable(again)
    assert set(first) == set(second)
    for node_id in first:
        assert first[node_id] == second[node_id], f"{node_id} differs between exports"
    assert disclosed_crate.metadata["@context"] == again.metadata["@context"]


# -- strictness, omissions and the manifest ---------------------------------------------------

def _model_with_broken_notes():
    model = load_fixture_model()
    shared = next(e for e in model.elements if e.kind == "sample" and e.id == 3)

    def broken():
        raise OSError("connection reset")
        yield b""  # pragma: no cover - makes this a generator

    shared.attachments[0].read = broken
    return model


def test_strict_export_with_a_failing_download_writes_nothing(tmp_path):
    out = tmp_path / "strict.eln"
    with pytest.raises(RuntimeError, match="notes.txt"):
        export_fixture(out, model=_model_with_broken_notes(), strict=True)
    assert not out.exists()
    assert not (tmp_path / "strict.eln.part").exists()
    assert list(tmp_path.iterdir()) == [], "staging leftovers"


def test_lenient_export_withdraws_the_file_and_declares_it(tmp_path):
    crate = open_crate(export_fixture(tmp_path / "lenient.eln", model=_model_with_broken_notes()))
    assert NOTES not in crate.nodes
    for node in crate.metadata["@graph"]:
        assert NOTES not in ref_ids(node.get("hasPart", [])), node["@id"]
    assert not any(n.endswith("notes.txt") for n in crate.names)
    assert f"{crate.root_folder}/{SHARED_SAMPLE.removeprefix('./')}" in crate.names
    assert MANIFEST_ID in crate.nodes
    assert MANIFEST_ID in ref_ids(crate.node("./")["mentions"])
    measured = {pv["propertyID"]: pv["value"] for pv in crate.deref(
        crate.node(MANIFEST_ID)["variableMeasured"])}
    assert measured["chemotion:omitted/attachment"] == "1"
    # the spectrum, unaffected, still arrives intact
    assert crate.node(SPECTRUM)["sha256"] == hashlib.sha256(crate.payload(SPECTRUM)).hexdigest()


def test_session_crate_declares_its_notes_in_the_manifest(any_crate: ExportedCrate):
    # Three fixture elements carry no timestamps: a metadata note, declared but not blocking.
    assert MANIFEST_ID in any_crate.nodes
    assert MANIFEST_ID in ref_ids(any_crate.node("./")["mentions"])


def test_declare_skipped_false_omits_the_manifest(tmp_path):
    crate = open_crate(export_fixture(tmp_path / "quiet.eln", model=_model_with_broken_notes(),
                                      declare_skipped=False))
    assert NOTES not in crate.nodes
    assert MANIFEST_ID not in crate.nodes
    assert not any(i.startswith(MANIFEST_ID) for i in crate.nodes)
    assert MANIFEST_ID not in ref_ids(crate.node("./").get("mentions", []))


@pytest.mark.parametrize("authors", [
    [],
    None,
    [Author(name="No Orcid", orcid="", affiliations=[AuthorAffiliation(name="X", ror=FIXTURE_ROR)])],
    [Author(name="Bad Orcid", orcid="0000-0002-1825-0098",
            affiliations=[AuthorAffiliation(name="X", ror=FIXTURE_ROR)])],
    [Author(name="Nowhere", orcid="0000-0002-1825-0097")],
    [Author(name="Bad Ror", orcid="0000-0002-1825-0097",
            affiliations=[AuthorAffiliation(name="X", ror="https://ror.org/zzzzzzzzz")])],
    [Author(name="No Email", orcid="0000-0002-1825-0097",
            affiliations=[AuthorAffiliation(name="X", ror=FIXTURE_ROR)])],
    [Author(name="Bad Email", orcid="0000-0002-1825-0097", email="not-an-address",
            affiliations=[AuthorAffiliation(name="X", ror=FIXTURE_ROR)])],
], ids=["empty", "none", "no-orcid", "bad-orcid-checksum", "no-affiliation", "bad-ror",
        "no-email", "bad-email"])
def test_invalid_authors_raise_before_anything_is_written(tmp_path, authors):
    out = tmp_path / "unwritten.eln"
    with pytest.raises(ValueError):
        export_model(load_fixture_model(), out, authors=authors, ontology_lookup=False)
    assert list(tmp_path.iterdir()) == []


def test_single_values_are_not_wrapped_in_arrays(any_crate: ExportedCrate):
    # RO-Crate 1.2 (appendix/jsonld.md): single-element arrays SHOULD be unpacked. hasPart and
    # comment stay arrays because TheELNFileFormat requires them (COMPATIBILITY.md K-1).
    for node in any_crate.metadata["@graph"]:
        for key, value in node.items():
            if isinstance(value, list) and len(value) == 1:
                assert key.startswith("@") or key in {"hasPart", "comment"}, f"{node['@id']}: {key}"


def test_withheld_fields_are_named_on_the_element_and_in_the_manifest(tmp_path):
    model = load_fixture_model()
    sample = next(e for e in model.elements if e.kind == "sample")
    sample.withheld = ["density", "name"]
    model.skipped.append({"kind": "sample field", "stage": "withheld", "id": sample.id, "count": 2})
    out = export_model(model, tmp_path / "w.eln", authors=FIXTURE_AUTHORS, ontology_lookup=False)
    with zipfile.ZipFile(out) as z:
        meta = json.loads(z.read(next(n for n in z.namelist()
                                      if n.endswith("/ro-crate-metadata.json"))))
    graph = {n["@id"]: n for n in meta["@graph"]}
    withheld = [n for n in graph.values() if n.get("propertyID") == "chemotion:withheld"]
    assert len(withheld) == 1 and withheld[0]["value"] == "density, name"
    manifest = graph["#export-manifest"]
    assert "2 × sample field: the element is shared below full detail level" in manifest["description"]


def test_a_non_latin_name_is_transliterated_in_the_path_and_kept_in_name(tmp_path):
    model = load_fixture_model()
    sample = next(e for e in model.elements if e.kind == "sample" and e.attachments)
    sample.short_label, sample.name = None, "Benzoesäure"
    out = export_model(model, tmp_path / "t.eln", authors=FIXTURE_AUTHORS, ontology_lookup=False)
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        meta = json.loads(z.read(next(n for n in names if n.endswith("/ro-crate-metadata.json"))))
    path = f"./samples/Benzoesaure_{sample.id}/"
    assert f"t.eln/samples/Benzoesaure_{sample.id}/" in names
    node = next(n for n in meta["@graph"] if n["@id"] == path)
    assert node["name"] == "Benzoesäure", "the original name is kept"



def test_a_screen_mentions_the_linked_elements_that_are_in_the_export(tmp_path):
    from chemotion_crate.model import ElementRecord
    model = load_fixture_model()
    wellplate = next(e for e in model.elements if e.kind == "wellplate")
    model.elements.append(ElementRecord(kind="screen", id=77, name="Panel",
                                        collection_ids=list(wellplate.collection_ids),
                                        links=[["wellplate", wellplate.id], ["wellplate", 999]],
                                        property_values=[{"name": "collaborator", "value": "B"}]))
    out = export_model(model, tmp_path / "s.eln", authors=FIXTURE_AUTHORS, ontology_lookup=False)
    with zipfile.ZipFile(out) as z:
        meta = json.loads(z.read(next(n for n in z.namelist() if n.endswith("/ro-crate-metadata.json"))))
    graph = {n["@id"]: n for n in meta["@graph"]}
    screen = graph["#screens/Panel_77"]
    assert ref_ids(screen["mentions"]) == [f"#wellplates/{wellplate.label}_{wellplate.id}"], \
        "only the wellplate in the export is mentioned"


def test_the_manifest_credits_the_vocabularies_the_crate_uses(any_crate: ExportedCrate):
    manifest = any_crate.node("#export-manifest")
    assert "are used under CC BY 4.0" in manifest["description"]
    assert "CHMO" in manifest["description"] and "RXNO" in manifest["description"]


def test_material_roles_and_sample_fields_reach_the_crate(tmp_path):
    model = load_fixture_model()
    reaction = next(e for e in model.elements if e.kind == "reaction")
    product = next(m for m in reaction.materials["products"])
    product.properties.update({"waste": True, "coefficient": 1})
    product.equivalent = 0.85
    sample = next(e for e in model.elements if e.key == product.key)
    sample.properties.update({"purity": 0.98, "stereo": {"abs": "rac"}, "is_top_secret": False})
    reaction.properties["dangerous_products"] = ["explosive"]
    out = export_model(model, tmp_path / "f.eln", authors=FIXTURE_AUTHORS, ontology_lookup=False)
    with zipfile.ZipFile(out) as z:
        meta = json.loads(z.read(next(n for n in z.namelist() if n.endswith("/ro-crate-metadata.json"))))
    graph = {n["@id"]: n for n in meta["@graph"]}
    def values_of(node_id: str) -> dict[str, dict]:
        return {graph[r["@id"]]["propertyID"]: graph[r["@id"]]
                for r in as_list(graph[node_id]["variableMeasured"])}
    values = values_of(next(p for p in graph if p.endswith(f"_{sample.id}") or p.endswith(f"_{sample.id}/")))
    assert values["chemotion:purity"]["value"] == 0.98
    assert json.loads(values["chemotion:stereo"]["value"]) == {"abs": "rac"}
    assert "chemotion:is_top_secret" not in values, "a flag that is off is left out"
    # The place in the reaction is the material's, not the sample's.
    assert not any(k.startswith("chemotion:reaction-material/") for k in values)
    in_reaction = values_of("#reactions-MT-R1_5-product-1")
    assert in_reaction["chemotion:reaction-material/waste"]["value"] is True
    assert in_reaction["chemotion:reaction-material/equivalent"]["value"] == 0.85
    assert in_reaction["chemotion:reaction-material/waste"]["name"] == "waste"


def test_segments_reach_sample_and_reaction_nodes_too(tmp_path):
    # Samples and reactions get their nodes from chem-dcat-ap; their property values (LabIMotion
    # segments among them) must still be added.
    model = load_fixture_model()
    pv = {"name": "Electrochemistry / Cell / Temperature", "value": 25, "unitText": "C"}
    targets = [next(e for e in model.elements if e.kind == k) for k in ("sample", "reaction")]
    for elem in targets:
        elem.property_values.append(dict(pv))
    out = export_model(model, tmp_path / "seg.eln", authors=FIXTURE_AUTHORS, ontology_lookup=False)
    with zipfile.ZipFile(out) as z:
        meta = json.loads(z.read(next(n for n in z.namelist() if n.endswith("/ro-crate-metadata.json"))))
    found = {n["propertyID"] for n in meta["@graph"]
             if n.get("@type") == "PropertyValue" and n.get("name") == pv["name"]}
    assert found == {"chemotion:sample/electrochemistry-cell-temperature",
                     "chemotion:reaction/electrochemistry-cell-temperature"}


def test_an_element_type_term_set_in_the_map_types_the_export(tmp_path, monkeypatch):
    from chemotion_crate import ontology_map
    monkeypatch.setitem(ontology_map.ELEMENT_TYPE_IRI, "wellplate",
                       "http://purl.obolibrary.org/obo/OBI_0000192")
    out = export_model(load_fixture_model(), tmp_path / "t.eln", authors=FIXTURE_AUTHORS,
                       ontology_lookup=False)
    with zipfile.ZipFile(out) as z:
        meta = json.loads(z.read(next(n for n in z.namelist() if n.endswith("/ro-crate-metadata.json"))))
    graph = {n["@id"]: n for n in meta["@graph"]}
    wellplate = next(n for n in graph.values() if n.get("genre") == "wellplate")
    assert "OBI:0000192" in types(wellplate)
    assert graph["OBI:0000192"]["@type"] == "DefinedTerm"


def _export_graph(model, tmp_path, name="m.eln") -> dict[str, dict]:
    out = export_model(model, tmp_path / name, authors=FIXTURE_AUTHORS, ontology_lookup=False)
    with zipfile.ZipFile(out) as z:
        meta = json.loads(z.read(next(n for n in z.namelist() if n.endswith("/ro-crate-metadata.json"))))
    return {n["@id"]: n for n in meta["@graph"]}


def _mass_mg(graph: dict[str, dict], node: dict) -> list[float]:
    """The node's masses in mg (chem-dcat-ap writes every quantity under ``SIO:000008``)."""
    quantities = [graph[r["@id"]] for r in as_list(node.get("SIO:000008"))]
    return sorted(float(q["prov:value"]) for q in quantities
                  if q.get("qudt:hasQuantityKind", {}).get("@id", "").endswith("/Mass"))


def test_a_sample_in_two_reactions_gets_one_material_node_per_reaction(tmp_path):
    import copy
    model = load_fixture_model()
    first = next(e for e in model.elements if e.kind == "reaction")
    sample = next(e for e in model.elements if e.key == first.materials["starting_materials"][0].key)
    sample.mass_g = 10.0  # the stock, larger than what either reaction used
    second = copy.deepcopy(first)
    second.id, second.short_label, second.url = 6, "MT-R2", first.url.replace("/5", "/6")
    reused = second.materials["starting_materials"][0]
    second.materials = {"products": [reused]}
    reused.mass_g, reused.role = 0.5, "products"
    model.elements.append(second)
    graph = _export_graph(model, tmp_path)

    stock = graph[f"#samples/{sample.label}_{sample.id}"]
    assert "SIO:001378" in types(stock), "the stock is a SubstanceSample, not typed by one role"
    assert "PROCO:0000029" not in types(stock), "not a StartingMaterial"
    assert _mass_mg(graph, stock) == [10000.0], "the sample keeps its own amount"
    used = graph["#reactions-MT-R1_5-starting_material-1"]
    made = graph["#reactions-MT-R2_6-product-1"]
    assert "PROCO:0000029" in types(used) and "NCIT:C48810" in types(made)  # StartingMaterial, ChemicalProduct
    assert _mass_mg(graph, used) == [4607.0] and _mass_mg(graph, made) == [500.0]
    assert used["isPartOf"] == made["isPartOf"] == {"@id": stock["@id"]}


def test_a_material_not_exported_on_its_own_keeps_its_fields(tmp_path):
    from chemotion_crate.model import ElementRecord, MoleculeRecord
    model = load_fixture_model()
    reaction = next(e for e in model.elements if e.kind == "reaction")
    reaction.materials["reactants"] = [ElementRecord(
        kind="sample", id=99, short_label="X-99", role="reactants", mass_g=0.1,
        molecule=MoleculeRecord(id=77, iupac_name="ethanol", cas=["64-17-5"], names=["grain alcohol"]),
        properties={"purity": 0.9, "coefficient": 2, "melting_point": "-114",
                    "external_label": "EXT-1"},
        property_values=[{"name": "Batch", "value": "B7"}])]
    graph = _export_graph(model, tmp_path)
    material = graph["#reactions-MT-R1_5-reactant-1"]
    assert "isPartOf" not in material, "nothing to point at"
    values = {graph[r["@id"]]["propertyID"]: graph[r["@id"]] for r in as_list(material["variableMeasured"])}
    assert values["chemotion:purity"]["value"] == 0.9
    assert values["chemotion:reaction-material/coefficient"]["value"] == 2
    assert values["chemotion:sample/batch"]["value"] == "B7"
    assert values["chemotion:melting_point"]["value"] == "-114"
    assert values["chemotion:external_label"]["value"] == "EXT-1"
    molecule = graph["#molecule_77"]
    assert molecule["name"] == "ethanol" and molecule["identifier"] == "64-17-5"
    assert molecule["alternateName"] == "grain alcohol"


def _keys_sorted_everywhere(value: Any) -> bool:
    if isinstance(value, dict):
        return list(value) == sorted(value) and all(_keys_sorted_everywhere(v) for v in value.values())
    if isinstance(value, list):
        return all(_keys_sorted_everywhere(v) for v in value)
    return True


def test_metadata_keys_are_sorted_at_every_level(any_crate: ExportedCrate):
    # Key order then depends on nothing but the keys; "@id" and "@type" still lead.
    assert _keys_sorted_everywhere(any_crate.metadata)
    assert all(list(n)[0] == "@id" for n in any_crate.metadata["@graph"])


def test_patch_keeps_the_keys_sorted(tmp_path):
    from chemotion_crate.patch import patch_eln_ontology
    out = export_model(load_fixture_model(), tmp_path / "p.eln", authors=FIXTURE_AUTHORS,
                       ontology_lookup=False)
    patch_eln_ontology(out, {"wellplate": "http://purl.obolibrary.org/obo/OBI_0000192"})
    with zipfile.ZipFile(out) as z:
        meta = json.loads(z.read(next(n for n in z.namelist() if n.endswith("/ro-crate-metadata.json"))))
    assert _keys_sorted_everywhere(meta)
