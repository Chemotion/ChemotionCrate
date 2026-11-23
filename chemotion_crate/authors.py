"""Explicit authorship for an exported crate.

Every exported ``.eln`` must declare its author(s): the caller sets them rather than the export
defaulting to the logged-in user. Each :class:`Author` carries an ORCID iD and one or more
:class:`AuthorAffiliation`, each with a ROR (Research Organization Registry) id.

Identifiers are validated with ``idutils`` (ORCID syntax and ISO 7064 check digit, ROR syntax);
the ROR check digits, which ``idutils`` does not verify, are checked here. Both are rebuilt from
their validated bare form, so only ``orcid.org`` and ``ror.org`` can ever be named in a crate.

Example::

    from chemotion_crate import Author, AuthorAffiliation
    authors = [
        Author(
            name="Ada Lovelace",
            orcid="0000-0002-1825-0097",
            affiliations=[AuthorAffiliation(name="KIT", ror="https://ror.org/04t3en479")],
        ),
    ]
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import idutils

logger = logging.getLogger(__name__)

#: Set once the authorship notice has been given (once per process).
_told_authorship = False

#: Crockford base32 as ROR uses it (no i, l, o, u); the alphabet the check digits are computed over.
_ROR_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"
_ROR_BARE_RE = re.compile(r"^0[0-9a-hj-km-np-tv-z]{6}\d{2}$")


def _bare_id(value: str, host: str) -> str:
    """Strip an optional ``https://<host>/`` prefix, returning the bare identifier.

    The prefix is matched **anchored**: ``https://evil.example/orcid.org/0000-0002-1825-0097``
    keeps its full text and therefore fails validation, rather than being quietly reduced to
    the real iD it name-drops.
    """
    text = value.strip().rstrip("/")
    return re.sub(rf"^(?:https?://)?(?:www\.)?{re.escape(host)}/", "", text, flags=re.IGNORECASE)


def _ror_checksum_valid(ror: str) -> bool:
    """Verify a ROR id's ISO 7064 MOD 97-10 check digits.

    A mistyped ROR usually still has the right shape, and would then name the wrong institution
    as the crate's publisher.
    """
    number = 0
    for char in ror[:-2]:
        number = number * 32 + _ROR_ALPHABET.index(char)
    return f"{98 - ((number * 100) % 97):02d}" == ror[-2:]


def normalize_orcid(orcid: str) -> str:
    """Return ``orcid`` as a full ``https://orcid.org/...`` URL.

    :raises ValueError: if it is not a syntactically valid ORCID iD with a correct check digit.
    """
    bare = _bare_id(orcid, "orcid.org")
    if not idutils.is_orcid(bare) or "/" in bare:
        raise ValueError(
            f"{orcid!r} is not a valid ORCID iD. Expected 0000-0002-1825-0097 (four groups of "
            "four, last digit may be X); check for a typo, the final character is a checksum.")
    # ``idutils`` keeps the case of an ``x`` check digit; one person must get one @id.
    return f"https://orcid.org/{idutils.normalize_orcid(bare).upper()}"


def normalize_ror(ror: str) -> str:
    """Return ``ror`` as a full ``https://ror.org/...`` URL.

    :raises ValueError: if it is not a syntactically valid ROR id with correct check digits.
    """
    bare = _bare_id(ror, "ror.org").lower()
    if not _ROR_BARE_RE.match(bare) or not idutils.is_ror(bare) or not _ror_checksum_valid(bare):
        raise ValueError(
            f"{ror!r} is not a valid ROR id. Expected 05gq02987 ('0' then six characters then "
            "two check digits). Look it up at https://ror.org.")
    return f"https://ror.org/{bare}"


@dataclass
class AuthorAffiliation:
    """An author's affiliation, identified by a ROR id.

    :param name: human-readable organization name (e.g. ``"Karlsruhe Institute of Technology"``).
    :param ror: ROR id, either bare (``"04t3en479"``) or the full URL
        (``"https://ror.org/04t3en479"``). Look it up at https://ror.org.
    """
    name: str
    ror: str

    @property
    def ror_url(self) -> str:
        """The affiliation's ROR id as a full ``https://ror.org/...`` URL."""
        return normalize_ror(self.ror)

    def validate(self, author_name: str) -> None:
        """Raise ``ValueError`` if the affiliation is missing its name or ROR id."""
        if not self.name or not self.name.strip():
            raise ValueError(
                f"Affiliation of author '{author_name}' is missing a name. Provide "
                "AuthorAffiliation(name='Your Organization', ror='https://ror.org/<id>').")
        if not self.ror or not self.ror.strip():
            raise ValueError(
                f"Affiliation '{self.name}' of author '{author_name}' is missing a ROR id. "
                "Provide ror='https://ror.org/<id>' (find your organization's id at https://ror.org).")
        normalize_ror(self.ror)


@dataclass
class Author:
    """A crate author. Requires a name, an ORCID iD, at least one affiliation and an e-mail.

    The authors given to an export are recorded as the authors of the crate **and of every element
    in it**. Who created each element in the ELN is not exported.

    :param name: the author's full name.
    :param orcid: ORCID iD, bare (``"0000-0002-1825-0097"``) or full URL. See https://orcid.org.
    :param affiliations: one or more :class:`AuthorAffiliation` (each carrying a ROR id).
    :param email: contact e-mail, required: RO-Crate asks for an author with a contact point, and
        the crate's contact point is made from it. If privacy is a concern, pass the placeholder
        ``"no-reply@eln.invalid"``: the ``.invalid`` domain (RFC 2606) never resolves, so the
        address never delivers.
    """
    name: str
    orcid: str
    affiliations: list[AuthorAffiliation] = field(default_factory=list)
    email: str | None = None

    @property
    def orcid_url(self) -> str:
        """The author's ORCID iD as a full ``https://orcid.org/...`` URL."""
        return normalize_orcid(self.orcid)

    def validate(self) -> None:
        """Raise ``ValueError`` with actionable guidance if anything required is missing."""
        if not self.name or not self.name.strip():
            raise ValueError(
                "An author is missing a name. Provide "
                "Author(name='Full Name', orcid='0000-...', affiliations=[AuthorAffiliation(...)]).")
        if not self.orcid or not self.orcid.strip():
            raise ValueError(
                f"Author '{self.name}' is missing an ORCID iD. Provide "
                "orcid='https://orcid.org/0000-0002-1825-0097' (register or look it up at https://orcid.org).")
        if not self.affiliations:
            raise ValueError(
                f"Author '{self.name}' has no affiliation. Provide at least one "
                "AuthorAffiliation(name='Your Organization', ror='https://ror.org/<id>').")
        for aff in self.affiliations:
            aff.validate(self.name)
        # Checked last: a *missing* ORCID or affiliation is the more useful thing to report first.
        normalize_orcid(self.orcid)
        email = (self.email or "").strip()
        local, at, domain = email.partition("@")
        if not at or not local or not domain or "@" in domain or any(c.isspace() for c in email):
            raise ValueError(
                f"Author '{self.name}' needs a contact e-mail (got {self.email!r}). Provide "
                "email='name@example.org', or email='no-reply@eln.invalid' if privacy is a concern "
                "(the .invalid domain never delivers).")


def validate_authors(authors: list[Author] | None) -> list[Author]:
    """Validate the author list for an export; raise a helpful ``ValueError`` if unusable."""
    if not authors:
        raise ValueError(
            "export() requires at least one author; authorship is not taken from the logged-in "
            "user. For example:\n"
            "    from chemotion_crate import Author, AuthorAffiliation\n"
            "    authors = [Author(name='Ada Lovelace', orcid='0000-0002-1825-0097',\n"
            "                      affiliations=[AuthorAffiliation(name='KIT', ror='https://ror.org/04t3en479')],\n"
            "                      email='ada@example.org')]\n"
            "    export(instance, collection, 'out.eln', authors=authors)")
    for author in authors:
        author.validate()
    global _told_authorship
    if not _told_authorship:
        _told_authorship = True
        logger.warning(
            "The authors given (%s) are recorded as the authors of the crate and of every element "
            "in it; who created each element in the ELN is not exported. This notice is given once.",
            ", ".join(a.name for a in authors))
    return list(authors)
