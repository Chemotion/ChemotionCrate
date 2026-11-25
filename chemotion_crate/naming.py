"""Crate-safe names for elements and their files."""
from __future__ import annotations

import logging
import os
import re

from anyascii import anyascii

from chemotion_crate.model import AttachmentRecord

logger = logging.getLogger(__name__)

#: Set once the transliteration warning has been given (it is given once per process).
_warned_transliteration = False


#: Longest sanitised stem. Filesystems cap a path component at 255 bytes, and the element id or
#: a collision suffix still has to fit beside it.
MAX_STEM = 120


def sanitize_filename(name: str, max_length: int = MAX_STEM) -> str:
    """Reduce a name to the characters that are safe in a ZIP entry path and a crate ``@id``.

    Non-ASCII letters are transliterated first (``anyascii``: ``Benzoesäure`` → ``Benzoesaure``,
    ``Образец`` → ``Obrazets``, ``苯甲酸`` → ``BenJiaSuan``), so a name in any script stays
    readable in a path; everything else outside ``[A-Za-z0-9_-]`` becomes ``_``. Only the path and
    the ``@id`` change: the original name is kept in the entity's ``name``. The first
    transliteration in a process logs a warning saying so.
    """
    if not name:
        return "unnamed"
    if not name.isascii():
        folded = anyascii(name)
        if folded != name:
            _warn_transliteration(name, folded)
        name = folded
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", name)[:max_length]
    return safe if safe.strip("_") else "unnamed"


def _warn_transliteration(original: str, folded: str) -> None:
    global _warned_transliteration
    if _warned_transliteration:
        return
    _warned_transliteration = True
    logger.warning(
        "Names with non-ASCII characters are transliterated to ASCII in crate paths and ids "
        "(e.g. %r becomes %r); the original names are kept in each entity's 'name'. This warning "
        "is given once.", original, folded)


class CrateFileNamer:
    """Assign each of an element's attachments a unique name inside the crate.

    The name is computed once here and reused verbatim by every consumer (ZIP entry path, File
    ``@id``, ``hasPart`` refs). Attachments are deduplicated by **record identity**, not by
    filename: two analyses each holding ``spectrum.jdx`` are two different files, and a name
    collision is resolved with a numeric suffix rather than by dropping one.
    """

    def __init__(self) -> None:
        #: ``(record, name-inside-the-crate)`` pairs, in insertion order.
        self.files: list[tuple[AttachmentRecord, str]] = []
        self._claimed: dict[str, str] = {}
        self._seen: set = set()

    def add(self, att: AttachmentRecord) -> None:
        record_id = att.identifier or att.id
        if record_id is not None:
            if record_id in self._seen:
                return
            self._seen.add(record_id)
        name_part, ext_part = os.path.splitext(os.path.basename(att.filename))
        # The extension is sanitised too: ``spec.jdx#1`` would otherwise put a fragment
        # delimiter into the File @id.
        safe_name = sanitize_filename(name_part) + ("." + sanitize_filename(ext_part[1:], 20)
                                                    if ext_part else "")
        # Claims are compared case-insensitively: the files are staged on a local filesystem,
        # and on macOS or Windows ``IR.jdx`` and ``ir.jdx`` would overwrite each other.
        if safe_name.casefold() not in self._claimed:
            self._claimed[safe_name.casefold()] = att.filename
            self.files.append((att, safe_name))
            return
        if record_id is None and self._claimed.get(safe_name.casefold()) == att.filename:
            return  # nothing to tell two such records apart: treat as a duplicate
        base_name, ext = os.path.splitext(safe_name)
        n = 2
        while f"{base_name}_{n}{ext}".casefold() in self._claimed:
            n += 1
        candidate = f"{base_name}_{n}{ext}"
        self._claimed[candidate.casefold()] = att.filename
        self.files.append((att, candidate))

    def name_of(self, att: AttachmentRecord) -> str | None:
        """The crate name assigned to ``att``, or ``None`` if it was not added."""
        for record, name in self.files:
            if record.key == att.key:
                return name
        return None
