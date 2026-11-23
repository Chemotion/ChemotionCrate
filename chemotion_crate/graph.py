"""From chem-dcat-ap entities to a compacted RO-Crate JSON-LD document.

The chem-dcat-ap pydantic objects are converted to their LinkML twins, dumped into an ``rdflib``
graph, every absolute identifier is rewritten to a crate-relative one, and the graph is
serialised as JSON-LD compacted against the RO-Crate 1.2 context (a vendored subset, so this
never fetches ``w3id.org`` at runtime) plus the crate's own prefixes.
"""
from __future__ import annotations

import json
import logging
import os
import re
from importlib import resources
from typing import Any

from rdflib import Graph, URIRef

from chemotion_crate.model import ExportModel
from chemotion_crate.namespaces import context_namespaces, crate_namespaces
from chemotion_crate.nodes import (MOLECULE_SCHEME, has_dcat_mapping, material_ref, sample_to_dcat,
                                   to_dcat)

logger = logging.getLogger(__name__)

#: RO-Crate 1.2. Its context defines ``sha256``, which 1.1's did not.
RO_CRATE_CONTEXT_URL = "https://w3id.org/ro/crate/1.2/context"
RO_CRATE_PROFILE = "https://w3id.org/ro/crate/1.2"
#: ``purl.archive.org``, not ``purl.org``: TheELNFileFormat's checker (``tests/checks/versions.py``)
#: matches only the former; a crate using ``purl.org`` is read as declaring no ELN version.
ELN_SPEC_PROFILE = "https://purl.archive.org/purl/elnconsortium/eln-spec/1.2+20260923"
ELN_SPEC_NAME = "ELN-File Format 1.2+20260923 Specification"

_SCHEMAVIEW_CACHE: dict[tuple, Any] = {}


def export_schemaview():
    """The merged chem-dcat-ap ``SchemaView``, built once per process.

    The ``dcat_ap_plus`` import is resolved from the installed package instead of ``w3id.org``
    so the export works offline.
    """
    from chem_dcat_ap.datamodel import MAIN_SCHEMA_PATH

    importmap: dict[str, str] = {}
    try:
        import dcat_ap_plus
        importmap["dcatapplus:latest/schema/dcat_ap_plus"] = os.path.join(
            os.path.dirname(dcat_ap_plus.__file__), "schema", "dcat_ap_plus")
    except Exception as exc:
        logger.warning("dcat_ap_plus is not installed locally; the schema import is fetched "
                       "remotely: %s", exc)
    key = (str(MAIN_SCHEMA_PATH), tuple(sorted(importmap.items())))
    view = _SCHEMAVIEW_CACHE.get(key)
    if view is None:
        from linkml_runtime.utils.schemaview import SchemaView
        view = SchemaView(str(MAIN_SCHEMA_PATH), merge_imports=True, importmap=importmap)
        _SCHEMAVIEW_CACHE[key] = view
    return view


def base_context() -> dict:
    """The vendored RO-Crate 1.2 context (its ``@context`` object)."""
    text = resources.files("chemotion_crate").joinpath("data/ro-crate-context.jsonld").read_text("utf-8")
    return json.loads(text)["@context"]


_API_URL_RE = re.compile(r"https?://[^/]+/api/v1/(.+)$")


def crate_term(term: Any, uri_mapping: dict[str, str]) -> Any:
    """Rewrite one RDF term to its crate-relative identifier.

    * ``chemotion://molecule/<id>`` -> ``#molecule_<id>`` (one node per molecule);
    * an exported element's API URL (optionally with a fragment) -> its crate path;
    * any other API URL (a material not exported standalone) -> a ``#fragment``, so the
      crate-relative identifier policy holds for every record.
    """
    if not isinstance(term, URIRef):
        return term
    value = str(term)
    if value.startswith(MOLECULE_SCHEME):
        return URIRef(f"#molecule_{value[len(MOLECULE_SCHEME):]}")
    relative = uri_mapping.get(value)
    if relative is not None:
        return URIRef(relative)
    base, sep, fragment = value.partition("#")
    if sep and (relative := uri_mapping.get(base)) is not None:
        return URIRef(f"{relative}#{fragment}")
    match = _API_URL_RE.match(value)
    if match:
        frag = re.sub(r"[^A-Za-z0-9_-]+", "_", match.group(1).replace(".json", "")).strip("_")
        return URIRef(f"#{frag}")
    return term


def material_node_id(reaction_path: str, role: str, index: int) -> str:
    """The crate id of a reaction's material: ``#reactions-MT-R1_5-starting_material-1``."""
    stem = reaction_path.removeprefix("#").removeprefix("./").strip("/").replace("/", "-")
    return f"#{stem}-{role.removesuffix('s')}-{index + 1}"


def _progress(callback, index: int, total: int, name: str) -> None:
    if callback is None:
        return
    try:
        callback(index, total, name)
    except Exception as exc:  # a faulty callback must never abort the export
        logger.debug("progress reporting raised: %s", exc)


def dump_model(model: ExportModel, paths: dict[tuple, str], skipped: list[dict],
               progress=None) -> tuple[dict, dict[str, str]]:
    """Map every element with a chem-dcat-ap class and serialise the result.

    :param paths: ``element key -> crate path`` for every exported element.
    :param skipped: receives a ``dcat_mapping`` record for each element whose mapping failed
        (such an element is still exported, as a plain Dataset).
    :param progress: optional ``callable(index, total, name)``.
    :return: ``(metadata_dict, crate_namespaces)``; the document's ``@context`` is set to the
        canonical URL plus the prefix map.
    """
    from linkml_runtime.dumpers import RDFLibDumper
    import chem_dcat_ap.datamodel as dm

    schemaview = export_schemaview()
    dumper = RDFLibDumper()
    graph = Graph()
    uri_mapping: dict[str, str] = {}
    total = len(model.elements)

    def _dump(elem, dcat=None, node_id: str | None = None) -> bool:
        try:
            dcat_obj = dcat(elem) if dcat else to_dcat(elem)
            uri_mapping[str(getattr(dcat_obj, "id"))] = node_id or paths[elem.key]  # noqa: B009
            linkml_cls = getattr(dm, dcat_obj.__class__.__name__)
            dumper.inject_triples(linkml_cls(**dcat_obj.model_dump(exclude_none=True)),
                                  schemaview, graph)
            return True
        except Exception as exc:
            logger.warning("Could not map %s %s to chem-dcat-ap; exported as a plain Dataset: %s",
                           elem.kind, elem.id, exc)
            skipped.append({"kind": "dcat_mapping", "stage": "to_dcat_ap", "id": elem.id,
                            "error": str(exc)})
            return False

    # A reaction's materials are nodes of their own, one per reaction and role, carrying the
    # role and the amounts *in that reaction*. The sample exported on its own keeps its own node
    # and amounts; the crate links each material to it (``isPartOf``, set in build_crate).
    # A purification solvent is not a role of chem-dcat-ap's reaction, so it is dumped here.
    in_reaction: set = set()
    index = 0
    for elem in model.elements:
        if elem.kind != "reaction":
            continue
        index += 1
        _progress(progress, index, total, elem.label)
        if not _dump(elem):
            continue
        for role, items in elem.materials.items():
            for i, material in enumerate(items):
                ref, node_id = material_ref(elem, role, i), material_node_id(paths[elem.key], role, i)
                uri_mapping[ref] = node_id
                # The compound of a material with no molecule id hangs off the material.
                uri_mapping[f"{ref}#EvaluatedCompound"] = f"{node_id}-compound"
                in_reaction.add(material.key)
                if role == "purification_solvents":
                    _dump(material, lambda m, ref=ref: sample_to_dcat(m, ref), node_id)
    for elem in model.elements:
        if elem.kind == "reaction":
            continue
        index += 1
        _progress(progress, index, total, elem.label)
        if not has_dcat_mapping(elem):
            continue
        if elem.kind == "sample" and elem.key in in_reaction:
            _dump(elem, lambda s: sample_to_dcat(s, stock=True))
        else:
            _dump(elem)

    mapped = Graph()
    for prefix, ns in graph.namespaces():
        mapped.bind(prefix, ns)
    for s, p, o in graph:
        mapped.add((crate_term(s, uri_mapping), crate_term(p, uri_mapping),
                    crate_term(o, uri_mapping)))

    namespaces = crate_namespaces(schemaview)
    # Compact against the prefixes too, or rdflib emits chem-dcat-ap predicates as absolute IRIs,
    # which is the REQUIRED violation the prefixes exist to prevent.
    context = {**base_context(), **namespaces}
    metadata = json.loads(mapped.serialize(format="json-ld", context=context))
    metadata["@context"] = [RO_CRATE_CONTEXT_URL, context_namespaces(namespaces)]
    if "@graph" not in metadata:
        node = {k: v for k, v in metadata.items() if k != "@context"}
        metadata = {"@context": metadata["@context"], "@graph": [node] if node else []}
    return metadata, namespaces
