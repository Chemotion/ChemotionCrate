"""The instance's own OLS term trees (offline; the session is a stub)."""
import logging

import pytest

from chemotion_crate.ontology import InstanceVocabulary, index_instance_terms, index_terms_by_iri
from chemotion_crate.ontology.instance_terms import INSTANCE_TERMS_PATH

OBO = "http://purl.obolibrary.org/obo/"

# Shaped like what a live instance serves for public/ontologies/chmo.json: `term_id` and
# `synonyms` are null, the id lives only in `value` (which may carry a leading space), and the
# first root is a group header naming no term.
PAYLOAD = {
    "ols_terms": [
        {"title": "-- Recently selected --", "value": "-- Recently selected --",
         "selectable": False, "children": []},
        {
            "term_id": None,
            "value": "CHMO:0000228 | spectroscopy",
            "title": "spectroscopy",
            "synonym": None,
            "synonyms": None,
            "children": [
                {
                    "term_id": None,
                    "value": " CHMO:0000595 | house 1H NMR (1H NMR)",
                    "title": "house 1H NMR (1H NMR)",
                    "synonym": "1H NMR",
                    "synonyms": ["proton NMR"],
                    "children": [
                        {"value": "BFO:0000051 | has part", "title": "has part", "children": None},
                    ],
                },
                "not a term object",
            ],
        },
        {"term_id": None, "value": "no-such-code | junk", "children": []},
    ]
}


def _response(payload):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return payload
    return _R()


class _Session:
    host_url = "https://eln.example.org"

    def __init__(self, payloads=None):
        #: ontology name -> payload; anything missing serves {"ols_terms": None}
        self._payloads = payloads if payloads is not None else {"chmo": PAYLOAD}
        self.calls = []

    def get(self, path, params=None, timeout=None):
        self.calls.append((path, params))
        return _response(self._payloads.get(params["name"], {"ols_terms": None}))


# -- indexing ---------------------------------------------------------------------------------

def test_codes_come_from_value_not_term_id():
    index = index_instance_terms(PAYLOAD)
    assert index
    assert all(t.iri.startswith(OBO) for t in index.values())


def test_group_header_and_junk_are_skipped():
    index = index_instance_terms(PAYLOAD)
    assert "-- recently selected --" not in index
    assert "junk" not in index
    assert not any("no-such-code" in t.iri for t in index.values())


@pytest.mark.parametrize("payload", [None, [], "x", {}, {"ols_terms": None}, {"ols_terms": {}}])
def test_unusable_payloads_index_to_nothing(payload):
    assert index_instance_terms(payload) == {}
    assert index_terms_by_iri(payload) == {}


def test_label_title_synonym_and_synonyms_all_resolve():
    index = index_instance_terms(PAYLOAD)
    assert index["spectroscopy"].iri == OBO + "CHMO_0000228"
    for key in ("house 1h nmr", "house 1h nmr (1h nmr)", "1h nmr", "proton nmr"):
        assert index[key].iri == OBO + "CHMO_0000595", key
    assert index["has part"].iri == OBO + "BFO_0000051"  # nested two levels deep


def test_displayed_synonym_suffix_is_stripped_from_the_label():
    index = index_instance_terms(PAYLOAD)
    assert index["1h nmr"].label == "house 1H NMR"


def test_suffix_that_is_not_the_synonym_is_kept():
    payload = {"ols_terms": [{"value": "CHMO:1 | foo (bar)", "synonym": "baz"}]}
    assert index_instance_terms(payload)["foo (bar)"].label == "foo (bar)"


def test_label_outranks_another_terms_synonym():
    """A term named X wins over a different term that merely lists X as a synonym."""
    payload = {"ols_terms": [
        {"value": "CHMO:0000001 | other technique", "synonym": "NMR", "synonyms": ["MS"]},
        {"value": "CHMO:0000002 | NMR"},
        {"value": "CHMO:0000003 | MS"},
    ]}
    index = index_instance_terms(payload)
    assert index["nmr"].iri == OBO + "CHMO_0000002"
    assert index["ms"].iri == OBO + "CHMO_0000003"
    assert index["other technique"].iri == OBO + "CHMO_0000001"


def test_first_label_wins_on_duplicates():
    payload = {"ols_terms": [{"value": "CHMO:0000001 | NMR"}, {"value": "CHMO:0000002 | nmr"}]}
    assert index_instance_terms(payload)["nmr"].iri == OBO + "CHMO_0000001"


def test_index_by_iri():
    index = index_terms_by_iri(PAYLOAD)
    assert index[OBO + "CHMO_0000595"].label == "house 1H NMR"
    assert index[OBO + "BFO_0000051"].label == "has part"
    assert len(index) == 3


# -- InstanceVocabulary -----------------------------------------------------------------------

def test_lookup_is_case_and_whitespace_insensitive():
    vocab = InstanceVocabulary(_Session())
    assert vocab.lookup("  SPECTROSCOPY  ").iri == OBO + "CHMO_0000228"
    assert vocab.lookup("Proton   NMR").iri == OBO + "CHMO_0000595"
    assert vocab.lookup("nothing like this") is None
    assert vocab.lookup(None) is None
    assert vocab.lookup("   ") is None


def test_each_ontology_is_fetched_once_with_edited_false():
    session = _Session()
    vocab = InstanceVocabulary(session)
    vocab.lookup("spectroscopy")
    vocab.lookup("1H NMR")
    vocab.lookup("missing", ontology="CHMO")
    assert session.calls == [(INSTANCE_TERMS_PATH, {"name": "chmo", "edited": "false"})]


def test_unsupported_ontology_is_never_requested_and_is_reported(caplog):
    session = _Session()
    with caplog.at_level(logging.INFO, logger="chemotion_crate.ontology.instance_terms"):
        assert InstanceVocabulary(session).lookup("spectroscopy", ontology="mop") is None
    assert session.calls == []
    assert any("mop" in r.getMessage() for r in caplog.records)


def test_fetch_failure_is_tolerated_and_not_retried():
    class _Broken(_Session):
        def get(self, path, params=None, timeout=None):
            self.calls.append((path, params))
            raise ConnectionError("instance down")

    session = _Broken()
    vocab = InstanceVocabulary(session)
    assert vocab.lookup("spectroscopy") is None
    assert vocab.lookup("1H NMR") is None
    assert len(session.calls) == 1


def test_http_error_is_tolerated():
    class _R:
        def raise_for_status(self):
            raise RuntimeError("HTTP 403")

    class _Forbidden(_Session):
        def get(self, path, params=None, timeout=None):
            return _R()

    assert InstanceVocabulary(_Forbidden()).lookup("spectroscopy") is None


def test_unpublished_ontology_is_empty_not_an_error():
    vocab = InstanceVocabulary(_Session())
    assert vocab.lookup("anything", ontology="bao") is None


def test_label_for_iri_searches_every_published_tree():
    """The trees are not confined to their own ontology: the BFO term lives in chmo's tree."""
    rxno = {"ols_terms": [{"value": "RXNO:0000087 | aldol condensation"}]}
    session = _Session({"chmo": PAYLOAD, "rxno": rxno})
    vocab = InstanceVocabulary(session)
    assert vocab.label_for_iri(OBO + "BFO_0000051") == "has part"
    assert vocab.label_for_iri(OBO + "RXNO_0000087") == "aldol condensation"
    assert vocab.label_for_iri(OBO + "NOPE_9999999") is None
    assert vocab.label_for_iri(None) is None
    assert vocab.label_for_iri("") is None
    assert sorted(p["name"] for _, p in session.calls) == ["bao", "chmo", "rxno"]


def test_label_for_iri_stops_at_first_tree_that_has_it():
    session = _Session()
    assert InstanceVocabulary(session).label_for_iri(OBO + "CHMO_0000228") == "spectroscopy"
    assert [p["name"] for _, p in session.calls] == ["chmo"]


# -- from_session -----------------------------------------------------------------------------

def test_from_session_returns_none_without_an_instance():
    assert InstanceVocabulary.from_session(None) is None
    assert InstanceVocabulary.from_session(object()) is None  # no .get


@pytest.mark.parametrize("host", ["", None, "https://offline.invalid", "https://eln.invalid/"])
def test_from_session_returns_none_for_offline_hosts(host):
    session = _Session()
    session.host_url = host
    assert InstanceVocabulary.from_session(session) is None


def test_from_session_builds_for_a_real_host():
    vocab = InstanceVocabulary.from_session(_Session())
    assert isinstance(vocab, InstanceVocabulary)
