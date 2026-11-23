"""Per-element metadata and ontology annotation of crate nodes.

Two concerns, both working on one element's Dataset node:

* :func:`annotate_element_metadata` adds the descriptive metadata TheELNFileFormat's checker
  lists as suggested on every Dataset (``dateCreated``, ``dateModified``, ``identifier``,
  ``keywords``, ``text``) and carries ELN fields with no chem-dcat-ap home as ``PropertyValue``
  entries.
* :class:`Annotator` resolves ontology terms (analysis techniques, dataset klasses, a reaction's
  RXNO type, a device's own TIB terms) and records each as a ``DefinedTerm`` once.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from chemotion_crate.model import AnalysisRecord, ElementRecord
from chemotion_crate.ontology import (
    InstanceVocabulary,
    OlsCache,
    obo_code_to_iri,
    resolve_ontology_term,
    resolve_term_label,
)

logger = logging.getLogger(__name__)

#: ELN fields carried as ``variableMeasured`` PropertyValue entries. A curated list rather than
#: "everything left over": a crate padded with administrative flags is harder to read, not richer.
ELN_EXTRA_FIELDS = (
    "external_label", "location", "melting_point", "boiling_point",
    "tlc_description", "tlc_solvents", "rf_value", "rxno", "role",
)


def _extra_field_value(elem: ElementRecord, field: str) -> Any:
    if field == "role":
        return elem.role
    return elem.properties.get(field)


def extra_field_values(elem: ElementRecord) -> list[dict]:
    """The ``ELN_EXTRA_FIELDS`` an element has, as PropertyValue entries."""
    extras = []
    for field in ELN_EXTRA_FIELDS:
        value = _extra_field_value(elem, field)
        if value is None or isinstance(value, dict | list | tuple | set):
            continue
        text_value = str(value).strip()
        if not text_value or text_value.lower() in ("none", "false"):
            continue
        extras.append({
            "@type": "PropertyValue",
            "propertyID": f"chemotion:{field}",
            "name": field.replace("_", " "),
            "value": text_value,
        })
    return extras


def annotate_element_metadata(elem: ElementRecord, node: dict, undated: list[str]) -> int:
    """Attach dates, identifier, genre, keywords, text and extra property values to ``node``.

    :return: how many keywords had a comma replaced by ``;`` (declared in the export manifest).
    """
    if elem.created_at:
        node["dateCreated"] = elem.created_at
    if elem.updated_at:
        node["dateModified"] = elem.updated_at
    if not elem.created_at and not elem.updated_at:
        undated.append(f"{elem.kind} {elem.id}")

    identifier = elem.short_label or (str(elem.id) if elem.id is not None else None)
    if identifier:
        node["identifier"] = str(identifier)
    node["genre"] = elem.kind

    # keywords MUST be one delimited string: an array fails TheELNFileFormat's schema. The
    # delimiter is the comma, so a comma inside a label would split it on reading; it is
    # replaced rather than letting one label read as two.
    raw = [k.strip() for k in elem.keywords if k and k.strip()]
    tag = elem.properties.get("tag")
    if isinstance(tag, str) and tag.strip():
        raw.append(tag.strip())
    replaced = sum(1 for k in raw if "," in k)
    keywords = [k.replace(",", ";") for k in raw]
    if keywords:
        node["keywords"] = ", ".join(dict.fromkeys(keywords))

    text = elem.observation or elem.description
    if text and "text" not in node:
        node["text"] = text

    extras = extra_field_values(elem)
    if extras:
        node["variableMeasured"] = node.get("variableMeasured", []) + extras
    return replaced


class Annotator:
    """Resolve and record ontology terms for the crate.

    :param graph: the crate's ``@graph`` list, which receives one ``DefinedTerm`` per IRI.
    :param vocabulary: the instance's own vocabulary (outranks OLS), or ``None``.
    :param cache: the OLS cache (read and updated).
    :param enable_ols: whether EBI OLS may be asked at all.
    """

    def __init__(self, graph: list, vocabulary: InstanceVocabulary | None,
                 cache: OlsCache, enable_ols: bool,
                 compress: Callable[[str], str] | None = None,
                 expand: Callable[[str], str] | None = None) -> None:
        self._graph = graph
        #: Spells an IRI the way the rest of the crate does (``CHMO:0000595``), so a term added
        #: here and the same term emitted by chem-dcat-ap are one node, not two spellings.
        self._compress = compress or (lambda iri: iri)
        self._expand: Callable[[str], str] = expand or (lambda iri: iri)
        self._vocabulary = vocabulary
        self._cache = cache
        self._enable_ols = enable_ols
        self._seen: set[str] = set()

    # -- shared -------------------------------------------------------------------------------
    def define(self, iri: str, label: str | None) -> None:
        """Describe ``iri`` as a DefinedTerm once."""
        iri = self._compress(iri)
        if iri in self._seen:
            return
        self._seen.add(iri)
        self._graph.append({"@id": iri, "@type": "DefinedTerm", "name": label or iri})

    def add_type_term(self, node: dict, iri: str) -> None:
        """Add ``iri`` to the node's ``@type`` and describe it as a DefinedTerm."""
        self._add_type(node, iri)
        self.define(iri, self.label_for(iri))

    def _add_type(self, node: dict, iri: str) -> None:
        iri = self._compress(iri)
        types = node.get("@type", [])
        types = types if isinstance(types, list) else [types]
        if iri not in types:
            node["@type"] = [*types, iri]

    def _add_technique(self, node: dict, iri: str, label: str | None) -> None:
        iri = self._compress(iri)
        techniques = node.setdefault("measurementTechnique", [])
        if {"@id": iri} not in techniques:
            techniques.append({"@id": iri})
        self.define(iri, label)

    def label_for(self, iri: str) -> str | None:
        """A label for an IRI the crate references: the instance first, then OLS (if allowed).

        OLS is asked only when there is an instance behind the export, so a fixture or offline
        run makes no network call to decorate identifiers.
        """
        return resolve_term_label(self._expand(iri), cache=self._cache,
                                  enable_ols=self._enable_ols and self._vocabulary is not None,
                                  vocabulary=self._vocabulary)

    # -- per element --------------------------------------------------------------------------
    def annotate(self, elem: ElementRecord, node: dict) -> None:
        """Run every applicable annotation for ``elem`` on its ``node`` (all best-effort)."""
        for step in (self._annotate_analyses, self._annotate_reaction_type,
                     self._annotate_device_terms):
            try:
                step(elem, node)
            except Exception as exc:  # never break an export on annotation
                logger.debug("%s failed for %s %s: %s", step.__name__, elem.kind, elem.id, exc)

    def _annotate_analyses(self, elem: ElementRecord, node: dict) -> None:
        for analysis in elem.analyses:
            self.annotate_analysis(analysis, node)

    def annotate_analysis(self, analysis: AnalysisRecord, node: dict) -> None:
        """Add the analysis' technique(s) as ``measurementTechnique`` on ``node`` (best-effort)."""
        try:
            # Every LabIMotion dataset klass is bound to an ontology term by schema constraint,
            # and the payload carries both halves, so this resolves with no lookup at all.
            for dataset in analysis.datasets:
                iri = obo_code_to_iri(dataset.klass_code) if dataset.klass_code else None
                if iri:
                    self._add_technique(node, iri, dataset.klass_label or dataset.klass_code)
            term = resolve_ontology_term(analysis.name, embedded_kind=analysis.kind,
                                         cache=self._cache, enable_ols=self._enable_ols,
                                         vocabulary=self._vocabulary)
            if term is not None:
                self._add_technique(node, term.iri, term.label)
        except Exception as exc:  # never break an export on annotation
            logger.debug("analysis annotation failed for %s: %s", analysis.id, exc)

    def _annotate_reaction_type(self, elem: ElementRecord, node: dict) -> None:
        """Type a reaction with its RXNO term, an additional ``@type`` like chem-dcat-ap's own."""
        if not elem.rxno:
            return
        term = resolve_ontology_term(None, embedded_kind=elem.rxno, ontology="rxno",
                                     cache=self._cache, enable_ols=self._enable_ols,
                                     vocabulary=self._vocabulary)
        if term is None:
            return
        self._add_type(node, term.iri)
        self.define(term.iri, term.label)

    def _annotate_device_terms(self, elem: ElementRecord, node: dict) -> None:
        """A device description's TIB terms carry full IRIs already; nothing to resolve."""
        for term in elem.ontology_terms:
            iri = term.get("iri")
            if not isinstance(iri, str) or not iri.strip():
                continue
            self._add_type(node, iri)
            self.define(iri, term.get("label") or iri)


LabelFor = Callable[[str], str | None]
