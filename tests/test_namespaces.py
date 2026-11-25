"""The crate prefix map and CURIE <-> IRI conversion."""
import pytest

from chemotion_crate.namespaces import (
    FALLBACK_NAMESPACES,
    SHADOWING_PREFIXES,
    Namespaces,
    context_namespaces,
    crate_namespaces,
    prefixes_of_context,
)

OBO = "http://purl.obolibrary.org/obo/"
DCT = "http://purl.org/dc/terms/"
SIO = "http://semanticscience.org/resource/"


class _SchemaView:
    def __init__(self, namespaces):
        self._namespaces = namespaces

    def namespaces(self):
        if isinstance(self._namespaces, Exception):
            raise self._namespaces
        return self._namespaces


# -- crate_namespaces -------------------------------------------------------------------------

def test_no_schemaview_gives_the_fallback_copy():
    out = crate_namespaces(None)
    assert out == FALLBACK_NAMESPACES and out is not FALLBACK_NAMESPACES


def test_unreadable_schemaview_gives_the_fallback():
    assert crate_namespaces(_SchemaView(RuntimeError("broken"))) == FALLBACK_NAMESPACES


def test_shadowing_prefixes_are_dropped_and_fallback_kept_underneath():
    declared = {
        "schema": "http://schema.org/", "sdo": "https://schema.org/",
        "linkml": "https://w3id.org/linkml/", "ex": "http://example.org/",
        "dcterms": DCT, "dct": DCT,
        "CHMO": OBO + "CHMO_",
        "qudt": "http://qudt.org/schema/qudt/override/",
        "local": "urn:not-http:", "": "http://empty.example/",
    }
    out = crate_namespaces(_SchemaView(declared))
    for prefix in SHADOWING_PREFIXES:
        assert prefix not in out
    assert out["dct"] == DCT
    assert out["CHMO"] == OBO + "CHMO_"
    assert "local" not in out and "" not in out
    # schema declarations win over the fallback, which stays for everything it alone covers
    assert out["qudt"] == "http://qudt.org/schema/qudt/override/"
    for prefix in ("obo", "sio", "adms", "skos"):
        assert out[prefix] == FALLBACK_NAMESPACES[prefix]


# -- context_namespaces / prefixes_of_context -------------------------------------------------

def test_context_marks_underscore_namespaces_as_prefixes():
    ctx = context_namespaces({"CHMO": OBO + "CHMO_", "obo": OBO, "adms": "http://www.w3.org/ns/adms#"})
    assert ctx["CHMO"] == {"@id": OBO + "CHMO_", "@prefix": True}
    assert ctx["obo"] == OBO
    assert ctx["adms"] == "http://www.w3.org/ns/adms#"


def test_prefixes_of_context_reads_both_shapes_and_skips_terms():
    ctx = {
        "CHMO": {"@id": OBO + "CHMO_", "@prefix": True},
        "obo": OBO,
        "name": "http://schema.org/name",
        "author": {"@id": "http://schema.org/author", "@type": "@id"},
        "@vocab": "http://schema.org/",
        "bare": "x/",
    }
    out = prefixes_of_context(ctx)
    assert out["CHMO"] == OBO + "CHMO_" and out["obo"] == OBO
    assert "name" not in out and "author" not in out and "bare" not in out


# -- Namespaces -------------------------------------------------------------------------------

@pytest.fixture()
def ns():
    return Namespaces({
        "dct": DCT, "dcterms": DCT,
        "obo": OBO, "CHMO": OBO + "CHMO_",
        "sio": SIO, "SIO": SIO + "SIO_",
        "doi": "https://doi.org/",
    })


@pytest.mark.parametrize("term,iri", [
    ("CHMO:0000595", OBO + "CHMO_0000595"),
    ("obo:CHMO_0000595", OBO + "CHMO_0000595"),
    ("dct:title", DCT + "title"),
    ("dcterms:title", DCT + "title"),
])
def test_expand(ns, term, iri):
    assert ns.expand(term) == iri


@pytest.mark.parametrize("term", ["name", "unknown:x", "_:b0", "@id", OBO + "CHMO_1", 5, None])
def test_expand_leaves_other_terms_alone(ns, term):
    assert ns.expand(term) == term


def test_compress_picks_the_specific_prefix_never_coarse_obo(ns):
    assert ns.compress("obo:CHMO_0000595") == "CHMO:0000595"
    assert ns.compress(OBO + "CHMO_0000595") == "CHMO:0000595"
    assert ns.compress("sio:SIO_000300") == "SIO:000300"


def test_compress_never_produces_a_coarse_curie(ns):
    assert ns.compress(OBO + "FOO_1") == OBO + "FOO_1"
    assert ns.compress("obo:FOO_1") == "obo:FOO_1"  # left as it came


def test_compress_dct_dcterms_synonyms_to_first_declared(ns):
    assert ns.compress("dcterms:title") == "dct:title"
    assert ns.compress(DCT + "title") == "dct:title"


def test_compress_leaves_slash_local_parts_absolute(ns):
    assert ns.compress("https://doi.org/10.5281/zenodo.1") == "https://doi.org/10.5281/zenodo.1"
    assert ns.compress("doi:10.5281/zenodo.1") == "doi:10.5281/zenodo.1"


@pytest.mark.parametrize("term", ["name", "unknown:x", "http://unmapped.example/x", 5, None])
def test_compress_leaves_unmapped_terms_alone(ns, term):
    assert ns.compress(term) == term
