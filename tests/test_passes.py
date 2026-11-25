"""Unit tests for the post-serialisation passes, on small hand-built metadata."""
import logging

import pytest

from chemotion_crate.namespaces import Namespaces
from chemotion_crate.passes import (
    DATA_ENTITY_TYPES,
    METADATA_ID,
    declare_unmapped_terms,
    flatten_crate_graph,
    is_opaque_payload,
    normalise_curies,
    polish_contextual_entities,
    replace_host,
)

BASE_CONTEXT = {
    "name": "http://schema.org/name",
    "Dataset": "http://schema.org/Dataset",
    "mentions": "http://schema.org/mentions",
    "description": "http://schema.org/description",
    "dct": "http://purl.org/dc/terms/",
    "prov": "http://www.w3.org/ns/prov#",
}
CRATE_PREFIXES = {
    "qudt": "http://qudt.org/schema/qudt/",
    "obo": "http://purl.obolibrary.org/obo/",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "CHMO": "http://purl.obolibrary.org/obo/CHMO_",
    "CHEMINF": "http://semanticscience.org/resource/CHEMINF_",
}
NS = Namespaces({"dct": "http://purl.org/dc/terms/", "prov": "http://www.w3.org/ns/prov#",
                 **CRATE_PREFIXES})


def _by_id(metadata: dict) -> dict:
    return {n["@id"]: n for n in metadata["@graph"] if isinstance(n, dict) and "@id" in n}


def test_constants():
    assert METADATA_ID == "ro-crate-metadata.json"
    assert DATA_ENTITY_TYPES == {"Dataset", "File"}


# ------------------------------------------------------------------------------ replace_host

def test_replace_host_walks_ids_values_and_prose():
    metadata = {"@graph": [
        {"@id": "./", "@type": "Dataset", "hasPart": [{"@id": "https://eln.example.org/x/1"}]},
        {"@id": "#m", "description": "compound assigned to https://eln.example.org/api/v1/m/3",
         "nested": {"note": ["seen on eln.example.org"]}},
        {"@id": "#clean", "name": "nothing here"},
    ]}
    changed = replace_host(metadata, "https://eln.example.org", "https://eln.invalid")
    assert changed == 3
    nodes = _by_id(metadata)
    assert nodes["./"]["hasPart"] == [{"@id": "https://eln.invalid/x/1"}]
    assert nodes["#m"]["description"].endswith("https://eln.invalid/api/v1/m/3")
    assert nodes["#m"]["nested"]["note"] == ["seen on eln.invalid"]
    assert "eln.example.org" not in repr(metadata)


def test_replace_host_mutates_in_place():
    root = {"@id": "./", "url": "https://h.example/a"}
    metadata = {"@graph": [root]}
    replace_host(metadata, "https://h.example", "https://x.invalid")
    assert metadata["@graph"][0] is root
    assert root["url"] == "https://x.invalid/a"


# ------------------------------------------------------------------------- is_opaque_payload

@pytest.mark.parametrize("value, expected", [
    ({"a": 1}, True),
    ([{"a": 1}], True),
    ([[1, 2]], True),
    ([1, "two", 3.0], False),
    ("text", False),
    (3, False),
    (None, False),
    ([], False),
])
def test_is_opaque_payload(value, expected):
    assert is_opaque_payload(value) is expected


# ------------------------------------------------------------------------ flatten_crate_graph

def test_flatten_renames_blank_nodes_and_rewrites_references():
    metadata = {"@graph": [
        {"@id": "./s/", "@type": "Dataset", "name": "s", "adms:identifier": {"@id": "_:Nabc"}},
        {"@id": "_:Nabc", "@type": "adms:Identifier", "skos:notation": "X"},
    ]}
    assert flatten_crate_graph(metadata) == 1
    nodes = _by_id(metadata)
    assert "_:Nabc" not in nodes
    assert nodes["./s/"]["adms:identifier"] == {"@id": "#adms_identifier-1"}
    assert nodes["#adms_identifier-1"]["skos:notation"] == "X"


def test_flatten_collapses_typed_literals():
    metadata = {"@graph": [{"@id": "#q", "qudt:numericValue": {"@type": "xsd:float",
                                                               "@value": "0.0"},
                            "label": {"@value": "hi", "@language": "en"}}]}
    flatten_crate_graph(metadata)
    node = _by_id(metadata)["#q"]
    assert node["qudt:numericValue"] == "0.0"
    assert node["label"] == "hi"


def test_flatten_hoists_anonymous_and_nested_entities():
    metadata = {"@graph": [{
        "@id": "./s/", "@type": "Dataset", "name": "s",
        "variableMeasured": [
            {"@type": "PropertyValue", "propertyID": "chemotion:stereo", "value": "abs",
             "unitCode": {"@id": "#u", "@type": "DefinedTerm", "name": "unit"}},
        ],
        "author": {"@id": "#p", "@type": "Person", "name": "P"},
    }]}
    flatten_crate_graph(metadata)
    nodes = _by_id(metadata)
    root = nodes["./s/"]
    assert root["variableMeasured"] == [{"@id": "#s-variablemeasured-chemotion-stereo-1"}]
    assert root["author"] == {"@id": "#p"}
    assert nodes["#s-variablemeasured-chemotion-stereo-1"]["unitCode"] == {"@id": "#u"}
    assert nodes["#u"]["name"] == "unit"
    assert nodes["#p"]["name"] == "P"
    # every entity is top-level, and nothing nested richer than a reference remains
    for node in metadata["@graph"]:
        for key, value in node.items():
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, dict):
                    assert set(item) == {"@id"}, (node["@id"], key)


def test_flatten_merges_an_entity_nested_in_two_places():
    metadata = {"@graph": [
        {"@id": "#a", "author": {"@id": "#p", "@type": "Person", "name": "P"}},
        {"@id": "#b", "author": {"@id": "#p", "@type": "Person", "email": "p@x"}},
        {"@id": "#p", "@type": "Person", "affiliation": "KIT"},
    ]}
    flatten_crate_graph(metadata)
    persons = [n for n in metadata["@graph"] if n["@id"] == "#p"]
    assert len(persons) == 1
    assert persons[0]["affiliation"] == "KIT"
    assert persons[0]["name"] == "P" and persons[0]["email"] == "p@x"


def test_flatten_mirrors_dct_title_into_name_and_drops_the_duplicate():
    metadata = {"@graph": [
        {"@id": "./a/", "@type": ["Dataset"], "dct:title": "Alpha"},
        {"@id": "./b/", "@type": "Dataset", "name": "Kept", "dct:title": "Beta"},
        {"@id": "#c", "@type": "Thing", "dct:title": "Not a dataset"},
    ]}
    flatten_crate_graph(metadata)
    nodes = _by_id(metadata)
    assert nodes["./a/"]["name"] == "Alpha" and "dct:title" not in nodes["./a/"]
    assert nodes["./b/"]["name"] == "Kept" and nodes["./b/"]["dct:title"] == "Beta"
    assert "name" not in nodes["#c"]


def test_flatten_drops_empty_values(caplog):
    metadata = {"@graph": [{"@id": "#x", "@type": "Thing", "name": "x", "comment": "  ",
                            "other": ""}]}
    with caplog.at_level(logging.WARNING):
        flatten_crate_graph(metadata)
    assert _by_id(metadata)["#x"] == {"@id": "#x", "@type": "Thing", "name": "x"}
    assert "comment (1)" in caplog.text


def test_a_python_repr_never_reaches_the_crate(caplog):
    """A repr is never valid crate content: dropped, and reported loudly as the bug it is."""
    metadata = {"@graph": [
        {"@id": "./", "@type": "Dataset", "name": "fine"},
        {"@id": "#pv", "@type": "PropertyValue", "propertyID": "chemotion:stereo",
         "value": "<chemotion_api.utils.PropertyDict object at 0x74e3a1b2c3d0>"},
        {"@id": "#innocent", "@type": "PropertyValue", "value": "<a b c> angle brackets are fine"},
    ]}
    with caplog.at_level(logging.ERROR):
        flatten_crate_graph(metadata)
    nodes = _by_id(metadata)
    assert "value" not in nodes["#pv"], "the repr must be dropped"
    assert nodes["#innocent"]["value"].startswith("<a b c>"), "ordinary angle brackets are kept"
    assert "not data" in caplog.text


def test_flatten_warns_when_blank_nodes_reference_each_other(caplog):
    metadata = {"@graph": [
        {"@id": "_:a", "@type": "Thing", "x": {"@id": "_:b"}},
        {"@id": "_:b", "@type": "Thing", "y": "1"},
    ]}
    with caplog.at_level(logging.WARNING):
        assert flatten_crate_graph(metadata) == 2
    assert "canonicalisation" in caplog.text


def test_flatten_without_graph_is_a_no_op():
    assert flatten_crate_graph({}) == 0
    assert flatten_crate_graph({"@graph": "nonsense"}) == 0


# ----------------------------------------------------------------- polish_contextual_entities

def _crate(*nodes: dict) -> dict:
    return {"@graph": [
        {"@id": METADATA_ID, "@type": "CreativeWork", "about": {"@id": "./"}},
        {"@id": "./", "@type": "Dataset", "name": "root", "hasPart": [{"@id": "./s/"}]},
        *nodes,
    ]}


def test_polish_names_and_types_contextual_entities():
    metadata = _crate(
        {"@id": "./s/", "@type": "Dataset", "name": "s", "identifier": [{"@id": "#q"},
                                                                       {"@id": "#t"}]},
        {"@id": "#q", "@type": "qudt:Quantity", "prov:value": "12.5"},
        {"@id": "#t", "@type": "CHMO:0000595", "dct:title": "a technique"},
    )
    polish_contextual_entities(metadata, "./", NS)
    nodes = _by_id(metadata)
    assert nodes["#q"]["name"] == "12.5"
    assert nodes["#q"]["@type"] == ["qudt:Quantity", "PropertyValue"]
    assert nodes["#t"]["name"] == "a technique"
    assert nodes["#t"]["@type"] == ["CHMO:0000595", "Thing"]
    # data entities and the descriptor are left to their own rules
    assert nodes[METADATA_ID].get("name") is None


def test_polish_falls_back_to_the_type_for_a_name():
    metadata = _crate({"@id": "./s/", "@type": "Dataset", "name": "s",
                       "about": {"@id": "#x"}},
                      {"@id": "#x", "@type": "qudt:Quantity_Kind"})
    polish_contextual_entities(metadata, "./", NS)
    assert _by_id(metadata)["#x"]["name"] == "Quantity Kind"


def test_polish_describes_referenced_types_predicates_and_objects():
    metadata = _crate({"@id": "./s/", "@type": ["Dataset", "obo:CHMO_0000595"], "name": "s",
                       "qudt:unit": {"@id": "http://qudt.org/vocab/unit/MilliMOL"}})
    polish_contextual_entities(metadata, "./", NS)
    nodes = _by_id(metadata)
    unit = nodes["http://qudt.org/vocab/unit/MilliMOL"]
    assert unit == {"@id": "http://qudt.org/vocab/unit/MilliMOL", "@type": "DefinedTerm",
                    "name": "MilliMOL"}
    # stubs are spelled like the rest of the crate: compact where a prefix is declared
    assert nodes["qudt:unit"]["name"] == "unit"
    assert nodes["CHMO:0000595"]["name"] == "CHMO 0000595"


def test_polish_does_not_duplicate_an_entity_already_described_under_a_curie():
    metadata = _crate({"@id": "./s/", "@type": "Dataset", "name": "s",
                       "about": {"@id": "CHMO:0000595"}},
                      {"@id": "CHMO:0000595", "@type": "DefinedTerm", "name": "NMR"})
    polish_contextual_entities(metadata, "./", NS)
    ids = [n["@id"] for n in metadata["@graph"]]
    assert "http://purl.obolibrary.org/obo/CHMO_0000595" not in ids
    assert len(ids) == len(set(ids))


def test_polish_asks_for_a_label_only_for_opaque_accessions():
    asked = []

    def label_for(iri):
        asked.append(iri)
        return "nuclear magnetic resonance spectroscopy"

    metadata = _crate({"@id": "./s/", "@type": "Dataset", "name": "s",
                       "about": [{"@id": "http://purl.obolibrary.org/obo/CHMO_0000595"},
                                 {"@id": "http://qudt.org/vocab/unit/MOL-PER-L"}]})
    polish_contextual_entities(metadata, "./", NS, label_for=label_for)
    nodes = _by_id(metadata)
    assert asked == ["http://purl.obolibrary.org/obo/CHMO_0000595"]
    assert nodes["CHMO:0000595"]["name"] == "nuclear magnetic resonance spectroscopy"
    assert nodes["http://qudt.org/vocab/unit/MOL-PER-L"]["name"] == "MOL PER L"


def test_polish_survives_a_failing_label_lookup():
    def label_for(iri):
        raise RuntimeError("offline")

    metadata = _crate({"@id": "./s/", "@type": "Dataset", "name": "s",
                       "about": {"@id": "http://purl.obolibrary.org/obo/CHMO_0000595"}})
    polish_contextual_entities(metadata, "./", NS, label_for=label_for)
    assert _by_id(metadata)["CHMO:0000595"]["name"] == "CHMO 0000595"


def test_polish_gives_datasets_a_description():
    metadata = _crate({"@id": "./s/", "@type": "Dataset", "name": "Ethanol", "genre": "sample"},
                      {"@id": "./t/", "@type": "Dataset", "description": "kept"})
    polish_contextual_entities(metadata, "./", NS)
    nodes = _by_id(metadata)
    assert nodes["./s/"]["description"] == "sample 'Ethanol' as recorded in the Chemotion ELN."
    assert nodes["./t/"]["description"] == "kept"
    assert "description" not in nodes["./"]


def test_polish_adds_contact_points_and_organization_urls():
    metadata = _crate(
        {"@id": "https://orcid.org/0000-0002-1825-0097", "@type": "Person", "name": "A",
         "email": "a@example.org", "affiliation": {"@id": "https://ror.org/04t3en479"}},
        {"@id": "https://ror.org/04t3en479", "@type": "Organization", "name": "KIT"},
        {"@id": "#local-org", "@type": "Organization", "name": "Lab"},
    )
    metadata["@graph"][1]["author"] = {"@id": "https://orcid.org/0000-0002-1825-0097"}
    polish_contextual_entities(metadata, "./", NS)
    nodes = _by_id(metadata)
    contact = "#contact-0000-0002-1825-0097"
    assert nodes["https://orcid.org/0000-0002-1825-0097"]["contactPoint"] == {"@id": contact}
    assert nodes[contact]["@type"] == "ContactPoint"
    assert nodes[contact]["email"] == "a@example.org"
    assert nodes["https://ror.org/04t3en479"]["url"] == "https://ror.org/04t3en479"
    assert nodes["https://ror.org/04t3en479"]["contactPoint"] == {"@id": contact}
    assert "url" not in nodes["#local-org"]


def test_polish_mentions_stranded_entities_from_the_root():
    metadata = _crate({"@id": "./s/", "@type": "Dataset", "name": "s",
                       "about": {"@id": "#linked"}},
                      {"@id": "#linked", "@type": "Thing", "name": "linked"},
                      {"@id": "#stranded", "@type": "Thing", "name": "stranded"},
                      {"@id": "./orphan.txt", "@type": "File", "name": "orphan"})
    polish_contextual_entities(metadata, "./", NS)
    nodes = _by_id(metadata)
    mentioned = {m["@id"] for m in nodes["./"]["mentions"]}
    assert "#stranded" in mentioned
    assert "#linked" not in mentioned
    assert "./orphan.txt" not in mentioned           # data entities have their own rules
    assert METADATA_ID not in mentioned and "./" not in mentioned
    # a predicate stub is described but referenced by nothing, so it must be mentioned too
    assert {n["@id"] for n in metadata["@graph"] if n.get("@type") == "DefinedTerm"} <= mentioned


def test_polish_re_resolves_the_root_from_the_graph():
    """A root reference held from before a rebuilding pass must not be what gets mutated."""
    metadata = _crate({"@id": "#stranded", "@type": "Thing", "name": "s"})
    stale_root = dict(metadata["@graph"][1])
    flatten_crate_graph(metadata)          # rebuilds every node dict
    polish_contextual_entities(metadata, stale_root["@id"], NS)
    assert {"@id": "#stranded"} in _by_id(metadata)["./"]["mentions"]
    assert "mentions" not in stale_root


def test_polish_normalises_a_single_mentions_object_to_a_list():
    metadata = _crate({"@id": "#a", "@type": "Thing", "name": "a"},
                      {"@id": "#b", "@type": "Thing", "name": "b"})
    metadata["@graph"][1]["mentions"] = {"@id": "#a"}
    polish_contextual_entities(metadata, "./", NS)
    assert _by_id(metadata)["./"]["mentions"] == [{"@id": "#a"}, {"@id": "#b"}]


# --------------------------------------------------------------------- declare_unmapped_terms

def test_declare_unmapped_terms():
    metadata = {"@graph": [
        {"@id": "./", "@type": "Dataset", "name": "r", "dct:title": "t",
         "CHMO:0000595": "x", "qudt:unit": {"@id": "u"},
         "http://example.org/pred": "y", "mystery:thing": "z", "bareword": "w"},
        {"@id": "#q", "qudt:unit": {"@id": "v"}},
    ]}
    extra = declare_unmapped_terms(metadata, BASE_CONTEXT, NS)
    assert extra == {
        "CHMO:0000595": "http://purl.obolibrary.org/obo/CHMO_0000595",
        "dct:title": "http://purl.org/dc/terms/title",
        "http://example.org/pred": "http://example.org/pred",
        "qudt:unit": "http://qudt.org/schema/qudt/unit",
    }
    assert list(extra) == sorted(extra)


def test_declare_unmapped_terms_reads_prefixes_from_the_base_context_too():
    """A prefix the base context declares resolves even if ``ns`` was built without it."""
    ns = Namespaces(CRATE_PREFIXES)
    metadata = {"@graph": [{"@id": "#x", "prov:wasGeneratedBy": {"@id": "#a"}}]}
    assert declare_unmapped_terms(metadata, BASE_CONTEXT, ns) == {
        "prov:wasGeneratedBy": "http://www.w3.org/ns/prov#wasGeneratedBy"}


def test_declare_after_normalise_covers_every_rewritten_key():
    metadata = {"@graph": [{"@id": "#x", "obo:CHMO_0000595": "a",
                            "http://semanticscience.org/resource/CHEMINF_000113": "b"}]}
    normalise_curies(metadata, NS)
    extra = declare_unmapped_terms(metadata, BASE_CONTEXT, NS)
    assert extra == {
        "CHEMINF:000113": "http://semanticscience.org/resource/CHEMINF_000113",
        "CHMO:0000595": "http://purl.obolibrary.org/obo/CHMO_0000595",
    }


def test_a_host_with_a_port_is_also_scrubbed_where_the_port_is_not_written():
    from chemotion_crate.passes import replace_host
    md = {"@graph": [{"@id": "#m", "description": "see https://eln.example.org:8443/x, "
                                                  "https://eln.example.org/y and eln.example.org"}]}
    replace_host(md, "https://eln.example.org:8443", "https://withheld.chemotion.invalid")
    text = md["@graph"][0]["description"]
    assert "eln.example.org" not in text and ":8443" not in text, text
