"""The vendored RO-Crate 1.2 context subset covers every term the exporter emits.

``chemotion_crate/data/ro-crate-context.jsonld`` holds the terms in
``chemotion_crate.context_terms.CONTEXT_TERMS`` plus the official context's prefixes. A bare key
or bare ``@type`` in a crate that is missing from it would compact differently (or not at all)
offline than against the context URL the crate declares. Add the term to ``CONTEXT_TERMS`` and
rerun ``scripts/update_context_subset.py``.
"""
from __future__ import annotations

import json
from importlib import resources

from chemotion_crate.context_terms import CONTEXT_TERMS
from chemotion_crate.graph import RO_CRATE_CONTEXT_URL, base_context
from chemotion_crate.namespaces import prefixes_of_context

from .conftest import ExportedCrate

#: RFC 3986 gen-delims: a JSON-LD prefix IRI must end in one to be used as a CURIE prefix.
GEN_DELIMS = (":", "/", "?", "#", "[", "]", "@")


def _bare_terms(crate: ExportedCrate) -> set[str]:
    """Every key and ``@type`` value without a prefix, anywhere in the graph."""
    found: set[str] = set()

    def visit(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if not key.startswith("@") and ":" not in key:
                    found.add(key)
                if key == "@type":
                    found.update(t for t in (item if isinstance(item, list) else [item])
                                 if ":" not in t)
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(crate.metadata["@graph"])
    return found


def test_every_bare_term_is_listed_and_vendored(disclosed_crate, withheld_crate):
    context = base_context()
    for crate in (disclosed_crate, withheld_crate):
        terms = _bare_terms(crate)
        assert {"Dataset", "File", "hasPart", "name"} <= terms  # the walk sees the graph
        assert terms - CONTEXT_TERMS == set(), "bare terms missing from CONTEXT_TERMS"
        assert terms - set(context) == set(), "bare terms missing from the vendored context"


def test_every_listed_term_is_vendored():
    assert CONTEXT_TERMS - set(base_context()) == set()


def test_no_vocab_fallback():
    # A @vocab would silently make every undefined key "defined" and hide the gaps above.
    assert "@vocab" not in base_context()


def test_every_other_entry_is_a_prefix():
    context = base_context()
    prefixes = prefixes_of_context(context)
    for term, value in context.items():
        if term in CONTEXT_TERMS or term.startswith("@"):
            continue
        assert term in prefixes, f"{term} is neither a listed term nor a prefix"
        iri = value["@id"] if isinstance(value, dict) else value
        assert isinstance(iri, str) and iri.endswith(GEN_DELIMS), f"{term}: {iri!r}"
    for term, iri in prefixes.items():
        assert iri.endswith(GEN_DELIMS), f"{term}: {iri!r}"


def test_subset_comment_names_the_official_context():
    text = resources.files("chemotion_crate").joinpath("data/ro-crate-context.jsonld").read_text("utf-8")
    document = json.loads(text)
    assert RO_CRATE_CONTEXT_URL == "https://w3id.org/ro/crate/1.2/context"
    assert "https://w3id.org/ro/crate/1.2/context" in document["_comment"]
