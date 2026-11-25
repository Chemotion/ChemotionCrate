"""Map export records to chem-dcat-ap entities.

A :class:`~chemotion_crate.model.ElementRecord` of kind ``sample`` becomes a chem-dcat-ap
substance entity (``StartingMaterial``, ``Reagent``, ``ChemicalProduct``,
``DissolvingSubstance`` or ``SubstanceSample`` depending on its role), one of kind ``reaction`` a
``ChemicalReaction`` aggregating its materials. Every other kind has no chem-dcat-ap class and is
described as a plain RO-Crate ``Dataset`` by :mod:`chemotion_crate.crate`.

The mapping works on the plain model only, so it is testable from a fixture and never calls the
server.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

import isodate
from pydantic import BaseModel

from chemotion_crate.model import ElementRecord
from chemotion_crate.qudt import qudt_unit

logger = logging.getLogger(__name__)

QUDT_KIND = "http://qudt.org/vocab/quantitykind/"
QUDT_UNIT = "http://qudt.org/vocab/unit/"

#: Scheme under which a molecule's node is minted before the crate builder turns it into a
#: ``#molecule_<id>`` fragment. One molecule shared by several samples is then one node.
MOLECULE_SCHEME = "chemotion://molecule/"

_DURATION_UNIT_SECONDS = {
    "second": 1, "sec": 1, "s": 1,
    "minute": 60, "min": 60, "m": 60,
    "hour": 3600, "hr": 3600, "h": 3600,
    "day": 86400, "d": 86400,
    "week": 604800, "wk": 604800, "w": 604800,
}


def chemotion_duration_to_iso8601(text: str | None) -> str | None:
    """``"3.5 Hour(s)"`` -> ``"PT3H30M"``; ``None`` when the string cannot be read.

    chem-dcat-ap emits the duration as an ``xsd:duration`` literal, which must be ISO 8601; the
    ELN stores a display string. Fractions are normalised into whole H/M/S by ``isodate``.
    """
    if not text:
        return None
    m = re.match(r"\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]+)", str(text))
    if not m:
        return None
    unit = re.sub(r"[^a-z]", "", m.group(2).lower())
    if unit.endswith("s") and unit not in _DURATION_UNIT_SECONDS:
        unit = unit[:-1]
    if unit not in _DURATION_UNIT_SECONDS:
        return None
    seconds = round(float(m.group(1)) * _DURATION_UNIT_SECONDS[unit])
    if seconds < 0:
        return None
    return isodate.duration_isoformat(timedelta(seconds=seconds))


def _elapsed_iso8601(start: str | None, stop: str | None) -> str | None:
    """The duration between two ISO timestamps, or ``None`` when either is missing or odd."""
    if not start or not stop:
        return None
    try:
        delta = datetime.fromisoformat(stop) - datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    if delta.total_seconds() < 0:
        return None
    return isodate.duration_isoformat(timedelta(seconds=int(delta.total_seconds())))


def _quantity(cls, value, kind: str, unit: str | None, title: str):
    """Build one chem-dcat-ap quantity, or ``None`` when the value is not numeric.

    ``unit=None`` (an unknown Chemotion unit) leaves the field out rather than guessing.
    """
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        logger.warning("Skipping %s in the DCAT-AP mapping (value=%r): %s", title, value, exc)
        return None
    kwargs = {"value": number, "has_quantity_type": QUDT_KIND + kind, "title": title}
    if unit:
        kwargs["unit"] = unit
    return cls(**kwargs)


def dcat_id(rec: ElementRecord) -> str:
    """The IRI an element's chem-dcat-ap entity is dumped under (rewritten to a crate id later)."""
    return rec.url or f"urn:chemotion:{rec.kind}:{rec.id}"


def material_ref(reaction: ElementRecord, role: str, index: int) -> str:
    """The IRI of the ``index``-th material in ``role`` of ``reaction``.

    One per reaction and role: a sample used in two reactions is two materials, each with its own
    amounts, and both point to the sample.
    """
    return f"{dcat_id(reaction)}#{role}-{index}"


def sample_to_dcat(rec: ElementRecord, node_id: str | None = None,
                   stock: bool = False) -> BaseModel:
    """Map a sample record to its chem-dcat-ap substance entity.

    :param node_id: the IRI to dump it under; a reaction's material passes its
        :func:`material_ref`.
    :param stock: the sample itself, as distinct from its use in a reaction: typed by what it is
        (``SubstanceSample``, ``DissolvingSubstance``), not by its role in one reaction.
    """
    from chem_dcat_ap.datamodel.chem_dcat_ap_pydantic import (
        AmountOfSubstance, ChemicalEntity, ChemicalProduct, Concentration, DefinedTerm, Density,
        DissolvingSubstance, Identifier, InChi, InChIKey, IUPACName, Mass, MolarEquivalent,
        MolarMass, MolecularFormula, Reagent, SMILES, StartingMaterial, SubstanceSample, Volume,
    )

    sample_id = node_id or dcat_id(rec)
    target_cls = {
        "starting_materials": StartingMaterial,
        "reactants": Reagent,
        "products": ChemicalProduct,
        "solvents": DissolvingSubstance,
    }.get("" if stock else rec.role or "", DissolvingSubstance if rec.is_solvent else SubstanceSample)

    composed_of = []
    mol = rec.molecule
    if mol is not None:
        mol_id = f"{MOLECULE_SCHEME}{mol.id}" if mol.id else f"{sample_id}#EvaluatedCompound"
        formula = mol.sum_formular or rec.properties.get("sum_formula")
        molar_mass = mol.molecular_weight or rec.properties.get("molecular_mass")
        other_ids = [Identifier(notation=cas, title="CAS Registry Number") for cas in mol.cas if cas]
        other_ids += [Identifier(notation=name, title="Synonym") for name in mol.names if name]
        molar_mass_list = []
        if molar_mass:
            quantity = _quantity(MolarMass, molar_mass, "MolarMass", QUDT_UNIT + "GM-PER-MOL",
                                 "Molar mass [g/mol]")
            if quantity is not None:
                molar_mass_list.append(quantity)
        composed_of.append(ChemicalEntity(
            id=mol_id,
            description=f"compound assigned to {sample_id}",
            smiles=[SMILES(value=mol.cano_smiles, title="assigned SMILES")] if mol.cano_smiles else None,
            inchi=[InChi(value=mol.inchistring, title="assigned InChI")] if mol.inchistring else None,
            inchikey=[InChIKey(value=mol.inchikey, title="assigned InChIKey")] if mol.inchikey else None,
            molecular_formula=[MolecularFormula(value=formula, title="assigned molecular formula")]
            if formula else None,
            iupac_name=[IUPACName(value=mol.iupac_name, description="Chemotion IUPAC name")]
            if mol.iupac_name else None,
            has_molar_mass=molar_mass_list or None,
            other_identifier=other_ids or None,
        ))

    def _milli(cls, value: float | None, kind: str, unit: str, title: str):
        """A quantity stated in the milli-unit, or ``None`` for an absent or non-positive value."""
        if value is None or value <= 0:
            return None
        return [_quantity(cls, value * 1000.0, kind, QUDT_UNIT + unit, title)]

    has_mass = _milli(Mass, rec.mass_g, "Mass", "MilliGM", "Mass [mg]")
    has_volume = _milli(Volume, rec.volume_l, "Volume", "MilliL", "Volume [mL]")
    has_amount = _milli(AmountOfSubstance, rec.amount_mol, "AmountOfSubstance", "MilliMOL",
                        "Amount [mmol]")
    has_density = ([_quantity(Density, rec.density, "Density", QUDT_UNIT + "GM-PER-MilliL",
                              "Density [g/mL]")] if rec.density else None)
    has_concentration = None
    if rec.molarity_value is not None:
        has_concentration = [_quantity(
            Concentration, rec.molarity_value, "AmountOfSubstanceConcentration",
            qudt_unit(rec.molarity_unit or "M"), f"Molarity [{rec.molarity_unit or 'M'}]")]
    has_molar_equivalent = None
    if rec.equivalent is not None:
        # UNITLESS, not PERCENT: an equivalent of 1.0 tagged PERCENT reads as 1 % to a unit-aware
        # consumer, a hundredfold error. It is a pure ratio.
        has_molar_equivalent = [_quantity(MolarEquivalent, rec.equivalent, "Dimensionless",
                                          QUDT_UNIT + "UNITLESS", "Molar Equivalent")]

    kwargs = {
        "id": sample_id,
        "title": rec.label,
        "description": rec.description or None,
        "rdf_type": DefinedTerm(id="CHEBI:59999", title="chemical substance"),
        "composed_of": composed_of or None,
        "has_mass": _drop_none(has_mass),
        "has_volume": _drop_none(has_volume),
        "has_amount": _drop_none(has_amount),
        "has_density": _drop_none(has_density),
        "has_concentration": _drop_none(has_concentration),
    }
    if target_cls in (StartingMaterial, Reagent) and has_molar_equivalent:
        kwargs["has_molar_equivalent"] = _drop_none(has_molar_equivalent)
    return target_cls(**kwargs)


def _drop_none(items):
    if not items:
        return None
    kept = [item for item in items if item is not None]
    return kept or None


def reaction_to_dcat(rec: ElementRecord) -> BaseModel:
    """Map a reaction record to a chem-dcat-ap ``ChemicalReaction``."""
    from chem_dcat_ap.datamodel.chem_dcat_ap_pydantic import (
        ChemicalReaction, DefinedTerm, Identifier, QualitativeAttribute, Temperature, Yield,
    )

    reaction_id = dcat_id(rec)
    materials = {role: [sample_to_dcat(m, material_ref(rec, role, i))
                        for i, m in enumerate(rec.materials.get(role, []))]
                 for role in ("starting_materials", "reactants", "products", "solvents")}

    has_duration = _elapsed_iso8601(rec.timestamp_start, rec.timestamp_stop)
    if not has_duration and rec.duration:
        has_duration = chemotion_duration_to_iso8601(rec.duration)

    has_yield = None
    if rec.yield_percent is not None:
        has_yield = _drop_none([_quantity(Yield, rec.yield_percent, "Dimensionless",
                                          QUDT_UNIT + "PERCENT", "Yield [%]")])

    has_temperature = None
    temp = rec.temperature or {}
    unit = qudt_unit(temp.get("valueUnit") or "°C")
    steps = [tp for tp in (temp.get("data") or [])
             if isinstance(tp, dict) and tp.get("value") not in (None, "")]
    if steps:
        has_temperature = _drop_none([
            _quantity(Temperature, tp["value"], "Temperature", unit, "Temperature step")
            for tp in steps])
    elif str(temp.get("userText") or "").strip():
        # A single temperature typed into the field rather than a time series: the common case.
        has_temperature = _drop_none([_quantity(Temperature, str(temp["userText"]).strip(),
                                                "Temperature", unit, "Temperature")])

    has_qualitative_attribute = None
    if rec.status:
        has_qualitative_attribute = [QualitativeAttribute(
            value=str(rec.status), title="Status", description="The status of the reaction",
            rdf_type=DefinedTerm(id="SIO:001326", title="status descriptor"))]

    # RInChI: long, short and web key, so a consumer can match on whichever it indexes.
    other_identifier = []
    for prop, label in (("rinchi_long_key", "RInChI"), ("rinchi_short_key", "RInChI short key"),
                        ("rinchi_web_key", "RInChI web key")):
        value = rec.properties.get(prop)
        if isinstance(value, str) and value.strip():
            other_identifier.append(Identifier(notation=value.strip(), title=label))

    return ChemicalReaction(
        id=reaction_id,
        other_identifier=other_identifier or None,
        title=[rec.name] if rec.name else [f"Reaction-{rec.id}"],
        description=[rec.description] if rec.description else None,
        used_starting_material=materials["starting_materials"] or None,
        used_reactant=materials["reactants"] or None,
        generated_product=materials["products"] or None,
        used_solvent=materials["solvents"] or None,
        has_duration=has_duration,
        has_yield=has_yield,
        has_temperature=has_temperature,
        has_qualitative_attribute=has_qualitative_attribute,
    )


_MAPPERS = {"sample": sample_to_dcat, "reaction": reaction_to_dcat}


def has_dcat_mapping(rec: ElementRecord) -> bool:
    return rec.kind in _MAPPERS


def to_dcat(rec: ElementRecord) -> BaseModel:
    """Map a record to chem-dcat-ap, or raise ``KeyError`` for a kind without a mapping."""
    return _MAPPERS[rec.kind](rec)
