"""The crate's prefix map, and CURIE/IRI conversion built on ``curies``.

chem-dcat-ap declares a prefix per ontology (``CHMO:``, ``CHEBI:``, ``CHEMINF:``, ``SIO:``, …),
which is what the registries those accessions come from expect. The map is read from the schema
itself so a newly mapped vocabulary brings its prefix along instead of silently falling back to a
coarse host-level one and stuttering (``obo:CHMO_0000595``).
"""
from __future__ import annotations

import logging
from typing import Any

import curies

logger = logging.getLogger(__name__)

#: Last-resort prefix map, used only if chem-dcat-ap's own cannot be read. Deliberately coarse:
#: one prefix per host. That stutters, but an absolute IRI used as a key that the context does not
#: map is a REQUIRED violation (``ro-crate-1.2_3.1``) and far worse.
FALLBACK_NAMESPACES: dict[str, str] = {
    "qudt": "http://qudt.org/schema/qudt/",
    "sio": "http://semanticscience.org/resource/",
    "obo": "http://purl.obolibrary.org/obo/",
    "adms": "http://www.w3.org/ns/adms#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
}

#: Host-level prefixes that cover a whole IRI host rather than one ontology. They stay available
#: for *expansion* but are never chosen when *compressing*.
COARSE_PREFIXES = ("obo", "sio")

#: Prefixes that must not reach the crate's ``@context``. ``schema``/``sdo`` are RO-Crate's own
#: and would shadow its bare terms; ``linkml``/``ex`` are modelling artefacts; ``dcterms`` would
#: compete with RO-Crate's ``dct`` for the same IRI and leave rdflib to pick one at random.
SHADOWING_PREFIXES = ("schema", "sdo", "linkml", "ex", "dcterms")

#: How a namespace IRI ends, so a prefix declaration can be told from a term definition in the
#: same context dictionary. ``_`` is for OBO-style prefixes (``…/obo/CHMO_``).
NAMESPACE_TERMINATORS = ("/", "#", ":", "_")

#: JSON-LD's gen-delims. A context entry is usable as a CURIE prefix only if its IRI ends in one;
#: otherwise ``CHEBI:59999`` is read as an absolute IRI with scheme ``CHEBI``.
_JSONLD_GEN_DELIMS = (":", "/", "?", "#", "[", "]", "@")


def crate_namespaces(schemaview: Any = None) -> dict[str, str]:
    """The prefix map for the crate, from chem-dcat-ap's merged ``SchemaView``."""
    if schemaview is None:
        return dict(FALLBACK_NAMESPACES)
    try:
        declared = {str(prefix): str(iri) for prefix, iri in dict(schemaview.namespaces()).items()
                    if prefix and str(iri).startswith(("http://", "https://"))}
    except Exception as exc:  # a prefix miss must never fail an export
        logger.warning("Could not read prefixes from chem-dcat-ap; using the fallback map: %s", exc)
        return dict(FALLBACK_NAMESPACES)
    for prefix in SHADOWING_PREFIXES:
        declared.pop(prefix, None)
    return {**FALLBACK_NAMESPACES, **declared}


def context_namespaces(namespaces: dict[str, str]) -> dict[str, Any]:
    """Render the prefix map as a JSON-LD ``@context`` needs it.

    OBO-style namespaces end in ``_``, which is not a gen-delim, so they are declared as
    ``{"@id": …, "@prefix": true}`` (JSON-LD 1.1) to make expansion work; the rest stay plain.
    """
    return {prefix: (iri if iri.endswith(_JSONLD_GEN_DELIMS) else {"@id": iri, "@prefix": True})
            for prefix, iri in namespaces.items()}


def prefixes_of_context(context: dict) -> dict[str, str]:
    """The prefix-like entries of a JSON-LD context (values ending in a namespace terminator)."""
    out: dict[str, str] = {}
    for term, value in context.items():
        if isinstance(value, dict) and value.get("@prefix") and isinstance(value.get("@id"), str):
            out[term] = value["@id"]
        elif isinstance(value, str) and value.endswith(NAMESPACE_TERMINATORS) and "://" in value:
            out[term] = value
    return out


class Namespaces:
    """CURIE <-> IRI conversion over a prefix map.

    :param prefixes: ``prefix -> namespace IRI``; the crate's own map plus whatever the base
        context declares.
    """

    def __init__(self, prefixes: dict[str, str]) -> None:
        self.prefixes = dict(prefixes)
        self._expander = _converter(self.prefixes)
        self._compressor = _converter({p: iri for p, iri in self.prefixes.items()
                                       if p not in COARSE_PREFIXES})

    def expand(self, term: str) -> str:
        """``CHMO:0000595`` -> its IRI; absolute IRIs, blank-node and keyword terms unchanged."""
        if not isinstance(term, str) or ":" not in term or term.startswith(("http://", "https://", "_:", "@")):
            return term
        expanded = self._expander.expand(term)
        return expanded if expanded is not None else term

    def compress(self, term: str) -> str:
        """Re-abbreviate a compact or absolute IRI onto its most specific per-ontology prefix.

        ``obo:CHMO_0000595`` -> ``CHMO:0000595``. A local part containing ``/`` or ``#`` is not an
        accession (``doi:10.5281/zenodo.1``) and is left as it came.
        """
        if not isinstance(term, str):
            return term
        iri = self.expand(term)
        if not iri.startswith(("http://", "https://")):
            return term
        curie = self._compressor.compress(iri)
        if curie is None:
            return term
        _, _, local = curie.partition(":")
        if not local or "/" in local or "#" in local:
            return term
        return curie


def _converter(prefixes: dict[str, str]) -> curies.Converter:
    """A ``curies`` converter.

    Two prefixes on one namespace (``dct`` and ``dcterms``) both expand; the first-declared one
    is what compression produces.
    """
    by_iri: dict[str, list[str]] = {}
    for prefix, iri in prefixes.items():
        by_iri.setdefault(iri, []).append(prefix)
    return curies.Converter([
        curies.Record(prefix=names[0], uri_prefix=iri, prefix_synonyms=names[1:])
        for iri, names in by_iri.items()])
