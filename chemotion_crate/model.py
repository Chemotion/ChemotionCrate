"""The export model: what an export knows about a collection, as plain data.

:mod:`chemotion_crate.extract` fills this from a live ``chemotion_api`` instance;
:mod:`chemotion_crate.crate` turns it into an RO-Crate graph. Nothing in between touches the
client, so the whole crate pipeline can be exercised from a committed JSON fixture
(:meth:`ExportModel.from_json`) without a server.

Every value here is already *plain*: JSON-native scalars, lists and dicts, ISO 8601 timestamp
strings, text extracted from rich-text fields. Attachment bytes are the one exception: an
:class:`AttachmentRecord` carries a ``read`` callable that streams them on demand, so a large
file is never held in memory and the model stays cheap to build.
"""
from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

#: Element kinds the extractor models with dedicated fields. Anything else is a LabIMotion
#: generic type, carried with its segment fields as property values.
MODELLED_KINDS = ("sample", "reaction", "wellplate", "research_plan", "device_description",
                  "cell_line", "screen", "sequence_based_macromolecule_sample", "vessel")

#: Key of an element in the model: ``(kind, id)``. Samples, reactions, wellplates… each have
#: independent id sequences, so a same-numbered sample and reaction must not share a key.
ElementKey = tuple[str, Any]


@dataclass
class AttachmentRecord:
    """One file attached to an element or to an analysis dataset."""
    filename: str
    identifier: str | None = None       #: the ELN's attachment uuid; stable across re-exports
    id: int | None = None
    filesize: int | None = None         #: as the server records it; the writer restates it
    mime_type: str | None = None
    description: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    #: Streams the file's bytes. ``None`` when the source has no bytes for it, which the writer
    #: records as an omission rather than failing the export.
    read: Callable[[], Iterator[bytes]] | None = field(default=None, repr=False, compare=False)

    @property
    def key(self) -> Any:
        """A stable identity for cross-referencing within one export."""
        return self.identifier or self.id or id(self)


@dataclass
class DatasetRecord:
    """An analysis *dataset* container (the thing files are uploaded into)."""
    id: int | None = None
    name: str | None = None
    #: LabIMotion dataset klass, bound to an ontology term by schema constraint
    #: (``klass_ols`` is the ``CHMO:…`` code, ``klass_label`` the instance's wording).
    klass_code: str | None = None
    klass_label: str | None = None
    attachments: list[AttachmentRecord] = field(default_factory=list)


@dataclass
class AnalysisRecord:
    """An analysis container of an element."""
    id: int | None = None
    name: str | None = None
    #: ``extended_metadata.kind``: ``"CHMO:0000595 | proton nuclear magnetic resonance…"``.
    kind: str | None = None
    status: str | None = None
    content: str | None = None          #: the analysis' narrative, as plain text
    datasets: list[DatasetRecord] = field(default_factory=list)


@dataclass
class MoleculeRecord:
    """The molecule assigned to a sample."""
    id: int | None = None
    cano_smiles: str | None = None
    inchikey: str | None = None
    inchistring: str | None = None
    sum_formular: str | None = None
    iupac_name: str | None = None
    molecular_weight: float | None = None
    cas: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)


@dataclass
class ElementRecord:
    """One ELN element, with everything the crate will say about it."""
    kind: str
    id: Any
    name: str | None = None
    short_label: str | None = None
    #: The element's absolute API URL on the source instance. It is the identifier chem-dcat-ap
    #: nodes are minted under, and the crate builder rewrites it to a crate-relative path.
    url: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    description: str | None = None      #: plain text (rich text already flattened)
    observation: str | None = None      #: plain text
    keywords: list[str] = field(default_factory=list)
    #: Selected scalar/plain fields, keyed as the ELN names them.
    properties: dict[str, Any] = field(default_factory=dict)
    analyses: list[AnalysisRecord] = field(default_factory=list)
    attachments: list[AttachmentRecord] = field(default_factory=list)
    #: Ids of every collection this element belongs to, in traversal order.
    collection_ids: list[Any] = field(default_factory=list)
    #: Device descriptions: ``{"iri", "label"}`` pairs picked from the TIB Terminology Service.
    ontology_terms: list[dict] = field(default_factory=list)
    #: Extra schema.org ``PropertyValue`` entries for kinds without a chem-dcat-ap mapping
    #: (occupied wells, device fields, generic segment fields): ``{"name", "value"[, "unitText"]}``.
    property_values: list[dict] = field(default_factory=list)
    #: Other elements this one links to, as ``[kind, id]`` pairs (a screen's wellplates and
    #: research plans). The crate mentions those that are in the export.
    links: list[list] = field(default_factory=list)
    #: Names of fields the server withheld (the element is shared below full detail level); they
    #: are left out of the record. A reaction also names those withheld from its materials.
    withheld: list[str] = field(default_factory=list)

    # -- samples --
    role: str | None = None             #: role in its reaction, if reached through one
    is_solvent: bool = False
    molecule: MoleculeRecord | None = None
    mass_g: float | None = None
    volume_l: float | None = None
    amount_mol: float | None = None
    density: float | None = None
    equivalent: float | None = None
    molarity_value: float | None = None
    molarity_unit: str | None = None

    # -- reactions --
    #: ``{"starting_materials": [...], "reactants": [...], "products": [...], "solvents": [...]}``
    #: of sample records as they appear *in the reaction* (role and amounts set).
    materials: dict[str, list[ElementRecord]] = field(default_factory=dict)
    duration: str | None = None         #: the ELN's display string, e.g. ``"3.5 Hour(s)"``
    timestamp_start: str | None = None
    timestamp_stop: str | None = None
    yield_percent: float | None = None
    #: ``{"valueUnit": "°C", "userText": "25", "data": [{"time": …, "value": …}, …]}``
    temperature: dict[str, Any] = field(default_factory=dict)
    status: str | None = None
    rxno: str | None = None

    @property
    def key(self) -> ElementKey:
        return (self.kind, self.id)

    @property
    def label(self) -> str:
        """The name a human would use for this element."""
        return self.short_label or self.name or f"{self.kind}-{self.id}"

    def all_attachments(self) -> Iterator[AttachmentRecord]:
        """The element's own attachments, then every dataset's, in order."""
        yield from self.attachments
        for analysis in self.analyses:
            for dataset in analysis.datasets:
                yield from dataset.attachments


@dataclass
class CollectionRecord:
    """One ELN collection in the exported tree."""
    id: Any
    label: str | None = None
    description: str | None = None
    parent_id: Any = None
    #: ``False`` when ``id`` is a stand-in minted by the extractor, not the ELN's.
    has_server_id: bool = True


@dataclass
class SourceInfo:
    """Which ELN the data came from."""
    host: str | None = None             #: ``https://…``, no trailing slash
    version: str | None = None          #: the full version string from ``/about``
    revision: str | None = None


@dataclass
class ExportModel:
    """Everything the crate builder needs, and nothing it has to ask the server for."""
    root_collection_id: Any
    collections: list[CollectionRecord] = field(default_factory=list)
    elements: list[ElementRecord] = field(default_factory=list)
    source: SourceInfo = field(default_factory=SourceInfo)
    #: Omission records (``kind``, ``stage``, …) accumulated while extracting.
    skipped: list[dict] = field(default_factory=list)

    @property
    def root(self) -> CollectionRecord:
        for col in self.collections:
            if col.id == self.root_collection_id:
                return col
        raise ValueError("export model names no root collection")

    def element(self, key: ElementKey) -> ElementRecord | None:
        for elem in self.elements:
            if elem.key == key:
                return elem
        return None

    # -- JSON round trip (fixtures) ----------------------------------------------------------
    def to_json(self, inline_bytes: bool = False) -> dict:
        """A JSON-native rendering of the model.

        ``read`` callables are dropped; with ``inline_bytes`` each attachment's content is
        embedded base64-encoded under ``content_b64`` instead, which is what a committed fixture
        wants.
        """
        data = asdict(self)

        def _strip(obj: Any, attachments: list[AttachmentRecord] | None = None) -> Any:
            if isinstance(obj, dict):
                return {k: _strip(v) for k, v in obj.items() if k != "read"}
            if isinstance(obj, list):
                return [_strip(v) for v in obj]
            return obj

        data = _strip(data)
        if inline_bytes:
            for record, rendered in zip(_walk_attachments(self), _walk_attachment_dicts(data),
                                        strict=True):
                if record.read is not None:
                    rendered["content_b64"] = base64.b64encode(
                        b"".join(record.read())).decode("ascii")
        return data

    @classmethod
    def from_json(cls, data: dict, base_dir: str | Path | None = None) -> ExportModel:
        """Rebuild a model from :meth:`to_json` output.

        An attachment dict may carry ``content_b64`` (bytes inline) or ``path`` (a file relative
        to ``base_dir``); either becomes its ``read`` callable. One with neither has no bytes.
        """
        def _attachment(d: dict) -> AttachmentRecord:
            d = dict(d)
            content = d.pop("content_b64", None)
            path = d.pop("path", None)
            record = AttachmentRecord(**{k: v for k, v in d.items() if k in _ATTACHMENT_FIELDS})
            if content is not None:
                raw = base64.b64decode(content)
                record.read = lambda raw=raw: iter([raw])
            elif path is not None:
                file_path = Path(base_dir or ".") / path
                record.read = lambda p=file_path: _iter_file(p)
            return record

        def _dataset(d: dict) -> DatasetRecord:
            return DatasetRecord(**{**{k: v for k, v in d.items() if k != "attachments"},
                                    "attachments": [_attachment(a) for a in d.get("attachments", [])]})

        def _analysis(d: dict) -> AnalysisRecord:
            return AnalysisRecord(**{**{k: v for k, v in d.items() if k != "datasets"},
                                     "datasets": [_dataset(x) for x in d.get("datasets", [])]})

        def _element(d: dict) -> ElementRecord:
            plain = {k: v for k, v in d.items()
                     if k not in ("analyses", "attachments", "materials", "molecule")}
            return ElementRecord(
                **plain,
                analyses=[_analysis(a) for a in d.get("analyses", [])],
                attachments=[_attachment(a) for a in d.get("attachments", [])],
                materials={role: [_element(m) for m in items]
                           for role, items in (d.get("materials") or {}).items()},
                molecule=MoleculeRecord(**d["molecule"]) if d.get("molecule") else None,
            )

        return cls(
            root_collection_id=data["root_collection_id"],
            collections=[CollectionRecord(**c) for c in data.get("collections", [])],
            elements=[_element(e) for e in data.get("elements", [])],
            source=SourceInfo(**(data.get("source") or {})),
            skipped=list(data.get("skipped") or []),
        )

    @classmethod
    def load(cls, path: str | Path) -> ExportModel:
        """Read a fixture file written by :meth:`to_json`."""
        path = Path(path)
        return cls.from_json(json.loads(path.read_text(encoding="utf-8")), base_dir=path.parent)


_ATTACHMENT_FIELDS = {f.name for f in fields(AttachmentRecord)}


def _iter_file(path: Path, chunk_size: int = 1 << 20) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            yield chunk


def _walk_attachments(model: ExportModel) -> Iterator[AttachmentRecord]:
    for elem in model.elements:
        yield from _element_attachments(elem)


def _element_attachments(elem: ElementRecord) -> Iterator[AttachmentRecord]:
    yield from elem.all_attachments()
    for items in elem.materials.values():
        for material in items:
            yield from _element_attachments(material)


def _walk_attachment_dicts(data: dict) -> Iterator[dict]:
    for elem in data.get("elements", []):
        yield from _element_attachment_dicts(elem)


def _element_attachment_dicts(elem: dict) -> Iterator[dict]:
    yield from elem.get("attachments", [])
    for analysis in elem.get("analyses", []):
        for dataset in analysis.get("datasets", []):
            yield from dataset.get("attachments", [])
    for items in (elem.get("materials") or {}).values():
        for material in items:
            yield from _element_attachment_dicts(material)
