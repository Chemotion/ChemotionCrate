"""Archive handling against hostile input: decompression bombs, oversized members, decoy
descriptors, symlinks out of an extracted crate, and malformed or truncated archives.

The common thread: every code path that reads an archive member needs the guard, not just one.
"""
from __future__ import annotations

import json
import zipfile

import pytest

import chemotion_crate.reader as reader_mod
from chemotion_crate.patch import patch_eln_ontology
from chemotion_crate.producer import PRODUCER_ID, PRODUCER_NAME
from chemotion_crate.reader import (
    MAX_COMPRESSION_RATIO,
    MAX_ZIP_MEMBER_SIZE,
    ElnCrate,
    ElnDataset,
    read,
)


def _metadata(name="C", extra=()):
    return {
        "@context": "https://w3id.org/ro/crate/1.2/context",
        "@graph": [
            {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"}},
            {"@id": "./", "@type": "Dataset", "name": name,
             "hasPart": [{"@id": "big.bin"}]},
            {"@id": "big.bin", "@type": "File", "name": "big.bin"},
            {"@id": "#ro-crate_created", "@type": "CreateAction", "object": {"@id": "./"},
             "instrument": {"@id": PRODUCER_ID}},
            {"@id": PRODUCER_ID, "@type": "SoftwareApplication", "name": PRODUCER_NAME},
            *extra,
        ],
    }


def test_limits_are_the_documented_values():
    assert MAX_ZIP_MEMBER_SIZE == 500 * 1024 * 1024
    assert MAX_COMPRESSION_RATIO == 200


def test_patch_shares_the_reader_limits():
    import chemotion_crate.patch as patch_mod
    assert patch_mod.MAX_ZIP_MEMBER_SIZE is MAX_ZIP_MEMBER_SIZE
    assert patch_mod.MAX_COMPRESSION_RATIO is MAX_COMPRESSION_RATIO


# --- decompression bombs -----------------------------------------------------------

def _bomb_bytes(size=16 * 1024 * 1024):
    return b"\0" * size                         # deflates ~1000x


def test_reader_refuses_a_bomb_payload(tmp_path):
    path = tmp_path / "bomb.eln"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("c.eln/ro-crate-metadata.json", json.dumps(_metadata()))
        z.writestr("c.eln/big.bin", _bomb_bytes())
    with read(path) as crate:
        with pytest.raises(ValueError, match="above the .* limit"):
            crate.files[0].read()


def test_reader_refuses_a_bomb_in_the_metadata_before_parsing(tmp_path):
    path = tmp_path / "bomb.eln"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("c.eln/ro-crate-metadata.json",
                   json.dumps({"@graph": [{"@id": "x", "pad": "A" * (16 * 1024 * 1024)}]}))
    with pytest.raises(ValueError, match="above the .* limit"):
        read(path)


def test_reader_zip_member_size_cap(tmp_path, monkeypatch):
    path = tmp_path / "big.eln"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("c.eln/ro-crate-metadata.json", json.dumps(_metadata()))
        z.writestr("c.eln/big.bin", b"x" * 4096)
    monkeypatch.setattr(reader_mod, "MAX_ZIP_MEMBER_SIZE", 1024)
    with read(path) as crate:
        with pytest.raises(ValueError, match="exceeding the"):
            crate.read_file("big.bin")


def test_ontology_patch_refuses_a_bomb_in_the_metadata(tmp_path):
    path = tmp_path / "bomb.eln"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("crate/ro-crate-metadata.json",
                   json.dumps({"@graph": [{"@id": "x", "pad": "A" * (16 * 1024 * 1024)}]}))
    with pytest.raises(ValueError, match="above the .* limit|exceeding the"):
        patch_eln_ontology(str(path), {"sample": "http://example.org/S"},
                           output_path=str(tmp_path / "out.eln"))
    assert not (tmp_path / "out.eln").exists()


def test_ontology_patch_refuses_a_bomb_payload_and_cleans_up(tmp_path):
    """Copying is streamed, but the bytes still land in a temp file: checked before copying."""
    path = tmp_path / "bomb.eln"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("crate/ro-crate-metadata.json", json.dumps({"@graph": []}))
        z.writestr("crate/big.bin", _bomb_bytes())
    out_dir = tmp_path / "out"
    with pytest.raises(ValueError, match="above the .* limit"):
        patch_eln_ontology(path, {"sample": "http://example.org/S"},
                           output_path=out_dir / "out.eln")
    assert list(out_dir.iterdir()) == []       # no temp file left behind


# --- extracted directories ---------------------------------------------------------

def test_crate_directory_read_is_size_capped(tmp_path, monkeypatch):
    crate = tmp_path / "crate"
    crate.mkdir()
    (crate / "ro-crate-metadata.json").write_text(json.dumps(_metadata()))
    (crate / "big.bin").write_bytes(b"x" * 4096)

    monkeypatch.setattr(reader_mod, "MAX_ZIP_MEMBER_SIZE", 1024 * 1024)
    with ElnCrate(crate) as opened:
        monkeypatch.setattr(reader_mod, "MAX_ZIP_MEMBER_SIZE", 1024)
        with pytest.raises(ValueError, match="exceeding the"):
            opened.read_file("big.bin")


def test_crate_directory_does_not_follow_symlinks_out_of_the_crate(tmp_path):
    """rglob can descend through symlinked directories, so a crate could enumerate (and then
    hand a caller the bytes of) files outside itself."""
    secret_dir = tmp_path / "outside"
    secret_dir.mkdir()
    (secret_dir / "id_rsa").write_text("PRIVATE KEY")

    crate = tmp_path / "crate"
    crate.mkdir()
    (crate / "ro-crate-metadata.json").write_text(json.dumps(_metadata(extra=[
        {"@id": "payload/id_rsa", "@type": "File"}])))
    try:
        (crate / "payload").symlink_to(secret_dir, target_is_directory=True)
        (crate / "link.txt").symlink_to(secret_dir / "id_rsa")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")

    with ElnCrate(crate) as opened:
        assert not any("id_rsa" in m or m == "link.txt" for m in opened._members)
        assert not opened.has_file("payload/id_rsa")
        with pytest.raises(FileNotFoundError):
            opened.read_file("payload/id_rsa")
        with pytest.raises(FileNotFoundError):
            opened.read_file("link.txt")


# --- descriptor selection ----------------------------------------------------------

def test_the_shallowest_metadata_wins_regardless_of_order(tmp_path):
    path = tmp_path / "decoy.eln"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("c.eln/payload/ro-crate-metadata.json", json.dumps(_metadata("DECOY")))
        z.writestr("c.eln/ro-crate-metadata.json", json.dumps(_metadata("REAL")))
    with read(path) as crate:
        assert crate.root.name == "REAL"


def test_a_suffix_match_is_not_the_metadata(tmp_path):
    path = tmp_path / "suffix.eln"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("evil-ro-crate-metadata.json", json.dumps(_metadata("DECOY")))
        z.writestr("c.eln/ro-crate-metadata.json", json.dumps(_metadata("REAL")))
    with read(path) as crate:
        assert crate.root.name == "REAL"

    only_decoy = tmp_path / "only.eln"
    with zipfile.ZipFile(only_decoy, "w") as z:
        z.writestr("c.eln/evil-ro-crate-metadata.json", json.dumps(_metadata()))
    with pytest.raises(ValueError, match="No ro-crate-metadata.json"):
        read(only_decoy)


def test_payload_ids_resolve_against_the_chosen_root(tmp_path):
    """Crate-relative @ids are relative to the descriptor's folder, not the archive root."""
    path = tmp_path / "rooted.eln"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("c.eln/ro-crate-metadata.json", json.dumps(_metadata()))
        z.writestr("c.eln/big.bin", b"inside")
        z.writestr("big.bin", b"outside")
    with read(path) as crate:
        assert crate.read_file("big.bin") == b"inside"


# --- malformed and truncated archives ----------------------------------------------

def _write_crate(tmp_path, metadata, name="mini.eln"):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mini.eln/ro-crate-metadata.json", json.dumps(metadata))
    return path


def test_metadata_that_is_a_list_is_refused_clearly(tmp_path):
    with pytest.raises(ValueError, match="not an object"):
        read(_write_crate(tmp_path, []))


def test_a_non_list_graph_reads_as_empty(tmp_path):
    with ElnCrate(_write_crate(tmp_path, {"@graph": "nonsense"})) as crate:
        assert crate.files == [] and crate.collections == []
        with pytest.raises(ValueError, match="descriptor"):
            crate.root  # noqa: B018


def test_truncated_zip_is_not_reported_as_not_an_archive(tmp_path):
    """A partial download must not be reported as "neither a ZIP nor a crate directory"."""
    whole = _write_crate(tmp_path, {"@graph": []}).read_bytes()
    truncated = tmp_path / "partial.eln"
    truncated.write_bytes(whole[: len(whole) // 2])

    with pytest.raises(ValueError, match="truncated or still downloading"):
        read(truncated)


def test_not_an_archive_at_all_keeps_its_own_message(tmp_path):
    plain = tmp_path / "notes.txt"
    plain.write_text("hello")
    with pytest.raises(ValueError, match="neither a .eln ZIP archive"):
        read(plain)


def test_missing_path_is_neither(tmp_path):
    with pytest.raises(ValueError, match="neither"):
        read(tmp_path / "does-not-exist.eln")


@pytest.mark.parametrize("suffix, expected", [
    ("Water_42", 42),
    ("Water_007", 7),
    ("Water_²", None),      # superscript two: isdigit() True, int() raises
    ("Water_٩", None),      # arabic-indic nine: isdecimal() True, int() returns a WRONG 9
    ("Water", None),
    ("Water_", None),
])
def test_source_id_accepts_only_ascii_digits(suffix, expected):
    ds = ElnDataset(None, {"@id": f"./samples/{suffix}/"})    # type: ignore[arg-type]
    assert ds.source_id == expected


def test_a_member_swapped_for_a_symlink_after_opening_is_refused(tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("PRIVATE")
    crate = tmp_path / "crate"
    crate.mkdir()
    (crate / "data.txt").write_text("payload")
    (crate / "ro-crate-metadata.json").write_text(json.dumps(_metadata(extra=[
        {"@id": "data.txt", "@type": "File"}])))
    with ElnCrate(crate) as opened:
        (crate / "data.txt").unlink()
        try:
            (crate / "data.txt").symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        with pytest.raises(ValueError, match="outside the crate"):
            opened.read_file("data.txt")
