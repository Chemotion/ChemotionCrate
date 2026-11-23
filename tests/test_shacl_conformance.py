"""chem-dcat-ap SHACL conformance tracking for the exported crate.

Ported from the chemotion-api ``v4/eln-export`` branch (``tests/test_shacl_conformance.py``).
Scope decision kept from there: SHACL conformance is a *tracked baseline with documented known
gaps*, NOT a release gate. The residual results stem from chem-dcat-ap profile strictness (AP
wrapper classes on attribute nodes, ``DefinedTerm``/``Identifier`` wrapping, single-datatype
``prov:value`` shapes) in the sibling chem-dcat-ap project.

The old test validated the raw ``to_dcat_ap`` graph of objects created on a live server. Here
the input is what this package actually ships: the fixture crate's ``ro-crate-metadata.json``,
parsed as JSON-LD into rdflib. The ``@context`` URL is swapped for the vendored subset so
parsing never fetches ``w3id.org``. The counts are asserted for *exact* equality: more results
usually means more data emitted, fewer usually means data was lost; either way, re-triage and
update the number deliberately.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

pyshacl = pytest.importorskip("pyshacl")
from rdflib import Graph, Namespace  # noqa: E402

from chemotion_crate.graph import base_context  # noqa: E402

from .conftest import ExportedCrate  # noqa: E402

HERE = Path(__file__).parent
BASELINE_TTL = HERE / "data" / "dcat_baseline.ttl"
#: Vendored copy of the chem-dcat-ap SHACL shapes.
SHACL_PATH = HERE / "data" / "chem_dcat_ap.shacl.ttl"
SH = Namespace("http://www.w3.org/ns/shacl#")

#: The committed ``dcat_baseline.ttl`` (a SubstanceSample, its ChemicalEntity and a
#: ChemicalReaction, frozen 2026-06-24). Guards against chem-dcat-ap / SHACL / pyshacl drift;
#: does not move when the mapping changes.
FIXTURE_VIOLATIONS = 103

#: The mini-model fixture crate (2026-11-15, chem-dcat-ap 0.1.0rc4, dcat-ap-plus 0.1.0rc5).
#: Above the old live number (112) because the fixture carries three samples, two molecules
#: and a reaction with materials, and the crate graph adds DefinedTerm/PropertyValue typing on
#: top of the chem-dcat-ap dump. Dominated by the known classes: "does not have class
#: dcatapplus:DefinedTerm" (347), "not Literal with datatype xsd:string" (166), "not Literal
#: with datatype xsd:float" (71), and the chemical-entity AP wrapper classes (18 each).
#: 1123 since a reaction's materials are nodes of their own (2026-11-23): the two materials and
#: their quantities add 286, and the two stock samples, now typed SubstanceSample, repeat their
#: known closed-shape results (+65); no new kind of result. (769 while a material shared its
#: sample's node; 1352 before a sample used by an exported reaction stopped being dumped a
#: second time; 857 before identical blank nodes hanging off one parent, a shared molecule's
#: identifiers, were merged into one.)
CRATE_VIOLATIONS = 1123

pytestmark = [
    pytest.mark.skipif(not SHACL_PATH.exists(), reason="chem-dcat-ap SHACL shapes not present"),
    # rdflib's own JSON-LD parser, not ours.
    pytest.mark.filterwarnings("ignore:ConjunctiveGraph is deprecated:DeprecationWarning"),
]


@pytest.fixture(scope="module")
def shapes_graph() -> Graph:
    # Parsed once: passing the path to pyshacl re-parses the 262 KB shapes on every call.
    return Graph().parse(SHACL_PATH, format="turtle")


def _validate(data_graph: Graph, shapes: Graph) -> tuple[bool, int, Counter]:
    conforms, results, _ = pyshacl.validate(data_graph, shacl_graph=shapes, inference="none",
                                            advanced=True)
    messages = Counter(str(m) for m in results.objects(None, SH.resultMessage))
    return conforms, len(list(results.subjects(SH.resultMessage, None))), messages


def crate_graph(crate: ExportedCrate) -> Graph:
    """The crate's metadata as RDF, parsed offline against the vendored context subset."""
    metadata = dict(crate.metadata)
    context = metadata["@context"]
    assert context[0] == "https://w3id.org/ro/crate/1.2/context"
    metadata["@context"] = [base_context(), *context[1:]]
    return Graph().parse(data=json.dumps(metadata), format="json-ld")


def test_shacl_baseline_fixture_offline(shapes_graph):
    """The committed baseline graph still has exactly the documented number of results."""
    conforms, n, _ = _validate(Graph().parse(BASELINE_TTL, format="turtle"), shapes_graph)
    assert not conforms  # documented: not fully conformant yet
    assert n == FIXTURE_VIOLATIONS, (
        f"Baseline fixture drifted: {n} results vs recorded {FIXTURE_VIOLATIONS}. "
        "Re-triage and update FIXTURE_VIOLATIONS if intentional.")


def test_crate_parses_as_rdf(disclosed_crate: ExportedCrate):
    graph = crate_graph(disclosed_crate)
    assert len(graph) > 0
    # Compact keys and types expand to their ontology IRIs, so the shapes can target them.
    predicates = {str(p) for p in graph.predicates()}
    assert "http://purl.obolibrary.org/obo/BFO_0000051" in predicates
    assert "http://schema.org/hasPart" in predicates
    classes = {str(o) for o in graph.objects(None, None) if str(o).startswith("http://purl.obo")}
    assert "http://purl.obolibrary.org/obo/CHEBI_59999" in classes


def test_shacl_no_regression_on_the_fixture_crate(any_crate: ExportedCrate, shapes_graph):
    """The exported fixture crate produces exactly the recorded number of SHACL results.

    The withheld variant differs only in the host, which no shape inspects, so both crates
    must land on the same number.
    """
    conforms, n, messages = _validate(crate_graph(any_crate), shapes_graph)
    assert not conforms
    assert n == CRATE_VIOLATIONS, (
        f"SHACL count moved: {n} vs baseline {CRATE_VIOLATIONS}. More results usually means "
        f"more data is being emitted; fewer usually means data was lost. Re-triage, then update. "
        f"Top messages: {messages.most_common(5)}")
