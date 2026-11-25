"""Compact IRIs in a crate are spelled the way the accession's own registry spells them.

rdflib's JSON-LD serializer builds a CURIE by splitting an IRI at its last ``/`` or ``#``, so an
OBO-style accession can only ever match the host-level ``obo:`` prefix and comes out as
``obo:CHMO_0000595`` — the ontology name repeated, which is not what CHMO publishes and not what
a consumer keying on Bioregistry prefixes will match. ``normalise_curies`` rewrites those onto
the per-ontology prefixes chem-dcat-ap declares.

The rewrite is *spelling only*, so the central guarantee tested here is that it changes no
triples: every CURIE must expand to exactly the IRI it expanded to before.
"""
import copy
import json

import pytest
from rdflib import Graph

from chemotion_crate.namespaces import Namespaces, context_namespaces
from chemotion_crate.passes import normalise_curies

NAMESPACES = {
    "obo": "http://purl.obolibrary.org/obo/",
    "sio": "http://semanticscience.org/resource/",
    "qudt": "http://qudt.org/schema/qudt/",
    "CHMO": "http://purl.obolibrary.org/obo/CHMO_",
    "CHEBI": "http://purl.obolibrary.org/obo/CHEBI_",
    "RO": "http://purl.obolibrary.org/obo/RO_",
    "SIO": "http://semanticscience.org/resource/SIO_",
    "CHEMINF": "http://semanticscience.org/resource/CHEMINF_",
    "AFR": "http://purl.allotrope.org/ontologies/result#AFR_",
    "doi": "https://doi.org/",
}
NS = Namespaces(NAMESPACES)


def _normalised_type(term: str) -> str:
    doc = {"@graph": [{"@id": "#x", "@type": term}]}
    normalise_curies(doc, NS)
    return doc["@graph"][0]["@type"]


@pytest.mark.parametrize("term, expected", [
    # the host-level prefix gives way to the ontology's own
    ("obo:CHMO_0000595", "CHMO:0000595"),
    ("obo:CHEBI_59999", "CHEBI:59999"),
    ("obo:RO_0004008", "RO:0004008"),
    ("sio:SIO_010345", "SIO:010345"),
    # CHEMINF lives on semanticscience.org but is not SIO, and must not be labelled as it
    ("sio:CHEMINF_000113", "CHEMINF:000113"),
    # an absolute IRI rdflib could not compact at all (the namespace ends in `#AFR_`)
    ("http://purl.allotrope.org/ontologies/result#AFR_0002409", "AFR:0002409"),
    ("http://purl.obolibrary.org/obo/CHMO_0000595", "CHMO:0000595"),
])
def test_rewrites_onto_the_registered_prefix(term, expected):
    assert _normalised_type(term) == expected


@pytest.mark.parametrize("term", [
    "./",                                       # the crate root
    "./reactions/Oxidation_8011/",              # a relative data-entity path
    "#collection-1",                            # a local contextual entity
    "Dataset",                                  # a bare schema.org term
    "qudt:unit",                                # already abbreviated, nothing more specific
    "https://orcid.org/0000-0002-1825-0097",    # no declared prefix covers it
    "https://doi.org/10.5281/zenodo.1",         # `doi:` matches, but the local part has a `/`
    "https://qudt.org/vocab/unit/MOL-PER-L",    # likewise: cutting here would be wrong
])
def test_leaves_everything_else_alone(term):
    assert _normalised_type(term) == term
    doc = {"@graph": [{"@id": term}]}
    normalise_curies(doc, NS)
    assert doc["@graph"][0]["@id"] == term


def test_is_idempotent():
    doc = {"@graph": [{"@id": "#s", "@type": ["obo:CHMO_0000595"],
                       "obo:RO_0004008": {"@id": "sio:SIO_010345"}}]}
    normalise_curies(doc, NS)
    once = copy.deepcopy(doc)
    normalise_curies(doc, NS)
    assert doc == once


def _triples(doc: dict) -> set:
    """The doc's triples, parsed offline through the real ``@context`` rendering — a prefix
    declared the wrong way does not expand, and that must show up here."""
    payload = {**doc, "@context": context_namespaces(NAMESPACES)}
    g = Graph()
    g.parse(data=json.dumps(payload), format="json-ld")
    return set(g)


def test_preserves_every_triple():
    """The point of the whole pass: spelling changes, the graph does not."""
    doc = {"@graph": [{
        "@id": "http://example.org/sample-1",
        "@type": ["obo:CHEBI_59999", "sio:CHEMINF_000113"],
        "obo:RO_0004008": {"@id": "sio:SIO_010345"},
        "http://purl.allotrope.org/ontologies/result#AFR_0002409": {"@id": "obo:CHMO_0000595"},
        "qudt:unit": {"@id": "https://qudt.org/vocab/unit/MOL-PER-L"},
    }]}
    before = _triples(doc)
    assert before
    normalise_curies(doc, NS)
    assert _triples(doc) == before


def test_rewrites_keys_types_and_ids_but_not_values():
    doc = {"@graph": [{
        "@id": "#sample-1",
        "@type": ["Thing", "obo:CHEBI_59999"],
        "obo:RO_0004008": {"@id": "sio:CHEMINF_000113"},
        "name": "obo:CHEBI_59999",  # a *value*, not an IRI — must survive untouched
    }]}
    normalise_curies(doc, NS)
    node = doc["@graph"][0]
    assert node["@type"] == ["Thing", "CHEBI:59999"]
    assert node["RO:0004008"] == {"@id": "CHEMINF:000113"}
    assert node["name"] == "obo:CHEBI_59999"


def test_keeps_key_order_and_references_into_the_graph():
    """Rewriting happens in place, so a node held across the pass is still the crate's node."""
    node = {"@id": "#s", "name": "a", "obo:RO_0004008": {"@id": "x"}, "description": "b"}
    doc = {"@graph": [node]}
    normalise_curies(doc, NS)
    assert doc["@graph"][0] is node
    assert list(node) == ["@id", "name", "RO:0004008", "description"]
    node["later"] = 1
    assert doc["@graph"][0]["later"] == 1


def test_no_graph_is_a_no_op():
    doc: dict = {}
    normalise_curies(doc, NS)
    assert doc == {}
