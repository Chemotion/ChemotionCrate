"""Registry mapping Chemotion element kinds to ontology class IRIs.

``sample`` and ``reaction`` are annotated richly through chem-dcat-ap and are NOT listed here.
The kinds below do not yet have an agreed ontology term. They are exported as spec-conformant
RO-Crate ``Dataset`` nodes carrying a forward-compatible ontology *placeholder* (a
``PropertyValue`` in ``variableMeasured``); when a term is decided, fill its IRI in here and
re-export, or patch existing ``.eln`` files with :mod:`chemotion_crate.patch`.
"""

#: ``propertyID`` used to tag the ontology placeholder PropertyValue so it can be located later.
ELEMENT_TYPE_PROPERTY_ID = "chemotion:elementType"

#: element_type -> ontology class IRI (or None = "to be defined later").
ELEMENT_TYPE_IRI: dict[str, str | None] = {
    "wellplate": None,
    "research_plan": None,
    # A device description often carries its own TIB-sourced terms, which the exporter adds as
    # additional @types. This placeholder is for the *element kind* itself.
    "device_description": None,
    "screen": None,
    "cell_line": None,
    "sequence_based_macromolecule_sample": None,
    "vessel": None,
    # Generic LabIMotion element types share a single placeholder until typed individually.
    "generic": None,
}


def iri_for(element_kind: str) -> str | None:
    """Return the ontology IRI for an element kind, or None if not yet defined."""
    if element_kind in ELEMENT_TYPE_IRI:
        return ELEMENT_TYPE_IRI[element_kind]
    # Unknown/custom generic types fall back to the generic placeholder.
    return ELEMENT_TYPE_IRI.get("generic")


def ontology_placeholder(element_kind: str) -> dict:
    """Build a spec-conformant ontology placeholder PropertyValue for an element kind.

    Per TheELNFileFormat, ``variableMeasured`` entries are ``PropertyValue`` nodes whose
    ``identifier`` MAY be an ontology IRI. The IRI is emitted when known; otherwise the key is
    left out (an empty value is flagged by the conformance suite) for the patch helper to fill.
    """
    iri = iri_for(element_kind)
    placeholder = {
        "@type": "PropertyValue",
        "propertyID": ELEMENT_TYPE_PROPERTY_ID,
        "name": "chemotion element type",
        "value": element_kind,
    }
    if iri:
        placeholder["identifier"] = iri
    return placeholder
