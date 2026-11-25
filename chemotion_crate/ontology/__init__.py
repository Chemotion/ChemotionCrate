"""Ontology-term resolution for the export.

For free-text technique and method labels that chem-dcat-ap does not cover, an ontology IRI is
resolved using, in order:

1. an ontology code **already embedded** in the Chemotion data (analyses carry
   ``extended_metadata.kind`` like ``"CHMO:0000595 | …"``),
2. the **instance's own** OLS term trees, which an admin can edit and extend
   (:mod:`~chemotion_crate.ontology.instance_terms`),
3. an **OLS** lookup (EBI OLS4, cached, offline-safe), then
4. ``None`` (the caller falls back to the ontology placeholder).

Steps 1 and 2 are the ELN's own answer and come first by design: a crate should record the term
the instance shows its users, not a public-ontology near-match for the same words.
"""
from __future__ import annotations

from chemotion_crate.ontology.instance_terms import (
    InstanceVocabulary,
    index_instance_terms,
    index_terms_by_iri,
)
from chemotion_crate.ontology.ols_lookup import (
    OlsCache,
    OntologyTerm,
    obo_code_to_iri,
    parse_embedded_kind,
    query_ols,
    query_ols_label,
    resolve_ontology_term,
    resolve_term_label,
)

__all__ = [
    "InstanceVocabulary",
    "OntologyTerm",
    "OlsCache",
    "index_instance_terms",
    "index_terms_by_iri",
    "obo_code_to_iri",
    "parse_embedded_kind",
    "query_ols",
    "query_ols_label",
    "resolve_ontology_term",
    "resolve_term_label",
]
