"""ORCID/ROR normalisation and the author-list validation messages (offline)."""
import pytest

from chemotion_crate.authors import (
    Author,
    AuthorAffiliation,
    normalize_orcid,
    normalize_ror,
    validate_authors,
)

KIT = AuthorAffiliation(name="KIT", ror="04t3en479")


def _author(**overrides):
    kwargs = {"name": "Ada Lovelace", "orcid": "0000-0002-1825-0097", "affiliations": [KIT],
              "email": "ada@example.org"}
    kwargs.update(overrides)
    return Author(**kwargs)


# -- ORCID ------------------------------------------------------------------------------------

@pytest.mark.parametrize("form", [
    "0000-0002-1825-0097",
    "https://orcid.org/0000-0002-1825-0097",
    "http://orcid.org/0000-0002-1825-0097",
    "orcid.org/0000-0002-1825-0097",
    "https://www.orcid.org/0000-0002-1825-0097",
    "HTTPS://ORCID.ORG/0000-0002-1825-0097",
    "https://orcid.org/0000-0002-1825-0097/",
    "  0000-0002-1825-0097  ",
])
def test_orcid_bare_and_url_forms_normalise_to_one_url(form):
    assert normalize_orcid(form) == "https://orcid.org/0000-0002-1825-0097"


def test_orcid_with_x_check_digit_is_accepted():
    assert normalize_orcid("0000-0002-1694-233X") == "https://orcid.org/0000-0002-1694-233X"
    assert normalize_orcid("https://orcid.org/0000-0002-1694-233X") == \
        "https://orcid.org/0000-0002-1694-233X"


def test_orcid_lowercase_x_is_canonicalised():
    assert normalize_orcid("0000-0002-1694-233x") == "https://orcid.org/0000-0002-1694-233X"


@pytest.mark.parametrize("orcid", [
    "0000-0002-1825-0098",                                  # right shape, wrong check digit
    "0000-0002-1694-2330",                                  # X check digit replaced by 0
    "banana",
    "0000-0002-1825-009",                                   # too short
    "http://evil.example/ada",
    "https://evil.example/orcid.org/0000-0002-1825-0097",   # name-drops a real iD (anchoring)
    "https://orcid.org.evil.example/0000-0002-1825-0097",
    "https://orcid.org/0000-0002-1825-0097/extra",
])
def test_invalid_orcid_is_rejected(orcid):
    with pytest.raises(ValueError, match="not a valid ORCID"):
        normalize_orcid(orcid)


def test_orcid_error_names_the_offending_value():
    with pytest.raises(ValueError, match="banana"):
        normalize_orcid("banana")


def test_orcid_url_property():
    assert _author().orcid_url == "https://orcid.org/0000-0002-1825-0097"


# -- ROR --------------------------------------------------------------------------------------

@pytest.mark.parametrize("form", [
    "04t3en479",
    "https://ror.org/04t3en479",
    "http://ror.org/04t3en479",
    "ror.org/04t3en479",
    "https://www.ror.org/04t3en479/",
    "04T3EN479",
    "https://ROR.ORG/04T3EN479",
])
def test_ror_forms_normalise_to_lowercase_url(form):
    assert normalize_ror(form) == "https://ror.org/04t3en479"


def test_another_valid_ror():
    assert normalize_ror("01hhn8329") == "https://ror.org/01hhn8329"


@pytest.mark.parametrize("ror", [
    "not-a-ror",
    "abc",
    "04t3en478",                                # wrong MOD 97-10 check digits
    "04t3en47i",                                # 'i' is not in ROR's crockford alphabet
    "14t3en479",                                # must start with 0
    "04t3en4791",                               # too long
    "https://evil.example/ror.org/04t3en479",   # name-drops a real id (anchoring)
    "https://ror.org/04t3en479/extra",
])
def test_invalid_ror_is_rejected(ror):
    with pytest.raises(ValueError, match="not a valid ROR"):
        normalize_ror(ror)


def test_ror_url_property():
    assert AuthorAffiliation(name="KIT", ror="https://ror.org/04t3en479").ror_url == \
        "https://ror.org/04t3en479"


# -- validation -------------------------------------------------------------------------------

@pytest.mark.parametrize("authors", [None, []])
def test_validate_authors_requires_one(authors):
    with pytest.raises(ValueError, match="requires at least one author"):
        validate_authors(authors)


def test_validate_authors_returns_a_copy():
    authors = [_author()]
    out = validate_authors(authors)
    assert out == authors and out is not authors


@pytest.mark.parametrize("overrides,message", [
    ({"name": "  "}, "missing a name"),
    ({"orcid": ""}, "missing an ORCID"),
    ({"orcid": "   "}, "missing an ORCID"),
    ({"affiliations": []}, "has no affiliation"),
    ({"affiliations": [AuthorAffiliation(name="", ror="04t3en479")]}, "missing a name"),
    ({"affiliations": [AuthorAffiliation(name="KIT", ror="")]}, "missing a ROR id"),
    ({"affiliations": [AuthorAffiliation(name="KIT", ror="04t3en478")]}, "not a valid ROR"),
    ({"orcid": "0000-0002-1825-0098"}, "not a valid ORCID"),
    ({"email": None}, "needs a contact e-mail"),
    ({"email": "  "}, "needs a contact e-mail"),
    ({"email": "ada.example.org"}, "needs a contact e-mail"),
    ({"email": "ada@"}, "needs a contact e-mail"),
    ({"email": "a da@example.org"}, "needs a contact e-mail"),
])
def test_validate_authors_messages(overrides, message):
    with pytest.raises(ValueError, match=message):
        validate_authors([_author(**overrides)])


def test_missing_affiliation_is_reported_before_a_bad_orcid():
    with pytest.raises(ValueError, match="has no affiliation"):
        validate_authors([_author(orcid="banana", affiliations=[])])


def test_affiliation_error_names_author_and_affiliation():
    with pytest.raises(ValueError, match="'KIT' of author 'Ada Lovelace'"):
        validate_authors([_author(affiliations=[AuthorAffiliation(name="KIT", ror=" ")])])


def test_the_privacy_placeholder_email_is_accepted():
    assert validate_authors([_author(email="no-reply@eln.invalid")])


def test_every_author_is_validated():
    with pytest.raises(ValueError, match="Bad"):
        validate_authors([_author(), _author(name="Bad", affiliations=[])])


def test_the_authorship_notice_is_given_once(monkeypatch, caplog):
    import chemotion_crate.authors as authors_module
    monkeypatch.setattr(authors_module, "_told_authorship", False)
    with caplog.at_level("WARNING", logger="chemotion_crate.authors"):
        validate_authors([_author()])
        validate_authors([_author()])
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "authors of the crate and of every element" in message and "Ada Lovelace" in message
