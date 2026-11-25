"""Reading a ``.eln`` RO-Crate back into Python objects (chemotion_crate.reader).

Every crate here is built by hand in ``tmp_path``: a metadata dict plus a payload file, laid out
the way the exporter writes them. No server, no network.
"""
from __future__ import annotations

import copy
import json
import zipfile

import pytest

from chemotion_crate.ontology_map import ELEMENT_TYPE_PROPERTY_ID
from chemotion_crate.producer import PRODUCER_ID, PRODUCER_NAME, WITHHELD_HOST
from chemotion_crate.reader import (
    ElnCollection,
    ElnCrate,
    ElnDataset,
    ElnFile,
    import_eln,
    read,
    safe_join,
)

ORCID = "https://orcid.org/0000-0002-1825-0097"
ROR = "https://ror.org/04t3en479"

SAMPLE = "./samples/Water_42/"
REACTION = "./reactions/R1_7/"
DEVICE = "./device_descriptions/Spectrometer_3/"
GENERIC = "./generics/MyType/Thing_5/"
SPECTRUM = "./samples/Water_42/spectrum.txt"

METADATA = {
    "@context": "https://w3id.org/ro/crate/1.2/context",
    "@graph": [
        {
            "@id": "ro-crate-metadata.json",
            "@type": "CreativeWork",
            "about": {"@id": "./"},
            "conformsTo": {"@id": "https://w3id.org/ro/crate/1.2"},
            "dateCreated": "2026-07-03T12:00:00",
            # sdPublisher names the publishing organization; the writing software is the
            # CreateAction's instrument, which is what identifies the producer.
            "sdPublisher": {"@id": "https://ror.org/05gq02987"},
        },
        {"@id": "https://ror.org/05gq02987", "@type": "Organization", "name": "Example Org"},
        {
            "@id": "./",
            "@type": "Dataset",
            "name": "My Project",
            "author": {"@id": ORCID},
            "isBasedOn": {"@id": WITHHELD_HOST},
            "hasPart": [{"@id": SAMPLE}, {"@id": REACTION}, {"@id": DEVICE}, {"@id": GENERIC}],
            "mentions": [{"@id": "#collection-1"}, {"@id": "#collection-2"},
                         {"@id": "#collection-3"}],
        },
        {"@id": WITHHELD_HOST, "@type": "SoftwareApplication", "name": "Chemotion ELN",
         "version": "2.0.0"},
        {
            # kind via the ontology placeholder written by the exporter
            "@id": SAMPLE,
            "@type": "Dataset",
            "name": "Water",
            "variableMeasured": [
                {"@type": "PropertyValue", "propertyID": ELEMENT_TYPE_PROPERTY_ID,
                 "value": "sample"},
            ],
            "hasPart": [{"@id": SPECTRUM}],
            "isPartOf": [{"@id": "#collection-1"}, {"@id": "#collection-3"}],
        },
        {
            # kind only inferable from the crate folder (reactions/)
            "@id": REACTION,
            "@type": ["Dataset", "https://example.org/SomeOntologyClass"],
            "name": "R1",
            "isPartOf": {"@id": "#collection-2"},
        },
        {"@id": DEVICE, "@type": "Dataset", "name": "Spectrometer"},
        {"@id": GENERIC, "@type": "Dataset", "name": "Thing"},
        {
            "@id": SPECTRUM,
            "@type": "File",
            "name": "spectrum.txt",
            "encodingFormat": "text/plain",
            "contentSize": "8",
        },
        {"@id": "#collection-1", "@type": "Collection", "name": "Parent Col",
         "description": "top level", "hasPart": [{"@id": SAMPLE}]},
        {"@id": "#collection-2", "@type": "Collection", "name": "Child Col",
         "isPartOf": {"@id": "#collection-1"}, "hasPart": {"@id": REACTION}},
        {"@id": "#collection-3", "@type": "Collection", "name": "Other Root",
         "hasPart": [{"@id": SAMPLE}]},
        {"@id": ORCID, "@type": "Person", "name": "Ada Lovelace",
         "identifier": ORCID, "email": "ada@example.org", "affiliation": {"@id": ROR}},
        {"@id": ROR, "@type": "Organization", "name": "KIT", "identifier": ROR},
        # An unrelated CreateAction placed first: the producer must still be found on ours.
        {"@id": "#instrument-run", "@type": "CreateAction", "object": {"@id": SAMPLE},
         "instrument": {"@id": "#nmr"}},
        {"@id": "#nmr", "@type": "SoftwareApplication", "name": "TopSpin"},
        {"@id": "#ro-crate_created", "@type": "CreateAction", "object": {"@id": "./"},
         "instrument": {"@id": PRODUCER_ID}},
        {"@id": PRODUCER_ID, "@type": "SoftwareApplication", "name": PRODUCER_NAME,
         "version": "0.1.0"},
    ],
}


def _crate_zip(tmp_path, metadata=METADATA, with_payload=True, name="mini.eln"):
    """Write a minimal .eln ZIP (single root folder, as per TheELNFileFormat)."""
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mini.eln/ro-crate-metadata.json", json.dumps(metadata))
        if with_payload:
            z.writestr("mini.eln/samples/Water_42/spectrum.txt", b"1.0 2.0\n")
    return path


def _with_node(metadata, node_id, **changes):
    out = copy.deepcopy(metadata)
    for node in out["@graph"]:
        if node["@id"] == node_id:
            node.update(changes)
    return out


def test_reads_crate_structure_from_zip(tmp_path):
    with read(_crate_zip(tmp_path)) as crate:
        assert isinstance(crate, ElnCrate)
        assert crate.root.name == "My Project"
        assert crate.date_created == "2026-07-03T12:00:00"
        assert {ds.id for ds in crate.datasets} == {SAMPLE, REACTION, DEVICE, GENERIC}
        # root's hasPart navigation resolves the same datasets
        assert {ds.id for ds in crate.root.datasets} == {ds.id for ds in crate.datasets}
        assert all(isinstance(ds, ElnDataset) for ds in crate.datasets)


def test_import_eln_is_an_alias_of_read():
    assert import_eln is read


def test_kind_detection_placeholder_and_path(tmp_path):
    with read(_crate_zip(tmp_path)) as crate:
        by_id = {ds.id: ds for ds in crate.elements}
        assert by_id[SAMPLE].kind == "sample"                  # from the placeholder
        assert by_id[REACTION].kind == "reaction"              # from the folder name
        assert by_id[DEVICE].kind == "device_description"      # from the folder name
        assert by_id[GENERIC].kind == "generic"                # ./generics/<type>/<label>_<id>/
        assert by_id[SAMPLE].source_id == 42
        assert by_id[REACTION].source_id == 7
        assert by_id[DEVICE].source_id == 3
        assert by_id[GENERIC].source_id == 5
        assert "https://example.org/SomeOntologyClass" in by_id[REACTION].types


def test_kind_from_a_referenced_placeholder(tmp_path):
    """A flattened graph puts the PropertyValue in its own node and references it."""
    metadata = copy.deepcopy(METADATA)
    metadata["@graph"].append({"@id": "#pv-1", "@type": "PropertyValue",
                               "propertyID": ELEMENT_TYPE_PROPERTY_ID, "value": "wellplate"})
    metadata["@graph"].append({"@id": "./elsewhere/X_1/", "@type": "Dataset",
                               "variableMeasured": [{"@id": "#pv-1"}]})
    with read(_crate_zip(tmp_path, metadata)) as crate:
        assert crate.get_dataset("./elsewhere/X_1/").kind == "wellplate"


def test_dataset_with_no_kind_signal_is_not_an_element(tmp_path):
    metadata = copy.deepcopy(METADATA)
    metadata["@graph"].append({"@id": "./misc/", "@type": "Dataset", "name": "misc"})
    with read(_crate_zip(tmp_path, metadata)) as crate:
        assert crate.get_dataset("./misc/").kind is None
        assert "./misc/" in {ds.id for ds in crate.datasets}
        assert "./misc/" not in {ds.id for ds in crate.elements}


def test_files_and_payload_access(tmp_path):
    with read(_crate_zip(tmp_path)) as crate:
        sample = crate.get_dataset(SAMPLE)
        (f,) = sample.files
        assert isinstance(f, ElnFile)
        assert (f.name, f.encoding_format, f.content_size) == ("spectrum.txt", "text/plain", 8)
        assert f.read() == b"1.0 2.0\n"
        out = f.extract(tmp_path / "out")
        assert out == (tmp_path / "out" / "spectrum.txt").resolve()
        assert out.read_bytes() == b"1.0 2.0\n"
        assert crate.has_file(f.id)
        assert [x.id for x in crate.files] == [f.id]


def test_unparsable_content_size_is_none(tmp_path):
    metadata = _with_node(METADATA, SPECTRUM, contentSize="lots")
    with read(_crate_zip(tmp_path, metadata)) as crate:
        assert crate.files[0].content_size is None


def test_get_dataset_misses(tmp_path):
    with read(_crate_zip(tmp_path)) as crate:
        assert crate.get_dataset("./nope/") is None
        assert crate.get_dataset(SPECTRUM) is None        # a File, not a Dataset


def test_metadata_only_file_is_reported_missing(tmp_path):
    """File nodes may outlive their payload (an export keeps metadata for a failed download)."""
    with read(_crate_zip(tmp_path, with_payload=False)) as crate:
        (f,) = crate.files
        assert not crate.has_file(f.id)
        with pytest.raises(FileNotFoundError):
            f.read()


def test_authors_reconstructed(tmp_path):
    with read(_crate_zip(tmp_path)) as crate:
        (author,) = crate.authors
        assert author.name == "Ada Lovelace"
        assert author.orcid_url == ORCID
        assert author.email == "ada@example.org"
        assert [(a.name, a.ror_url) for a in author.affiliations] == [("KIT", ROR)]


def test_author_without_a_person_node_comes_back_with_empty_fields(tmp_path):
    metadata = _with_node(METADATA, "./", author={"@id": "#someone"})
    with read(_crate_zip(tmp_path, metadata)) as crate:
        (author,) = crate.authors
        assert (author.name, author.orcid, author.affiliations) == ("", "", [])


def test_foreign_crate_identifiers_are_not_laundered(tmp_path):
    """A crate is an untrusted document: a claimed ORCID/ROR that is not one is not reported.

    A substring test (``"orcid.org" in person_id``) would return an attacker-chosen URI as the
    author's ORCID, and read -> export would then publish it as the crate's authorship ``@id``.
    """
    fake_person = "https://evil.example/orcid.org/fake"
    fake_org = "https://evil.example/ror.org/04t3en479"
    metadata = copy.deepcopy(METADATA)
    for node in metadata["@graph"]:
        if node["@id"] == "./":
            node["author"] = {"@id": fake_person}
        elif node["@id"] == ORCID:
            node.update({"@id": fake_person, "identifier": fake_person,
                         "affiliation": {"@id": fake_org}})
        elif node["@id"] == ROR:
            node.update({"@id": fake_org, "identifier": fake_org})

    with read(_crate_zip(tmp_path, metadata)) as crate:
        (author,) = crate.authors
        assert author.name == "Ada Lovelace"      # the readable metadata still comes back
        assert author.orcid == ""                 # the claimed identifier does not
        assert author.affiliations[0].name == "KIT"
        assert author.affiliations[0].ror == ""


def test_bad_stated_identifier_falls_back_to_a_valid_node_id(tmp_path):
    metadata = _with_node(METADATA, ORCID, identifier="https://evil.example/orcid.org/fake")
    with read(_crate_zip(tmp_path, metadata)) as crate:
        assert crate.authors[0].orcid == ORCID


def test_reads_extracted_directory(tmp_path):
    d = tmp_path / "crate" / "mini.eln"
    (d / "samples" / "Water_42").mkdir(parents=True)
    (d / "ro-crate-metadata.json").write_text(json.dumps(METADATA), encoding="utf-8")
    (d / "samples" / "Water_42" / "spectrum.txt").write_bytes(b"1.0 2.0\n")
    with read(tmp_path / "crate") as crate:
        assert crate.root.name == "My Project"
        assert crate.get_dataset(SAMPLE).files[0].read() == b"1.0 2.0\n"


@pytest.mark.parametrize("evil", [
    "../outside.txt", "samples/../../outside.txt", "/etc/passwd", "..", "C:/Windows/win.ini",
])
def test_zip_slip_ids_are_rejected(tmp_path, evil):
    with read(_crate_zip(tmp_path)) as crate:
        assert not crate.has_file(evil)
        with pytest.raises(ValueError, match="unsafe crate path"):
            crate.read_file(evil)


def test_a_name_that_merely_starts_with_dots_is_not_a_traversal(tmp_path):
    path = tmp_path / "dots.eln"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mini.eln/ro-crate-metadata.json", json.dumps(METADATA))
        z.writestr("mini.eln/..notes.txt", b"ok")
    with read(path) as crate:
        assert crate.read_file("./..notes.txt") == b"ok"


def test_producer_marker_identifies_our_crates(tmp_path):
    with read(_crate_zip(tmp_path)) as crate:
        assert crate.is_chemotion_crate
        assert crate.producer["@id"] == PRODUCER_ID
        assert crate.producer["name"] == PRODUCER_NAME
        assert crate.producer["version"]


def test_producer_is_found_through_object_root_without_the_conventional_id(tmp_path):
    metadata = copy.deepcopy(METADATA)
    for node in metadata["@graph"]:
        if node["@id"] == "#ro-crate_created":
            node["@id"] = "#some-other-name"
    with read(_crate_zip(tmp_path, metadata)) as crate:
        assert crate.is_chemotion_crate


def test_source_eln(tmp_path):
    with read(_crate_zip(tmp_path)) as crate:
        assert crate.source_eln["@id"] == WITHHELD_HOST
        assert crate.source_eln["version"] == "2.0.0"
    metadata = copy.deepcopy(METADATA)
    for node in metadata["@graph"]:
        node.pop("isBasedOn", None)
    with read(_crate_zip(tmp_path, metadata, name="nosource.eln")) as crate:
        assert crate.source_eln is None


def test_collections_and_membership(tmp_path):
    with read(_crate_zip(tmp_path)) as crate:
        cols = {c.id: c for c in crate.collections}
        assert list(cols) == ["#collection-1", "#collection-2", "#collection-3"]
        assert all(isinstance(c, ElnCollection) for c in cols.values())
        # an element in two collections is stored once and names both
        sample = crate.get_dataset(SAMPLE)
        assert sorted(c.name for c in sample.collections) == ["Other Root", "Parent Col"]
        # a single isPartOf reference (not a list) still resolves
        assert [c.name for c in crate.get_dataset(REACTION).collections] == ["Child Col"]
        assert crate.root.collections == []
        assert cols["#collection-1"].description == "top level"
        assert [e.id for e in cols["#collection-1"].elements] == [SAMPLE]
        assert [e.id for e in cols["#collection-2"].elements] == [REACTION]
        # collections are never elements or datasets
        assert not any(ds.id.startswith("#collection") for ds in crate.datasets)


def test_hasPart_and_isPartOf_are_read_as_inverses(tmp_path):
    with read(_crate_zip(tmp_path)) as crate:
        forward = {(c.id, e.id) for c in crate.collections for e in c.elements}
        backward = {(c.id, e.id) for e in crate.elements for c in e.collections}
        assert forward and forward == backward


def test_sub_collections_keep_their_parent(tmp_path):
    with read(_crate_zip(tmp_path)) as crate:
        cols = {c.name: c for c in crate.collections}
        assert cols["Child Col"].parent.name == "Parent Col"
        assert cols["Parent Col"].parent is None


def test_dangling_references_are_dropped(tmp_path):
    metadata = _with_node(METADATA, SAMPLE, isPartOf=[{"@id": "#gone"}],
                          hasPart=[{"@id": "./missing.txt"}, {"@id": SPECTRUM}])
    with read(_crate_zip(tmp_path, metadata)) as crate:
        sample = crate.get_dataset(SAMPLE)
        assert sample.collections == []
        assert [f.id for f in sample.files] == [SPECTRUM]


def test_foreign_crates_are_rejected_by_default(tmp_path):
    """.eln files from other ELNs are a deferred feature: refuse unless allow_foreign=True."""
    foreign = _with_node(METADATA, PRODUCER_ID, name="eLabFTW")
    path = _crate_zip(tmp_path, metadata=foreign)
    with pytest.raises(ValueError, match="eLabFTW.*not supported yet"):
        read(path)
    # escape hatch: best-effort generic parsing
    with read(path, allow_foreign=True) as crate:
        assert not crate.is_chemotion_crate
        assert crate.root.name == "My Project"


def test_crates_without_a_producer_are_rejected(tmp_path):
    unmarked = copy.deepcopy(METADATA)
    unmarked["@graph"] = [n for n in unmarked["@graph"] if n["@id"] != "#ro-crate_created"]
    with pytest.raises(ValueError, match="unknown producer"):
        read(_crate_zip(tmp_path, metadata=unmarked))


def test_another_create_action_does_not_count_as_the_producer(tmp_path):
    """Only the action that created *this crate* names the producer."""
    metadata = copy.deepcopy(METADATA)
    metadata["@graph"] = [n for n in metadata["@graph"] if n["@id"] != "#ro-crate_created"]
    with read(_crate_zip(tmp_path, metadata), allow_foreign=True) as crate:
        assert crate.producer is None


def test_rejects_non_crates(tmp_path):
    not_zip = tmp_path / "plain.eln"
    not_zip.write_text("not a zip")
    with pytest.raises(ValueError, match="neither"):
        read(not_zip)

    no_meta = tmp_path / "empty.eln"
    with zipfile.ZipFile(no_meta, "w") as z:
        z.writestr("empty.eln/readme.txt", "hi")
    with pytest.raises(ValueError, match="ro-crate-metadata.json"):
        read(no_meta)


def test_missing_descriptor_node_is_a_clear_error(tmp_path):
    metadata = copy.deepcopy(METADATA)
    metadata["@graph"] = [n for n in metadata["@graph"] if n["@id"] != "ro-crate-metadata.json"]
    with ElnCrate(_crate_zip(tmp_path, metadata)) as crate:
        with pytest.raises(ValueError, match="descriptor"):
            crate.root  # noqa: B018
        assert crate.source_eln is None    # a crate with no root simply does not say


def test_safe_join_keeps_names_inside_base(tmp_path):
    assert safe_join(tmp_path, "a.txt") == (tmp_path / "a.txt").resolve()
    assert safe_join(tmp_path, "../../etc/passwd") == (tmp_path / "passwd").resolve()
    assert safe_join(tmp_path, "/etc/passwd") == (tmp_path / "passwd").resolve()
    assert safe_join(tmp_path, "..\\..\\evil.bat") == (tmp_path / "evil.bat").resolve()
    assert safe_join(tmp_path, "") == (tmp_path / "unnamed").resolve()
    for bad in ("..", "."):
        with pytest.raises(ValueError, match="refusing to write outside"):
            safe_join(tmp_path, bad)


def test_safe_join_refuses_a_symlink_pointing_out(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "base"
    base.mkdir()
    try:
        (base / "x.txt").symlink_to(outside / "x.txt")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    with pytest.raises(ValueError, match="refusing to write outside"):
        safe_join(base, "x.txt")


def test_extract_cannot_be_steered_out_of_dest(tmp_path):
    metadata = _with_node(METADATA, SPECTRUM, name="../../.bashrc")
    with read(_crate_zip(tmp_path, metadata)) as crate:
        out = crate.files[0].extract(tmp_path / "out")
    assert out == (tmp_path / "out" / ".bashrc").resolve()


def test_ids_spelled_another_way_still_resolve(tmp_path):
    # Other writers write "x/" where we write "./x/", and percent-encode payload names.
    import json as _json
    import zipfile as _zipfile

    from chemotion_crate import read as _read
    meta = {"@context": "https://w3id.org/ro/crate/1.2/context", "@graph": [
        {"@id": "ro-crate-metadata.json", "@type": "CreativeWork", "about": {"@id": "./"},
         "conformsTo": {"@id": "https://w3id.org/ro/crate/1.2"}},
        {"@id": "./", "@type": "Dataset", "name": "root", "hasPart": [{"@id": "./exp/"}]},
        {"@id": "exp/", "@type": "Dataset", "name": "experiment",
         "hasPart": [{"@id": "exp/my%20file.txt"}]},
        {"@id": "exp/my%20file.txt", "@type": "File", "name": "my file.txt"},
    ]}
    path = tmp_path / "foreign.eln"
    with _zipfile.ZipFile(path, "w") as z:
        z.writestr("foreign/ro-crate-metadata.json", _json.dumps(meta))
        z.writestr("foreign/exp/my file.txt", b"hello")
    with _read(path, allow_foreign=True) as crate:
        assert crate.get_dataset("./exp/").name == "experiment"
        assert crate.has_file("exp/my%20file.txt")
        assert crate.read_file("exp/my%20file.txt") == b"hello"
        assert not crate.has_file("%2e%2e/escape"), "an encoded traversal is still refused"


def test_a_generic_type_named_like_a_builtin_folder_is_still_generic():
    from chemotion_crate.reader import ElnDataset
    ds = ElnDataset(None, {"@id": "./generics/samples/S_1/"})  # type: ignore[arg-type]
    assert ds.kind == "generic"


def test_a_vessels_uuid_is_its_source_id():
    from chemotion_crate.reader import ElnDataset
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    assert ElnDataset(None, {"@id": f"#vessels/AB-V1_{uuid}"}).source_id == uuid  # type: ignore[arg-type]
    assert ElnDataset(None, {"@id": "./samples/Water_42/"}).source_id == 42  # type: ignore[arg-type]
    assert ElnDataset(None, {"@id": "./samples/Water_abc/"}).source_id is None  # type: ignore[arg-type]


ELN_PROFILE = "https://purl.archive.org/purl/elnconsortium/eln-spec/1.2+20260923"


def test_versions_are_read_where_the_eln_format_declares_them(tmp_path):
    meta = _with_node(METADATA, "./", conformsTo=[{"@id": "https://w3id.org/ro/crate/1.2"},
                                                  {"@id": ELN_PROFILE}])
    with read(_crate_zip(tmp_path, meta)) as crate:
        assert crate.ro_crate_version == "1.2"
        assert crate.eln_version == "1.2+20260923"


def test_versions_unknown_to_the_reader_are_still_reported(tmp_path):
    meta = _with_node(METADATA, "ro-crate-metadata.json",
                      conformsTo={"@id": "https://w3id.org/ro/crate/1.3/"})
    meta = _with_node(meta, "./", conformsTo=ELN_PROFILE.replace("1.2+20260923", "1.3"))
    with read(_crate_zip(tmp_path, meta)) as crate:
        assert crate.ro_crate_version == "1.3"
        assert crate.eln_version == "1.3"


def test_undeclared_versions_are_none(tmp_path):
    meta = _with_node(METADATA, "ro-crate-metadata.json", conformsTo={"@id": "https://example.org/p"})
    with read(_crate_zip(tmp_path, meta)) as crate:
        assert crate.ro_crate_version is None
        assert crate.eln_version is None, "the root declares no ELN profile"
