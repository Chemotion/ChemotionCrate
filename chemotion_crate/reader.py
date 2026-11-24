"""Read a ``.eln`` RO-Crate archive back into navigable Python objects.

A ``.eln`` file (see `TheELNFileFormat <https://github.com/TheELNConsortium/TheELNFileFormat>`_)
is a ZIP whose single root folder contains an RO-Crate ``ro-crate-metadata.json`` plus the
crate's payload files. This module parses that JSON-LD graph and exposes it as
:class:`ElnCrate` / :class:`ElnDataset` / :class:`ElnFile` / :class:`ElnCollection` objects with
zip-slip-safe payload access.

Supported input is what :func:`chemotion_crate.export` writes, identified by the
``chemotion-crate`` producer marker (see :mod:`chemotion_crate.producer`). Crates from other
ELNs are rejected by default (importing them is a deferred feature); ``allow_foreign=True``
opens them anyway with best-effort generic RO-Crate parsing.

A crate is an untrusted document. Everything here assumes the archive may have been written by
someone hostile: payload paths are resolved without leaving the crate root, archive members are
size- and compression-ratio-capped before they are read, and identifiers the crate *claims*
(ORCID, ROR) are only reported when they validate.

Entry point: :func:`read`::

    from chemotion_crate.reader import read

    with read("my_project.eln") as crate:
        print(crate.root.name, [a.name for a in crate.authors])
        for ds in crate.elements:
            print(ds.kind, ds.name, [f.name for f in ds.files])
            for f in ds.files:
                data = f.read()
"""
from __future__ import annotations

import json
import logging
import os
import posixpath
from urllib.parse import unquote
import re
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from chemotion_crate.authors import Author, AuthorAffiliation, normalize_orcid, normalize_ror
from chemotion_crate.ontology_map import ELEMENT_TYPE_PROPERTY_ID
from chemotion_crate.producer import PRODUCER_NAME

logger = logging.getLogger(__name__)

#: Largest archive member (uncompressed bytes) any reader in this package will load.
MAX_ZIP_MEMBER_SIZE = 500 * 1024 * 1024

#: Largest uncompressed/compressed ratio accepted for an archive member. A size cap alone is not
#: a decompression-bomb defence: 400 MB inflated out of a 400 KB archive passes a 500 MiB limit
#: comfortably, so an improbable ratio is refused on its own. Real payloads (spectra, images,
#: JSON) stay well below it; runs of zeros are what reach it.
MAX_COMPRESSION_RATIO = 200

_METADATA_NAME = "ro-crate-metadata.json"

#: Profile IRI prefixes a crate declares its versions with, as TheELNFileFormat's checker reads
#: them (``tests/checks/versions.py``): RO-Crate in the descriptor's ``conformsTo``, the ELN
#: format in the root's.
_RO_CRATE_PROFILE_PREFIX = "https://w3id.org/ro/crate/"
_ELN_PROFILE_PREFIX = "https://purl.archive.org/purl/elnconsortium/eln-spec/"

#: @id of the CreateAction recording the export itself.
_CREATE_ACTION_ID = "#ro-crate_created"

#: Crate folder segment (as the exporter writes it) -> element kind.
_PATH_KINDS = {
    "samples": "sample",
    "reactions": "reaction",
    "wellplates": "wellplate",
    "research_plans": "research_plan",
    "device_descriptions": "device_description",
    "cell_lines": "cell_line",
    "screens": "screen",
    "sbmm_samples": "sequence_based_macromolecule_sample",
    "vessels": "vessel",
    "generics": "generic",
}


def _path_key(node_id: str) -> str:
    """A crate path id in normal form: no leading ``./``, percent-decoded. Other ids unchanged."""
    if node_id.startswith("#") or "://" in node_id or ":" in node_id.split("/", 1)[0]:
        return node_id
    return unquote(node_id).removeprefix("./") or "./"


def safe_join(base: str | Path, name: str) -> Path:
    """Join ``name`` under ``base``, refusing anything that would escape it.

    Filenames that arrive in data (a crate's ``File.name``) are not trustworthy paths. Two ways
    they escape:

    * an **absolute** name: ``Path('/safe/out') / '/etc/passwd'`` is ``/etc/passwd``; pathlib
      discards the base entirely;
    * a **relative** traversal: ``../../`` is not normalised away by ``/``, and code that calls
      ``mkdir(parents=True)`` first will helpfully build the path there.

    Both are defused by keeping only the basename; the resolved result is then required to lie
    strictly inside ``base``, which refuses ``..`` and ``.`` and also catches a pre-existing
    symlink at the destination.

    :param base: the directory the result must stay inside.
    :param name: the untrusted filename.
    :return: the resolved path, guaranteed to be inside ``base``.
    :raises ValueError: if ``name`` escapes ``base``.
    """
    root = Path(base).resolve()
    # Both separators: a Windows-style name ("..\\..\\x") is a traversal on Windows even when
    # this runs on POSIX, and the file may be extracted there later.
    leaf = os.path.basename(str(name or "").strip().replace("\\", "/")) or "unnamed"
    dest = (root / leaf).resolve()
    if not dest.is_relative_to(root) or dest == root:
        raise ValueError(f"refusing to write outside {root}: {name!r}")
    return dest


def reject_duplicate_members(names: list[str]) -> None:
    """Raise ``ValueError`` if an archive names a member twice.

    ``zipfile`` resolves a name to its last entry, so a second entry can hide a different (and
    unchecked) payload behind a name that was already inspected.
    """
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"archive member {name!r} appears more than once; refusing it")
        seen.add(name)


def check_zip_member(archive: zipfile.ZipFile, member: str | zipfile.ZipInfo) -> None:
    """Raise ``ValueError`` if ``member`` is oversized or looks like a decompression bomb.

    The sizes come from the archive's central directory, so this runs *before* any byte is
    inflated. Pass the ``ZipInfo`` itself when iterating ``infolist()``: looking a name up again
    returns the *last* entry of that name, which is not necessarily the one being read.
    """
    info = member if isinstance(member, zipfile.ZipInfo) else archive.getinfo(member)
    member = info.filename
    if info.file_size > MAX_ZIP_MEMBER_SIZE:
        raise ValueError(
            f"{member} is {info.file_size} bytes, exceeding the "
            f"{MAX_ZIP_MEMBER_SIZE}-byte member size limit")
    if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
        raise ValueError(
            f"{member} expands {info.file_size // max(info.compress_size, 1)}x "
            f"({info.compress_size} -> {info.file_size} bytes), above the "
            f"{MAX_COMPRESSION_RATIO}x limit; refusing to read it")


def metadata_member(names: list[str]) -> str | None:
    """Return the member that is the crate's ``ro-crate-metadata.json``, or ``None``.

    Matches on the exact basename, not a suffix: ``endswith(...)`` would also match a hostile or
    incidental ``evil-ro-crate-metadata.json``. When several members match (a nested crate, a
    decoy), the shallowest wins: the crate root's own descriptor, not one buried in a subfolder,
    and independent of ``namelist()`` order, which whoever wrote the archive controls.
    """
    candidates = sorted(
        (n for n in names if posixpath.basename(n) == _METADATA_NAME),
        key=lambda n: n.count("/"),
    )
    return candidates[0] if candidates else None


def _as_list(value: Any) -> list:
    """Normalize a JSON-LD value that may be a single object or a list into a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _ref_ids(value: Any) -> list[str]:
    """Extract the ``@id`` strings from a JSON-LD reference value (single ref, list, or bare id)."""
    ids = []
    for ref in _as_list(value):
        if isinstance(ref, dict) and isinstance(ref.get("@id"), str):
            ids.append(ref["@id"])
        elif isinstance(ref, str):
            ids.append(ref)
    return ids


def _claimed_identifier(stated: Any, node_id: str, normalise: Callable[[str], str]) -> str:
    """Return a validated identifier from a crate, or ``""`` when neither candidate is one.

    Reading is lenient about *missing* identifiers but must not be lenient about *false* ones:
    a crate is an untrusted document, and reporting an attacker-chosen URI as somebody's ORCID
    would launder it into the next export. A substring test (``"orcid.org" in node_id``) would
    accept ``https://evil.example/orcid.org/fake``; only a value that normalises is reported.
    """
    for candidate in (stated, node_id):
        if isinstance(candidate, str) and candidate.strip():
            try:
                return normalise(candidate)
            except ValueError:
                continue
    return ""


def _is_type(node: dict, type_name: str) -> bool:
    """Whether ``type_name`` appears in the node's ``@type`` (string or list)."""
    return type_name in _as_list(node.get("@type"))


class ElnFile:
    """A ``File`` node of the crate, with access to its payload bytes."""

    def __init__(self, crate: ElnCrate, node: dict) -> None:
        self._crate = crate
        self.node = node

    @property
    def id(self) -> str:
        """The file's crate-relative ``@id`` (its path inside the crate)."""
        return self.node["@id"]

    @property
    def name(self) -> str:
        """The file's name (falls back to the last path segment of its ``@id``)."""
        return self.node.get("name") or posixpath.basename(self.id.rstrip("/"))

    @property
    def encoding_format(self) -> str | None:
        """The file's MIME type, if recorded."""
        return self.node.get("encodingFormat")

    @property
    def content_size(self) -> int | None:
        """The file's size in bytes as recorded in the metadata (``None`` if absent/unparsable)."""
        raw = self.node.get("contentSize")
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def read(self) -> bytes:
        """Return the file's payload bytes from the archive.

        :raises FileNotFoundError: if the metadata references a file the archive does not contain.
        """
        return self._crate.read_file(self.id)

    def extract(self, dest_dir: str | Path) -> Path:
        """Write the payload inside ``dest_dir`` and return the written path.

        ``name`` comes from the crate's metadata, which is untrusted: a crate can ask for
        ``/home/you/.ssh/authorized_keys`` or ``../../.bashrc``. :func:`safe_join` reduces it to a
        basename inside ``dest_dir``. ``read()`` is already traversal-checked by the crate's
        member resolution; this is the half that actually touches the filesystem.

        :raises ValueError: if the crate's name would escape ``dest_dir``.
        """
        dest = safe_join(dest_dir, self.name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.read())
        return dest

    def __repr__(self) -> str:
        return f"<ElnFile {self.id!r}>"


class ElnDataset:
    """A ``Dataset`` node of the crate: the root, or an element.

    Collections are *not* Datasets: they are contextual :class:`ElnCollection` entities, so an
    element can belong to several of them without being stored more than once.
    """

    def __init__(self, crate: ElnCrate, node: dict) -> None:
        self._crate = crate
        self.node = node

    @property
    def id(self) -> str:
        """The dataset's crate-relative ``@id``."""
        return self.node["@id"]

    @property
    def name(self) -> str | None:
        """The dataset's human-readable name."""
        return self.node.get("name")

    @property
    def description(self) -> str | None:
        """The dataset's description, if any."""
        return self.node.get("description")

    @property
    def types(self) -> list[str]:
        """The node's ``@type`` value(s), always as a list."""
        return [t for t in _as_list(self.node.get("@type")) if isinstance(t, str)]

    @property
    def kind(self) -> str | None:
        """The element kind (``"sample"``, ``"reaction"``, …) if it can be determined.

        Resolution order: the ontology placeholder the exporter writes (a ``variableMeasured``
        entry with ``propertyID == ELEMENT_TYPE_PROPERTY_ID``), then the crate folder the dataset
        lives in (``samples/``, ``reactions/``, …). ``None`` for crates from other producers
        that carry neither.
        """
        for pv in _as_list(self.node.get("variableMeasured")):
            # RO-Crate wants a flattened graph, so the entry may be a reference to a
            # PropertyValue node rather than the node itself.
            if isinstance(pv, dict) and set(pv) == {"@id"}:
                pv = self._crate._node(pv["@id"]) or pv
            if isinstance(pv, dict) and pv.get("propertyID") == ELEMENT_TYPE_PROPERTY_ID:
                value = pv.get("value")
                if isinstance(value, str):
                    return value
        # Folder elements are `./samples/<label>_<id>/`; file-less ones `#samples/<label>_<id>`.
        parts = [p for p in self.id.removeprefix("#").split("/") if p and p != "."]
        # Generic elements are written as `./generics/<element_type>/<label>_<id>/`: the type
        # segment keeps two generic types with the same id apart, so "generics" is one level up.
        # Checked first: a generic type may be called like a built-in folder ("samples").
        if len(parts) >= 3 and parts[-3] == "generics":
            return _PATH_KINDS["generics"]
        if len(parts) >= 2 and parts[-2] in _PATH_KINDS:
            return _PATH_KINDS[parts[-2]]
        return None

    @property
    def source_id(self) -> int | str | None:
        """The originating element's id, parsed from the ``…_<id>`` path suffix the exporter
        writes: an ``int`` for most kinds, the UUID ``str`` for a vessel, ``None`` when the
        ``@id`` does not follow that convention."""
        last = self.id.rstrip("/").rsplit("/", 1)[-1]
        # A UUID contains "-" but no "_", so the last "_" still separates label and id.
        _, _, tail = last.rpartition("_")
        # `[0-9]+`, not `isdigit()`/`isdecimal()`/`\d`: all three are Unicode-aware, so "²" raises
        # in int() and "٩" parses to a *wrong* id of 9. Only ASCII digits are an element id.
        if re.fullmatch(r"[0-9]+", tail):
            return int(tail)
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", tail):
            return tail
        return None

    @property
    def files(self) -> list[ElnFile]:
        """The ``File`` nodes referenced by this dataset's ``hasPart``."""
        return [ElnFile(self._crate, n) for n in self._parts() if _is_type(n, "File")]

    @property
    def datasets(self) -> list[ElnDataset]:
        """The ``Dataset`` nodes referenced by this dataset's ``hasPart`` (sub-datasets)."""
        return [ElnDataset(self._crate, n) for n in self._parts() if _is_type(n, "Dataset")]

    @property
    def collections(self) -> list[ElnCollection]:
        """The ELN collections this element belongs to, from its ``isPartOf`` references.

        An element in several collections is stored in the crate once and names every collection
        here; the crate path says nothing about membership. Empty for the crate root and for
        crates whose producer does not record collections.
        """
        return [ElnCollection(self._crate, n)
                for n in self._crate._resolve(self.node.get("isPartOf"))
                if _is_type(n, "Collection")]

    def _parts(self) -> list[dict]:
        """The resolved graph nodes of this dataset's ``hasPart`` references."""
        return self._crate._resolve(self.node.get("hasPart"))

    def __repr__(self) -> str:
        return f"<ElnDataset {self.id!r} name={self.name!r} kind={self.kind!r}>"


class ElnCollection:
    """A ``Collection`` contextual entity: one ELN collection the crate was exported from.

    Collections are not crate folders. They are contextual entities the root ``mentions``, and
    membership runs both ways (:attr:`elements` is the inverse of
    :attr:`ElnDataset.collections`), so one element can belong to several collections without
    being stored twice.
    """

    def __init__(self, crate: ElnCrate, node: dict) -> None:
        self._crate = crate
        self.node = node

    @property
    def id(self) -> str:
        """The collection's ``@id`` (a ``#collection-…`` fragment as the exporter writes it)."""
        return self.node["@id"]

    @property
    def name(self) -> str | None:
        """The collection's label."""
        return self.node.get("name")

    @property
    def description(self) -> str | None:
        """The collection's description, if any."""
        return self.node.get("description")

    @property
    def parent(self) -> ElnCollection | None:
        """The parent collection, or ``None`` for a top-level one."""
        for node in self._crate._resolve(self.node.get("isPartOf")):
            if _is_type(node, "Collection"):
                return ElnCollection(self._crate, node)
        return None

    @property
    def elements(self) -> list[ElnDataset]:
        """The element datasets in this collection, from its ``hasPart`` references."""
        return [ElnDataset(self._crate, n)
                for n in self._crate._resolve(self.node.get("hasPart"))
                if _is_type(n, "Dataset")]

    def __repr__(self) -> str:
        return f"<ElnCollection {self.id!r} name={self.name!r}>"


class ElnCrate:
    """An opened ``.eln`` RO-Crate archive (ZIP or already-extracted directory).

    Usable as a context manager; :meth:`close` releases the underlying ZIP handle.
    """

    def __init__(self, path: str | Path) -> None:
        """Open ``path`` as a ``.eln`` ZIP or an extracted crate directory.

        :raises ValueError: if ``path`` is neither, or contains no ``ro-crate-metadata.json``.
        """
        self.path = Path(path)
        self._zip: zipfile.ZipFile | None = None
        if self.path.is_dir():
            members = self._directory_members()
        elif zipfile.is_zipfile(self.path):
            try:
                self._zip = zipfile.ZipFile(self.path)
                members = self._zip.namelist()
                reject_duplicate_members(members)
            except zipfile.BadZipFile as exc:
                # is_zipfile() only sniffs for an end-of-central-directory record; opening can
                # still fail on a damaged one.
                raise ValueError(f"{self.path} is a damaged ZIP archive: {exc}") from exc
        else:
            # Reporting a truncated download as "neither a ZIP nor a crate directory" sends people
            # looking for the wrong problem. A local file header with no central directory is the
            # signature of a partial file.
            if self.path.is_file():
                with self.path.open("rb") as fh:
                    if fh.read(4) == b"PK\x03\x04":
                        raise ValueError(
                            f"{self.path} starts like a ZIP archive but has no readable central "
                            "directory; it is most likely truncated or still downloading.")
            raise ValueError(f"{self.path} is neither a .eln ZIP archive nor a crate directory")

        meta_member = metadata_member(members)
        if meta_member is None:
            self.close()
            raise ValueError(f"No {_METADATA_NAME} found in {self.path}")
        # Crate-relative @ids resolve against the metadata file's folder (the crate root).
        self._root_prefix = meta_member[: -len(_METADATA_NAME)]
        self._members = set(members)
        try:
            self.metadata = json.loads(self._read_member(meta_member))
        except BaseException:
            self.close()
            raise
        if not isinstance(self.metadata, dict):
            self.close()
            raise ValueError(
                f"{meta_member} in {self.path} is a JSON {type(self.metadata).__name__}, not an "
                "object; an RO-Crate descriptor must be a JSON object with an @graph.")
        graph = self.metadata.get("@graph", [])
        self._nodes: dict[str, dict] = {
            n["@id"]: n for n in (graph if isinstance(graph, list) else [])
            if isinstance(n, dict) and isinstance(n.get("@id"), str)
        }
        # Other writers spell one path several ways ("x/", "./x/", "x%20y/"); look ids up by a
        # normal form too, so their references resolve.
        self._by_path: dict[str, dict] = {}
        for node_id, node in self._nodes.items():
            self._by_path.setdefault(_path_key(node_id), node)

    def _directory_members(self) -> list[str]:
        """Crate-relative paths of the regular files under an extracted crate directory.

        ``rglob`` can descend through symlinked directories, so an extracted crate containing
        ``payload -> /home/you`` would enumerate files outside the crate, and :meth:`read_file`
        would then hand their bytes to a caller that trusts the crate's ``@id``s. Symlinks are
        skipped and every member is resolved and required to stay inside the root. (The ZIP path
        is immune: zipfile never recreates symlinks.)
        """
        root = self.path.resolve()
        members = []
        for candidate in self.path.rglob("*"):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                if not candidate.resolve().is_relative_to(root):
                    continue
            except OSError:                      # broken link, unreadable parent
                continue
            members.append(candidate.relative_to(self.path).as_posix())
        return members

    # -- context manager ----------------------------------------------------
    def close(self) -> None:
        """Release the underlying ZIP handle (no-op for directory crates)."""
        if self._zip is not None:
            self._zip.close()
            self._zip = None

    def __enter__(self) -> ElnCrate:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- graph access ---------------------------------------------------------
    def _resolve(self, value: Any) -> list[dict]:
        """The graph nodes a JSON-LD reference value points at (dangling references dropped)."""
        out = []
        for ref_id in _ref_ids(value):
            node = self._node(ref_id)
            if node is not None:
                out.append(node)
        return out

    @property
    def descriptor(self) -> dict:
        """The crate's metadata descriptor node (``@id == "ro-crate-metadata.json"``).

        :raises ValueError: if the graph has no descriptor (not a valid RO-Crate).
        """
        node = self._nodes.get(_METADATA_NAME)
        if node is None:
            raise ValueError(f"{self.path} has no RO-Crate metadata descriptor node")
        return node

    @staticmethod
    def _declared_version(node: dict, prefix: str) -> str | None:
        """The version after ``prefix`` in the node's ``conformsTo``, or ``None``."""
        for profile in _ref_ids(node.get("conformsTo")):
            if profile.startswith(prefix) and (version := profile.removeprefix(prefix).strip("/")):
                return version
        return None

    @property
    def ro_crate_version(self) -> str | None:
        """The RO-Crate version the crate declares (``"1.2"``), from the descriptor's ``conformsTo``.

        Reported as declared, also a version this reader does not know (``"1.3"``), so a caller
        can warn about it or refuse it; ``None`` when none is declared.
        """
        return self._declared_version(self.descriptor, _RO_CRATE_PROFILE_PREFIX)

    @property
    def eln_version(self) -> str | None:
        """The ELN file format version the crate declares (``"1.2+20260923"``), from the root's
        ``conformsTo``; reported as declared, ``None`` when none is.
        """
        return self._declared_version(self.root.node, _ELN_PROFILE_PREFIX)

    @property
    def root(self) -> ElnDataset:
        """The crate's root Dataset (the descriptor's ``about`` target, conventionally ``"./"``).

        :raises ValueError: if the root dataset cannot be located.
        """
        for root_id in _ref_ids(self.descriptor.get("about")) + ["./"]:
            node = self._node(root_id)
            if node is not None:
                return ElnDataset(self, node)
        raise ValueError(f"{self.path} has no root Dataset node")

    @property
    def date_created(self) -> str | None:
        """The crate's creation timestamp from the descriptor, if recorded (ISO string)."""
        return self.descriptor.get("dateCreated")

    @property
    def source_eln(self) -> dict | None:
        """The ELN instance this crate's data came from, if the crate records one.

        Resolved from the root dataset's ``isBasedOn``. The node carries the instance URL as its
        ``@id`` and, when the exporter could read it, the server's ``version``. A crate whose
        hostname was deliberately withheld names :data:`~chemotion_crate.producer.WITHHELD_HOST`
        instead of a real host.

        :return: the source ELN node, or ``None`` when the crate does not say.
        """
        try:
            root_node = self.root.node
        except ValueError:      # a crate with no root Dataset simply does not say
            return None
        nodes = self._resolve(root_node.get("isBasedOn"))
        return nodes[0] if nodes else None

    @property
    def producer(self) -> dict | None:
        """The graph node of the software that wrote the crate, if declared.

        Read from the ``instrument`` of the crate's ``CreateAction``, which is where the writing
        software is recorded. (The descriptor's ``sdPublisher`` names the publishing
        *organization*, not the software.)

        :return: the producer node, or ``None`` when the crate declares none.
        """
        # Match the action that created *this crate*, not merely the first CreateAction in the
        # graph. The exporter appends it after every element and file node, so a scan taking the
        # first match would deterministically lose to any other CreateAction: element
        # provenance, an instrument run, or one carried in from a re-exported foreign crate.
        for candidate in self._nodes.values():
            if not _is_type(candidate, "CreateAction"):
                continue
            if candidate.get("@id") != _CREATE_ACTION_ID and \
                    _ref_ids(candidate.get("object")) != ["./"]:
                continue
            instruments = self._resolve(candidate.get("instrument"))
            if instruments:
                return instruments[0]
        return None

    @property
    def is_chemotion_crate(self) -> bool:
        """Whether this crate was written by this package.

        Keys on the producer marker every export embeds: a ``SoftwareApplication`` node named
        :data:`~chemotion_crate.producer.PRODUCER_NAME`, referenced as the ``instrument`` of the
        crate's ``CreateAction`` (see :attr:`producer`).
        """
        producer = self.producer
        return producer is not None and producer.get("name") == PRODUCER_NAME

    @property
    def datasets(self) -> list[ElnDataset]:
        """All ``Dataset`` nodes of the crate except the root itself."""
        root_id = self.root.id
        return [ElnDataset(self, n) for i, n in self._nodes.items()
                if i != root_id and _is_type(n, "Dataset")]

    @property
    def elements(self) -> list[ElnDataset]:
        """The datasets recognizable as ELN elements (those with a resolvable kind).

        Datasets from producers without kind information are excluded; use :attr:`datasets` for
        the full list. Collections never appear here; see :attr:`collections`.
        """
        return [ds for ds in self.datasets if ds.kind is not None]

    @property
    def collections(self) -> list[ElnCollection]:
        """Every ELN collection the crate records, in graph order.

        Empty for crates from producers that do not describe collections.
        """
        return [ElnCollection(self, n) for n in self._nodes.values()
                if _is_type(n, "Collection")]

    @property
    def files(self) -> list[ElnFile]:
        """All ``File`` nodes of the crate."""
        return [ElnFile(self, n) for n in self._nodes.values() if _is_type(n, "File")]

    def _node(self, node_id: str) -> dict | None:
        """The node with this ``@id``, also when it is spelled ``./x/`` for ``x/`` or percent-encoded."""
        node = self._nodes.get(node_id)
        return node if node is not None else self._by_path.get(_path_key(node_id))

    def get_dataset(self, dataset_id: str) -> ElnDataset | None:
        """Return the dataset with this ``@id``, or ``None`` if the graph has no such node."""
        node = self._node(dataset_id)
        return ElnDataset(self, node) if node is not None and _is_type(node, "Dataset") else None

    @property
    def authors(self) -> list[Author]:
        """The crate's authors as :class:`~chemotion_crate.authors.Author` objects.

        Reconstructed from the root dataset's ``author`` references and their ``Person`` /
        ``Organization`` contextual entities (ORCID as the Person ``@id``/``identifier``, ROR as
        the Organization's). Persons without ORCID/affiliations still come back, just with
        empty fields: reading is lenient where exporting is strict. An identifier that does not
        validate is reported as ``""``, never as the crate's claimed value.
        """
        authors = []
        for person_id in _ref_ids(self.root.node.get("author")):
            person = self._node(person_id) or {"@id": person_id}
            affiliations = []
            for org_id in _ref_ids(person.get("affiliation")):
                org = self._node(org_id) or {}
                affiliations.append(AuthorAffiliation(
                    name=org.get("name") or "",
                    ror=_claimed_identifier(org.get("identifier"), org_id, normalize_ror),
                ))
            orcid = _claimed_identifier(person.get("identifier"), person_id, normalize_orcid)
            authors.append(Author(name=person.get("name") or "", orcid=orcid,
                                  affiliations=affiliations, email=person.get("email")))
        return authors

    # -- payload access -------------------------------------------------------
    def _member_for(self, crate_relative_id: str) -> str:
        """Resolve a crate-relative ``@id`` to an archive member path, zip-slip safe.

        :raises ValueError: if the id escapes the crate root (``..``) or is absolute.
        """
        # Ids are URI paths, so a payload named "my file.txt" may be written "my%20file.txt".
        # Decoded before the safety check, so an encoded "%2e%2e/" is caught too.
        rel = posixpath.normpath(unquote(crate_relative_id).removeprefix("./"))
        # A `..`-prefixed *name* ("..notes.txt") is legitimate; only a `..` segment escapes.
        if rel == ".." or rel.startswith(("../", "/")) or (len(rel) > 1 and rel[1] == ":"):
            raise ValueError(f"unsafe crate path {crate_relative_id!r}")
        return self._root_prefix + rel

    def has_file(self, crate_relative_id: str) -> bool:
        """Whether the archive actually contains a payload for this crate-relative ``@id``."""
        try:
            return self._member_for(crate_relative_id) in self._members
        except ValueError:
            return False

    def read_file(self, crate_relative_id: str) -> bytes:
        """Return the payload bytes for a crate-relative ``@id``.

        :raises FileNotFoundError: if the archive contains no such member.
        :raises ValueError: if the id is unsafe (path traversal) or the member is over the
            size/compression limits.
        """
        member = self._member_for(crate_relative_id)
        if member not in self._members:
            raise FileNotFoundError(f"{crate_relative_id} not present in {self.path}")
        return self._read_member(member)

    def _read_member(self, member: str) -> bytes:
        """Raw bytes of an archive member (ZIP entry or file under the crate directory).

        Size- and ratio-capped: without the cap a 408 KB ``.eln`` whose metadata inflates to
        400 MB would be read in full, before any validation, and exhaust memory.

        :raises ValueError: if the member is oversized or compresses too improbably.
        """
        if self._zip is not None:
            check_zip_member(self._zip, member)
            return self._zip.read(member)
        # An extracted crate directory is an equally untrusted input. There is no compression
        # ratio to check on disk, so only the size limit applies.
        on_disk = self.path / member
        # Listing already skips symlinks; this also catches one swapped in after the crate opened.
        if on_disk.is_symlink() or not on_disk.resolve().is_relative_to(self.path.resolve()):
            raise ValueError(f"{member} leads outside the crate directory")
        size = on_disk.stat().st_size
        if size > MAX_ZIP_MEMBER_SIZE:
            raise ValueError(
                f"{member} is {size} bytes, exceeding the "
                f"{MAX_ZIP_MEMBER_SIZE}-byte member size limit")
        return on_disk.read_bytes()

    def __repr__(self) -> str:
        try:
            root_name = self.root.name
        except ValueError:
            root_name = None
        return f"<ElnCrate {str(self.path)!r} root={root_name!r}>"


def read(path: str | Path, allow_foreign: bool = False) -> ElnCrate:
    """Open a ``.eln`` RO-Crate archive (ZIP or extracted directory) for reading.

    Only crates written by this package (identified by the ``chemotion-crate``
    ``SoftwareApplication`` named as the ``instrument`` of the crate's ``CreateAction``) are
    accepted; reading ``.eln`` files from other ELNs is not supported yet.

    :param allow_foreign: open crates from other producers anyway (best-effort parsing: the
        generic RO-Crate structure is read, but element kinds and authors may be absent).
    :raises ValueError: if the crate declares a different (or no) producer and
        ``allow_foreign`` is not set, or the archive is not a readable crate.

    Example::

        from chemotion_crate.reader import read

        with read("my_project.eln") as crate:
            for ds in crate.elements:
                print(ds.kind, ds.name)
    """
    crate = ElnCrate(path)
    if not allow_foreign and not crate.is_chemotion_crate:
        producer = crate.producer or {}
        described = producer.get("name") or "an unknown producer"
        crate.close()
        raise ValueError(
            f"{path} was written by {described}, not by {PRODUCER_NAME}; reading .eln "
            "files from other ELNs is not supported yet. Pass allow_foreign=True to open "
            "it anyway (best-effort, generic RO-Crate parsing)."
        )
    return crate


#: Alias of :func:`read`.
import_eln = read
