"""The producer marker identifying a crate written by this package.

:func:`chemotion_crate.export` writes a ``SoftwareApplication`` contextual entity into the crate
graph and references it as the ``instrument`` of the crate's ``CreateAction``, "the software that
wrote this crate". Its ``name``, :data:`PRODUCER_NAME`, is the stable string that identifies our
crates::

    grep '"name": "chemotion-crate"' ro-crate-metadata.json

:func:`chemotion_crate.read` keys its own-crate check on the same marker.
"""
from __future__ import annotations

from importlib import metadata

#: ``name`` of the SoftwareApplication node: the identifying string of our crates.
PRODUCER_NAME = "chemotion-crate"

#: crate-local ``@id`` of the SoftwareApplication node.
PRODUCER_ID = "#chemotion-crate"

#: RO-Crate 1.2 makes ``url`` REQUIRED on a SoftwareApplication (``ro-crate-1.2_32.2``).
PRODUCER_URL = "https://github.com/Chemotion/ChemotionCrate"

#: Host recorded for the source ELN when its hostname is deliberately withheld. ``.invalid`` is
#: the RFC 2606 reserved top-level domain, so it can never resolve to anything real: it states
#: "no server named" instead of pointing somewhere misleading.
WITHHELD_HOST = "https://withheld.chemotion.invalid"


def _version(package: str) -> str | None:
    try:
        return metadata.version(package)
    except metadata.PackageNotFoundError:
        return None


def producer_version() -> str:
    """The installed chemotion-crate version (``"unknown"`` outside an installation)."""
    return _version("chemotion-crate") or "unknown"


#: Packages whose versions decide how the chemistry in a crate is expressed, and the client the
#: data was read with. All are moving targets, so a crate read a year from now must say which
#: produced its terms.
_REQUIREMENTS = ("chemotion-api", "chem-dcat-ap", "dcat-ap-plus")


def requirement_versions() -> list[str]:
    """``name version`` for each package the crate's content depends on."""
    out = []
    for package in _REQUIREMENTS:
        version = _version(package)
        if version is not None:
            out.append(f"{package} {version}")
    return out


def producer_node() -> dict:
    """The SoftwareApplication graph node embedded in every crate.

    ``version`` is this package's; ``softwareRequirements`` carries the client and
    metadata-model versions, kept apart so the two are never confused for one another.
    """
    node = {
        "@id": PRODUCER_ID,
        "@type": "SoftwareApplication",
        "name": PRODUCER_NAME,
        "url": PRODUCER_URL,
        "version": producer_version(),
    }
    requirements = requirement_versions()
    if requirements:
        node["softwareRequirements"] = ", ".join(requirements)
    return node
