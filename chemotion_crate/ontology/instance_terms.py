"""The ontology vocabulary an instance publishes about itself.

A Chemotion instance ships its own OLS term trees (CHMO, RXNO, BAO) and an admin can edit and
extend them, so the label an ELN *displays* for a technique is not always the label EBI would
return for the same text. A crate should say what the ELN says: these terms therefore outrank
the EBI OLS4 fallback in :func:`~chemotion_crate.ontology.resolve_ontology_term`.

The source is ``GET /api/v1/ols_terms/list?name=<ontology>&edited=false``, which serves the
instance's ``public/ontologies/<ontology>.json``, the full tree exactly as the instance stores
it. ``edited=true`` is deliberately *not* used: it returns only the enabled subset and overwrites
the first root's ``children`` with the calling user's recently-used term ids.

Best-effort throughout: a failure downgrades the annotation and is never fatal.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from chemotion_crate.ontology.ols_lookup import OntologyTerm, parse_embedded_kind

logger = logging.getLogger(__name__)

INSTANCE_TERMS_PATH = "/api/v1/ols_terms/list"
#: The ontologies ``ols_terms/list`` accepts; anything else is rejected by the server.
SUPPORTED_ONTOLOGIES = ("chmo", "rxno", "bao")


def _norm(text: str | None) -> str:
    """Fold a label to a comparison key: case-insensitive, whitespace-collapsed."""
    return re.sub(r"\s+", " ", (text or "").strip()).casefold()


def _iter_nodes(nodes: Any) -> Any:
    """Walk the nested ``children`` tree, skipping anything that is not a term object."""
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield node
        yield from _iter_nodes(node.get("children"))


def _term_of(node: dict) -> OntologyTerm | None:
    """Read one tree node as a term, or ``None`` for a node that does not name one.

    The code is taken from ``value``, which the server renders as ``"CHMO:0000595 | 13C NMR"``,
    the same shape as an analysis' embedded ``kind``. ``term_id`` is **not** used: the published
    tree is built from string-keyed hashes while the entity exposes ``term_id`` as an attribute,
    so it serialises as ``null`` throughout (measured against a live instance).
    """
    term = parse_embedded_kind(node.get("value"))
    if term is None:
        return None
    label = term.label
    synonym = node.get("synonym")
    # The tree displays "label (synonym)"; a DefinedTerm name wants the bare label.
    if isinstance(synonym, str) and synonym and label.endswith(f"({synonym})"):
        label = label[: -len(f"({synonym})")].strip() or label
    return OntologyTerm(iri=term.iri, label=label)


def index_terms_by_iri(payload: Any) -> dict[str, OntologyTerm]:
    """Flatten an ``ols_terms/list`` payload into ``IRI -> term``."""
    index: dict[str, OntologyTerm] = {}
    if not isinstance(payload, dict):
        return index
    for node in _iter_nodes(payload.get("ols_terms")):
        term = _term_of(node)
        if term is not None:
            index.setdefault(term.iri, term)
    return index


def index_instance_terms(payload: Any) -> dict[str, OntologyTerm]:
    """Flatten an ``ols_terms/list`` payload into ``normalised text -> term``.

    Labels are indexed before synonyms, so a term named *X* always wins over a different term
    that merely lists *X* as a synonym.
    """
    primary: dict[str, OntologyTerm] = {}
    secondary: dict[str, OntologyTerm] = {}
    if not isinstance(payload, dict):
        return primary
    for node in _iter_nodes(payload.get("ols_terms")):
        term = _term_of(node)
        if term is None:
            continue
        for key in (term.label, node.get("title")):
            if isinstance(key, str) and _norm(key):
                primary.setdefault(_norm(key), term)
        synonyms = node.get("synonyms")
        for key in (node.get("synonym"), *(synonyms if isinstance(synonyms, list) else ())):
            if isinstance(key, str) and _norm(key):
                secondary.setdefault(_norm(key), term)
    for key, term in secondary.items():
        primary.setdefault(key, term)
    return primary


class InstanceVocabulary:
    """Lazily fetched, per-ontology index of the terms an instance publishes.

    Each ontology is fetched at most once per instance of this class, including a failure, which
    is remembered as an empty index. The result is *not* written to
    :class:`~chemotion_crate.ontology.OlsCache`: that file is portable between runs and
    instances, and an instance-specific label must never be replayed against another instance.

    :param session: anything with ``get(path, params=..., timeout=...)`` returning a
        ``requests``-like response, authenticated against the instance; a
        :class:`chemotion_api.connection.Connection` is one.
    """

    def __init__(self, session: Any, timeout: float = 10.0) -> None:
        self._session = session
        self._timeout = timeout
        self._indexes: dict[str, dict[str, OntologyTerm]] = {}
        self._iri_indexes: dict[str, dict[str, OntologyTerm]] = {}

    @classmethod
    def from_session(cls, session: Any) -> InstanceVocabulary | None:
        """Build a vocabulary for ``session``, or ``None`` when there is no instance to ask."""
        if session is None or not hasattr(session, "get"):
            return None
        host = (getattr(session, "host_url", "") or "").strip("/")
        if not host or host.endswith(".invalid"):
            logger.debug("No instance behind this export; skipping its ontology vocabulary.")
            return None
        return cls(session)

    def _index(self, ontology: str) -> dict[str, OntologyTerm]:
        key = (ontology or "").lower()
        if key in self._indexes:
            return self._indexes[key]
        index: dict[str, OntologyTerm] = {}
        by_iri: dict[str, OntologyTerm] = {}
        if key in SUPPORTED_ONTOLOGIES:
            try:
                res = self._session.get(
                    INSTANCE_TERMS_PATH,
                    params={"name": key, "edited": "false"},
                    timeout=self._timeout,
                )
                res.raise_for_status()
                payload = res.json()
                index = index_instance_terms(payload)
                by_iri = index_terms_by_iri(payload)
            except Exception as exc:  # HTTP/JSON/transport: never fatal
                logger.warning("Could not read the instance's %s terms: %s", key, exc)
                index, by_iri = {}, {}
            else:
                logger.debug("Instance %s vocabulary: %d searchable names.", key, len(index))
        elif key:
            logger.info(
                "Ontology %r is not one the instance publishes (%s); using the public lookup "
                "instead.", key, "/".join(SUPPORTED_ONTOLOGIES))
        self._indexes[key] = index
        self._iri_indexes[key] = by_iri
        return index

    def lookup(self, text: str | None, ontology: str = "chmo") -> OntologyTerm | None:
        """Resolve ``text`` against the instance's own ``ontology`` terms."""
        key = _norm(text)
        if not key:
            return None
        return self._index(ontology).get(key)

    def label_for_iri(self, iri: str | None) -> str | None:
        """Name a term we hold an IRI for, searching every ontology the instance publishes.

        The trees are not confined to the ontology they are filed under (the ``chmo`` tree carries
        ``BFO`` terms), so the IRI prefix is no guide to which file holds it.
        """
        if not iri:
            return None
        for ontology in SUPPORTED_ONTOLOGIES:
            self._index(ontology)  # ensure fetched; populates the IRI index alongside
            term = self._iri_indexes.get(ontology, {}).get(iri)
            if term is not None:
                return term.label
        return None
