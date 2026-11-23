"""OLS lookup and its cache (offline; ``requests.get`` mocked)."""
import json
import logging
from unittest.mock import patch

import pytest

from chemotion_crate.ontology import (
    OlsCache,
    OntologyTerm,
    obo_code_to_iri,
    parse_embedded_kind,
    query_ols,
    query_ols_label,
)

MS_IRI = "http://purl.obolibrary.org/obo/CHMO_0000470"


def _response(docs=None, payload=None, status_error=None):
    class _R:
        def raise_for_status(self):
            if status_error is not None:
                raise status_error

        def json(self):
            return payload if payload is not None else {"response": {"docs": docs or []}}
    return _R()


# -- codes ------------------------------------------------------------------------------------

@pytest.mark.parametrize("code,iri", [
    ("CHMO:0000595", "http://purl.obolibrary.org/obo/CHMO_0000595"),
    ("chebi:1234", "http://purl.obolibrary.org/obo/CHEBI_1234"),
    ("  RXNO:0000087  ", "http://purl.obolibrary.org/obo/RXNO_0000087"),
])
def test_obo_code_to_iri(code, iri):
    assert obo_code_to_iri(code) == iri


@pytest.mark.parametrize("code", ["not a code", "", None, "CHMO_0000595", "CHMO:abc",
                                  "CHMO:0000595 | label", "C1:0001"])
def test_obo_code_to_iri_rejects_non_codes(code):
    assert obo_code_to_iri(code) is None


def test_parse_embedded_kind_with_label():
    term = parse_embedded_kind("CHMO:0000595 | proton NMR (1H NMR)")
    assert term == OntologyTerm("http://purl.obolibrary.org/obo/CHMO_0000595", "proton NMR (1H NMR)")


def test_parse_embedded_kind_leading_space_and_no_label():
    assert parse_embedded_kind(" CHMO:0000595 | x").iri.endswith("CHMO_0000595")
    bare = parse_embedded_kind("CHMO:0000595")
    assert bare.iri.endswith("CHMO_0000595") and bare.label == "CHMO:0000595"


@pytest.mark.parametrize("kind", ["1H NMR", "", None, "no-such-code | junk", "-- Recently selected --"])
def test_parse_embedded_kind_without_code(kind):
    assert parse_embedded_kind(kind) is None


# -- cache ------------------------------------------------------------------------------------

def test_cache_miss_hit_and_negative_entry():
    cache = OlsCache()
    assert cache.get("chmo", "x") == (False, None)
    cache.put("chmo", "x", OntologyTerm("http://e/IRI_1", "x"))
    assert cache.get("chmo", "x") == (True, OntologyTerm("http://e/IRI_1", "x"))
    cache.put("chmo", "nope", None)
    assert cache.get("chmo", "nope") == (True, None)
    assert cache.get("rxno", "x") == (False, None)


def test_cache_round_trips_to_disk(tmp_path):
    path = tmp_path / "sub" / "ols.json"
    cache = OlsCache(path)
    cache.put("chmo", "x", OntologyTerm("http://e/IRI_1", "x"))
    cache.put("chmo", "miss", None)
    cache.save()
    data = json.loads(path.read_text())
    assert data["chmo"]["x"] == {"iri": "http://e/IRI_1", "label": "x"}
    assert data["chmo"]["miss"] is None
    again = OlsCache(path)
    assert again.get("chmo", "x") == (True, OntologyTerm("http://e/IRI_1", "x"))
    assert again.get("chmo", "miss") == (True, None)


def test_cache_without_path_saves_nothing(tmp_path):
    cache = OlsCache()
    cache.put("chmo", "x", None)
    cache.save()  # no-op, no error
    assert cache.path is None


@pytest.mark.parametrize("content", ["not json", "[1, 2]", '"a string"'])
def test_corrupt_or_wrong_shape_cache_file_starts_empty(tmp_path, content):
    path = tmp_path / "ols.json"
    path.write_text(content)
    cache = OlsCache(path)
    assert cache.get("chmo", "x") == (False, None)


@pytest.mark.parametrize("entry", [
    "a string", 42, [], {"iri": 1, "label": "x"}, {"iri": "http://e/1"}, {"label": "x"},
])
def test_malformed_entry_is_a_miss_with_warning(tmp_path, caplog, entry):
    path = tmp_path / "ols.json"
    path.write_text(json.dumps({"chmo": {"x": entry}}))
    with caplog.at_level(logging.WARNING):
        assert OlsCache(path).get("chmo", "x") == (False, None)
    assert any("malformed" in r.getMessage() for r in caplog.records)


def test_non_dict_bucket_is_a_miss(tmp_path):
    path = tmp_path / "ols.json"
    path.write_text(json.dumps({"chmo": ["x"]}))
    assert OlsCache(path).get("chmo", "x") == (False, None)


# -- query_ols --------------------------------------------------------------------------------

def test_query_ols_hit_and_caches():
    cache = OlsCache()
    docs = [{"iri": MS_IRI, "label": "mass spectrometry"}]
    with patch("requests.get", return_value=_response(docs)) as get:
        term = query_ols("mass spec", ontology="chmo", cache=cache)
    assert term == OntologyTerm(MS_IRI, "mass spectrometry")
    params = get.call_args.kwargs["params"]
    assert params["q"] == "mass spec" and params["ontology"] == "chmo"
    with patch("requests.get") as again:
        assert query_ols("mass spec", ontology="chmo", cache=cache) == term
        again.assert_not_called()


def test_query_ols_skips_incomplete_docs():
    docs = [{"iri": None, "label": "x"}, {"iri": "http://e/1"}, {"iri": MS_IRI, "label": "ms"}]
    with patch("requests.get", return_value=_response(docs)):
        assert query_ols("ms") == OntologyTerm(MS_IRI, "ms")


def test_query_ols_negative_cache():
    cache = OlsCache()
    with patch("requests.get", return_value=_response([])):
        assert query_ols("nonsense", cache=cache) is None
    with patch("requests.get") as again:
        assert query_ols("nonsense", cache=cache) is None
        again.assert_not_called()
    assert cache.get("chmo", "nonsense") == (True, None)


@pytest.mark.parametrize("kwargs", [
    {"side_effect": ConnectionError("no net")},
    {"return_value": _response(status_error=RuntimeError("HTTP 500"))},
    {"return_value": _response(payload={"response": None})},
    {"return_value": _response(payload={"unexpected": True})},
])
def test_query_ols_errors_are_tolerated(kwargs):
    with patch("requests.get", **kwargs):
        assert query_ols("anything") is None


def test_query_ols_without_label_or_ontology_makes_no_request():
    with patch("requests.get") as get:
        assert query_ols("") is None
        assert query_ols("x", ontology="") is None
        get.assert_not_called()


def test_cached_hit_short_circuits_network():
    cache = OlsCache()
    cache.put("chmo", "ms", OntologyTerm(MS_IRI, "ms"))
    with patch("requests.get") as get:
        assert query_ols("ms", cache=cache).iri == MS_IRI
        get.assert_not_called()


# -- query_ols_label --------------------------------------------------------------------------

def test_query_ols_label_requires_exact_iri_match_and_caches():
    cache = OlsCache()
    docs = [{"iri": "http://e/other", "label": "wrong"}, {"iri": MS_IRI, "label": " mass spec "}]
    with patch("requests.get", return_value=_response(docs)) as get:
        assert query_ols_label(MS_IRI, cache=cache) == "mass spec"
    assert get.call_args.kwargs["params"]["q"] == MS_IRI
    with patch("requests.get") as again:
        assert query_ols_label(MS_IRI, cache=cache) == "mass spec"
        again.assert_not_called()


def test_query_ols_label_negative_cache_and_blank_label():
    cache = OlsCache()
    with patch("requests.get", return_value=_response([{"iri": MS_IRI, "label": "  "}])):
        assert query_ols_label(MS_IRI, cache=cache) is None
    with patch("requests.get") as again:
        assert query_ols_label(MS_IRI, cache=cache) is None
        again.assert_not_called()


def test_query_ols_label_bucket_is_separate_from_label_buckets():
    cache = OlsCache()
    with patch("requests.get", return_value=_response([{"iri": MS_IRI, "label": "ms"}])):
        query_ols_label(MS_IRI, cache=cache)
    assert cache.get("chmo", MS_IRI) == (False, None)


def test_query_ols_label_errors_are_tolerated():
    with patch("requests.get", side_effect=TimeoutError("slow")):
        assert query_ols_label(MS_IRI) is None
    with patch("requests.get") as get:
        assert query_ols_label("") is None
        get.assert_not_called()
