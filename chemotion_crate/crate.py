"""Assemble the RO-Crate metadata graph for an :class:`~chemotion_crate.model.ExportModel`.

:func:`build_crate` is pure with respect to the server: it takes the model (and the author
list, ontology services and options) and returns the finished ``ro-crate-metadata.json``
document plus the list of files the writer has to stream in. Nothing here performs I/O other
than the optional ontology lookups.

The layout it produces:

* the root Dataset ``./`` is the exported collection, declaring both the RO-Crate 1.2 and the
  ``.eln`` profile, a licence, the authors and the source ELN;
* every element is a Dataset *data entity* at a collection-independent path
  (``./samples/<label>_<id>/``), typed by chem-dcat-ap where a mapping exists and carrying an
  ontology placeholder otherwise; an element in several collections is stored once;
* collections are contextual ``Collection`` entities the root ``mentions``; membership runs
  both ways (element ``isPartOf`` / collection ``hasPart``) from one index;
* attachments are ``File`` entities inside their element's folder, listed from the element,
  from the analysis they belong to, and from the root (py-rocrate requires the latter);
* the writing software, the export action and the metadata descriptor close the graph.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from chemotion_crate.annotation import Annotator, annotate_element_metadata, extra_field_values
from chemotion_crate.authors import Author
from chemotion_crate.graph import (
    ELN_SPEC_NAME,
    ELN_SPEC_PROFILE,
    RO_CRATE_CONTEXT_URL,
    RO_CRATE_PROFILE,
    base_context,
    dump_model,
    material_node_id,
)
from chemotion_crate.model import AttachmentRecord, ElementKey, ElementRecord, ExportModel
from chemotion_crate.namespaces import Namespaces, context_namespaces, prefixes_of_context
from chemotion_crate.naming import CrateFileNamer, sanitize_filename
from chemotion_crate.ontology import InstanceVocabulary, OlsCache
from chemotion_crate.ontology_map import iri_for, ontology_placeholder
from chemotion_crate.passes import (
    declare_unmapped_terms,
    flatten_crate_graph,
    is_opaque_payload,
    normalise_curies,
    polish_contextual_entities,
    replace_host,
)
from chemotion_crate.producer import PRODUCER_NAME, WITHHELD_HOST, producer_node

logger = logging.getLogger(__name__)

#: Licence assigned when the collection specifies none. RO-Crate makes ``license`` REQUIRED on
#: the root data entity, so something has to be stated.
DEFAULT_LICENSE = "https://creativecommons.org/licenses/by/4.0/"
CREATE_ACTION_ID = "#ro-crate_created"
METADATA_ID = "ro-crate-metadata.json"

_PLURALS = {
    "sample": "samples",
    "reaction": "reactions",
    "wellplate": "wellplates",
    "research_plan": "research_plans",
    "device_description": "device_descriptions",
    "cell_line": "cell_lines",
    "screen": "screens",
    "sequence_based_macromolecule_sample": "sbmm_samples",
    "vessel": "vessels",
}

#: Sample fields carried on the node as JSON (opaque payloads as strings).
_SAMPLE_NODE_FIELDS = ("dry_solvent", "impurities", "xref", "purity", "solvent", "stereo",
                       "decoupled", "is_top_secret", "sample_type", "target_amount",
                       "real_amount", "gas_type", "gas_phase_data", "inventory_sample",
                       "pubchem_tag", "elemental_compositions", "residues", "components",
                       "sample_details", "parent_id", "imported_readout")
#: Reaction fields carried on the node.
_REACTION_NODE_FIELDS = ("conditions", "purification",
                         "volume", "use_reaction_volume", "weight_percentage", "rinchi_string",
                         "rinchi_web_key", "variations", "vessel_size", "dangerous_products",
                         "gaseous", "concentration_mode", "ph_operator", "ph_value", "origin",
                         "reaction_type", "reaction_role", "solvent", "lock_reaction_volume")


def _property_values(elem: ElementRecord) -> list[dict]:
    """An element's own property values (LabIMotion segments among them), ready for a node."""
    return [{"@type": "PropertyValue",
             "propertyID": f"chemotion:{_slug(elem.kind)}/{_slug(str(pv.get('name', '')))}",
             **pv}
            for pv in elem.property_values]


def _field_values(elem: ElementRecord) -> list[dict]:
    """ELN fields with no vocabulary term behind them, and the fields the server withheld.

    A bare key the context does not define is a REQUIRED violation (``ro-crate-1.2_2.1``), so
    they travel as PropertyValue entries, the format's own mechanism for structured extras. A
    nested payload (``variations``) is carried as a JSON string: the data is preserved, it just
    stops pretending to be linked data; a flat list of literals is a normal multi-valued property.
    """
    fields = (_SAMPLE_NODE_FIELDS if elem.kind == "sample"
              else _REACTION_NODE_FIELDS if elem.kind == "reaction" else ())
    extras = []
    for field in fields:
        value = elem.properties.get(field)
        # A flag that is off is left out: its absence reads the same, and the crate stays short.
        if value in (None, "", [], {}) or value is False:
            continue
        extras.append({
            "@type": "PropertyValue",
            "propertyID": f"chemotion:{field}",
            "name": field.replace("_", " "),
            "value": (json.dumps(value, ensure_ascii=False, default=str)
                      if is_opaque_payload(value) else value),
        })
    if elem.withheld:
        # Shared below full detail level: the fields are left out, and the crate says which.
        extras.append({
            "@type": "PropertyValue",
            "propertyID": "chemotion:withheld",
            "name": "withheld fields",
            "value": ", ".join(elem.withheld),
            "description": "The source shared this element below full detail level, so the "
                           "server withheld these fields; they are not in the crate.",
        })
    return extras


def _measure(node: dict, values: list[dict]) -> None:
    """Append property values to a node's ``variableMeasured``."""
    if values:
        measured: Any = node.get("variableMeasured") or []
        node["variableMeasured"] = (measured if isinstance(measured, list) else [measured]) + values


class CrateBuild:
    """What :func:`build_crate` hands to the writer."""

    def __init__(self) -> None:
        self.metadata: dict = {}
        #: omission records: the model's, plus what building the crate found. The model's own
        #: list is left untouched so a second export of the same model starts clean.
        self.skipped: list[dict] = []
        #: ``(crate-relative File @id, record)`` for every attachment to stream in.
        self.files: list[tuple[str, AttachmentRecord]] = []
        #: crate-relative element directories (each must exist in the payload).
        self.element_dirs: list[str] = []
        #: the real host to scrub from the written crate when it is withheld; else ``None``.
        self.mask_host: str | None = None


def element_path(elem: ElementRecord) -> str:
    """The crate folder of an element: ``./samples/<label>_<id>/``.

    Generic LabIMotion types share the ``generics`` folder with a type segment, so two generic
    types with the same id and name stay apart. ``id`` is sanitised too: a hostile server could
    return ``../..`` and put traversal segments into ZIP entry paths.
    """
    plural = _PLURALS.get(elem.kind) or f"generics/{sanitize_filename(elem.kind, 60)}"
    return f"./{plural}/{sanitize_filename(elem.label, 80)}_{sanitize_filename(str(elem.id), 40)}/"


def local_element_id(path: str) -> str:
    """The ``#`` id of a file-less element: ``./samples/A_1/`` becomes ``#samples/A_1``."""
    return "#" + path.removeprefix("./").rstrip("/")


def _collection_node_id(col_id: Any, label: str | None, taken: dict) -> str:
    token = sanitize_filename(str(col_id)) if col_id is not None else ""
    if not token:
        token = f"{sanitize_filename(label or 'collection')}-{len(taken) + 1}"
    node_id = f"#collection-{token}"
    suffix = 2
    while node_id in set(taken.values()):
        node_id = f"#collection-{token}-{suffix}"
        suffix += 1
    return node_id


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "value"


def _types(node: dict) -> list:
    types = node.get("@type", [])
    return types if isinstance(types, list) else [types]


def _is(node: dict, type_name: str) -> bool:
    return type_name in _types(node)


def build_crate(
    model: ExportModel,
    authors: list[Author],
    *,
    disclose_host: bool = True,
    ontology_lookup: bool = True,
    ols_cache: OlsCache | None = None,
    vocabulary: InstanceVocabulary | None = None,
    progress: Callable[[int, int, str | None], None] | None = None,
) -> CrateBuild:
    """Build the crate graph for ``model``.

    :param authors: validated authors (see :func:`chemotion_crate.authors.validate_authors`).
    :param disclose_host: record the ELN's URL; when ``False`` the source is identified by the
        ``.invalid`` sentinel everywhere, while its version is still recorded.
    :param ontology_lookup: allow EBI OLS lookups for terms neither embedded nor known to the
        instance (a third-party request; see :func:`chemotion_crate.export`).
    :param ols_cache: the OLS cache to read and update.
    :param vocabulary: the instance's own vocabulary, or ``None`` when there is no instance.
    :param progress: ``callable(index, total, name)`` per element during the mapping phase.
    """
    build = CrateBuild()
    skipped = build.skipped = list(model.skipped)
    cache = ols_cache if ols_cache is not None else OlsCache(None)

    # -- index: paths and collection ids ----------------------------------------------------
    paths: dict[ElementKey, str] = {}
    claimed: dict[str, ElementKey] = {}
    for elem in model.elements:
        path = element_path(elem)
        if path in claimed and claimed[path] != elem.key:
            suffix = 2
            while f"{path.rstrip('/')}~{suffix}/" in claimed:
                suffix += 1
            logger.warning("Crate path %s is already taken by %s; storing %s %s at a "
                           "disambiguated path instead.", path, claimed[path], elem.kind, elem.id)
            skipped.append({"kind": elem.kind, "stage": "path", "id": elem.id,
                            "error": f"crate path collision with {claimed[path]}"})
            path = f"{path.rstrip('/')}~{suffix}/"
        claimed[path] = elem.key
        paths[elem.key] = path
    # An element without files gets a local "#" id and no folder: a folder Dataset SHOULD list
    # what it contains (RO-Crate 1.2 data-entities, "hasPart listing directly contained Data
    # Entities"), and an empty one has nothing to list. The id keeps the folder's shape, so a
    # reader can still tell the element's kind from it.
    folders = []
    for elem in model.elements:
        if next(elem.all_attachments(), None) is not None:
            folders.append(paths[elem.key])
        else:
            paths[elem.key] = local_element_id(paths[elem.key])
    build.element_dirs = folders

    col_node_ids: dict[Any, str] = {}
    for col in model.collections:
        col_node_ids[col.id] = _collection_node_id(col.id if col.has_server_id else None,
                                                   col.label, col_node_ids)

    # -- the chem-dcat-ap graph ---------------------------------------------------------------
    metadata, namespaces = dump_model(model, paths, skipped, progress=progress)
    base_ctx = base_context()
    ns = Namespaces({**prefixes_of_context(base_ctx), **namespaces})
    # Spelling only: rdflib cannot abbreviate an OBO-style IRI onto its own prefix, so every
    # accession arrives as ``obo:CHMO_0000595``. Rewrite before anything downstream reads keys.
    normalise_curies(metadata, ns)
    graph: list = metadata["@graph"]
    nodes: dict[str, dict[str, Any]] = {n["@id"]: n for n in graph if isinstance(n, dict) and "@id" in n}

    # -- files ----------------------------------------------------------------------------------
    namers: dict[ElementKey, CrateFileNamer] = {}
    for elem in model.elements:
        namer = CrateFileNamer()
        for att in elem.all_attachments():
            namer.add(att)
        namers[elem.key] = namer
        for att, safe_name in namer.files:
            file_id = f"{paths[elem.key]}{safe_name}"
            file_node: dict[str, Any] = {
                "@id": file_id,
                "@type": "File",
                # The original filename; the sanitised one lives in @id and the ZIP path only.
                "name": att.filename,
                # Data entities describe themselves (``ro-crate-1.2_60.2``): the attachment's own
                # description when the ELN has one, else which record it came from.
                "description": att.description or f"Attachment of {elem.kind} {elem.id}.",
                "encodingFormat": att.mime_type or "application/octet-stream",
            }
            if att.identifier:
                file_node["identifier"] = att.identifier
            if att.created_at:
                file_node["dateCreated"] = att.created_at
            if att.updated_at:
                file_node["dateModified"] = att.updated_at
            if att.filesize is not None:
                file_node["contentSize"] = str(att.filesize)
            graph.append(file_node)
            nodes[file_id] = file_node
            build.files.append((file_id, att))

    # -- element nodes --------------------------------------------------------------------------
    annotator = Annotator(graph, vocabulary if ontology_lookup else None, cache, ontology_lookup,
                          compress=ns.compress, expand=ns.expand)
    undated: list[str] = []
    for elem in model.elements:
        node_id = paths[elem.key]
        existing = nodes.get(node_id)
        if existing is not None:
            node = existing
            if not _is(node, "Dataset"):
                node["@type"] = ["Dataset", *_types(node)]
        else:
            # No chem-dcat-ap class: a spec-required ``Dataset`` with a forward-compatible
            # ontology placeholder (see ontology_map) and the element's own content.
            node = {
                "@id": node_id,
                "@type": "Dataset",
                "name": elem.name or elem.short_label or elem.kind,
                "variableMeasured": [ontology_placeholder(elem.kind)],
            }
            if elem.description:
                node["description"] = elem.description
            graph.append(node)
            nodes[node_id] = node
            # A kind whose ontology term is set in ELEMENT_TYPE_IRI carries it as a type too, the
            # same way chemotion_crate.patch adds it to crates exported before the term was set.
            if kind_iri := iri_for(elem.kind):
                annotator.add_type_term(node, kind_iri)
        # On every node, the chem-dcat-ap ones included: a sample's or reaction's LabIMotion
        # segments are property values too.
        _measure(node, _property_values(elem))

        annotator.annotate(elem, node)
        replaced = annotate_element_metadata(elem, node, undated)
        if replaced:
            skipped.append({"kind": "keyword", "stage": "keyword", "id": elem.id, "count": replaced})

        # A screen links its wellplates and research plans; those in the export are mentioned.
        linked = [{"@id": paths[(kind, i)]} for kind, i in elem.links if (kind, i) in paths]
        if linked:
            node.setdefault("mentions", []).extend(linked)

        member_of = [{"@id": col_node_ids[c]} for c in elem.collection_ids if c in col_node_ids]
        if member_of:
            node["isPartOf"] = member_of if len(member_of) > 1 else member_of[0]

        _measure(node, _field_values(elem))

        namer = namers[elem.key]
        if namer.files:
            node["hasPart"] = [{"@id": f"{node_id}{name}"} for _, name in namer.files]
        # Each analysis is a Dataset of its own, about its element, so a reader can tell which
        # files belong to which measurement and what technique produced them.
        # - The id starts with "#": a path-like Dataset id is read as a payload folder and fails
        #   four REQUIRED RO-Crate rules.
        # - The subject is schema.org ``about``. chem-dcat-ap's ``dct:subject`` fails a REQUIRED
        #   RO-Crate rule even next to ``about``.
        # - No ``dcat:Dataset`` type and no generating activity: both pass RO-Crate but only add
        #   closed chem-dcat-ap SHACL shapes, and neither tells an analysis apart from other data.
        # - The element ``mentions`` it; listing it in ``hasPart`` adds consortium warnings.
        for index, analysis in enumerate(elem.analyses):
            ana_key = analysis.id if analysis.id is not None else index
            ana_id = ("#" + node_id.removeprefix("#").removeprefix("./").strip("/").replace("/", "-")
                      + f"-analysis-{ana_key}")
            ana_node: dict[str, Any] = {
                "@id": ana_id, "@type": "Dataset",
                "name": analysis.name or f"Analysis {ana_key}",
                "about": {"@id": node_id},
                "isPartOf": {"@id": node_id},
            }
            if analysis.content:
                ana_node["description"] = analysis.content
            if analysis.kind:
                ana_node["keywords"] = analysis.kind.replace(",", ";")
            if analysis.status:
                ana_node["variableMeasured"] = [{"@type": "PropertyValue",
                                                 "propertyID": "chemotion:analysis/status",
                                                 "name": "status", "value": analysis.status}]
            annotator.annotate_analysis(analysis, ana_node)
            parts = [{"@id": f"{node_id}{namer.name_of(att)}"}
                     for dataset in analysis.datasets for att in dataset.attachments
                     if namer.name_of(att)]
            if parts:
                ana_node["hasPart"] = parts
            graph.append(ana_node)
            nodes[ana_id] = ana_node
            node.setdefault("mentions", []).append({"@id": ana_id})

    # Molecule nodes: CAS and synonyms are schema.org concepts already (``identifier``,
    # ``alternateName``); as bare column names they would be unmapped keys.
    molecules: dict[Any, Any] = {}
    # A reaction's materials too: one whose sample is not exported on its own still has a molecule.
    for elem in [*model.elements, *(m for e in model.elements for ms in e.materials.values() for m in ms)]:
        if elem.molecule and elem.molecule.id is not None:
            molecules.setdefault(elem.molecule.id, elem.molecule)
    for mol_id, mol in molecules.items():
        mol_node = nodes.get(f"#molecule_{mol_id}")
        if mol_node is None:
            continue
        mol_node.setdefault("name", mol.iupac_name or mol.sum_formular or mol.cano_smiles
                            or f"molecule {mol_id}")
        if mol.cas:
            mol_node["identifier"] = mol.cas if len(mol.cas) > 1 else mol.cas[0]
        if mol.names:
            mol_node["alternateName"] = mol.names

    # -- each material's place in its reaction ------------------------------------------------
    # Every material has a node of its own per reaction (see graph.dump_model). It points to the
    # sample exported on its own (``isPartOf``: the portion used is part of that sample); a
    # material whose sample is not exported carries the sample's fields itself. Reference,
    # coefficient and waste belong to the material, and so does the equivalent of a product or
    # solvent (chem-dcat-ap types only starting materials' and reagents'). If the reaction could
    # not be mapped there are no material nodes, and these go on the reaction's node.
    for reaction in model.elements:
        if reaction.kind != "reaction":
            continue
        reaction_path = paths[reaction.key]
        reaction_node = nodes.get(reaction_path)
        for role, materials in reaction.materials.items():
            for index, material in enumerate(materials):
                material_node = nodes.get(material_node_id(reaction_path, role, index))
                if material_node is not None:
                    if (sample_path := paths.get(material.key)) is not None:
                        material_node["isPartOf"] = {"@id": sample_path}
                    else:
                        _measure(material_node, extra_field_values(material)
                                 + _property_values(material) + _field_values(material))
                    if role == "purification_solvents" and reaction_node is not None:
                        reaction_node.setdefault("mentions", []).append({"@id": material_node["@id"]})
                values = {k: material.properties[k]
                          for k in ("reference", "coefficient", "waste", "conversion_rate",
                                    "weight_percentage", "weight_percentage_reference")
                          if material.properties.get(k) is not None
                          and material.properties.get(k) is not False
                          and material.properties.get(k) != ""}
                if role == "purification_solvents":
                    values["purification solvent"] = True
                if role in ("products", "solvents") and material.equivalent is not None:
                    values["equivalent"] = material.equivalent
                if not values:
                    continue
                target = material_node or reaction_node
                if target is None:
                    continue
                own = target is material_node
                _measure(target, [
                    {"@type": "PropertyValue",
                     "propertyID": f"chemotion:reaction-material/{key}",
                     "name": (key if own
                              else f"{role.rstrip('s').replace('_', ' ')} {material.label}: {key}"),
                     "value": value}
                    for key, value in values.items()])

    # -- collections --------------------------------------------------------------------------
    # Contextual ``Collection`` entities, not directories: typing them Dataset would force each
    # element back inside exactly one collection folder.
    for col in model.collections:
        col_node: dict[str, Any] = {"@id": col_node_ids[col.id], "@type": "Collection",
                                    "name": col.label or "Collection"}
        if col.description:
            col_node["description"] = col.description
        if col.parent_id is not None and col.parent_id in col_node_ids:
            col_node["isPartOf"] = {"@id": col_node_ids[col.parent_id]}
        members = [{"@id": paths[e.key]} for e in model.elements if col.id in e.collection_ids]
        if members:
            col_node["hasPart"] = members
        graph.append(col_node)
        nodes[col_node["@id"]] = col_node

    # -- root -----------------------------------------------------------------------------------
    root_col = model.root
    root: dict[str, Any] = nodes.get("./") or {"@id": "./"}
    if root["@id"] not in nodes:
        graph.append(root)
        nodes["./"] = root
    root["@type"] = "Dataset"
    root["name"] = root_col.label or "Collection"
    root.setdefault("description", root_col.description or
                    f"Chemotion ELN collection '{root['name']}' exported as an RO-Crate by {PRODUCER_NAME}.")
    root.setdefault("datePublished", datetime.now(UTC).date().isoformat())
    if root_col.id is not None and root_col.has_server_id:
        # A plain string: TheELNFileFormat's schema REQUIRES the root identifier to be a string,
        # which outranks RO-Crate's RECOMMENDED PropertyValue shape (``ro-crate-1.2_53.0``).
        root.setdefault("identifier", str(root_col.id))
    root["conformsTo"] = [{"@id": RO_CRATE_PROFILE}, {"@id": ELN_SPEC_PROFILE}]
    for profile_id, profile_name in ((RO_CRATE_PROFILE, "RO-Crate 1.2 Specification"),
                                     (ELN_SPEC_PROFILE, ELN_SPEC_NAME)):
        if profile_id not in nodes:
            node = {"@id": profile_id, "@type": ["CreativeWork", "Profile"], "name": profile_name}
            graph.append(node)
            nodes[profile_id] = node
    root.setdefault("license", {"@id": DEFAULT_LICENSE})
    if DEFAULT_LICENSE not in nodes:
        node = {"@id": DEFAULT_LICENSE, "@type": "CreativeWork",
                "name": "Creative Commons Attribution 4.0 International",
                "description": "Default export licence. Set a different one on the crate if the "
                               "data is under other terms."}
        graph.append(node)
        nodes[DEFAULT_LICENSE] = node
    # Every data entity, Files included: py-rocrate requires each to be linked from the root.
    root["hasPart"] = [{"@id": other_id} for other_id, node in nodes.items()
                       if other_id not in ("./", METADATA_ID)
                       and (_is(node, "Dataset") or _is(node, "File"))]
    if col_node_ids:
        root["mentions"] = [{"@id": node_id} for node_id in col_node_ids.values()]

    # -- authors --------------------------------------------------------------------------------
    author_refs: list[dict] = []
    publisher_ref: dict | None = None
    for author in authors:
        person: dict[str, Any] = {"@id": author.orcid_url, "@type": "Person", "name": author.name,
                  "identifier": author.orcid_url}
        if author.email:
            person["email"] = author.email
        org_refs = []
        for aff in author.affiliations:
            org_id = aff.ror_url
            if org_id not in nodes:
                org: dict[str, Any] = {"@id": org_id, "@type": "Organization", "name": aff.name,
                                       "identifier": org_id}
                graph.append(org)
                nodes[org_id] = org
            org_refs.append({"@id": org_id})
            publisher_ref = publisher_ref or {"@id": org_id}
        if org_refs:
            person["affiliation"] = org_refs if len(org_refs) > 1 else org_refs[0]
        if person["@id"] not in nodes:
            graph.append(person)
            nodes[person["@id"]] = person
        author_refs.append({"@id": person["@id"]})
    author_value = author_refs if len(author_refs) > 1 else author_refs[0]
    for other_id, node in nodes.items():
        if other_id != METADATA_ID and _is(node, "Dataset") and "author" not in node:
            node["author"] = author_value
    if publisher_ref is not None:
        root.setdefault("publisher", publisher_ref)

    # -- provenance -----------------------------------------------------------------------------
    software = producer_node()
    graph.append(software)
    nodes[software["@id"]] = software
    export_time = datetime.now(UTC).isoformat()
    action: dict[str, Any] = {
        "@id": CREATE_ACTION_ID, "@type": "CreateAction", "name": "RO-Crate created",
        "object": {"@id": "./"}, "endTime": export_time, "instrument": {"@id": software["@id"]},
        "actionStatus": {"@id": "http://schema.org/CompletedActionStatus"},
    }
    graph.append(action)
    nodes[action["@id"]] = action

    real_host = model.source.host
    if disclose_host:
        source_host = real_host or WITHHELD_HOST
    else:
        source_host = WITHHELD_HOST
        # Naming the sentinel on one node is not enough: the hostname also reaches the crate
        # through prose (a molecule's description names the API URL it was assigned from).
        if real_host:
            replaced = replace_host(metadata, real_host, WITHHELD_HOST)
            logger.info("Withholding the ELN hostname; recorded as %s and replaced in %d further "
                        "value(s). The ELN version is still recorded.", WITHHELD_HOST, replaced)
            build.mask_host = real_host
    # RO-Crate 1.2 makes ``version`` REQUIRED on a SoftwareApplication. Without one the source
    # stays a CreativeWork: still what the data came from, not pretending to identify a build.
    source_node: dict[str, Any] = {
        "@id": source_host,
        "@type": "SoftwareApplication" if model.source.version else "CreativeWork",
        "name": "Chemotion ELN", "url": source_host}
    if model.source.version:
        source_node["version"] = model.source.version
    else:
        logger.info("The source ELN's version is unknown, so the crate records it as a "
                    "CreativeWork rather than claiming a software version.")
    if model.source.revision:
        source_node["identifier"] = model.source.revision
    if source_host not in nodes:
        graph.append(source_node)
        nodes[source_host] = source_node
    root.setdefault("isBasedOn", {"@id": source_host})

    descriptor: dict[str, Any] = {
        "@id": METADATA_ID, "@type": "CreativeWork", "name": "RO-Crate metadata descriptor",
        "about": {"@id": "./"}, "conformsTo": {"@id": RO_CRATE_PROFILE},
        "dateCreated": export_time, "version": "1.0",
    }
    if publisher_ref is not None:
        # sdPublisher names the publishing *organization*: the first author's affiliation.
        descriptor["sdPublisher"] = publisher_ref
        logger.info("Publishing this crate as %s, taken from the first author's affiliation.",
                    nodes.get(publisher_ref["@id"], {}).get("name") or publisher_ref["@id"])
    graph.append(descriptor)

    if undated:
        logger.info("%d of %d element(s) carry no timestamps, so dateCreated/dateModified were "
                    "omitted for them.", len(undated), len(model.elements))
        skipped.append({"kind": "timestamps", "stage": "metadata", "count": len(undated),
                        "ids": undated[:20],
                        "error": "source carried no created_at/updated_at; dateCreated/dateModified omitted"})

    # -- final passes ---------------------------------------------------------------------------
    renamed = flatten_crate_graph(metadata)
    if renamed:
        logger.debug("Flattened %d blank node(s) into addressable crate entities", renamed)
    polish_contextual_entities(metadata, "./", ns, label_for=annotator.label_for)
    # Declare every key the RO-Crate context does not define. Last, so it sees every node.
    extra_terms = declare_unmapped_terms(metadata, base_ctx, ns)
    metadata["@context"] = [RO_CRATE_CONTEXT_URL, {**context_namespaces(namespaces), **extra_terms}]
    logger.debug("Declared %d term(s) in the crate's @context.", len(extra_terms))

    build.metadata = metadata
    return build
