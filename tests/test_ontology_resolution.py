"""Resolution order: embedded code -> instance vocabulary -> EBI OLS4 -> None (offline)."""
from unittest.mock import patch

from chemotion_crate.ontology import (
    InstanceVocabulary,
    OlsCache,
    OntologyTerm,
    resolve_ontology_term,
    resolve_term_label,
)

OBO = "http://purl.obolibrary.org/obo/"
MS_IRI = OBO + "CHMO_0000470"


def _response(payload):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return payload
    return _R()


def _ols(docs):
    return _response({"response": {"docs": docs}})


CHMO = {"ols_terms": [
    {"value": "CHMO:0000228 | spectroscopy"},
    {"value": "CHMO:0000595 | house 1H NMR (1H NMR)", "synonym": "1H NMR"},
]}
RXNO = {"ols_terms": [{"value": "RXNO:0000087 | aldol condensation"}]}


class _Session:
    host_url = "https://eln.example.org"

    def __init__(self):
        self.calls = []

    def get(self, path, params=None, timeout=None):
        self.calls.append(params["name"])
        return _response({"chmo": CHMO, "rxno": RXNO}.get(params["name"], {"ols_terms": None}))


def test_embedded_code_wins_without_any_lookup():
    session = _Session()
    with patch("requests.get") as ebi:
        term = resolve_ontology_term("1H NMR", embedded_kind="CHMO:0000228 | spectroscopy",
                                     vocabulary=InstanceVocabulary(session))
        ebi.assert_not_called()
    assert term == OntologyTerm(OBO + "CHMO_0000228", "spectroscopy")
    assert session.calls == []


def test_instance_outranks_ebi():
    with patch("requests.get") as ebi:
        term = resolve_ontology_term("1H NMR", vocabulary=InstanceVocabulary(_Session()))
        ebi.assert_not_called()
    assert term == OntologyTerm(OBO + "CHMO_0000595", "house 1H NMR")


def test_kind_label_is_the_query_when_no_label_and_no_code():
    with patch("requests.get") as ebi:
        term = resolve_ontology_term(None, embedded_kind="free text | spectroscopy",
                                     vocabulary=InstanceVocabulary(_Session()))
        ebi.assert_not_called()
    assert term.iri == OBO + "CHMO_0000228"


def test_ontology_argument_selects_the_instance_tree():
    term = resolve_ontology_term("aldol condensation", ontology="rxno",
                                 vocabulary=InstanceVocabulary(_Session()), enable_ols=False)
    assert term.iri == OBO + "RXNO_0000087"


def test_falls_through_to_ebi_when_the_instance_lacks_the_term():
    cache = OlsCache()
    with patch("requests.get", return_value=_ols([{"iri": MS_IRI, "label": "mass spectrometry"}])) as ebi:
        term = resolve_ontology_term("mass spectrometry", vocabulary=InstanceVocabulary(_Session()),
                                     cache=cache)
        ebi.assert_called_once()
    assert term.iri == MS_IRI
    assert cache.get("chmo", "mass spectrometry")[0]


def test_ebi_without_vocabulary():
    with patch("requests.get", return_value=_ols([{"iri": MS_IRI, "label": "ms"}])):
        assert resolve_ontology_term("ms").iri == MS_IRI


def test_none_when_everything_misses():
    with patch("requests.get", return_value=_ols([])):
        assert resolve_ontology_term("no such thing",
                                     vocabulary=InstanceVocabulary(_Session())) is None


def test_none_when_ols_disabled_and_instance_misses():
    with patch("requests.get") as ebi:
        assert resolve_ontology_term("mass spec", enable_ols=False) is None
        assert resolve_ontology_term("mass spec", enable_ols=False,
                                     vocabulary=InstanceVocabulary(_Session())) is None
        ebi.assert_not_called()


def test_nothing_to_resolve():
    with patch("requests.get") as ebi:
        assert resolve_ontology_term(None) is None
        assert resolve_ontology_term("", embedded_kind="") is None
        ebi.assert_not_called()


def test_ebi_failure_is_none_not_an_error():
    with patch("requests.get", side_effect=ConnectionError("offline")):
        assert resolve_ontology_term("mass spec") is None


# -- label_for_iri / resolve_term_label -------------------------------------------------------

def test_term_label_prefers_the_instance_across_ontologies():
    vocab = InstanceVocabulary(_Session())
    with patch("requests.get") as ebi:
        assert resolve_term_label(OBO + "CHMO_0000228", vocabulary=vocab) == "spectroscopy"
        assert resolve_term_label(OBO + "RXNO_0000087", vocabulary=vocab) == "aldol condensation"
        ebi.assert_not_called()


def test_term_label_falls_back_to_ols():
    vocab = InstanceVocabulary(_Session())
    with patch("requests.get", return_value=_ols([{"iri": MS_IRI, "label": "mass spectrometry"}])):
        assert resolve_term_label(MS_IRI, vocabulary=vocab) == "mass spectrometry"


def test_term_label_makes_no_request_when_lookups_are_off():
    with patch("requests.get") as ebi:
        assert resolve_term_label(OBO + "BFO_0000051", enable_ols=False) is None
        assert resolve_term_label(None) is None
        ebi.assert_not_called()
