"""chemotion-crate: export a Chemotion ELN collection as an ``.eln`` RO-Crate, and read one back.

Export::

    from chemotion_api import Instance
    from chemotion_crate import Author, AuthorAffiliation, export

    instance = Instance("https://my.chemotion").test_connection().login_token("<token>")
    collection = instance.get_root_collection().get_collection("My project")
    export(instance, collection, "my_project.eln", authors=[
        Author(name="Ada Lovelace", orcid="0000-0002-1825-0097",
               affiliations=[AuthorAffiliation(name="KIT", ror="https://ror.org/04t3en479")],
               email="ada@example.org"),
    ])

Read::

    from chemotion_crate import read

    with read("my_project.eln") as crate:
        for element in crate.elements:
            print(element.kind, element.name, [f.name for f in element.files])
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from chemotion_crate.authors import Author, AuthorAffiliation, validate_authors
from chemotion_crate.manifest import is_note
from chemotion_crate.model import ExportModel
from chemotion_crate.ontology import InstanceVocabulary, OlsCache
from chemotion_crate.reader import ElnCollection, ElnCrate, ElnDataset, ElnFile, read

__all__ = [
    "Author", "AuthorAffiliation", "ElnCollection", "ElnCrate", "ElnDataset", "ElnFile",
    "ExportModel", "export", "export_model", "read",
]

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str | None], None]


def export(
    instance: Any,
    collection: Any,
    file_path: str | Path,
    *,
    authors: list[Author],
    strict: bool = False,
    ontology_lookup: bool = True,
    ols_cache: str | Path | None = None,
    disclose_host: bool = True,
    declare_skipped: bool = True,
    progress_callback: ProgressCallback | None = None,
    interactive: bool = False,
) -> str:
    """Export ``collection`` (and every collection below it) as a ``.eln`` file.

    :param instance: the :class:`chemotion_api.Instance` the collection belongs to.
    :param collection: the collection to export.
    :param file_path: destination ``.eln`` path.
    :param authors: the crate's author(s), each with an ORCID iD and ROR-identified
        affiliation(s). Required: authorship is never taken from the logged-in user.
    :param strict: raise ``RuntimeError`` instead of writing a crate whenever anything would be
        left out (an element that could not be loaded, an attachment that could not be
        downloaded, …). Nothing is written at ``file_path`` in that case.
    :param ontology_lookup: annotate analyses with ``measurementTechnique`` ontology terms.
        Resolution order: the ``CHMO:…`` code embedded in the analysis kind, then this instance's
        own ``ols_terms`` vocabulary, then an EBI OLS lookup.

        .. warning::
           This makes outbound requests to a third party, and defaults to on: every analysis
           name with no embedded code and no match in the instance's vocabulary is sent to EBI's
           OLS at ``www.ebi.ac.uk``, independently of ``disclose_host``. Pass
           ``ontology_lookup=False`` to use embedded codes only and make no such request, or
           ``ols_cache`` to look each term up once.
    :param ols_cache: path of a JSON cache of OLS lookups (read and updated).
    :param disclose_host: record the ELN's URL in the crate. ``False`` identifies the source by
        the ``.invalid`` sentinel instead, everywhere; its version is still recorded.
    :param declare_skipped: record what the export left out in the crate itself, as a
        ``CreativeWork`` the root ``mentions`` (counts and stable reasons; never raw error
        text). Without it a partial crate reads exactly like a complete one.
    :param progress_callback: ``callable(processed, total, name)`` once per element during
        mapping and once per file while writing; a faulty callback never aborts the export.
    :param interactive: show ``tqdm`` progress bars when tqdm is installed.
    :return: ``str(file_path)``.
    """
    from chemotion_crate.extract import extract, make_session, read_source_info

    export_authors = validate_authors(authors)
    _check_collection_belongs(instance, collection)
    session = make_session(instance)
    model = extract(instance, collection, source=read_source_info(instance, session))
    vocabulary = InstanceVocabulary.from_session(session) if ontology_lookup else None
    return export_model(
        model, file_path, authors=export_authors, strict=strict, ontology_lookup=ontology_lookup,
        ols_cache=ols_cache, disclose_host=disclose_host, declare_skipped=declare_skipped,
        progress_callback=progress_callback, interactive=interactive, vocabulary=vocabulary)


def _check_collection_belongs(instance: Any, collection: Any) -> None:
    """Refuse a collection that is not one of ``instance``'s own or shared collections (D7).

    Ids and links must never mix two servers. The collection is looked up by id in the
    instance's collection tree, including collections shared with the user. A collection
    without an id (such as a root) cannot be checked and is let through.
    """
    target = getattr(collection, "id", None)
    if target is None:
        return
    label = getattr(collection, "label", None)

    def found(cols: list) -> bool:
        # Collection ids are small integers, so the same id on another server is likely: the
        # label has to match too.
        pending, seen = list(cols), set()
        while pending:
            col = pending.pop()
            if col is collection or (getattr(col, "id", None) == target
                                     and getattr(col, "label", None) == label):
                return True
            if id(col) in seen:
                continue
            seen.add(id(col))
            pending += list(getattr(col, "children", None) or [])
        return False

    root = instance.get_root_collection()
    if found(list(getattr(root, "children", None) or [])):
        return
    try:  # shared collections only when the collection is not one of the user's own
        if found(list(root.sync_root.children or [])):
            return
    except Exception as exc:  # an instance without shared collections, or an older server
        logger.debug("No shared collections to check against: %s", exc)
    raise ValueError(
        f"Collection {getattr(collection, 'label', target)!r} (id {target}) is not one of this "
        f"instance's collections ({getattr(instance, 'host_url', 'unknown host')}). Pass the "
        "Instance the collection was loaded from.")


def export_model(
    model: ExportModel,
    file_path: str | Path,
    *,
    authors: list[Author],
    strict: bool = False,
    ontology_lookup: bool = True,
    ols_cache: str | Path | None = None,
    disclose_host: bool = True,
    declare_skipped: bool = True,
    progress_callback: ProgressCallback | None = None,
    interactive: bool = False,
    vocabulary: InstanceVocabulary | None = None,
    validate: bool = True,
) -> str:
    """Write a crate from an already extracted :class:`~chemotion_crate.model.ExportModel`.

    This is :func:`export` without the server: what the fixture-based tests and the
    conformance gate use. Parameters are as for :func:`export`; ``vocabulary`` is the
    instance's own ontology vocabulary when there is one, and ``validate`` runs py-rocrate over
    the staged crate before it is zipped.
    """
    from chemotion_crate.crate import build_crate
    from chemotion_crate.writer import CrateFile, write_crate

    export_authors = validate_authors(authors)
    cache = OlsCache(ols_cache)
    total = len(model.elements)
    bar = _progress_bar(total, "Mapping elements", "element") if interactive else None

    def _report(index: int, count: int, name: str | None) -> None:
        if bar is not None:
            if name:
                bar.set_postfix_str(str(name)[:40], refresh=False)
            bar.update(1)
        if progress_callback is not None:
            progress_callback(index, count, name)

    try:
        build = build_crate(model, export_authors, disclose_host=disclose_host,
                            ontology_lookup=ontology_lookup, ols_cache=cache,
                            vocabulary=vocabulary, progress=_report)
    finally:
        if bar is not None:
            bar.close()

    # Strict mode fails before any bytes reach file_path. Every omission but a download failure
    # is known here; the writer raises on the first of those, before its temp output is moved.
    skipped = build.skipped
    blocking = [s for s in skipped if not is_note(s)]
    if strict and blocking:
        cache.save()  # the (slow) lookups are not worth repeating because the export was refused
        raise RuntimeError(f"strict export: {len(blocking)} issue(s) during export: {blocking}")

    n_files = len(build.files)
    bar2 = _progress_bar(n_files, "Writing attachments", "file") if interactive and n_files else None
    done = 0

    def _tick(filename: str) -> None:
        nonlocal done
        done += 1
        try:
            if bar2 is not None:
                bar2.set_postfix_str(str(filename)[:40], refresh=False)
                bar2.update(1)
            if progress_callback is not None:
                progress_callback(total + done, total + n_files, filename)
        except Exception as exc:
            logger.debug("progress reporting raised: %s", exc)

    try:
        write_crate(file_path, build.metadata,
                    [CrateFile(crate_id=file_id, record=record) for file_id, record in build.files],
                    build.element_dirs, skipped=skipped, strict=strict,
                    declare_skipped=declare_skipped, mask_host=build.mask_host, progress=_tick,
                    validate=validate)
    finally:
        if bar2 is not None:
            bar2.close()
        # Saved even when a strict export was refused: the lookups are not worth repeating.
        cache.save()

    logger.info("Exported %d element(s) and %d collection(s) to %s",
                len(model.elements), len(model.collections), file_path)
    if skipped:
        notes = [s for s in skipped if is_note(s)]
        generic = [s for s in skipped if s.get("stage") == "to_dcat_ap"]
        omitted = [s for s in skipped if s not in notes and s not in generic]
        logger.warning("%d issue(s) during export (%d omitted, %d mapped generically, %d note(s)); "
                       "see the export manifest.", len(skipped), len(omitted), len(generic),
                       len(notes))
    return str(file_path)


def _progress_bar(total: int, desc: str, unit: str):
    """A ``tqdm.auto`` bar, or ``None`` when tqdm is not installed."""
    try:
        import warnings

        from tqdm import TqdmWarning
        from tqdm.auto import tqdm
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", TqdmWarning)
            return tqdm(total=total, desc=desc, unit=unit)
    except Exception:
        return None
