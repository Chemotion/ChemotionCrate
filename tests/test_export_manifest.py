"""The export manifest: a partial crate says what it left out."""
from __future__ import annotations

import json

from chemotion_crate.manifest import MANIFEST_ID, OMISSION_REASONS, build_skipped_manifest


def test_a_complete_export_declares_nothing():
    """No manifest when nothing was omitted: an empty one would be noise."""
    assert build_skipped_manifest([]) == []


def test_omissions_are_counted_per_kind_and_stage():
    nodes = build_skipped_manifest([
        {"kind": "attachment", "stage": "download", "filename": "a.jdx"},
        {"kind": "attachment", "stage": "download", "filename": "b.jdx"},
        {"kind": "wellplate", "stage": "retrieve", "count": 4},
    ])
    manifest, *measured = nodes
    assert manifest["@id"] == MANIFEST_ID
    assert manifest["@type"] == "CreativeWork"
    assert manifest["variableMeasured"] == [{"@id": n["@id"]} for n in measured]
    values = {n["propertyID"]: n["value"] for n in measured}
    assert values == {"chemotion:omitted/attachment": "2", "chemotion:omitted/wellplate": "4"}
    assert all(n["@type"] == "PropertyValue" for n in measured)
    assert "2 × attachment" in manifest["description"]
    assert "4 × wellplate" in manifest["description"]
    assert OMISSION_REASONS["download"] in manifest["description"]


def test_ids_are_stable_and_ordered():
    entries = [{"kind": "b", "stage": "load"}, {"kind": "a", "stage": "load"}]
    first = build_skipped_manifest(entries)
    assert first == build_skipped_manifest(list(reversed(entries)))
    assert [n["@id"] for n in first[1:]] == [f"{MANIFEST_ID}-1", f"{MANIFEST_ID}-2"]
    assert first[1]["propertyID"] == "chemotion:omitted/a"


def test_unknown_stage_still_states_a_reason():
    nodes = build_skipped_manifest([{"kind": "thing", "stage": "mystery"}])
    assert nodes[1]["description"] == "it could not be included"


def test_the_manifest_never_carries_raw_error_text():
    """Exception text is unstable and embeds the server hostname on a failed download.

    Declaring omissions must not become a channel that defeats withholding the host.
    """
    nodes = build_skipped_manifest([
        {"kind": "attachment", "stage": "download", "filename": "x.jdx",
         "error": "HTTPSConnectionPool(host='eln.internal.example.org', port=443): Max retries"},
    ])
    rendered = json.dumps(nodes)
    assert "eln.internal.example.org" not in rendered
    assert "HTTPSConnectionPool" not in rendered
    assert "could not be downloaded" in rendered


def test_notes_do_not_make_a_crate_read_incomplete():
    (manifest, *measured) = build_skipped_manifest([
        {"kind": "sample", "stage": "path", "id": 3},
        {"kind": "collection", "stage": "index", "id": 7},
        {"kind": "keyword", "stage": "keyword", "id": 3, "count": 2},
    ])
    assert manifest["description"].startswith("Nothing was omitted. Notes: ")
    assert "incomplete" not in manifest["description"]
    assert "2 × keyword: a comma inside it was replaced by ';'" in manifest["description"]
    assert {m["propertyID"] for m in measured} == {
        "chemotion:note/sample", "chemotion:note/collection", "chemotion:note/keyword"}


def test_omissions_and_notes_are_told_apart():
    manifest = build_skipped_manifest([
        {"kind": "attachment", "stage": "absent"},
        {"kind": "sample", "stage": "path", "id": 3},
    ])[0]
    assert manifest["description"].startswith("This crate is incomplete. The following were omitted: "
                                              "1 × attachment: the server has no file content for it.")
    assert "Notes: 1 × sample: its name collides" in manifest["description"]


def test_a_note_does_not_fail_a_strict_export():
    from chemotion_crate.manifest import is_note
    assert all(is_note({"stage": s}) for s in ("metadata", "path", "index", "keyword"))
    assert not any(is_note({"stage": s}) for s in ("download", "absent", "load", "retrieve",
                                                     "withheld", "to_dcat_ap"))


def test_the_cc_by_vocabularies_used_are_credited_in_one_line():
    from chemotion_crate.manifest import vocabulary_attribution
    graph = [
        {"@id": "CHMO:0000595", "@type": "DefinedTerm", "name": "1H NMR"},
        {"@id": "http://purl.obolibrary.org/obo/RXNO_0000024", "@type": "DefinedTerm"},
        {"@id": "CHEMINF:000113", "@type": "DefinedTerm"},  # CC0: no credit needed
        {"@id": "SIO:000008", "@type": "DefinedTerm"},  # licence not on record: not named
        {"@id": "CHMO:0000593", "@type": "DefinedTerm"},
    ]
    assert vocabulary_attribution(graph) == (
        "Term labels from CHMO and RXNO are used under CC BY 4.0 "
        "(https://creativecommons.org/licenses/by/4.0/); each term links to its source.")
    assert vocabulary_attribution([{"@id": "CHEMINF:000113", "@type": "DefinedTerm"}]) is None


def test_a_crate_with_nothing_omitted_still_carries_the_attribution():
    (manifest, attribution) = build_skipped_manifest([], "Term labels from CHMO are used under CC BY 4.0.")
    assert manifest["description"] == "Nothing was omitted. Term labels from CHMO are used under CC BY 4.0."
    assert attribution["propertyID"] == "chemotion:attribution"
    assert attribution["url"] == "https://creativecommons.org/licenses/by/4.0/"
