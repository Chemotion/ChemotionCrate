"""Export the fixture, read it back with :func:`chemotion_crate.read`, and get the model back."""
from __future__ import annotations

import hashlib

import pytest

from chemotion_crate import read
from chemotion_crate.producer import PRODUCER_NAME, WITHHELD_HOST

from .conftest import FIXTURE_ORCID, FIXTURE_ROR, REAL_HOST, ExportedCrate

#: ``(kind, source_id)`` of every fixture element as the reader should report it.
EXPECTED = {
    "#samples/MT-1_1": ("sample", 1),
    "#samples/MT-2_2": ("sample", 2),
    "./samples/MT-3_3/": ("sample", 3),
    "./reactions/MT-R1_5/": ("reaction", 5),
    "#wellplates/MT-WP1_8": ("wellplate", 8),
    "#research_plans/Plan_9": ("research_plan", 9),
    "#device_descriptions/MT-D1_10": ("device_description", 10),
    "#generics/lab_notebook_page/Page_3_3": ("generic", 3),
}
GENERIC = "#generics/lab_notebook_page/Page_3_3"


@pytest.fixture
def crate(any_crate: ExportedCrate):
    with read(any_crate.path) as opened:
        yield opened


def test_is_recognised_as_a_chemotion_crate(crate):
    assert crate.is_chemotion_crate
    assert crate.producer["name"] == PRODUCER_NAME
    assert crate.root.name == "Mini Test Collection"
    assert crate.date_created


def test_every_element_comes_back_with_its_source_id(crate):
    elements = {ds.id: ds for ds in crate.elements}
    assert set(elements) == set(EXPECTED)
    for element_id, (_, source_id) in EXPECTED.items():
        assert elements[element_id].source_id == source_id, element_id


def test_element_kinds_of_modelled_types(crate):
    kinds = {ds.id: ds.kind for ds in crate.elements}
    for element_id, (kind, _) in EXPECTED.items():
        if element_id != GENERIC:
            assert kinds[element_id] == kind, element_id
    assert {k for k, _ in EXPECTED.values()} - {"generic"} <= set(kinds.values())


def test_generic_element_reads_back_with_its_type_name(crate):
    # The placeholder names the LabIMotion type; "generic" is only the path fallback for
    # crates that carry no placeholder.
    assert crate.get_dataset(GENERIC).kind == "lab_notebook_page"


def test_files_are_readable_and_match_their_checksums(crate):
    files = {f.id: f for f in crate.files}
    assert set(files) == {"./samples/MT-3_3/notes.txt", "./reactions/MT-R1_5/product_1H.jdx"}
    for f in files.values():
        data = f.read()
        assert hashlib.sha256(data).hexdigest() == f.node["sha256"], f.id
        assert f.content_size == len(data), f.id
    assert files["./samples/MT-3_3/notes.txt"].read() == b"hello"
    assert files["./samples/MT-3_3/notes.txt"].encoding_format == "text/plain"
    by_element = {ds.id: [f.id for f in ds.files] for ds in crate.elements}
    assert by_element["./samples/MT-3_3/"] == ["./samples/MT-3_3/notes.txt"]
    assert by_element["./reactions/MT-R1_5/"] == ["./reactions/MT-R1_5/product_1H.jdx"]


def test_files_extract_inside_the_target_directory(crate, tmp_path):
    for f in crate.files:
        written = f.extract(tmp_path)
        assert written.parent == tmp_path.resolve()
        assert written.read_bytes() == f.read()


def test_collections_and_memberships_both_ways(crate):
    collections = {c.id: c for c in crate.collections}
    assert set(collections) == {"#collection-1", "#collection-2"}
    root_col, shared_col = collections["#collection-1"], collections["#collection-2"]
    assert root_col.name == "Mini Test Collection"
    assert root_col.description == "Fixture root"
    assert root_col.parent is None
    assert shared_col.name == "Shared Collection"
    assert shared_col.parent.id == "#collection-1"

    assert {e.id for e in shared_col.elements} == {
        "./samples/MT-3_3/", "#wellplates/MT-WP1_8", GENERIC}
    assert {e.id for e in root_col.elements} == {
        "#samples/MT-1_1", "#samples/MT-2_2", "./samples/MT-3_3/", "./reactions/MT-R1_5/",
        "#research_plans/Plan_9", "#device_descriptions/MT-D1_10"}

    shared = crate.get_dataset("./samples/MT-3_3/")
    assert {c.id for c in shared.collections} == {"#collection-1", "#collection-2"}
    for col in crate.collections:
        for element in col.elements:
            assert col.id in {c.id for c in element.collections}
    for element in crate.elements:
        for col in element.collections:
            assert element.id in {e.id for e in col.elements}


def test_authors_are_reconstructed(crate):
    authors = crate.authors
    assert len(authors) == 1
    author = authors[0]
    assert author.name == "Conformance Fixture"
    assert author.orcid == FIXTURE_ORCID
    assert len(author.affiliations) == 1
    assert author.affiliations[0].name == "Example University"
    assert author.affiliations[0].ror == FIXTURE_ROR
    # Round-trips into a valid export author.
    author.validate()


def test_source_eln_disclosed(disclosed_crate: ExportedCrate):
    with read(disclosed_crate.path) as crate:
        source = crate.source_eln
    assert source["@id"] == REAL_HOST
    assert source["version"] == "v4.0.1"


def test_source_eln_withheld(withheld_crate: ExportedCrate):
    with read(withheld_crate.path) as crate:
        source = crate.source_eln
    assert source["@id"] == WITHHELD_HOST
    assert source["version"] == "v4.0.1"
