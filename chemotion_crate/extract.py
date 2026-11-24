"""Read a collection from a live Chemotion instance into an :class:`~chemotion_crate.model.ExportModel`.

This is the only module that talks to ``chemotion_api``, and it uses nothing but the client's
public surface. Everything downstream works on the plain model.

Failures are recorded, not raised: a kind that cannot be listed, an element that cannot be
loaded, an attachment with no bytes each become an entry in ``model.skipped`` so the crate can
declare what it lacks. ``strict`` exports turn those into errors later, in :func:`chemotion_crate.export`.
"""
from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from enum import Enum
from typing import Any

from chemotion_api.connection import Connection
from chemotion_api.utils import normalise_range_value, quill_to_text

from chemotion_crate.model import (
    MODELLED_KINDS,
    AnalysisRecord,
    AttachmentRecord,
    CollectionRecord,
    DatasetRecord,
    ElementKey,
    ElementRecord,
    ExportModel,
    MoleculeRecord,
    SourceInfo,
)

logger = logging.getLogger(__name__)

#: Elements are listed in pages; one page of this size per request.
PER_PAGE = 100

#: Sample fields carried as plain properties (chem-dcat-ap has no home for them).
_SAMPLE_PROPERTIES = ("external_label", "location", "sum_formula", "molecular_mass",
                      "dry_solvent", "impurities", "xref", "purity", "solvent", "stereo",
                      "decoupled", "is_top_secret", "target_amount", "real_amount",
                      "gas_type", "gas_phase_data")
#: A sample's place in one reaction, read from the reaction's copy of the sample.
_MATERIAL_LINK_PROPERTIES = ("reference", "coefficient", "waste")
#: Reaction fields carried as plain properties.
_REACTION_PROPERTIES = ("rxno", "conditions", "purification", "tlc_solvents", "tlc_description",
                        "rf_value", "volume", "use_reaction_volume", "weight_percentage",
                        "rinchi_string", "rinchi_long_key", "rinchi_short_key", "rinchi_web_key",
                        "variations", "vessel_size", "dangerous_products", "gaseous",
                        "concentration_mode")
#: Reaction fields the ELN sends (app/api/entities/reaction_entity.rb, v4.0.2) that the client's
#: properties do not carry; read from the raw payload so they are not lost.
_REACTION_RAW_FIELDS = ("ph_operator", "ph_value", "origin", "reaction_type", "role", "solvent",
                        "lock_reaction_volume")
#: Sample fields the ELN sends (sample_entity.rb) that the client's properties do not carry.
_SAMPLE_RAW_FIELDS = ("inventory_sample", "pubchem_tag", "elemental_compositions", "residues",
                      "components", "sample_details", "parent_id", "imported_readout")
#: A material's fields in one reaction that only the raw payload carries (reaction_material_entity.rb).
_MATERIAL_RAW_FIELDS = ("conversion_rate", "weight_percentage", "weight_percentage_reference")


# -- withheld values ----------------------------------------------------------------------------
# ELN v4 sends an element shared below full detail level with ``is_restricted`` set and each
# withheld field replaced by ``'***'`` (the client turns it into its ``WITHHELD`` sentinel, a str
# reading ``'***'`` that refuses to act as a number). The exporter leaves such a field out, keeps
# the element, and records the field's name, so the crate never carries the mask as a value and
# never drops an element because one of its numbers is hidden.

#: The server's mask for a withheld field (``chemotion_api.utils.withheld.SERVER_MASK``).
SERVER_MASK = "***"

#: Names of the fields withheld from the element being read; ``None`` outside a restricted one.
_withheld: ContextVar[list[str] | None] = ContextVar("chemotion_crate_withheld", default=None)


@contextmanager
def _restricted_scope(elem: Any, into: list[str]) -> Iterator[None]:
    """Collect withheld fields into ``into`` while reading ``elem``.

    Values are checked for the mask only on an element the client marks restricted, or one read
    inside such an element (a reaction's materials): elsewhere ``'***'`` is ordinary text.
    """
    try:
        restricted = bool(getattr(elem, "is_restricted", False))
    except Exception:
        restricted = False
    token = _withheld.set(into if restricted or _withheld.get() is not None else None)
    try:
        yield
    finally:
        _withheld.reset(token)


def _note_withheld(field: str | None) -> None:
    fields = _withheld.get()
    if fields is not None and field and field not in fields:
        fields.append(field)


def _is_withheld(value: Any, field: str | None = None) -> bool:
    """True, and the field noted, when ``value`` is the mask on a restricted element."""
    if _withheld.get() is None or not (isinstance(value, str) and value == SERVER_MASK):
        return False
    _note_withheld(field)
    return True


def _has_data(value: Any) -> bool:
    """Whether ``value`` says anything beyond the ELN's empty default.

    Units alone are not data (``{"amount": None, "unit": "ml"}``), nor is a flag that is off.
    """
    # By identity and type, not ==: 0 == False, and a purity of 0 is data.
    if value is None or value is False or (isinstance(value, str | list | Mapping) and not value):
        return False
    if isinstance(value, Mapping):
        return any(_has_data(v) for k, v in value.items() if k != "unit")
    if isinstance(value, list):
        return any(_has_data(v) for v in value)
    return True


#: ELN defaults that mean "not set": exported as nothing.
_UNSET = {"gas_type": ("off",), "stereo": ({"abs": "any", "rel": "any"},)}


def _keep(field: str, value: Any) -> bool:
    return _has_data(value) and value not in _UNSET.get(field, ())


def _str(value: Any, field: str | None = None) -> str | None:
    """``value`` as text, ``None`` when empty or withheld."""
    if value in (None, "") or _is_withheld(value, field):
        return None
    return str(value)


# -- sessions -----------------------------------------------------------------------------------

def make_session(instance: Any) -> Connection:
    """An authenticated :class:`~chemotion_api.connection.Connection` for ``instance``.

    The client keeps its own session private; this builds a second one from the public
    ``host_url`` and ``token``, for the few GETs the export makes itself (``/about`` for the
    version, ``ols_terms`` for the instance vocabulary).
    """
    session = Connection(instance.host_url)
    token = getattr(instance, "token", None)
    if token:
        session.set_bearer_token(token)
    return session


def read_source_info(instance: Any, session: Connection | None = None) -> SourceInfo:
    """The instance's host, version string and revision, from its ``/about`` page.

    Best-effort: a crate must say what it does not know rather than invent it, so an
    unreadable ``/about`` yields a source with no version.
    """
    host = (getattr(instance, "host_url", "") or "").strip("/") or None
    info = SourceInfo(host=host)
    if host is None:
        return info
    try:
        from chemotion_api.schema_manager import SchemaManager
        res = (session or make_session(instance)).get("/about", timeout=30)
        res.raise_for_status()
        manager = SchemaManager(res, check_version=False)
        info.version = getattr(manager, "version_string", None)
        info.revision = getattr(manager, "revision", None)
    except Exception as exc:  # never fatal; recorded as "unknown"
        logger.warning("Could not read the ELN version from %s/about: %s", host, exc)
    return info


# -- plain values -------------------------------------------------------------------------------

def plain(value: Any, field: str | None = None) -> Any:
    """Reduce a client value (PropertyDict, Temperature, datetime, …) to JSON-native data.

    A withheld value becomes ``None``; inside a mapping or list it is left out. ``field`` names
    the value for the record of withheld fields.
    """
    if _is_withheld(value, field):
        return None
    if isinstance(value, Enum):  # e.g. the client's GasType: its value is the ELN's own text
        value = value.value
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "to_dict"):
        try:
            return plain(value.to_dict(), field)
        except Exception:
            pass
    if isinstance(value, Mapping):
        out = {}
        for k, v in value.items():
            # A quantity's value is named by its field: "target_amount", not "target_amount.value".
            sub = field if (k == "value" and field) else (f"{field}.{k}" if field else str(k))
            if not _is_withheld(v, sub):
                out[str(k)] = plain(v, sub)
        return out
    if isinstance(value, list | tuple | set):
        return [plain(v, field) for v in value if not _is_withheld(v, field)]
    # Anything else (a client object, a Mock) has no plain rendering; a Python repr is never
    # crate content.
    return None


def _text(value: Any, field: str | None = None) -> str | None:
    """Plain text of a rich-text or string field; ``None`` when empty or withheld."""
    if value is None or _is_withheld(value, field):
        return None
    try:
        text = quill_to_text(value) if not isinstance(value, str) else value
    except Exception:
        return None
    text = (text or "").strip()
    return text or None


def _iso(value: Any, field: str | None = None) -> str | None:
    if _is_withheld(value, field):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _float(value: Any, field: str | None = None) -> float | None:
    """A finite float, or ``None``: the client reports an unset amount as ``NaN``.

    ``None`` too for a withheld value.
    """
    if value in (None, "") or _is_withheld(value, field):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _range(props: Mapping, raw: Mapping, field: str) -> str | None:
    """A melting/boiling point from the parsed bounds, else from the raw Ruby range."""
    low, high = props.get(f"{field}_lowerbound"), props.get(f"{field}_upperbound")
    if _is_withheld(low, field) or _is_withheld(high, field) or _is_withheld(raw.get(field), field):
        return None
    if low is not None or high is not None:
        if high is None or low == high:
            return str(low if low is not None else high)
        return f"{low}-{high}" if low is not None else str(high)
    value = raw.get(field)
    return normalise_range_value(str(value)) if value is not None else None


# -- attachments and analyses -------------------------------------------------------------------

def _attachment_record(att: Any) -> AttachmentRecord:
    get = att.get if hasattr(att, "get") else (lambda k, d=None: d)
    reader: Callable[[], Iterator[bytes]] | None = None
    if hasattr(att, "iter_file"):
        reader = att.iter_file
    elif hasattr(att, "load_file"):
        reader = lambda att=att: iter([att.load_file()])  # noqa: E731
    size = get("filesize") or get("contentSize")
    try:
        size = int(size) if size not in (None, "") else None
    except (TypeError, ValueError):
        size = None
    return AttachmentRecord(
        filename=str(get("filename") or "attachment"),
        identifier=str(get("identifier")) if get("identifier") else None,
        id=get("id"),
        filesize=size,
        mime_type=get("mime_type") or get("content_type"),
        description=_text(get("description")),
        created_at=_iso(get("created_at")),
        updated_at=_iso(get("updated_at")),
        read=reader,
    )


def _attachments(container: Any) -> list[AttachmentRecord]:
    try:
        items = list(container or [])
    except Exception as exc:
        logger.warning("Could not list attachments: %s", exc)
        return []
    return [_attachment_record(att) for att in items]


def _analyses(elem: Any) -> list[AnalysisRecord]:
    out: list[AnalysisRecord] = []
    try:
        analyses = list(elem.analyses or [])
    except Exception as exc:
        logger.warning("Could not read analyses of %s %s: %s", elem.element_type, elem.id, exc)
        return out
    for ana in analyses:
        get = ana.get if hasattr(ana, "get") else (lambda k, d=None: d)
        datasets = []
        for ds in getattr(ana, "datasets", None) or []:
            klass = (getattr(ds, "json_data", None) or {}).get("dataset") or {}
            datasets.append(DatasetRecord(
                id=getattr(ds, "id", None),
                name=getattr(ds, "name", None),
                klass_code=klass.get("klass_ols") if isinstance(klass, Mapping) else None,
                klass_label=klass.get("klass_label") if isinstance(klass, Mapping) else None,
                attachments=_attachments(getattr(ds, "attachments", None)),
            ))
        out.append(AnalysisRecord(
            id=getattr(ana, "id", None),
            name=get("name"),
            kind=get("kind") or getattr(ana, "type", None) or None,
            status=get("status"),
            content=_text(get("content")),
            datasets=datasets,
        ))
    return out


def _keywords(elem: Any) -> list[str]:
    keywords: list[str] = []
    try:
        # Accessed directly, not through getattr(..., []): a default would swallow an error raised
        # *inside* the property, and the labels would vanish from the crate without a word.
        for label in elem.user_labels_resolved or []:
            title = getattr(label, "title", None)
            if title:
                keywords.append(str(title))
    except Exception as exc:
        logger.warning("Could not resolve user labels of %s %s, so none are recorded: %s",
                       elem.element_type, getattr(elem, "id", "?"), exc)
    return keywords


# -- per-kind records ---------------------------------------------------------------------------

def _base_record(elem: Any, kind: str, host: str | None) -> ElementRecord:
    props = elem.properties if elem.properties is not None else {}
    url = None
    if host and getattr(elem, "id", None) is not None:
        try:
            url = elem.json_ld.get("@id") or None
        except Exception:
            url = None
        if not url:
            url = f"{host}/api/v1/{kind}s/{elem.id}"
    record = ElementRecord(
        kind=kind,
        id=elem.id,
        name=_str(elem.name, "name"),
        short_label=(_str(getattr(elem, "short_label", None), "short_label")
                     or _str(props.get("short_label"), "short_label")),
        url=url,
        created_at=_iso(getattr(elem, "created_at", None)),
        updated_at=_iso(getattr(elem, "last_update", None)),
        description=_text(props.get("description"), "description"),
        observation=_text(props.get("observation"), "observation"),
        keywords=_keywords(elem),
        analyses=_analyses(elem),
        attachments=_attachments(getattr(elem, "attachments", None)),
    )
    _add_segments(record, elem)
    return record


def _add_segments(rec: ElementRecord, elem: Any) -> None:
    """The element's LabIMotion segments (tabs the lab added), as ``segment / layer / field``.

    Only segments the element has: iterating never adds one (``segments.get`` would).
    """
    try:
        segments = elem.segments
        items = list(segments.items()) if isinstance(segments, Mapping) else []
    except Exception as exc:
        logger.debug("%s %s: no segments read: %s", rec.kind, rec.id, exc)
        return
    for segment, layers in items:
        if segment in ("Properties", "Analyses") or not isinstance(layers, Mapping):
            continue
        for layer, fields in layers.items():
            if not isinstance(fields, Mapping):
                continue
            for name, value in fields.items():
                _add_value(rec, f"{segment} / {layer} / {name}", value,
                           f"segment {segment}.{layer}.{name}")


def _sample_record(elem: Any, host: str | None) -> ElementRecord:
    rec = _base_record(elem, "sample", host)
    props = elem.properties if elem.properties is not None else {}
    raw = elem.json_data if isinstance(elem.json_data, Mapping) else {}
    rec.properties = {k: v for k in _SAMPLE_PROPERTIES if props.get(k) not in (None, "")
                      if _keep(k, v := plain(props.get(k), k))}
    sample_type = _str(raw.get("sample_type"), "sample_type")
    if sample_type:
        rec.properties["sample_type"] = sample_type
    for key in _SAMPLE_RAW_FIELDS:
        value = plain(raw.get(key), key)
        if _keep(key, value):
            rec.properties[key] = value
    for field in ("melting_point", "boiling_point"):
        value = _range(props, raw, field)
        if value:
            rec.properties[field] = value
    try:
        rec.role = elem.role_in_reaction if isinstance(getattr(elem, "role_in_reaction", None), str) else None
    except Exception:
        rec.role = None
    for attr, target in (("gram", "mass_g"), ("mol", "amount_mol"), ("liter", "volume_l")):
        try:
            setattr(rec, target, _float(getattr(elem, attr), attr))
        except PermissionError:  # the client's WithheldValueError: the amount behind it is withheld
            # gram, mol and liter are views of one withheld amount; name it once.
            if not {"target_amount", "real_amount"} & set(_withheld.get() or ()):
                _note_withheld("amount")
        except Exception as exc:
            logger.warning("Sample %s: could not read its %s, so it is exported without one: %s",
                           elem.id, attr, exc)
    rec.density = _float(props.get("density"), "density")
    rec.equivalent = _float(props.get("equivalent"), "equivalent")
    molarity = plain(props.get("molarity"), "molarity") or {}
    if isinstance(molarity, Mapping) and molarity.get("value") not in (None, ""):
        rec.molarity_value = _float(molarity.get("value"), "molarity.value")
        rec.molarity_unit = _str(molarity.get("unit"), "molarity.unit")
    try:
        mol = elem.molecule
    except Exception:
        mol = None
    if mol:
        cas = mol.get("cas")
        names = mol.get("names")
        rec.molecule = MoleculeRecord(
            id=getattr(mol, "id", None) or mol.get("id"),
            cano_smiles=mol.get("cano_smiles") or None,
            inchikey=mol.get("inchikey") or None,
            inchistring=mol.get("inchistring") or None,
            sum_formular=mol.get("sum_formular") or None,
            iupac_name=mol.get("iupac_name") or None,
            molecular_weight=_float(mol.get("molecular_weight")),
            # The ELN sends `cas: [null]` for a molecule without one; a None is no CAS number.
            cas=[str(c).strip() for c in (cas if isinstance(cas, list | tuple) else [cas] if cas else [])
                 if c is not None and str(c).strip()],
            names=[str(n) for n in ([names] if isinstance(names, str) else names or []) if n],
        )
    return rec


def _reaction_record(elem: Any, host: str | None) -> ElementRecord:
    rec = _base_record(elem, "reaction", host)
    props = elem.properties if elem.properties is not None else {}
    raw = elem.json_data if isinstance(elem.json_data, Mapping) else {}
    if rec.description is None:
        rec.description = _text(props.get("reaction_description"), "reaction_description")
    for key in _REACTION_PROPERTIES:
        value = props.get(key)
        if value in (None, "", [], {}) and key in ("tlc_description", "rinchi_long_key",
                                                   "rinchi_short_key", "rinchi_web_key"):
            value = raw.get(key)
        if value not in (None, "", [], {}):
            value = plain(value, key)
            if _keep(key, value):
                rec.properties[key] = value
    rxno = props.get("rxno")
    rec.rxno = (_str(rxno, "rxno") or "").strip() or None if isinstance(rxno, str) else None
    duration = props.get("duration")
    if isinstance(duration, Mapping):  # the client may give {value, unit} instead of the ELN's text
        value = plain(duration, "duration") or {}
        rec.duration = (f"{value['value']} {value['unit']}"
                        if _has_data(value.get("value")) and value.get("unit") else None)
    else:
        rec.duration = _str(duration, "duration")
    rec.timestamp_start = _iso(props.get("timestamp_start"), "timestamp_start")
    rec.timestamp_stop = _iso(props.get("timestamp_stop"), "timestamp_stop")
    rec.yield_percent = _float(props.get("yield"), "yield")
    rec.temperature = plain(props.get("temperature"), "temperature") or {}
    rec.status = _str(props.get("status"), "status")
    for key in _REACTION_RAW_FIELDS:
        value = plain(raw.get(key), key)
        if _keep(key, value):
            # "role" is the reaction's own (e.g. a general procedure); a sample's role in a
            # reaction is another field, so the reaction's is stored under its own name.
            rec.properties["reaction_role" if key == "role" else key] = value
    if "ph_value" not in rec.properties:
        rec.properties.pop("ph_operator", None)  # an operator with no value says nothing
    for role in ("starting_materials", "reactants", "products", "solvents", "purification_solvents"):
        try:
            samples = list(getattr(elem, role) or [])
        except Exception as exc:
            logger.warning("Reaction %s: could not read its %s: %s", elem.id, role, exc)
            continue
        rec.materials[role] = []
        for sample in samples:
            material_withheld: list[str] = []
            with _restricted_scope(sample, material_withheld):
                material = _sample_record(sample, host)
            material.withheld = material_withheld
            material.role = material.role or role
            for key in _MATERIAL_LINK_PROPERTIES:
                value = plain(sample.properties.get(key), key)
                if _keep(key, value):
                    material.properties[key] = value
            material_raw = sample.json_data if isinstance(getattr(sample, "json_data", None), Mapping) else {}
            for key in _MATERIAL_RAW_FIELDS:
                value = plain(material_raw.get(key), key)
                if _keep(key, value):
                    material.properties[key] = value
            # A solvent is a sample in the reaction's solvent list; read from the list itself,
            # not from the client's private ``Sample._is_solvent``.
            material.is_solvent = role == "solvents"
            rec.materials[role].append(material)
            # The reaction carries its materials, so it states what was withheld from them too.
            for name in material_withheld:
                _note_withheld(f"{role} sample {material.id}: {name}")
    return rec


def _wellplate_record(elem: Any, host: str | None) -> ElementRecord:
    rec = _base_record(elem, "wellplate", host)
    try:
        for x, col in enumerate(elem.wells or []):
            for y, sample in col.items():
                if sample is None:
                    continue
                try:
                    label = (_str(sample.properties.get("short_label"), "well label")
                             or _str(sample.name, "well label")
                             or (sample.molecule or {}).get("cano_smiles"))
                except Exception:
                    label = None
                rec.property_values.append({"name": f"well {y}{x + 1}", "value": label or "occupied"})
    except Exception as exc:
        logger.warning("Wellplate %s: could not read its wells: %s", elem.id, exc)
    return rec


def _research_plan_record(elem: Any, host: str | None) -> ElementRecord:
    rec = _base_record(elem, "research_plan", host)
    texts: list[str] = []
    try:
        for entry in elem.body or []:
            value = entry.get("value") if isinstance(entry, Mapping) else getattr(entry, "value", None)
            text = _text(value, "body") if value is not None else None
            if text:
                texts.append(text)
    except Exception as exc:
        logger.warning("Research plan %s: could not read its body: %s", elem.id, exc)
    if texts and not rec.description:
        rec.description = "\n".join(texts)
    return rec


def _device_record(elem: Any, host: str | None) -> ElementRecord:
    rec = _base_record(elem, "device_description", host)
    props = elem.properties if elem.properties is not None else {}
    try:
        from chemotion_api.elements.device_description import DEVICE_FIELDS
    except ImportError:  # pragma: no cover - older client
        DEVICE_FIELDS = tuple(props.keys())
    for key in DEVICE_FIELDS:
        if key in ("name", "description", "short_label"):
            continue
        value = plain(props.get(key), key)
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if v not in (None, ""))
            if not value:
                continue
        rec.property_values.append({"name": key.replace("_", " "), "value": str(value)})
    try:
        rec.ontology_terms = [dict(t) for t in elem.ontology_terms() or []]
    except Exception as exc:
        logger.warning("Device description %s: could not read its ontology terms: %s", elem.id, exc)
    return rec


def _add_value(rec: ElementRecord, name: str, value: Any, field: str | None = None) -> None:
    """Append ``name: value`` to ``rec.property_values``; a ``{value, unit}`` pair keeps its unit."""
    if isinstance(value, Mapping) and _is_withheld(value.get("value"), field or name):
        return  # a withheld quantity is named by its field, not as "<field>.value"
    value = plain(value, field or name)
    if isinstance(value, Mapping) and "value" in value:
        if not _has_data(value.get("value")):
            return
        inner = value["value"]
        pv: dict[str, Any] = {"name": name.replace("_", " "),
                              "value": _json_text(inner) if isinstance(inner, Mapping | list) else inner}
        if value.get("unit"):
            pv["unitText"] = str(value["unit"])
        rec.property_values.append(pv)
        return
    if not _has_data(value):
        return
    if isinstance(value, list) and all(isinstance(v, str | int | float | bool) for v in value if v is not None):
        value = ", ".join(str(v) for v in value if v not in (None, ""))
    elif isinstance(value, Mapping | list):
        value = _json_text(value)  # a table or a nested structure: JSON, never a Python repr
    rec.property_values.append({"name": name.replace("_", " "), "value": value})


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


#: Cell-line sample fields, then the fields of the cell-line material it belongs to.
_CELL_LINE_FIELDS = ("passage", "contamination")
_CELL_LINE_MATERIAL_FIELDS = ("name", "source", "organism", "tissue", "disease", "cell_type",
                              "biosafety_level", "growth_medium", "variant", "mutation",
                              "optimal_growth_temp", "cryo_pres_medium", "gender", "description")


def _cell_line_record(elem: Any, host: str | None) -> ElementRecord:
    rec = _base_record(elem, "cell_line", host)
    props = elem.properties if elem.properties is not None else {}
    _add_value(rec, "amount", {"value": props.get("amount"), "unit": props.get("unit")})
    for key in _CELL_LINE_FIELDS:
        _add_value(rec, key, props.get(key))
    try:
        material = elem.material or {}
    except Exception as exc:
        logger.warning("Cell line %s: could not read its material: %s", elem.id, exc)
        material = {}
    for key in _CELL_LINE_MATERIAL_FIELDS:
        _add_value(rec, f"material {key}", material.get(key), f"material.{key}")
    return rec


_SCREEN_FIELDS = ("collaborator", "requirements", "conditions", "result")


def _screen_record(elem: Any, host: str | None) -> ElementRecord:
    rec = _base_record(elem, "screen", host)
    props = elem.properties if elem.properties is not None else {}
    for key in _SCREEN_FIELDS:
        text = _text(props.get(key), key)
        if text:
            rec.property_values.append({"name": key, "value": text})
    for kind, attr in (("wellplate", "wellplate_ids"), ("research_plan", "research_plan_ids")):
        try:
            rec.links += [[kind, i] for i in (getattr(elem, attr) or []) if i is not None]
        except Exception as exc:
            logger.warning("Screen %s: could not read its %s: %s", elem.id, attr, exc)
    return rec


#: SBMM sample scalars and value/unit quantities, then fields of the protein it is a sample of.
_SBMM_FIELDS = ("external_label", "function_or_application", "inventory_sample", "obtained_by",
                "supplier", "formulation", "purity", "purity_detection", "purification_method")
_SBMM_QUANTITIES = ("concentration", "concentration_rt", "molarity", "activity_per_volume",
                    "activity_per_mass", "volume_as_used", "amount_as_used_mol",
                    "amount_as_used_mass", "activity")
_SBMM_PROTEIN_FIELDS = ("primary_accession", "short_name", "full_name", "sbmm_type", "sbmm_subtype",
                        "organism", "taxon_id", "molecular_weight", "sequence_length",
                        "link_uniprot", "link_pdb", "pdb_doi", "ec_numbers")


def _sbmm_record(elem: Any, host: str | None) -> ElementRecord:
    rec = _base_record(elem, "sequence_based_macromolecule_sample", host)
    props = elem.properties if elem.properties is not None else {}
    for key in (*_SBMM_FIELDS, *_SBMM_QUANTITIES):
        _add_value(rec, key, props.get(key))
    try:
        protein = elem.macromolecule or {}
    except Exception as exc:
        logger.warning("SBMM sample %s: could not read its macromolecule: %s", elem.id, exc)
        protein = {}
    for key in _SBMM_PROTEIN_FIELDS:
        _add_value(rec, f"protein {key}", protein.get(key), f"protein.{key}")
    return rec


def _vessel_record(vessel: Any, host: str | None) -> ElementRecord:
    """A vessel. Not an element in the client (no properties, analyses or attachments), so it is
    read from its own fields and its template (type, material, volume)."""
    raw = getattr(vessel, "raw", None) or {}
    rec = ElementRecord(
        kind="vessel", id=vessel.id,
        name=_str(getattr(vessel, "name", None), "name"),
        short_label=_str(getattr(vessel, "short_label", None), "short_label"),
        url=f"{host}/api/v1/vessels/{vessel.id}" if host else None,
        created_at=_iso(raw.get("created_at"), "created_at"),
        updated_at=_iso(raw.get("updated_at"), "updated_at"),
        description=_text(getattr(vessel, "description", None), "description"),
    )
    for key in ("bar_code", "qr_code"):
        _add_value(rec, key, getattr(vessel, key, None))
    _add_value(rec, "weight", {"value": getattr(vessel, "weight_amount", None),
                               "unit": getattr(vessel, "weight_unit", None)})
    template = getattr(vessel, "template", None)
    if template is not None:
        for key in ("name", "vessel_type", "material_type", "material_details", "details"):
            _add_value(rec, f"template {key}", getattr(template, key, None), f"template.{key}")
        _add_value(rec, "template volume", {"value": getattr(template, "volume_amount", None),
                                            "unit": getattr(template, "volume_unit", None)})
    return rec


def _generic_record(elem: Any, kind: str, host: str | None) -> ElementRecord:
    rec = _base_record(elem, kind, host)
    props = plain(elem.properties) or {}

    def _add(name: str, value: Any) -> None:
        if value in (None, "", [], {}):
            return
        pv: dict[str, Any] = {"name": name}
        if isinstance(value, Mapping) and "value" in value and "unit" in value:
            if value.get("value") in (None, ""):
                return
            pv["value"] = value["value"]
            if value.get("unit"):
                pv["unitText"] = str(value["unit"])
        elif isinstance(value, Mapping):
            for sub_key, sub_value in value.items():
                _add(f"{name}.{sub_key}", sub_value)
            return
        elif isinstance(value, str | int | float | bool):
            pv["value"] = value
        elif isinstance(value, list) and all(isinstance(v, str | int | float | bool) for v in value):
            pv["value"] = ", ".join(str(v) for v in value)
        else:
            # A table or a nested structure: carried as JSON, never as a Python repr.
            pv["value"] = json.dumps(value, ensure_ascii=False, default=str)
        rec.property_values.append(pv)

    for key, value in props.items():
        _add(str(key), value)
    return rec


def element_record(elem: Any, kind: str, host: str | None) -> ElementRecord:
    """Build the record for one loaded element of ``kind``.

    Fields the server withheld (the element is shared below full detail level) are left out and
    named in ``record.withheld``.
    """
    withheld: list[str] = []
    with _restricted_scope(elem, withheld):
        if kind == "sample":
            rec = _sample_record(elem, host)
        elif kind == "reaction":
            rec = _reaction_record(elem, host)
        elif kind == "wellplate":
            rec = _wellplate_record(elem, host)
        elif kind == "research_plan":
            rec = _research_plan_record(elem, host)
        elif kind == "device_description":
            rec = _device_record(elem, host)
        elif kind == "cell_line":
            rec = _cell_line_record(elem, host)
        elif kind == "screen":
            rec = _screen_record(elem, host)
        elif kind == "sequence_based_macromolecule_sample":
            rec = _sbmm_record(elem, host)
        elif kind == "vessel":
            rec = _vessel_record(elem, host)
        else:
            rec = _generic_record(elem, kind, host)
    rec.withheld = withheld
    return rec


# -- the walk -----------------------------------------------------------------------------------

def extract(instance: Any, collection: Any, source: SourceInfo | None = None) -> ExportModel:
    """Walk ``collection`` and its descendants into an :class:`ExportModel`.

    An element reachable through several collections is one record with several
    ``collection_ids``; it is loaded once. A loop in the collection tree is skipped and recorded.

    :param instance: the :class:`chemotion_api.Instance` the collection belongs to; it names the
        element types the instance knows (for LabIMotion generic kinds) and the host.
    :param collection: the collection to export.
    :param source: pre-read source info; read from ``/about`` when omitted.
    """
    source = source or read_source_info(instance)
    host = source.host
    _col_id = _CollectionIds()
    model = ExportModel(root_collection_id=_col_id(collection), source=source)
    records: dict[ElementKey, ElementRecord] = {}
    visited: set[Any] = {_col_id(collection)}

    try:
        # chemotion-api adds a synthetic "generic_element" entry; it names no real LabIMotion
        # class, so listing it is a wasted request per collection. Only real generic classes.
        generic_kinds = [name for name, info in (instance.all_element_classes or {}).items()
                         if name not in MODELLED_KINDS and name != "generic_element"
                         and (not isinstance(info, Mapping) or info.get("is_generic", True))]
    except Exception as exc:
        logger.warning("Could not list the instance's element types; generic elements are not "
                       "exported: %s", exc)
        generic_kinds = []

    def _index(col_id: Any, getter: Callable[[], Any], kind: str) -> None:
        try:
            items = list(getter())
        except Exception as exc:
            logger.warning("Could not retrieve %s for collection %s: %s", kind, col_id, exc)
            model.skipped.append({"kind": kind, "stage": "retrieve", "collection": col_id,
                                  "error": str(exc)})
            return
        for item in items:
            if getattr(item, "id", None) is None:
                logger.warning("A listed %s carries no id and cannot be exported.", kind)
                model.skipped.append({"kind": kind, "stage": "load", "id": None,
                                      "error": "listed element carries no id"})
                continue
            key: ElementKey = (kind, item.id)
            if key in records:
                if col_id not in records[key].collection_ids:
                    records[key].collection_ids.append(col_id)
                continue
            try:
                # A vessel is complete as listed and has no load().
                if not getattr(item, "is_loaded", True):
                    item.load()
                record = element_record(item, kind, host)
            except Exception as exc:
                logger.warning("Could not load %s %s: %s", kind, key[1], exc)
                model.skipped.append({"kind": kind, "stage": "load", "id": key[1], "error": str(exc)})
                continue
            if record.withheld:
                logger.info("%s %s is shared below full detail level; withheld, so left out: %s",
                            kind, key[1], ", ".join(record.withheld))
                model.skipped.append({"kind": f"{kind} field", "stage": "withheld", "id": key[1],
                                      "count": len(record.withheld)})
            record.collection_ids.append(col_id)
            records[key] = record
            model.elements.append(record)

    def _walk(col: Any) -> None:
        col_id = _col_id(col)
        parent = getattr(col, "parent", None)
        info = getattr(col, "collection_json", None) or {}
        model.collections.append(CollectionRecord(
            id=col_id, label=getattr(col, "label", None) or None,
            has_server_id=getattr(col, "id", None) is not None,
            description=(info.get("description") or None) if isinstance(info, Mapping) else None,
            parent_id=_col_id(parent) if parent is not None and _col_id(parent) in visited else None,
        ))
        _index(col_id, lambda: col.get_samples(per_page=PER_PAGE), "sample")
        _index(col_id, lambda: col.get_reactions(per_page=PER_PAGE), "reaction")
        _index(col_id, lambda: col.get_wellplates(per_page=PER_PAGE), "wellplate")
        _index(col_id, lambda: col.get_research_plans(per_page=PER_PAGE), "research_plan")
        if hasattr(col, "get_device_descriptions"):
            _index(col_id, lambda: col.get_device_descriptions(per_page=PER_PAGE), "device_description")
        _index(col_id, lambda: col.get_cell_lines(per_page=PER_PAGE), "cell_line")
        _index(col_id, lambda: col.get_screens(per_page=PER_PAGE), "screen")
        _index(col_id, lambda: col.get_sbmm_samples(per_page=PER_PAGE),
               "sequence_based_macromolecule_sample")
        _index(col_id, col.get_vessels, "vessel")
        for kind in generic_kinds:
            def _generics(kind: str = kind) -> Any:
                return col.get_generics_by_name(kind, per_page=PER_PAGE)
            _index(col_id, _generics, kind)
        for child in getattr(col, "children", None) or []:
            child_id = _col_id(child)
            if child_id in visited:
                logger.warning("Collection tree revisits %r; skipping to avoid looping.", child.label)
                model.skipped.append({"kind": "collection", "stage": "index", "id": child_id,
                                      "error": "collection tree contains a loop"})
                continue
            visited.add(child_id)
            _walk(child)

    _walk(collection)
    return model


class _CollectionIds:
    """Collection ids for one extraction: the server id, or ``local-N`` for one without.

    Numbered afresh by every extraction (two exports of the same data agree), and keyed by the
    object itself, held for the walk, so a reused memory address cannot inherit a number.
    """

    def __init__(self) -> None:
        self._local: dict[int, tuple[Any, str]] = {}

    def __call__(self, col: Any) -> Any:
        server_id = getattr(col, "id", None)
        if server_id is not None:
            return server_id
        held = self._local.get(id(col))
        if held is None or held[0] is not col:
            held = (col, f"local-{len(self._local) + 1}")
            self._local[id(col)] = held
        return held[1]
