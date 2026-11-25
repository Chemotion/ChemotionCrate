"""Write an RO-Crate graph and its attachment bytes out as an ``.eln`` ZIP.

The crate is assembled in a staging directory beside the target first, and zipped only once it
is complete and its metadata final. Staging on disk (rather than streaming straight into the
ZIP) means a download that dies partway leaves nothing to purge from an archive: the partial
file is simply deleted before anything is zipped, and the metadata is written last, when every
late finding (checksums, sizes, failed downloads) is known.

Nothing here talks to a server: attachment bytes arrive through
:attr:`~chemotion_crate.model.AttachmentRecord.read`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chemotion_crate.manifest import MANIFEST_ID, build_skipped_manifest, vocabulary_attribution
from chemotion_crate.model import AttachmentRecord
from chemotion_crate.naming import sanitize_filename
from chemotion_crate.passes import replace_host, unpack_singletons
from chemotion_crate.producer import WITHHELD_HOST

logger = logging.getLogger(__name__)

#: Fixed name of the RO-Crate metadata descriptor file.
METADATA_FILE = "ro-crate-metadata.json"

#: Above this size a member is written with ZIP64 headers from the start. ``ZipFile.open(…, "w")``
#: cannot know the final size in advance and refuses to grow past 2 GiB without them.
_ZIP64_THRESHOLD = 1 << 31

_COPY_CHUNK = 1 << 20


@dataclass
class CrateFile:
    """One attachment to place in the crate payload."""
    #: crate-relative File ``@id``, e.g. ``"./samples/Ethanol_12/spectrum.jdx"``. The same string
    #: names the payload path and the File node, so the two cannot drift apart.
    crate_id: str
    record: AttachmentRecord


def write_crate(file_path: str | os.PathLike, metadata: dict, files: list[CrateFile],
                element_dirs: list[str], *, skipped: list[dict], strict: bool = False,
                declare_skipped: bool = True, mask_host: str | None = None,
                progress: Callable[[str], None] | None = None, validate: bool = True) -> str:
    """Write ``metadata`` and the bytes of ``files`` as an ``.eln`` archive at ``file_path``.

    :param metadata: the crate's JSON-LD (``@context`` + ``@graph``); finalised in place
        (checksums, sizes, withdrawn nodes, manifest, sorted ``@type``).
    :param files: the attachments to stream into the payload.
    :param element_dirs: crate-relative directories of every element Dataset; each is created
        even when empty.
    :param skipped: omission records; download failures are appended here.
    :param strict: raise :class:`RuntimeError` on the first download failure instead of
        recording it. Nothing is left at ``file_path`` in that case.
    :param declare_skipped: add the export manifest when anything was omitted.
    :param mask_host: a hostname to replace with :data:`WITHHELD_HOST` throughout the graph.
    :param progress: called with the attachment's filename after each one is written.
    :param validate: parse the staged crate with ``rocrate`` before zipping it.
    :return: ``file_path`` as a string.
    """
    file_path = Path(file_path)
    part_path = Path(f"{file_path}.part")
    if part_path.exists():
        # Overwriting this silently would destroy an unrelated file that happens to sit here, or a
        # concurrent export's scratch file. The caller picked `file_path`, not `file_path.part`.
        raise FileExistsError(
            f"{part_path} already exists. The crate is written there before being renamed to "
            f"{file_path}; remove it, or export to a different path.")

    # Same directory, hence same filesystem: the final os.replace is then an atomic rename.
    parent = file_path.parent if str(file_path.parent) else Path(".")
    staging = Path(tempfile.mkdtemp(prefix=f".{file_path.name}.", suffix=".staging",
                                    dir=parent))
    try:
        if validate:
            _precheck(metadata)
        digests, sizes, failed = _stage_files(staging, files, skipped, strict, progress)
        _stage_element_dirs(staging, element_dirs)
        _finalise_metadata(metadata, digests, sizes, failed, skipped, declare_skipped, mask_host)
        # Sorted keys: their order depends on nothing but the keys, not on the code path or
        # rdflib's output order ("@id" and "@type" still come first: "@" sorts before letters).
        (staging / METADATA_FILE).write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False, sort_keys=True,
                       default=_json_default),
            encoding="utf-8")
        if validate:
            _validate(staging)
        root_folder = _root_folder_name(file_path)
        try:
            _zip_staging(staging, part_path, root_folder, element_dirs)
            os.replace(part_path, file_path)
        except BaseException:
            part_path.unlink(missing_ok=True)
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return str(file_path)


# -- staging ---------------------------------------------------------------------------------

def _staged_path(staging: Path, crate_id: str) -> Path:
    """Where ``crate_id`` lives in the staging directory; refuses ids that escape it."""
    rel = crate_id.removeprefix("./")
    target = (staging / rel).resolve()
    if not rel or not target.is_relative_to(staging.resolve()):
        raise ValueError(f"crate @id {crate_id!r} does not name a path inside the crate")
    return target


def _stage_files(staging: Path, files: list[CrateFile], skipped: list[dict], strict: bool,
                 progress: Callable[[str], None] | None
                 ) -> tuple[dict[str, str], dict[str, int], set[str]]:
    """Stream every attachment into staging.

    :return: ``(sha256 by @id, bytes written by @id, @ids whose bytes did not fully arrive)``.
    """
    digests: dict[str, str] = {}
    sizes: dict[str, int] = {}
    failed: set[str] = set()
    for crate_file in files:
        record = crate_file.record
        target = _staged_path(staging, crate_file.crate_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        # The checksum is computed from the bytes as they stream past: one pass over data we are
        # already moving, no second download, and never a whole attachment in memory.
        digest = hashlib.sha256()
        written = 0
        if record.read is None:
            # Not a failure: the source never had bytes to give. Declared as its own reason.
            logger.warning("Attachment %s has no file content on the server.", record.filename)
            skipped.append({"kind": "attachment", "stage": "absent", "filename": record.filename})
            if strict:
                raise RuntimeError(f"strict mode: attachment {record.filename} has no file content")
            failed.add(crate_file.crate_id)
            continue
        try:
            with target.open("wb") as dest:
                for chunk in record.read():
                    digest.update(chunk)
                    written += len(chunk)
                    dest.write(chunk)
        except Exception as e:
            logger.warning("Failed to download attachment %s: %s", record.filename, e)
            # A stream that dies partway leaves a truncated file. Delete it, and withdraw the File
            # node below: otherwise the crate would describe a file with the server's size and no
            # checksum, and a consumer would read silently corrupt bytes with nothing to detect it.
            target.unlink(missing_ok=True)
            status = getattr(e, "status_code", None) or getattr(getattr(e, "response", None),
                                                                "status_code", None)
            skipped.append({"kind": "attachment", "stage": "absent" if status == 404 else "download",
                            "filename": record.filename, "error": str(e)})
            if strict:
                raise RuntimeError(f"strict mode: failed to download attachment "
                                   f"{record.filename}: {e}") from e
            failed.add(crate_file.crate_id)
            continue
        digests[crate_file.crate_id] = digest.hexdigest()
        sizes[crate_file.crate_id] = written
        if progress is not None:
            progress(record.filename)
    return digests, sizes, failed


def _stage_element_dirs(staging: Path, element_dirs: list[str]) -> None:
    # Every element is a Dataset *data entity*, and RO-Crate 1.2 requires a data entity to be
    # present in the payload (`ro-crate-1.2_18.1`): a directory that exists only in the metadata
    # is a REQUIRED violation. An element with no attachments (or whose only attachment failed
    # to download) writes no file, so its directory has to be created explicitly.
    for rel in element_dirs:
        _staged_path(staging, rel).mkdir(parents=True, exist_ok=True)


# -- metadata --------------------------------------------------------------------------------

def _is_file_node(node: dict) -> bool:
    node_type = node.get("@type")
    return node_type == "File" or (isinstance(node_type, list) and "File" in node_type)


def _finalise_metadata(metadata: dict, digests: dict[str, str], sizes: dict[str, int],
                       failed: set[str], skipped: list[dict], declare_skipped: bool,
                       mask_host: str | None) -> None:
    graph = metadata.setdefault("@graph", [])

    # Only a File whose bytes actually arrived gets a digest, so nothing advertises a checksum
    # for content that is not in the payload.
    for node in graph:
        if isinstance(node, dict) and _is_file_node(node) and node.get("@id") in digests:
            node["sha256"] = digests[node["@id"]]
            # contentSize came from the server's recorded filesize, which can be stale. Now that
            # the bytes have gone past, state those: a size that disagrees with the checksum
            # beside it is worse than no size.
            node["contentSize"] = str(sizes[node["@id"]])

    if failed:
        # The crate makes no claim about a file it does not contain: drop the node and every
        # hasPart reference to it.
        graph[:] = [n for n in graph if not (isinstance(n, dict) and n.get("@id") in failed)]
        for node in graph:
            if not isinstance(node, dict) or "hasPart" not in node:
                continue
            parts = node["hasPart"]
            if isinstance(parts, dict):
                parts = [parts]
            if isinstance(parts, list):
                kept = [r for r in parts if not (isinstance(r, dict) and r.get("@id") in failed)]
                if kept:
                    node["hasPart"] = kept
                else:
                    node.pop("hasPart")  # every file withdrawn: no empty value left behind
        logger.warning("withdrew %d File node(s) whose bytes did not fully arrive: %s",
                       len(failed), ", ".join(sorted(failed)[:5]))

    # The manifest is built here, last, and not by the caller: download failures are only known
    # once the bytes have streamed, and the metadata is written after that precisely so findings
    # like these still reach it.
    if declare_skipped:
        manifest_nodes = build_skipped_manifest(skipped, vocabulary_attribution(graph))
        if manifest_nodes:
            graph.extend(manifest_nodes)
            for node in graph:
                if isinstance(node, dict) and node.get("@id") == "./":
                    # `mentions` is the format's own way for a dataset to point at something it
                    # refers to, and one of the properties the .eln checker suggests.
                    mentions = node.get("mentions")
                    if isinstance(mentions, dict):
                        mentions = [mentions]
                    elif not isinstance(mentions, list):
                        mentions = []
                    mentions.append({"@id": MANIFEST_ID})
                    node["mentions"] = mentions
                    break
    if mask_host:
        # Runs after the manifest is added, so nothing assembled here escapes the scrub.
        replace_host(metadata, mask_host, WITHHELD_HOST)

    # Last word on `@type` ordering: earlier passes append to `@type` lists, so the final order
    # is settled only here, immediately before serialisation, for byte-stable output.
    for node in graph:
        if isinstance(node, dict) and isinstance(node.get("@type"), list):
            node["@type"] = sorted(node["@type"], key=str)
        # Likewise for a multi-valued literal (a shared molecule's two descriptions): rdflib emits
        # it in set order, which changes with the process's hash seed. A JSON-LD array is a set,
        # so sorting changes nothing a reader sees, and makes the bytes reproducible.
        if isinstance(node, dict):
            for key, value in node.items():
                if (key != "@type" and isinstance(value, list) and len(value) > 1
                        and all(isinstance(v, str | int | float | bool) for v in value)):
                    node[key] = sorted(value, key=lambda v: (type(v).__name__, str(v)))
    unpack_singletons(graph)
    metadata["@graph"] = _sort_for_output(graph)


def _json_default(o: Any) -> Any:
    # Wrappers such as PropertyDict expose .to_dict(). Trust it only when it yields JSON-native
    # data: json would otherwise call this hook again on the result, and an object whose to_dict()
    # returns another such object (a Mock, say) would recurse until memory runs out.
    if hasattr(o, "to_dict"):
        d = o.to_dict()
        if d is None or isinstance(d, dict | list | str | int | float | bool):
            return d
    return str(o)


def _sort_for_output(graph: list) -> list:
    """Order the graph for byte-stable output: root and descriptor first, then by ``@id``.

    Multi-valued *reference* lists are sorted too, except where people read the order
    (``author``, ``conformsTo``); a JSON-LD array is unordered unless declared ``@list``.
    """
    def rank(node: Any) -> tuple:
        node_id = node.get("@id", "") if isinstance(node, dict) else ""
        return (0 if node_id == "./" else 1 if node_id == METADATA_FILE else 2, str(node_id))

    for node in graph:
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if (key not in ("author", "conformsTo") and isinstance(value, list) and value
                    and all(isinstance(v, dict) and set(v) == {"@id"} for v in value)):
                node[key] = sorted(value, key=lambda ref: str(ref["@id"]))
    return sorted(graph, key=rank)


#: Timestamp of every ZIP member. The staged files' own times differ on every run, so the archive's
#: bytes would too; the crate records when it was made in its metadata (``endTime``).
ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)


def _precheck(metadata: dict) -> None:
    # The same parse as _validate, on the graph alone, before any attachment is downloaded: a graph
    # bug then fails in milliseconds instead of after a long download. _validate still runs last.
    from rocrate.rocrate import ROCrate

    try:
        ROCrate(json.loads(json.dumps(metadata, default=_json_default)))
    except Exception as e:
        raise RuntimeError(f"the crate is not valid RO-Crate: {e}") from e


def _validate(staging: Path) -> None:
    # The same parse TheELNFileFormat's conformance suite runs on every .eln. A crate that fails
    # it is a bug in this exporter, not in the data, so it is never shipped.
    from rocrate.rocrate import ROCrate

    try:
        ROCrate(str(staging))
    except Exception as e:
        raise RuntimeError(f"the crate is not valid RO-Crate: {e}") from e


def _root_folder_name(file_path: Path) -> str:
    stem = file_path.name.removesuffix(".eln")
    # sanitize_filename turns "" into "unnamed"; an .eln's root folder reads better as "export".
    return f"{sanitize_filename(stem) if stem else 'export'}.eln"


def _zip_staging(staging: Path, part_path: Path, root_folder: str,
                 element_dirs: list[str]) -> None:
    # Every member sits under one root folder, as TheELNFileFormat requires.
    dir_entries = {f"{root_folder}/{d.removeprefix('./').strip('/')}/" for d in element_dirs
                   if d.removeprefix("./").strip("/")}
    # "x": refuse to overwrite, closing the race between the exists() check and here.
    with open(part_path, "xb") as raw, zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as zf:
        # The descriptor first, so a reader can find it without scanning the whole archive.
        meta_info = zipfile.ZipInfo(f"{root_folder}/{METADATA_FILE}", date_time=ZIP_DATE_TIME)
        meta_info.compress_type = zipfile.ZIP_DEFLATED
        meta_info.external_attr = (stat.S_IFREG | 0o644) << 16
        zf.writestr(meta_info, (staging / METADATA_FILE).read_bytes())
        for dirpath, dirnames, filenames in os.walk(staging):
            dirnames.sort()
            rel_dir = Path(dirpath).relative_to(staging).as_posix()
            prefix = f"{root_folder}/" if rel_dir == "." else f"{root_folder}/{rel_dir}/"
            if prefix in dir_entries:
                # A trailing slash and no content is how ZIP records a directory. Written for
                # every element so an empty one survives the round trip. A bare ZipInfo gets
                # mode 0o600 with no execute bit, so `unzip` made the folder unenterable and its
                # files unreadable; record it as a 0o755 directory (MS-DOS directory bit too).
                dir_info = zipfile.ZipInfo(prefix)
                dir_info.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
                zf.writestr(dir_info, b"")
            for name in sorted(filenames):
                arcname = f"{prefix}{name}"
                if arcname == f"{root_folder}/{METADATA_FILE}":
                    continue
                src = Path(dirpath) / name
                info = zipfile.ZipInfo.from_file(src, arcname)
                info.date_time = ZIP_DATE_TIME  # not the staging file's mtime: reproducible bytes
                info.compress_type = zipfile.ZIP_DEFLATED
                with src.open("rb") as fh, zf.open(
                        info, "w", force_zip64=src.stat().st_size >= _ZIP64_THRESHOLD) as dest:
                    shutil.copyfileobj(fh, dest, _COPY_CHUNK)
