"""Regenerating a crate from the same input does not churn its ids.

rdflib labels blank nodes randomly (``_:Nabc…``) and emits graph nodes and ``@type`` members in
set order. ``flatten_crate_graph`` must give the same crate ids and the same node content no matter
which labels and which order it was handed.
"""
import copy
import json
import random
import re

from chemotion_crate.passes import flatten_crate_graph

BLANK_ID = re.compile(r"^#[a-z0-9_]+-\d+$")


def _serialisation(labels: list[str], order: list[int], type_order: bool) -> dict:
    """One rdflib-like serialisation of a fixed graph, under the given labels and order."""
    q1, q2, ident = labels
    nodes = [
        {"@id": "./", "@type": "Dataset", "name": "root",
         "hasPart": [{"@id": "./sample/"}]},
        {"@id": "./sample/", "@type": "Dataset", "dct:title": "Ethanol",
         "qudt:hasQuantity": [{"@id": q1}, {"@id": q2}],
         "adms:identifier": {"@id": ident}},
        # Identical in every literal, differing only by unit: must not tie on the sort key.
        {"@id": q1, "@type": "qudt:Quantity", "qudt:numericValue": "1.0",
         "qudt:unit": {"@id": "http://qudt.org/vocab/unit/GM"}},
        {"@id": q2, "@type": "qudt:Quantity", "qudt:numericValue": "1.0",
         "qudt:unit": {"@id": "http://qudt.org/vocab/unit/MilliMOL"}},
        {"@id": ident,
         "@type": ["sio:CHEMINF_000113", "adms:Identifier"] if type_order
         else ["adms:Identifier", "sio:CHEMINF_000113"],
         "skos:notation": "InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3"},
    ]
    return {"@graph": [nodes[i] for i in order]}


def _flattened(seed: int) -> dict:
    rnd = random.Random(seed)
    labels = [f"_:N{rnd.getrandbits(64):x}" for _ in range(3)]
    order = list(range(5))
    rnd.shuffle(order)
    doc = _serialisation(labels, order, type_order=bool(seed % 2))
    flatten_crate_graph(doc)
    return {n["@id"]: n for n in doc["@graph"]}


def test_blank_node_ids_are_stable_across_regenerations():
    first, second = _flattened(1), _flattened(2)
    blank_first = {k for k in first if BLANK_ID.match(k)}
    assert blank_first, "the fixture produces blank nodes, so there is something to stabilise"
    assert set(first) == set(second), "the same input produced different crate ids"
    for key in first:
        assert first[key] == second[key], f"{key} describes something different on re-export"


def test_every_node_is_identical_across_many_regenerations():
    reference = json.dumps(_flattened(0), sort_keys=True)
    for seed in range(1, 20):
        assert json.dumps(_flattened(seed), sort_keys=True) == reference


def test_quantities_differing_only_by_unit_get_distinct_stable_ids():
    nodes = _flattened(3)
    quantities = {k: v for k, v in nodes.items() if k.startswith("#qudt_quantity-")}
    assert len(quantities) == 2
    units = sorted(q["qudt:unit"]["@id"] for q in quantities.values())
    assert units == ["http://qudt.org/vocab/unit/GM", "http://qudt.org/vocab/unit/MilliMOL"]
    by_unit = {q["qudt:unit"]["@id"]: k for k, q in quantities.items()}
    again = {q["qudt:unit"]["@id"]: k for k, q in _flattened(4).items()
             if k.startswith("#qudt_quantity-")}
    assert by_unit == again


def test_type_arrays_are_ordered():
    for node in _flattened(5).values():
        types = node.get("@type")
        if isinstance(types, list):
            assert types == sorted(types, key=str), f"{node.get('@id')} has unordered @type"


def test_no_blank_labels_survive():
    dumped = json.dumps(_flattened(6))
    assert '"_:' not in dumped


def test_flattening_is_idempotent():
    doc = {"@graph": list(_flattened(7).values())}
    once = copy.deepcopy(doc)
    assert flatten_crate_graph(doc) == 0
    assert doc == once


def test_references_taken_before_flattening_stay_valid():
    # Flattening works in place: the @graph list and every node that stays in it are the same
    # objects afterwards, so a node fetched earlier sees the renamed ids and the hoisted entities.
    metadata = _serialisation(["_:a", "_:b", "_:c"], [0, 1, 2, 3, 4], True)
    metadata["@graph"][1]["variableMeasured"] = [
        {"@type": "PropertyValue", "name": "purity", "value": {"@value": "0.99", "@type": "xsd:float"}}]
    graph = metadata["@graph"]
    sample = next(n for n in graph if n["@id"] == "./sample/")

    flatten_crate_graph(metadata)

    assert metadata["@graph"] is graph, "the list itself is kept"
    assert any(n is sample for n in metadata["@graph"]), "the node fetched earlier is still in the graph"
    assert all(ref["@id"].startswith("#") for ref in sample["qudt:hasQuantity"]), "renames reached it"
    (measured,) = sample["variableMeasured"]
    assert set(measured) == {"@id"}, "the nested entity was hoisted and replaced by a reference"
    hoisted = next(n for n in graph if n["@id"] == measured["@id"])
    assert hoisted["value"] == "0.99", "the typed literal was collapsed"
