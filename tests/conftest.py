"""Shared fixtures: the fixture model exported once per session, both with and without the host.

The crate-level tests (``test_crate``, ``test_roundtrip``, ``test_shacl_conformance``,
``test_context_subset``) assert many things about the same two archives, so each is written once
and parsed once. Exports never touch the network (``ontology_lookup=False``).
"""
from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from chemotion_crate import Author, AuthorAffiliation, export_model
from chemotion_crate.model import ExportModel

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mini_model.json"
REAL_HOST = "https://eln.example.org"
FIXTURE_ORCID = "https://orcid.org/0000-0002-1825-0097"
FIXTURE_ROR = "https://ror.org/05dxps055"
FIXTURE_AUTHORS = [Author(name="Conformance Fixture", orcid="0000-0002-1825-0097",
                          affiliations=[AuthorAffiliation(name="Example University",
                                                          ror=FIXTURE_ROR)],
                          email="no-reply@eln.invalid")]


def load_fixture_model() -> ExportModel:
    """A fresh model: an export appends notes to ``model.skipped``, so never reuse one."""
    return ExportModel.load(FIXTURE_PATH)


@dataclass
class ExportedCrate:
    """One written ``.eln`` archive, read back once."""
    path: Path
    #: every ZIP member name, in archive order
    names: list[str]
    #: member name -> bytes (the fixture's payload is a few KB)
    members: dict[str, bytes]
    #: the ``ro-crate-metadata.json`` member name
    metadata_member: str
    metadata: dict
    nodes: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.nodes = {n["@id"]: n for n in self.metadata["@graph"]}

    @property
    def root_folder(self) -> str:
        return self.metadata_member.split("/", 1)[0]

    def payload(self, crate_id: str) -> bytes:
        """Bytes of a crate-relative ``@id`` (``./samples/x/f.txt``)."""
        return self.members[f"{self.root_folder}/{crate_id.removeprefix('./')}"]

    def node(self, node_id: str) -> dict:
        return self.nodes[node_id]

    def deref(self, value: Any) -> list[dict]:
        """The nodes a reference value (one ``{"@id"}`` or a list of them) points at."""
        items = value if isinstance(value, list) else [value]
        return [self.nodes[item["@id"]] for item in items]


def open_crate(path: Path) -> ExportedCrate:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        members = {name: archive.read(name) for name in names}
    meta = [n for n in names if n.endswith("/ro-crate-metadata.json") and n.count("/") == 1]
    assert len(meta) == 1, f"expected one top-level ro-crate-metadata.json, got {meta}"
    return ExportedCrate(path=path, names=names, members=members, metadata_member=meta[0],
                         metadata=json.loads(members[meta[0]].decode("utf-8")))


def export_fixture(path: Path, model: ExportModel | None = None, **options: Any) -> Path:
    """Export the fixture model (or ``model``) to ``path`` with the fixture author, offline."""
    options.setdefault("authors", FIXTURE_AUTHORS)
    export_model(model or load_fixture_model(), path, ontology_lookup=False, **options)
    return path


@pytest.fixture(scope="session")
def disclosed_crate(tmp_path_factory: pytest.TempPathFactory) -> ExportedCrate:
    """The fixture exported with its host recorded."""
    path = tmp_path_factory.mktemp("disclosed") / "disclosed.eln"
    return open_crate(export_fixture(path, disclose_host=True))


@pytest.fixture(scope="session")
def withheld_crate(tmp_path_factory: pytest.TempPathFactory) -> ExportedCrate:
    """The fixture exported with ``disclose_host=False``."""
    path = tmp_path_factory.mktemp("withheld") / "withheld.eln"
    return open_crate(export_fixture(path, disclose_host=False))


@pytest.fixture(scope="session", params=["disclosed", "withheld"])
def any_crate(request: pytest.FixtureRequest) -> ExportedCrate:
    """Each of the two session crates in turn."""
    return request.getfixturevalue(f"{request.param}_crate")
