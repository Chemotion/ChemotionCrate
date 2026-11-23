"""The RO-Crate 1.2 context terms this exporter emits.

The official context (``https://w3id.org/ro/crate/1.2/context``) defines every schema.org class
and property, 2 899 terms and 184 KB. A crate uses a few dozen of them. ``data/ro-crate-context.jsonld``
is the subset listed here plus every prefix declaration of the official context, extracted
verbatim by ``scripts/update_context_subset.py`` so compaction against it agrees with the context
URL the crate declares. A term missing from this list is caught by
``tests/test_context_subset.py``: add it here and rerun the script.
"""

#: Bare terms (classes and properties) a crate may carry as keys or ``@type`` values.
CONTEXT_TERMS: frozenset[str] = frozenset({
    # classes
    "Collection", "ContactPoint", "CreateAction", "CreativeWork", "Dataset", "DefinedTerm",
    "File", "Organization", "Person", "Profile", "PropertyValue", "SoftwareApplication", "Thing",
    # properties
    "about", "actionStatus", "affiliation", "alternateName", "author", "conformsTo",
    "contactPoint", "contactType", "contentSize", "dateCreated", "dateModified", "datePublished",
    "description", "duration", "email", "encodingFormat", "endTime", "genre", "hasPart",
    "identifier", "instrument", "isBasedOn", "isPartOf", "keywords", "license",
    "measurementTechnique", "mentions", "name", "object", "propertyID", "publisher",
    "sdPublisher", "sha256", "softwareRequirements", "text", "unitText", "url", "value",
    "variableMeasured", "version",
})
