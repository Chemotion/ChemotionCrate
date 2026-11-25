"""The export manifest: what a partial crate left out, stated inside the crate itself.

Without it a partial crate is indistinguishable from a complete one: the root description reads
the same either way, and a reader has no way to learn that four wellplates never made it in.
"""
from __future__ import annotations

#: ``@id`` of the node declaring what the export left out.
MANIFEST_ID = "#export-manifest"

#: Human wording for each omission *stage*, so the crate explains the omission in stable terms
#: rather than echoing a raw exception (which is unstable across runs and, for a failed download,
#: carries the server's hostname).
OMISSION_REASONS = {
    "download": "its file could not be downloaded from the server",
    "absent": "the server has no file content for it",
    "to_dcat_ap": "it could not be mapped to chem-dcat-ap, so it is described generically",
    "retrieve": "it could not be retrieved from the server",
    "load": "it could not be loaded",
    "metadata": "the source did not provide this metadata",
    "withheld": "the element is shared below full detail level, so the server withheld it",
    # Notes (NOTE_STAGES): the item is in the crate; the export changed or lacks something about it.
    "path": "its name collides with another element's, so it is stored at a path ending in ~2, ~3, …",
    "index": "it appears more than once in the collection tree, so it is exported once",
    "keyword": "a comma inside it was replaced by ';' (keywords are one comma-separated string)",
}

#: Stages that record a note, not an omission: nothing is missing from the crate because of them,
#: so they neither make it read as incomplete nor fail a ``strict`` export.
NOTE_STAGES = frozenset({"metadata", "path", "index", "keyword"})


def is_note(entry: dict) -> bool:
    """True when ``entry`` (a ``skipped`` record) is a note rather than an omission."""
    return entry.get("stage") in NOTE_STAGES


#: Vocabularies whose term labels a crate can carry, licensed CC BY 4.0 (licences as recorded by
#: the OBO Foundry registry, checked 2026-11-23). Their labels are credited in the manifest.
#: CC0 vocabularies (CHEMINF, RO) need no credit; others are not named without a known licence.
CC_BY_VOCABULARIES = {"BFO": "BFO", "CHEBI": "ChEBI", "CHMO": "CHMO", "NCIT": "NCIt",
                      "OBI": "OBI", "PROCO": "PROCO", "RXNO": "RXNO"}
CC_BY_URL = "https://creativecommons.org/licenses/by/4.0/"


def vocabulary_attribution(graph: list) -> str | None:
    """One sentence crediting the CC BY vocabularies whose terms the graph uses, or ``None``."""
    used = set()
    for node in graph:
        if not isinstance(node, dict) or node.get("@type") != "DefinedTerm":
            continue
        term = str(node.get("@id", ""))
        prefix = (term.rsplit("/", 1)[-1].split("_", 1)[0] if term.startswith("http")
                  else term.split(":", 1)[0])
        if prefix.upper() in CC_BY_VOCABULARIES:
            used.add(CC_BY_VOCABULARIES[prefix.upper()])
    if not used:
        return None
    names = sorted(used, key=str.casefold)
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return (f"Term labels from {listed} are used under CC BY 4.0 ({CC_BY_URL}); "
            "each term links to its source.")


def build_skipped_manifest(skipped: list[dict], attribution: str | None = None) -> list[dict]:
    """Describe what the export left out, as ordinary RO-Crate entities.

    Deliberately plain: a ``CreativeWork`` carrying prose, plus one ``PropertyValue`` per
    ``(kind, stage)`` under ``variableMeasured``, which is the format's own mechanism for
    structured extras. No new vocabulary, and nothing a consumer has to special-case.

    Raw exception text is **not** included: it is unstable across runs and, for a failed
    download, embeds the server's hostname, which would defeat withholding the host.

    :param skipped: the export's omission records (``kind``, ``stage``, optionally ``count``).
    :param attribution: a sentence crediting the vocabularies used (:func:`vocabulary_attribution`).
    :return: the nodes to add to ``@graph``; empty when nothing was omitted (an empty manifest
        would be noise).
    """
    if not skipped and not attribution:
        return []

    counts: dict[tuple[str, str], int] = {}
    for entry in skipped:
        key = (str(entry.get("kind", "item")), str(entry.get("stage", "")))
        counts[key] = counts.get(key, 0) + int(entry.get("count", 1) or 1)

    measured: list[dict] = []
    omitted: list[str] = []
    noted: list[str] = []
    for index, ((kind, stage), count) in enumerate(sorted(counts.items()), start=1):
        note = stage in NOTE_STAGES
        reason = OMISSION_REASONS.get(stage, "it could not be included")
        (noted if note else omitted).append(f"{count} × {kind}: {reason}")
        measured.append({
            "@id": f"{MANIFEST_ID}-{index}",
            "@type": "PropertyValue",
            "propertyID": f"chemotion:{'note' if note else 'omitted'}/{kind}",
            "name": f"{kind} {'noted' if note else 'omitted'}",
            "value": str(count),
            "description": reason,
        })

    parts = []
    if omitted:
        parts.append("This crate is incomplete. The following were omitted: " + "; ".join(omitted) + ".")
    if noted:
        parts.append(("Notes: " if omitted else "Nothing was omitted. Notes: ") + "; ".join(noted) + ".")
    if not omitted and not noted:
        parts.append("Nothing was omitted.")
    if attribution:
        parts.append(attribution)
        measured.append({
            "@id": f"{MANIFEST_ID}-attribution",
            "@type": "PropertyValue",
            "propertyID": "chemotion:attribution",
            "name": "vocabulary attribution",
            "value": attribution,
            "url": CC_BY_URL,
        })
    manifest = {
        "@id": MANIFEST_ID,
        "@type": "CreativeWork",
        "name": "Export manifest",
        "description": " ".join(parts),
        "variableMeasured": [{"@id": node["@id"]} for node in measured],
    }
    return [manifest, *measured]
