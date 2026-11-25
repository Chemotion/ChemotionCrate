"""write_crate: staging, checksums, withdrawal, manifest, packaging. No server involved."""
from __future__ import annotations

import hashlib
import json
import stat
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from chemotion_crate.manifest import MANIFEST_ID
from chemotion_crate.model import AttachmentRecord
from chemotion_crate.producer import WITHHELD_HOST
from chemotion_crate.passes import replace_host as _replace_host
from chemotion_crate.writer import CrateFile, write_crate

SAMPLE_DIR = "./samples/Ethanol_12/"
EMPTY_DIR = "./reactions/R_1/"
SPECTRUM_ID = f"{SAMPLE_DIR}spectrum.jdx"
BROKEN_ID = f"{SAMPLE_DIR}big.dat"
HOST = "https://eln.example.org"


def _chunks(*parts: bytes):
    def read() -> Iterator[bytes]:
        yield from parts
    return read


def _dying(*parts: bytes):
    def read() -> Iterator[bytes]:
        yield from parts
        raise OSError("connection dropped mid-stream")
    return read


def _metadata(*file_ids: str) -> dict:
    """A minimal valid RO-Crate 1.2 graph: descriptor, root, two element Datasets, Files."""
    sample = {"@id": SAMPLE_DIR, "@type": "Dataset", "name": "Ethanol",
              "hasPart": [{"@id": fid} for fid in file_ids]}
    return {
        "@context": "https://w3id.org/ro/crate/1.2/context",
        "@graph": [
            {"@id": "ro-crate-metadata.json", "@type": "CreativeWork",
             "conformsTo": {"@id": "https://w3id.org/ro/crate/1.2"}, "about": {"@id": "./"}},
            {"@id": "./", "@type": "Dataset", "name": "Collection",
             "description": f"Exported from {HOST}/collection/1",
             "datePublished": "2026-11-15", "license": {"@id": "https://spdx.org/licenses/CC-BY-4.0"},
             "hasPart": [{"@id": SAMPLE_DIR}, {"@id": EMPTY_DIR}]},
            sample,
            {"@id": EMPTY_DIR, "@type": "Dataset", "name": "R-1"},
            *({"@id": fid, "@type": "File" if i == 0 else ["File"],  # both shapes of @type
               "name": fid.rsplit("/", 1)[-1], "contentSize": "1234"}
              for i, fid in enumerate(file_ids)),
        ],
    }


def _read_graph(path: Path) -> dict[str, dict]:
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.endswith("ro-crate-metadata.json"))
        return {n["@id"]: n for n in json.loads(z.read(name))["@graph"]}


def _leftovers(directory: Path, out: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p != out]


def test_checksum_and_size_are_restated_from_the_bytes_written(tmp_path):
    parts = (b"##TITLE=x\n", b"A" * 70_000, b"##END=\n")
    seen: list[str] = []
    out = tmp_path / "Ethanol export.eln"
    result = write_crate(out, _metadata(SPECTRUM_ID),
                         [CrateFile(SPECTRUM_ID, AttachmentRecord("spectrum.jdx",
                                                                  read=_chunks(*parts)))],
                         [SAMPLE_DIR, EMPTY_DIR], skipped=[], progress=seen.append)
    assert result == str(out)
    assert seen == ["spectrum.jdx"]
    data = b"".join(parts)
    node = _read_graph(out)[SPECTRUM_ID]
    assert node["sha256"] == hashlib.sha256(data).hexdigest()
    assert node["contentSize"] == str(len(data)), "the server's stale 1234 must be replaced"
    with zipfile.ZipFile(out) as z:
        assert z.read("Ethanol_export.eln/samples/Ethanol_12/spectrum.jdx") == data
    assert _leftovers(tmp_path, out) == [], "no staging dir or .part may remain"


def test_every_member_sits_under_one_root_folder_and_empty_dirs_survive(tmp_path):
    out = tmp_path / "crate.eln"
    write_crate(out, _metadata(SPECTRUM_ID),
                [CrateFile(SPECTRUM_ID, AttachmentRecord("spectrum.jdx", read=_chunks(b"x")))],
                [SAMPLE_DIR, EMPTY_DIR], skipped=[])
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    assert {n.split("/", 1)[0] for n in names} == {"crate.eln"}
    assert names[0] == "crate.eln/ro-crate-metadata.json"
    assert "crate.eln/reactions/R_1/" in names, "an element without files keeps its directory"
    assert "crate.eln/samples/Ethanol_12/" in names


def test_directory_entries_can_be_entered_after_unzip(tmp_path):
    # `unzip` applies the recorded mode; a folder without the execute bit hides its files.
    out = tmp_path / "crate.eln"
    write_crate(out, _metadata(SPECTRUM_ID),
                [CrateFile(SPECTRUM_ID, AttachmentRecord("spectrum.jdx", read=_chunks(b"x")))],
                [SAMPLE_DIR, EMPTY_DIR], skipped=[])
    with zipfile.ZipFile(out) as z:
        dirs = [i for i in z.infolist() if i.is_dir()]
    assert dirs
    for info in dirs:
        mode = info.external_attr >> 16
        assert stat.S_ISDIR(mode), info.filename
        assert mode & 0o755 == 0o755, f"{info.filename}: {oct(mode)}"


def test_root_folder_falls_back_when_the_name_is_empty(tmp_path):
    out = tmp_path / ".eln"
    write_crate(out, _metadata(), [], [SAMPLE_DIR, EMPTY_DIR], skipped=[])
    with zipfile.ZipFile(out) as z:
        assert {n.split("/", 1)[0] for n in z.namelist()} == {"export.eln"}


def test_a_partly_downloaded_attachment_is_withdrawn(tmp_path, caplog):
    """A stream that dies mid-file must not leave a File node claiming a complete file."""
    out = tmp_path / "partial.eln"
    skipped: list[dict] = []
    files = [CrateFile(SPECTRUM_ID, AttachmentRecord("spectrum.jdx", read=_chunks(b"ok"))),
             CrateFile(BROKEN_ID, AttachmentRecord("big.dat", read=_dying(b"A" * 100)))]
    write_crate(out, _metadata(SPECTRUM_ID, BROKEN_ID), files, [SAMPLE_DIR, EMPTY_DIR],
                skipped=skipped)
    graph = _read_graph(out)
    assert BROKEN_ID not in graph
    assert graph[SAMPLE_DIR]["hasPart"] == [{"@id": SPECTRUM_ID}]
    with zipfile.ZipFile(out) as z:
        assert not any(n.endswith("big.dat") for n in z.namelist()), "partial bytes are deleted"
    assert skipped == [{"kind": "attachment", "stage": "download", "filename": "big.dat",
                        "error": "connection dropped mid-stream"}]
    assert "Failed to download attachment big.dat" in caplog.text


def test_a_record_without_bytes_is_declared_absent(tmp_path):
    out = tmp_path / "nobytes.eln"
    skipped: list[dict] = []
    write_crate(out, _metadata(SPECTRUM_ID),
                [CrateFile(SPECTRUM_ID, AttachmentRecord("spectrum.jdx", read=None))],
                [SAMPLE_DIR, EMPTY_DIR], skipped=skipped)
    graph = _read_graph(out)
    assert SPECTRUM_ID not in graph
    assert "hasPart" not in graph[SAMPLE_DIR], "an empty hasPart is an empty value: removed"
    with zipfile.ZipFile(out) as z:
        assert "nobytes.eln/samples/Ethanol_12/" in z.namelist(), \
            "the element directory stays even when its only file was withdrawn"
    assert [s["stage"] for s in skipped] == ["absent"], "no bytes upstream is not a failed download"


def test_a_404_is_absent_and_a_dead_stream_is_a_failed_download(tmp_path):
    class NotFound(OSError):
        status_code = 404

    def _gone():
        raise NotFound("404 Not Found")
        yield b""  # pragma: no cover

    skipped: list[dict] = []
    write_crate(tmp_path / "x.eln", _metadata(SPECTRUM_ID),
                [CrateFile(SPECTRUM_ID, AttachmentRecord("spectrum.jdx", read=_gone))],
                [SAMPLE_DIR, EMPTY_DIR], skipped=skipped)
    assert [s["stage"] for s in skipped] == ["absent"]
    skipped = []
    write_crate(tmp_path / "y.eln", _metadata(SPECTRUM_ID),
                [CrateFile(SPECTRUM_ID, AttachmentRecord("spectrum.jdx", read=_dying(b"ab")))],
                [SAMPLE_DIR, EMPTY_DIR], skipped=skipped)
    assert [s["stage"] for s in skipped] == ["download"]


def test_strict_mode_leaves_nothing_behind(tmp_path):
    out = tmp_path / "strict.eln"
    files = [CrateFile(BROKEN_ID, AttachmentRecord("big.dat", read=_dying(b"A" * 100)))]
    with pytest.raises(RuntimeError, match="strict mode"):
        write_crate(out, _metadata(BROKEN_ID), files, [SAMPLE_DIR], skipped=[], strict=True)
    assert list(tmp_path.iterdir()) == [], "no crate, no .part, no staging directory"


def test_skipped_items_are_declared_and_mentioned_by_the_root(tmp_path):
    out = tmp_path / "manifest.eln"
    skipped = [{"kind": "wellplate", "stage": "retrieve", "count": 4}]
    files = [CrateFile(BROKEN_ID, AttachmentRecord("big.dat", read=_dying()))]
    write_crate(out, _metadata(BROKEN_ID), files, [SAMPLE_DIR, EMPTY_DIR], skipped=skipped)
    graph = _read_graph(out)
    manifest = graph[MANIFEST_ID]
    measured = manifest["variableMeasured"]
    assert {r["@id"] for r in (measured if isinstance(measured, list) else [measured])} <= graph.keys()
    assert "4 × wellplate" in manifest["description"]
    assert "1 × attachment" in manifest["description"], "download failures reach it too"
    assert graph["./"]["mentions"] == {"@id": MANIFEST_ID}, "a single mention is unpacked"


def test_declare_skipped_false_leaves_the_crate_bare(tmp_path):
    out = tmp_path / "bare.eln"
    write_crate(out, _metadata(), [], [SAMPLE_DIR, EMPTY_DIR],
                skipped=[{"kind": "sample", "stage": "load"}], declare_skipped=False)
    graph = _read_graph(out)
    assert MANIFEST_ID not in graph
    assert "mentions" not in graph["./"]


def test_a_complete_export_has_no_manifest(tmp_path):
    out = tmp_path / "complete.eln"
    write_crate(out, _metadata(), [], [SAMPLE_DIR, EMPTY_DIR], skipped=[])
    assert MANIFEST_ID not in _read_graph(out)


def test_mask_host_scrubs_the_graph_including_the_manifest(tmp_path):
    out = tmp_path / "masked.eln"
    skipped = [{"kind": "attachment", "stage": "download", "error": f"{HOST} unreachable"}]
    write_crate(out, _metadata(), [], [SAMPLE_DIR, EMPTY_DIR], skipped=skipped, mask_host=HOST)
    with zipfile.ZipFile(out) as z:
        text = z.read("masked.eln/ro-crate-metadata.json").decode()
    assert "eln.example.org" not in text
    assert f"{WITHHELD_HOST}/collection/1" in text
    assert MANIFEST_ID in text


def test_replace_host_matches_with_and_without_scheme():
    md = {"@graph": [{"@id": f"{HOST}/api/v1/samples/1", "note": ["seen on eln.example.org"]}]}
    assert _replace_host(md, HOST, WITHHELD_HOST) == 2
    assert md["@graph"][0]["@id"] == f"{WITHHELD_HOST}/api/v1/samples/1"
    assert md["@graph"][0]["note"] == ["seen on withheld.chemotion.invalid"]


def test_type_lists_are_sorted(tmp_path):
    out = tmp_path / "types.eln"
    md = _metadata()
    md["@graph"][3]["@type"] = ["Dataset", "BioChemEntity"]
    write_crate(out, md, [], [SAMPLE_DIR, EMPTY_DIR], skipped=[])
    assert _read_graph(out)[EMPTY_DIR]["@type"] == ["BioChemEntity", "Dataset"]


def test_validate_passes_a_minimal_valid_crate(tmp_path):
    out = tmp_path / "valid.eln"
    write_crate(out, _metadata(SPECTRUM_ID),
                [CrateFile(SPECTRUM_ID, AttachmentRecord("spectrum.jdx", read=_chunks(b"x")))],
                [SAMPLE_DIR, EMPTY_DIR], skipped=[], validate=True)
    assert out.exists()


def test_validate_rejects_an_invalid_graph_and_leaves_nothing(tmp_path):
    out = tmp_path / "invalid.eln"
    md = _metadata()
    md["@graph"][2]["author"] = {"name": "an embedded object without @id"}
    with pytest.raises(RuntimeError, match="not valid RO-Crate"):
        write_crate(out, md, [], [SAMPLE_DIR, EMPTY_DIR], skipped=[])
    assert list(tmp_path.iterdir()) == []
    # and the same graph is written when validation is off
    write_crate(out, md, [], [SAMPLE_DIR, EMPTY_DIR],
                skipped=[], validate=False)
    assert out.exists()


def test_an_existing_part_file_is_never_overwritten(tmp_path):
    out = tmp_path / "busy.eln"
    part = tmp_path / "busy.eln.part"
    part.write_bytes(b"someone else's")
    with pytest.raises(FileExistsError):
        write_crate(out, _metadata(), [], [SAMPLE_DIR], skipped=[])
    assert part.read_bytes() == b"someone else's"
    assert not out.exists()


def test_a_crate_id_escaping_the_crate_is_refused(tmp_path):
    out = tmp_path / "escape.eln"
    files = [CrateFile("./../../evil.txt", AttachmentRecord("evil.txt", read=_chunks(b"x")))]
    with pytest.raises(ValueError, match="inside the crate"):
        write_crate(out, _metadata(), files, [SAMPLE_DIR], skipped=[])
    assert list(tmp_path.iterdir()) == []


def test_json_default_uses_to_dict_only_when_json_native(tmp_path):
    class Wrapped:
        def to_dict(self):
            return {"a": 1}

    class Recursive:
        def to_dict(self):
            return Recursive()

        def __str__(self):
            return "recursive"

    out = tmp_path / "default.eln"
    md = _metadata()
    md["@graph"][3]["wrapped"] = Wrapped()
    md["@graph"][3]["weird"] = Recursive()
    write_crate(out, md, [], [SAMPLE_DIR, EMPTY_DIR], skipped=[], validate=False)
    node = _read_graph(out)[EMPTY_DIR]
    assert node["wrapped"] == {"a": 1}
    assert node["weird"] == "recursive"


def test_two_writes_of_the_same_crate_differ_in_nothing_but_the_root_folder(tmp_path):
    # Member timestamps are fixed, so the archive does not carry the staging files' mtimes.
    import time

    from chemotion_crate.writer import ZIP_DATE_TIME
    outs = []
    for name in ("a.eln", "b.eln"):
        out = tmp_path / name
        write_crate(out, _metadata(SPECTRUM_ID),
                    [CrateFile(SPECTRUM_ID, AttachmentRecord("spectrum.jdx", read=_chunks(b"x")))],
                    [SAMPLE_DIR, EMPTY_DIR], skipped=[])
        outs.append(out)
        time.sleep(2.1)  # past the ZIP format's 2-second time resolution
    with zipfile.ZipFile(outs[0]) as z:
        assert all(i.date_time == ZIP_DATE_TIME for i in z.infolist())
        root = z.namelist()[0].split("/", 1)[0]
    with zipfile.ZipFile(outs[1]) as z:
        assert z.namelist()[0].split("/", 1)[0] != root  # the root folder follows the file name
    a, b = (zipfile.ZipFile(o) for o in outs)
    assert [(i.date_time, i.CRC, i.file_size) for i in a.infolist()] == \
        [(i.date_time, i.CRC, i.file_size) for i in b.infolist()]


def test_a_broken_graph_fails_before_any_download(tmp_path):
    calls = []

    def _read():
        calls.append(1)
        yield b"x"

    metadata = _metadata(SPECTRUM_ID)
    metadata["@graph"].append({"name": "an entity without an @id"})
    with pytest.raises(RuntimeError, match="not valid RO-Crate"):
        write_crate(tmp_path / "bad.eln", metadata,
                    [CrateFile(SPECTRUM_ID, AttachmentRecord("spectrum.jdx", read=_read))],
                    [SAMPLE_DIR, EMPTY_DIR], skipped=[])
    assert calls == [], "nothing was downloaded"
