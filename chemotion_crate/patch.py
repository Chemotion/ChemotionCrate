"""Patch ontology annotations into an already-exported ``.eln`` file.

When the ontology term for an element kind (wellplate, research plan, generic element, ...) is
decided after export, this rewrites the ontology placeholders the exporter left in the crate's
``ro-crate-metadata.json``; no re-export needed.

It locates Dataset nodes whose ``variableMeasured`` contains the ontology placeholder
(``propertyID == ELEMENT_TYPE_PROPERTY_ID``), fills the placeholder's ``identifier`` with the
IRI, and adds the IRI as an additional ``@type`` on the node so consumers get real semantic
typing.

The input archive is as untrusted as any other crate: the metadata member is located with the
same shallowest-exact-basename rule as :mod:`chemotion_crate.reader`, and every member is
checked against the same size and compression-ratio limits before it is read or copied.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from chemotion_crate.model import MODELLED_KINDS
from chemotion_crate.namespaces import Namespaces, prefixes_of_context
from chemotion_crate.ontology_map import ELEMENT_TYPE_PROPERTY_ID
from chemotion_crate.reader import (
    MAX_COMPRESSION_RATIO,
    MAX_ZIP_MEMBER_SIZE,
    check_zip_member,
    reject_duplicate_members,
    metadata_member,
)

__all__ = [
    "MAX_COMPRESSION_RATIO",
    "MAX_ZIP_MEMBER_SIZE",
    "patch_eln_ontology",
    "patch_metadata_dict",
]

logger = logging.getLogger(__name__)


def patch_metadata_dict(metadata: dict, mapping: dict[str, str]) -> int:
    """Inject ontology IRIs into a parsed ``ro-crate-metadata.json`` dict, in place.

    Does what the exporter does when a term is set in ``ELEMENT_TYPE_IRI``: the IRI goes into
    the placeholder's ``identifier`` and the node's ``@type`` (kept sorted), spelled compact
    (``OBI:0000192``) when the crate's ``@context`` declares the prefix, and is described once
    as a ``DefinedTerm`` the root mentions. A LabIMotion type with no entry of its own takes the
    ``"generic"`` entry.

    :param mapping: ``{element_kind: ontology_IRI}``.
    :return: number of nodes patched.
    """
    graph = metadata.get("@graph", [])
    if not isinstance(graph, list):
        logger.warning("@graph is a %s, not a list; nothing to patch", type(graph).__name__)
        return 0
    # RO-Crate requires a flattened graph, so `variableMeasured` may hold {"@id": …} references
    # rather than inline PropertyValue objects. Resolve them, otherwise there is nothing with a
    # `propertyID` to match and this silently patches nothing.
    by_id = {n["@id"]: n for n in graph
             if isinstance(n, dict) and isinstance(n.get("@id"), str)}

    def _deref(value: Any) -> Any:
        if isinstance(value, dict) and set(value) == {"@id"}:
            return by_id.get(value["@id"], value)
        return value

    # The prefixes the crate itself declares: a term is compacted only onto one of those.
    context = metadata.get("@context")
    parts = context if isinstance(context, list) else [context]
    prefixes: dict[str, str] = {}
    for part in parts:
        if isinstance(part, dict):
            prefixes.update(prefixes_of_context(part))
    compress = Namespaces(prefixes).compress
    root = by_id.get("./")
    # Any node already carrying the term's id (a DefinedTerm however its @type is spelled, or
    # anything else): a second node with the same @id would break the graph.
    defined: set[str] = set(by_id)

    patched = 0
    for node in graph:
        # An @graph member need not be an object.
        if not isinstance(node, dict):
            continue
        node_patched = False
        var = node.get("variableMeasured")
        entries = var if isinstance(var, list) else [var] if isinstance(var, dict) else []
        for entry in entries:
            pv = _deref(entry)
            if not isinstance(pv, dict) or pv.get("propertyID") != ELEMENT_TYPE_PROPERTY_ID:
                continue
            kind = pv.get("value")
            if not isinstance(kind, str):
                continue
            iri = mapping.get(kind)
            if not iri and kind not in MODELLED_KINDS:
                iri = mapping.get("generic")  # a LabIMotion type without an entry of its own
            if not iri:
                continue
            pv["identifier"] = iri
            term = compress(iri)
            # The IRI becomes an additional @type: the structural type stays, the semantic one
            # is added beside it.
            t = node.get("@type", "Dataset")
            types = [t] if isinstance(t, str) else list(t)
            if term not in types:
                types.append(term)
            node["@type"] = sorted(types, key=str)
            if term not in defined:
                defined.add(term)
                graph.append({"@id": term, "@type": "DefinedTerm", "name": iri})
                if root is not None:
                    mentions = root.get("mentions")
                    mentions = mentions if isinstance(mentions, list) else [mentions] if mentions else []
                    mentions = [*mentions, {"@id": term}]
                    # One value stays a single reference, as the exporter writes it.
                    root["mentions"] = mentions if len(mentions) > 1 else mentions[0]
            node_patched = True
        patched += node_patched
    return patched


def patch_eln_ontology(eln_path: str | Path, mapping: dict[str, str],
                       output_path: str | Path | None = None) -> int:
    """Patch ontology IRIs into an ``.eln`` archive.

    :param eln_path: path to the existing ``.eln`` (RO-Crate ZIP).
    :param mapping: ``{element_kind: ontology_IRI}``
        (e.g. ``{"wellplate": "http://purl.obolibrary.org/obo/OBI_0400081"}``).
    :param output_path: where to write the patched archive; defaults to overwriting
        ``eln_path``.
    :return: number of nodes patched.
    :raises ValueError: if the archive has no ``ro-crate-metadata.json``, the metadata is not a
        JSON object, or a member exceeds the size/compression limits.
    """
    target = Path(output_path) if output_path is not None else Path(eln_path)
    with zipfile.ZipFile(eln_path, "r") as zin:
        reject_duplicate_members(zin.namelist())
        meta_name = metadata_member(zin.namelist())
        if meta_name is None:
            raise ValueError(f"No ro-crate-metadata.json found in {eln_path}")
        # Checked before reading: a 400 KB `.eln` whose metadata inflates to 400 MB would
        # otherwise be read whole, before any validation.
        check_zip_member(zin, meta_name)
        metadata = json.loads(zin.read(meta_name))
        if not isinstance(metadata, dict):
            raise ValueError(
                f"{meta_name} in {eln_path} is a JSON {type(metadata).__name__}, not an object")
        patched = patch_metadata_dict(metadata, mapping)

        # Written beside the target so the final move is a same-filesystem rename.
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=".eln", dir=target.parent)
        os.close(tmp_fd)
        try:
            with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    if item.filename == meta_name:
                        # Sorted keys, as the exporter writes them.
                        zout.writestr(item, json.dumps(metadata, indent=2, ensure_ascii=False,
                                                       sort_keys=True))
                    else:
                        # Streamed, so memory is bounded; but the bytes still land in a temp
                        # file, so a bomb member would fill the disk. Checked before copying.
                        check_zip_member(zin, item)
                        with zin.open(item) as src, zout.open(item, "w") as dst:
                            shutil.copyfileobj(src, dst)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    # The replace happens only after ``zin`` is closed: with no output_path the target *is*
    # ``eln_path``, and replacing a file that is still open for reading fails on Windows. It is
    # still guarded so a failing move (permissions, disk full) cannot leave the temp file behind.
    try:
        # mkstemp creates the file 0600; the patched crate keeps the input's permissions.
        os.chmod(tmp_name, stat.S_IMODE(os.stat(eln_path).st_mode))
        os.replace(tmp_name, target)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise

    return patched
