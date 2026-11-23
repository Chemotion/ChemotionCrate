"""Post-serialisation passes over a crate's ``ro-crate-metadata.json``.

The crate is first built as RDF (chem-dcat-ap) and serialised to JSON-LD by rdflib. What comes out
is valid JSON-LD but not yet a conformant RO-Crate: it has blank nodes, nested anonymous entities,
typed literals, stuttering CURIEs, undescribed references and keys the context does not define.
Each function here fixes one family of those, in place, on the parsed metadata dictionary.

The comments record *why* each rule exists — most are findings from TheELNFileFormat's checker and
from ``roc-validator`` — because without them several of these passes look like needless fuss.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from chemotion_crate.namespaces import Namespaces, prefixes_of_context

logger = logging.getLogger(__name__)

#: The metadata descriptor's ``@id``.
METADATA_ID = "ro-crate-metadata.json"

#: Types that make a node a *data* entity rather than a contextual one.
DATA_ENTITY_TYPES = {"Dataset", "File"}

#: Matches the default ``object.__repr__`` shape (``<module.Class object at 0x7f…>``) and the
#: bare ``<module.Class object …>`` reprs objects commonly produce. Used as a final guard: no such
#: string is ever legitimate crate content.
_REPR_LEAK_RE = re.compile(r'<[A-Za-z_][\w.]* object at 0x[0-9a-fA-F]+>|<[a-z_]+\.[\w.]+ object')

#: An IRI whose local part is a bare accession (``CHMO_0002820``, ``AFR_0002409``, and the
#: letter-prefixed ``NCIT_C48810``). Splitting such an IRI yields "CHMO 0002820", which names
#: nothing — unlike ``qudt:…/MolarMass`` or ``dct:description``, where the local part *is* the
#: label and needs no lookup. Units such as ``MOL-PER-L`` carry no digit run and are left alone.
_OPAQUE_ACCESSION_RE = re.compile(r"^[A-Za-z]+[_-][A-Za-z]?\d{3,}$")

_ABSOLUTE = ("http://", "https://")


# ------------------------------------------------------------------------------------ helpers

def _as_list(value: Any) -> list:
    """``@type`` (and friends) may be a single value or an array; always give an array."""
    return value if isinstance(value, list) else [value]


def _expand(ns: Namespaces, term: str) -> str:
    """``ns.expand`` made total: anything that is not a CURIE comes back unchanged.

    The same concept can appear both compacted (as a ``@type``) and absolute (as a node ``@id``),
    so comparisons must be made on the expanded form — comparing the two spellings literally
    produces duplicate ``@id`` nodes, which merge silently once the JSON-LD is expanded.

    ``curies`` refuses a term with no delimiter (``#contact-1``, ``name``) by raising, and expands
    only the first prefix declared for a namespace; neither must fail a pass, so both fall back:
    the first to the term itself, the second to a plain join against the declared prefix.
    """
    if not isinstance(term, str) or ":" not in term:
        return term
    try:
        expanded = ns.expand(term)
    except Exception:  # an unparseable term is simply not a CURIE
        return term
    if expanded == term and not term.startswith(_ABSOLUTE):
        prefix, _, local = term.partition(":")
        base = ns.prefixes.get(prefix)
        if isinstance(base, str):
            return f"{base}{local}"
    return expanded


def _compress(ns: Namespaces, term: str) -> str:
    """``ns.compress``, leaving anything that cannot be an IRI (no ``:`` at all) untouched."""
    if not isinstance(term, str) or ":" not in term:
        return term
    try:
        return ns.compress(term)
    except Exception:
        return term


def _is_schema_org_type(type_name: str) -> bool:
    """Whether a ``@type`` value is a schema.org term.

    The RO-Crate context is schema.org-based, so a bare term (``Dataset``, ``PropertyValue``) is
    one; a compact IRI (``qudt:Quantity``) or an absolute IRI is not.
    """
    return bool(type_name) and ":" not in type_name and not type_name.startswith("http")


def _readable_name(node: dict) -> str:
    """A human-readable ``name`` for a contextual entity that has none.

    RO-Crate asks every contextual entity to carry one (`ro-crate-1.2_75.1`), and a crate full of
    `#qudt_quantity-2` with no label is unreadable by a person even though it validates. The
    chem-dcat-ap mapping already puts a label on most nodes, so prefer the real thing and fall
    back to the type only when there is nothing better.
    """
    for key in ("dct:title", "dct:description", "skos:notation", "prov:value"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            return text if len(text) <= 120 else text[:117] + "..."
    for type_name in _as_list(node.get("@type", [])):
        if not isinstance(type_name, str):
            continue
        local = type_name.rsplit("/", 1)[-1].rsplit("#", 1)[-1].rsplit(":", 1)[-1]
        if local:
            return local.replace("_", " ")
    return str(node.get("@id", "entity")).lstrip("#")


def _term_label(iri: str) -> str:
    """A readable label for a vocabulary IRI we reference but do not otherwise describe."""
    local = iri.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    return local.replace("-", " ").replace("_", " ") or iri


def _has_opaque_accession(iri: str) -> bool:
    """True when :func:`_term_label` would produce an accession rather than a name."""
    local = iri.rstrip("/").rsplit("/", 1)[-1].rsplit("#", 1)[-1]
    return bool(_OPAQUE_ACCESSION_RE.match(local))


# ------------------------------------------------------------------------------- the passes

def replace_host(metadata: dict, real_host: str, replacement: str) -> int:
    """Replace the source host in the graph's string *values*, returning how many changed.

    Walks values as well as ``@id``s of contextual entities: the hostname does not only appear
    as an identifier. A molecule node records ``compound assigned to <host>/api/v1/...`` in
    prose, and an element referenced but not itself exported keeps its absolute URI.

    What is matched: the scheme-qualified URL, and the bare hostname at a host boundary (not as
    a substring of a longer word, and only when it contains a dot). ``http://chemotion`` would
    otherwise rewrite ``chemotion:elementType`` and the producer marker, and ``http://eln`` the
    ``elnconsortium`` profile IRI. Keys, ``@type`` and ``propertyID`` are never touched, nor
    are the ``@id`` of data entities (the payload paths must keep naming the staged files) or
    of the crate's own fragment entities.

    :param metadata: the crate metadata, modified in place.
    :param real_host: the host to remove, with or without its scheme.
    :param replacement: what to put in its place (scheme-qualified).
    :return: the number of strings changed.
    """
    bare_host = real_host.split("://", 1)[-1].strip("/")
    bare_replacement = replacement.split("://", 1)[-1].strip("/")
    if not bare_host:
        return 0
    # With a port (``eln.example.org:8443``), the host also appears without it in prose and links;
    # those are scrubbed too, after the longer form, so no ``:8443`` is left behind.
    hosts = [bare_host]
    if not bare_host.startswith("[") and re.fullmatch(r"[^:]+:\d+", bare_host):
        hosts.append(bare_host.rsplit(":", 1)[0])
    patterns = []
    for host in hosts:
        patterns.append((re.compile(r"https?://" + re.escape(host) + r"(?::\d+)?(?![\w.-])",
                                    re.IGNORECASE), replacement))
    for host in hosts:
        if "." in host:
            patterns.append((re.compile(r"(?<![\w.-])" + re.escape(host) + r"(?::\d+)?(?![\w.-])",
                                        re.IGNORECASE), bare_replacement))
    changed = 0

    def scrubbed(text: str) -> str:
        for pattern, target in patterns:
            text = pattern.sub(target, text)
        return text

    def _skip_key(key: Any) -> bool:
        return key in ("@type", "propertyID")

    # Mutates in place rather than rebuilding, so references held into the graph stay valid.
    def walk(container: dict | list) -> None:
        nonlocal changed
        items = container.items() if isinstance(container, dict) else enumerate(container)
        for key, value in list(items):
            if isinstance(value, str):
                if _skip_key(key):
                    continue
                if key == "@id" and (value.startswith(("./", "#")) or value == METADATA_ID):
                    continue
                replaced = scrubbed(value)
                if replaced != value:
                    container[key] = replaced  # type: ignore[index]
                    changed += 1
            elif isinstance(value, dict | list):
                walk(value)

    walk(metadata.get("@graph", []))
    return changed


def normalise_curies(metadata: dict, ns: Namespaces) -> None:
    """Rewrite every compact IRI in the crate onto its registered prefix, in place.

    rdflib's JSON-LD serializer builds a CURIE by splitting an IRI at its last ``/`` or ``#`` and
    looking up *that* namespace, so an OBO-style prefix — whose namespace ends in ``_`` — can
    never win: ``http://purl.obolibrary.org/obo/CHMO_0000595`` only ever matches the host-level
    ``obo:``, giving ``obo:CHMO_0000595``. The accession's own registry writes ``CHMO:0000595``,
    and the doubled ontology name is a well-known antipattern. Allotrope terms fare worse still:
    their namespace ends in ``#AFR_``, which rdflib cannot split at all, so they stay absolute.

    This pass runs over the finished document and re-abbreviates keys, ``@id`` and ``@type``
    against the per-ontology prefixes chem-dcat-ap declares (longest namespace wins, and the
    host-level prefixes are never chosen). It changes spelling only: every CURIE expands to the
    same IRI it did before, so the crate's triples are untouched.

    Every dict and list is rewritten *in place*, so references a caller holds into the graph stay
    live across this pass.
    """
    def rewrite(value: Any, key: str | None = None) -> Any:
        if isinstance(value, dict):
            new_items = []
            for k, v in value.items():
                # `@id` and `@type` carry IRIs; every other `@…` is JSON-LD syntax, and a plain
                # key is a predicate. Values of non-IRI keys are data and must not be rewritten.
                new_key = k if k.startswith("@") else _compress(ns, k)
                new_items.append((new_key, rewrite(v, k)))
            value.clear()
            value.update(new_items)
            return value
        if isinstance(value, list):
            value[:] = [rewrite(v, key) for v in value]
            return value
        if isinstance(value, str) and key in ("@id", "@type"):
            return _compress(ns, value)
        return value

    graph = metadata.get("@graph")
    if isinstance(graph, list):
        rewrite(graph)


def polish_contextual_entities(metadata: dict, root_id: str, ns: Namespaces,
                               label_for: Callable[[str], str | None] | None = None) -> None:
    """Satisfy RO-Crate's RECOMMENDED rules about contextual entities, in place.

    Three rules, all of which make a crate more readable rather than merely more compliant:

    * every contextual entity carries a ``name`` and at least one schema.org ``@type``
      (`75.1`, `42.1`, `42.0`);
    * every IRI the crate *references* is also *described* in the graph (`76.1`) — a bare
      ``{"@id": "http://qudt.org/vocab/unit/MilliMOL"}`` tells a reader nothing on its own;
    * nothing sits in the graph unreachable from anything else (`77.0`).

    Data entities additionally get a ``description``, and people/organizations a contact point.

    :param metadata: the crate metadata, modified in place.
    :param root_id: the root Dataset's ``@id``. The root node is looked up in the graph by this
        id rather than passed in: earlier passes may rebuild nodes as new dicts, and a reference
        held across them would point at a discarded copy whose mutations vanish without a trace.
    :param ns: the prefix map — the base RO-Crate context's prefixes plus the crate's own.
    :param label_for: optional ``iri -> label`` lookup for opaque ontology accessions.
    """
    graph = metadata.setdefault("@graph", [])
    root_node: dict = next((n for n in graph if isinstance(n, dict) and n.get("@id") == root_id),
                           {"@id": root_id})

    #: Everything that needs *describing*: entities, the ontology classes used as `@type`, and
    #: the predicate IRIs. Being a predicate is not the same as being referenced *by* an entity,
    #: though, so reachability is judged on ``linked`` alone — otherwise a described predicate
    #: looks reachable to us and stranded to the validator.
    referenced: set[str] = set()
    linked: set[str] = set()

    def _collect(value: Any) -> None:
        if isinstance(value, dict):
            if set(value) == {"@id"} and isinstance(value["@id"], str):
                referenced.add(value["@id"])
                linked.add(value["@id"])
            for item in value.values():
                _collect(item)
        elif isinstance(value, list):
            for item in value:
                _collect(item)

    for node in graph:
        if isinstance(node, dict):
            _collect({k: v for k, v in node.items() if k not in ("@id", "@type")})

    # An entity's `@type` is an RDF reference too, and the ontology classes the chem-dcat-ap
    # mapping uses (`qudt:Quantity`, `SIO:001406`, …) are referenced by every typed node; counting
    # only `{"@id": …}` objects would miss them, and they are most of what `76.1` reports.
    for node in graph:
        if not isinstance(node, dict):
            continue
        for type_name in _as_list(node.get("@type", [])):
            if isinstance(type_name, str) and not _is_schema_org_type(type_name):
                referenced.add(type_name)
        # So are the predicates: an object-valued property IRI such as `qudt:unit` is an entity
        # in the RDF graph, and the validator asks for it to be described like any other.
        for key in node:
            if not key.startswith("@") and not _is_schema_org_type(key):
                referenced.add(key)

    # -- names and schema.org types -------------------------------------------------------
    for node in graph:
        if not isinstance(node, dict) or node.get("@id") == METADATA_ID:
            continue
        types = _as_list(node.get("@type", []))
        if DATA_ENTITY_TYPES.intersection(types):
            continue
        if not node.get("name"):
            node["name"] = _readable_name(node)
        if not any(_is_schema_org_type(t) for t in types if isinstance(t, str)):
            # A measured value or an identifier is a PropertyValue; anything else gets the
            # schema.org root rather than a type we cannot justify.
            kind = "PropertyValue" if ("prov:value" in node or "skos:notation" in node) else "Thing"
            node["@type"] = [*types, kind] if types else kind

    # -- describe what we only referenced -------------------------------------------------
    # The set of ids present comes from the graph itself, not from any side registry: nodes are
    # appended in several places, and keying off a stale map produces a *second* node with an @id
    # that already exists — a duplicate @id, which is its own violation (`ro-crate-1.2_2.8`) and
    # silently merges two entities when the JSON-LD is expanded.
    present = {_expand(ns, node["@id"]) for node in graph
               if isinstance(node, dict) and isinstance(node.get("@id"), str)}
    for term in sorted(referenced):
        iri = _expand(ns, term)
        if iri in present or not iri.startswith(_ABSOLUTE):
            continue
        # RO-Crate only asks that a referenced IRI be *described* (`76.1`); a stub named
        # "CHMO 0002820" satisfies that and tells a reader nothing. Where the local part is a
        # bare accession, ask the ontology sources for the real label first.
        name = None
        if label_for is not None and _has_opaque_accession(iri):
            try:
                name = label_for(iri)
            except Exception as exc:  # a labelling miss must never fail an export
                logger.debug("Could not label %s: %s", iri, exc)
        # Spelled the way the rest of the crate spells it (``CHEBI:23367``), not absolute.
        graph.append({"@id": _compress(ns, iri), "@type": "DefinedTerm", "name": name or _term_label(iri)})
        present.add(iri)

    # -- data entities carry a description --------------------------------------------------
    for node in graph:
        if not isinstance(node, dict):
            continue
        types = _as_list(node.get("@type", []))
        if "Dataset" not in types or node.get("description") or node.get("@id") == "./":
            continue
        label = node.get("name") or str(node.get("@id", "")).strip("./")
        genre = node.get("genre") or "Element"
        node["description"] = f"{genre} '{label}' as recorded in the Chemotion ELN."

    # -- organizations: a URL, and a reachable contact point ----------------------------------
    contact_points: dict[str, str] = {}
    for node in graph:
        if not isinstance(node, dict):
            continue
        types = _as_list(node.get("@type", []))
        node_id = node.get("@id", "")
        if "Person" in types and node.get("email") and "contactPoint" not in node:
            contact_id = f"#contact-{str(node_id).lstrip('#').rsplit('/', 1)[-1]}"
            contact_points[contact_id] = node["email"]
            node["contactPoint"] = {"@id": contact_id}
        if "Organization" in types and isinstance(node_id, str):
            # A ROR id is the organization's @id; schema.org still wants an explicit `url`.
            node.setdefault("url", node_id if node_id.startswith("http") else None)
            if node.get("url") is None:
                node.pop("url", None)
    # Give the organizations a contact point too, reusing the author's where there is one.
    if contact_points:
        first_contact = next(iter(contact_points))
        for node in graph:
            if not isinstance(node, dict):
                continue
            if "Organization" in _as_list(node.get("@type", [])) and "contactPoint" not in node:
                node["contactPoint"] = {"@id": first_contact}
    existing_ids = {n.get("@id") for n in graph if isinstance(n, dict)}
    for contact_id, email in contact_points.items():
        if contact_id in existing_ids:
            continue
        graph.append({"@id": contact_id, "@type": "ContactPoint", "email": email,
                      "name": f"Contact for {email}", "contactType": "author"})
        existing_ids.add(contact_id)

    # -- and make sure nothing is stranded ------------------------------------------------
    mentions = root_node.setdefault("mentions", [])
    if isinstance(mentions, dict):
        mentions = [mentions]
        root_node["mentions"] = mentions
    mentioned = {ref["@id"] for ref in mentions if isinstance(ref, dict) and "@id" in ref}
    linked_iris = {_expand(ns, r) for r in linked}
    for node in graph:
        if not isinstance(node, dict):
            continue
        node_id = node.get("@id")
        if not isinstance(node_id, str) or node_id in mentioned:
            continue
        if _expand(ns, node_id) in linked_iris:
            continue
        if node_id in (METADATA_ID, root_node.get("@id")):
            continue
        if DATA_ENTITY_TYPES.intersection(_as_list(node.get("@type", []))):
            continue
        mentions.append({"@id": node_id})
        mentioned.add(node_id)


def declare_unmapped_terms(metadata: dict, base_context: dict, ns: Namespaces) -> dict[str, str]:
    """Return explicit term definitions for every graph key the base context does not define.

    Declaring namespace *prefixes* is not enough. `roc-validator` 0.11.3 — the version
    TheELNFileFormat's CI pins — requires each key to be **present in the @context** verbatim and
    rejects a compact IRI whose prefix alone is declared; 0.13.0 relaxed that to "a term or
    compact IRI". Emitting the full key as its own term satisfies both, and costs one line of
    @context per predicate actually used.

    The definitions are derived from the serialized document rather than from a hand-kept list, so
    a new chem-dcat-ap predicate cannot silently reintroduce the failure.

    :param metadata: the serialized crate, already compacted (and normalised).
    :param base_context: the RO-Crate context's term map (the object under ``@context``), used to
        tell which keys are already defined.
    :param ns: the prefix map. It must include the crate's own prefixes: after
        :func:`normalise_curies` every chemistry key is spelled with one of them, and without them
        each would be unresolvable and left undeclared — the very validator failure this exists
        to prevent.
    :return: ``{key: absolute_iri}`` for the keys needing a definition, sorted for a stable diff.
    """
    # The base context's own prefixes sit underneath, in case `ns` was built without them.
    prefixes = {**prefixes_of_context(base_context), **ns.prefixes}
    extra: dict[str, str] = {}
    for node in metadata.get("@graph", []):
        if not isinstance(node, dict):
            continue
        for key in node:
            if key.startswith("@") or key in base_context or key in extra:
                continue
            if key.startswith(_ABSOLUTE):
                extra[key] = key
                continue
            prefix, sep, local = key.partition(":")
            if not sep or prefix not in prefixes:
                logger.debug("Crate key %r has no resolvable namespace; leaving it undeclared.", key)
                continue
            iri = _expand(ns, key)
            extra[key] = iri if iri.startswith(_ABSOLUTE) else f"{prefixes[prefix]}{local}"
    return dict(sorted(extra.items()))


#: Properties TheELNFileFormat requires as arrays even with one entry (its tests/schema.json).
#: RO-Crate 1.2 says single-element arrays SHOULD be unpacked (appendix/jsonld.md), so these
#: two are a recorded gap in the .eln format (COMPATIBILITY.md K-1), not a choice of ours.
ARRAY_REQUIRED_BY_ELN = frozenset({"hasPart", "comment"})


def unpack_singletons(graph: list) -> int:
    """Write every one-element property array as its single value, as RO-Crate 1.2 asks.

    Keys starting with ``@`` and :data:`ARRAY_REQUIRED_BY_ELN` keep their arrays. Returns the
    number of properties unpacked.
    """
    unpacked = 0
    for node in graph:
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if (isinstance(value, list) and len(value) == 1 and not key.startswith("@")
                    and key not in ARRAY_REQUIRED_BY_ELN):
                node[key] = value[0]
                unpacked += 1
    return unpacked


def is_opaque_payload(value: Any) -> bool:
    """Whether a property value is a nested structure RO-Crate cannot represent as-is.

    RO-Crate allows literals, `{"@id": …}` references, and arrays of those. What it cannot hold is
    an arbitrary **nested object** — so a dict, or a list containing one, has to be carried as a
    JSON string instead. A flat list of scalars is a normal multi-valued property and must be left
    alone.

    :param value: a candidate property value.
    :return: True if it must be serialised to a string.
    """
    if isinstance(value, dict):
        return True
    if isinstance(value, list):
        return any(isinstance(item, dict | list) for item in value)
    return False


def flatten_crate_graph(metadata: dict) -> int:
    """Make an RO-Crate metadata graph spec-conformant in place.

    * **No blank nodes** ("fully flattened"). chem-dcat-ap emits nested value objects — the
      CHEMINF identifier entities for InChI, SMILES, IUPAC name and so on — and serialising that
      RDF to JSON-LD turns them into blank nodes (``_:Nabc…``). Each one is given a stable
      fragment id instead, and every reference to it is rewritten.
    * **No typed literals and no anonymous nested entities** — collapsed and hoisted.
    * **Datasets need `name`** (schema.org). chem-dcat-ap titles land in ``dct:title``, which
      RO-Crate consumers do not read; mirror it into ``name`` where absent.
    * **No empty values, and no Python reprs.**

    Everything is changed in place: the ``@graph`` list and every node dict that stays in it are
    the same objects afterwards, so a reference taken before the call stays valid (a node merged
    into an identical one, or hoisted out of its parent, is the one exception: look it up by
    ``@id``). Nested entities that are hoisted become top-level nodes as they are.

    :param metadata: the parsed ``ro-crate-metadata.json``, modified in place.
    :return: the number of blank nodes renamed.
    """
    graph = metadata.get("@graph")
    if not isinstance(graph, list):
        return 0

    # The replacement id carries a counter, so its value depends on the order the blank nodes are
    # visited in. That order comes from rdflib's JSON-LD serialisation, which labels blank nodes
    # randomly (`_:Nabc…`) and iterates its store set-wise: numbered in that order, the same input
    # produces different ids on every run, and a regenerated crate always diffs.
    #
    # Numbering a *content-sorted* order fixes that. The sort key deliberately ignores the `_:`
    # label — that is the random part — and uses the node's type and its literal content, which is
    # stable for identical input. Ties keep their original relative order, which only matters for
    # nodes whose content is identical, and those are interchangeable by definition.
    #
    # This holds only because chem-dcat-ap's blank nodes are *leaves*: the CHEMINF identifier
    # entities, `adms:Identifier` and `qudt:Quantity` carry literals plus stable IRI references,
    # never a pointer to another blank node. If that ever stops being true, a content key is no
    # longer stable either (it would contain the random label of whatever it points at) and the
    # general fix is RDF dataset canonicalisation. The guard below says so out loud rather than
    # leaving it to be discovered through mysteriously churning ids.
    blank_nodes = [n for n in graph
                   if isinstance(n, dict) and isinstance(n.get("@id"), str)
                   and n["@id"].startswith("_:")]
    blank_ids = {n["@id"] for n in blank_nodes}

    def _canonical(obj: Any) -> Any:
        """The node's content with its own id dropped and blank references neutralised.

        References to *named* entities (QUDT units, quantity kinds, ontology IRIs) are stable
        and must stay in the key: two quantities can be identical in every literal and differ
        only by their unit, and keying on literals alone would make those tie — leaving their
        relative order to the random graph order, which is the very thing this is fixing.
        """
        if isinstance(obj, dict):
            if set(obj) == {"@id"}:
                return {"@id": "_:blank" if obj["@id"] in blank_ids else obj["@id"]}
            return {k: _canonical(v) for k, v in obj.items() if k != "@id"}
        if isinstance(obj, list):
            return [_canonical(v) for v in obj]
        return obj

    # Who points at each blank node, and through which predicate. Two quantities with identical
    # content (three samples at 1.0 equivalents) are *not* interchangeable: each hangs off a
    # different parent, and numbering them by content alone leaves which sample gets `-31` to
    # rdflib's random order.
    referrers: dict[str, set[tuple[str, str]]] = {b: set() for b in blank_ids}

    def _collect_referrers(obj: Any, parent: str | None, key: str | None) -> None:
        if isinstance(obj, dict):
            own = obj.get("@id") if isinstance(obj.get("@id"), str) else None
            if set(obj) == {"@id"} and own in referrers and parent is not None and key is not None:
                referrers[own].add((parent, key))
                return
            for k, v in obj.items():
                if k != "@id":
                    _collect_referrers(v, own or parent, k)
        elif isinstance(obj, list):
            for v in obj:
                _collect_referrers(v, parent, key)

    for node in graph:
        _collect_referrers(node, None, None)

    def _sort_key(node: dict) -> str:
        parents = sorted(referrers.get(node["@id"], ()))
        return json.dumps([parents, _canonical(node)], sort_keys=True, default=str)

    # `@type` is a *set* in JSON-LD, and rdflib emits its members in a random order. Two
    # consequences, both of which would make a regenerated crate differ for no reason: the slug
    # below is taken from the first member, and the sort key above sees a different list. Sorting
    # the arrays settles both, and settles the crate's own bytes with them — nothing reads these
    # positionally.
    for node in graph:
        if isinstance(node, dict) and isinstance(node.get("@type"), list):
            node["@type"] = sorted(node["@type"], key=str)

    def _references(obj: Any) -> set[str]:
        if isinstance(obj, dict):
            if set(obj) == {"@id"} and isinstance(obj["@id"], str):
                return {obj["@id"]}
            return set().union(*(_references(v) for k, v in obj.items() if k != "@id"))
        if isinstance(obj, list):
            return set().union(*(_references(v) for v in obj))
        return set()

    nested = [n["@id"] for n in blank_nodes if _references(n) & blank_ids]
    if nested:
        logger.warning(
            "%d blank node(s) reference other blank nodes (%s). Their crate ids are numbered from "
            "content that now includes a random label, so they will not be stable between "
            "exports; RDF dataset canonicalisation would be needed to fix that.",
            len(nested), ", ".join(sorted(nested)[:3]))

    # A molecule shared by several samples is dumped once per sample, so its identifier and
    # molar-mass value nodes arrive as several blank nodes with identical content and identical
    # referrers. Those *are* the same entity: keep one, point the others' references at it.
    renames: dict[str, str] = {}
    kept: dict[str, str] = {}
    duplicates: set[str] = set()
    for node in sorted(blank_nodes, key=_sort_key):
        key = _sort_key(node)
        if key in kept:
            renames[node["@id"]] = kept[key]
            duplicates.add(node["@id"])
            continue
        node_type = node.get("@type") or "entity"
        if isinstance(node_type, list):
            node_type = node_type[0] if node_type else "entity"
        slug = re.sub(r"[^A-Za-z0-9]+", "_", str(node_type).rsplit("/", 1)[-1]).strip("_")
        renames[node["@id"]] = kept[key] = f"#{slug.lower() or 'entity'}-{len(kept) + 1}"
    if duplicates:
        graph[:] = [n for n in graph if not (isinstance(n, dict) and n.get("@id") in duplicates)]

    def _rewrite(obj: Any) -> None:
        """Apply ``renames`` to every ``@id`` under ``obj``, in place."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "@id" and isinstance(v, str):
                    obj[k] = renames.get(v, v)
                else:
                    _rewrite(v)
        elif isinstance(obj, list):
            kept_items: list = []
            for v in obj:
                _rewrite(v)
                # merged duplicates leave the same reference twice in one list
                if isinstance(v, dict) and set(v) == {"@id"} and v in kept_items:
                    continue
                kept_items.append(v)
            obj[:] = kept_items

    if renames:
        for node in graph:
            _rewrite(node)

    # RO-Crate JSON-LD is a *restricted* profile of JSON-LD, not the full language: every property
    # value must be a literal or a {"@id": …} reference, and every entity must be a top-level
    # member of @graph. Two shapes that are perfectly legal JSON-LD violate it, and both appear in
    # what chem-dcat-ap's RDF serialises to:
    #
    #   1. typed literals — {"@type": "xsd:float", "@value": "0.0"}
    #   2. anonymous entities — a nested object with no @id at all (an implicit blank node),
    #      e.g. PropertyValue entries in `variableMeasured`
    #
    # Collapse the first to a plain literal (the xsd datatype is dropped; RO-Crate has nowhere to
    # put it), and give the second a minted @id so it can be hoisted like any other entity.
    def _value_object(obj: Any) -> bool:
        return isinstance(obj, dict) and "@value" in obj and set(obj) <= {"@value", "@type", "@language"}

    def _collapse_value_objects(obj: Any) -> None:
        """Replace every typed literal under ``obj`` by its plain value, in place."""
        if isinstance(obj, dict):
            for k, v in obj.items():
                if _value_object(v):
                    obj[k] = v["@value"]
                else:
                    _collapse_value_objects(v)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                if _value_object(v):
                    obj[i] = v["@value"]
                else:
                    _collapse_value_objects(v)

    for node in graph:
        _collapse_value_objects(node)

    # "Fully flattened" means every entity is a *top-level* member of @graph: giving a nested
    # object an @id is not enough, it has to be hoisted out and replaced by a reference. Repeat
    # until nothing moves, since hoisted entities can themselves contain nested entities.
    # The minted id is derived from *where* the entity sits — its parent's @id and the property it
    # hangs off — plus a per-parent ordinal, never from a global counter: a global counter
    # numbers entities in graph order, which rdflib randomises, so two exports of one input would
    # name the same PropertyValue differently.
    taken: dict[str, int] = {}

    def _slug(text: str) -> str:
        return re.sub(r"[^A-Za-z0-9]+", "-", text.rstrip("/").rsplit("/", 1)[-1]).strip("-").lower()

    def _mint_id(entity: dict, parent_id: str | None, key: str | None) -> str:
        label = ""
        for field in ("propertyID", "name", "@type"):
            raw = entity.get(field)
            if isinstance(raw, list):
                raw = raw[0] if raw else None
            if isinstance(raw, str) and raw:
                label = _slug(raw)
                if label:
                    break
        base = "#" + "-".join(part for part in (
            _slug(parent_id) if isinstance(parent_id, str) else "",
            _slug(key) if isinstance(key, str) else "",
            label or "entity") if part)
        taken[base] = taken.get(base, 0) + 1
        return f"{base}-{taken[base]}"

    def _hoist(obj: Any, hoisted: list[dict], top: bool = False,
               parent_id: str | None = None, key: str | None = None) -> Any:
        """Hoist every nested entity under ``obj`` in place; return what replaces ``obj``.

        A nested entity is moved out as it is (the same dict becomes a top-level node) and is
        replaced where it sat by a reference.
        """
        if isinstance(obj, list):
            for i, v in enumerate(obj):
                obj[i] = _hoist(v, hoisted, parent_id=parent_id, key=key)
            return obj
        if not isinstance(obj, dict):
            return obj
        own_id = obj.get("@id") if isinstance(obj.get("@id"), str) else None
        # An anonymous entity's children are named after the nearest *named* ancestor, since
        # its own id does not exist until it has been hoisted.
        for k, v in obj.items():
            obj[k] = _hoist(v, hoisted, parent_id=own_id or parent_id,
                            key=k if (own_id or top) else key)
        # A bare {"@id": ...} is already a reference; anything richer is an entity definition.
        if not top and len(obj) > 1:
            if not isinstance(obj.get("@id"), str):
                obj["@id"] = _mint_id(obj, parent_id, key)
            hoisted.append(obj)
            return {"@id": obj["@id"]}
        return obj

    while True:
        hoisted: list[dict] = []
        for node in graph:
            _hoist(node, hoisted, top=True)
        if not hoisted:
            break
        by_id = {n["@id"]: n for n in graph if isinstance(n, dict) and isinstance(n.get("@id"), str)}
        for entity in hoisted:
            existing = by_id.get(entity["@id"])
            if existing is None:
                graph.append(entity)
                by_id[entity["@id"]] = entity
            else:
                # Same entity nested in two places: merge rather than duplicate the @id, which
                # would itself be a spec violation.
                for key, value in entity.items():
                    existing.setdefault(key, value)

    for node in graph:
        if not isinstance(node, dict) or "name" in node:
            continue
        if "Dataset" not in _as_list(node.get("@type")):
            continue
        title = node.get("dct:title") or node.get("title")
        if isinstance(title, list):
            title = title[0] if title else None
        if isinstance(title, dict):
            title = title.get("@value")
        if isinstance(title, str) and title:
            node["name"] = title
            # Having mirrored it into schema.org `name`, drop the chem-dcat-ap spelling: two keys
            # carrying the identical string is noise for a reader, and `name` is the one the .eln
            # checker requires on every Dataset.
            if node.get("dct:title") == title:
                node.pop("dct:title", None)
            if node.get("title") == title:
                node.pop("title", None)

    # Drop properties whose value is empty or whitespace-only. A key with no content says nothing
    # a missing key does not, and the .eln conformance suite reports such nodes as
    # "contains empty values in the key-value pairs".
    dropped: dict[str, int] = {}
    for node in graph:
        if not isinstance(node, dict):
            continue
        for key in [k for k, v in node.items()
                    if k not in ("@id", "@type") and not isinstance(v, dict | list)
                    and not str(v).strip()]:
            del node[key]
            dropped[key] = dropped.get(key, 0) + 1

    # Last line of defence against a whole family of bug: a value arriving from the server is
    # written out with str() without anyone checking its shape, and a Python repr — possibly
    # carrying a live memory address, as `<chemotion_api.utils.PropertyDict object at 0x…>`
    # would (it is a MutableMapping, not a dict subclass, so a dict check misses it) — ends up in
    # the crate. A repr is never valid crate content, so drop it rather than publish it, and say
    # so loudly: it means some upstream guard is wrong and should be fixed at the source.
    leaked_reprs: list[str] = []
    for node in graph:
        if not isinstance(node, dict):
            continue
        for key, value in list(node.items()):
            if isinstance(value, str) and _REPR_LEAK_RE.search(value):
                leaked_reprs.append(f"{node.get('@id', '?')}.{key}")
                del node[key]
    if leaked_reprs:
        logger.error(
            "Dropped %d propert(y/ies) whose value was a Python repr, not data: %s. "
            "This is a bug in the exporter — some value reached the crate via str() without its "
            "shape being checked. The crate is still valid; the affected values are absent.",
            len(leaked_reprs), ", ".join(leaked_reprs[:10]))

    if dropped:
        # One summary line per export, not one per field: this is routine (Chemotion stores unset
        # optional fields as empty strings), so it should inform without becoming noise.
        summary = ", ".join(f"{key} ({count})" for key, count in sorted(dropped.items()))
        logger.warning(
            "Omitted %d empty field(s) from the crate metadata — a key with no value carries no "
            "more meaning than an absent one: %s",
            sum(dropped.values()), summary,
        )

    return len(renames)
