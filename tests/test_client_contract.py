"""The parts of chemotion-api that chemotion-crate relies on (the client contract).

chemotion-crate reads a Chemotion ELN only through the chemotion-api client, and only through the
names listed here. This test checks that the installed client still offers every one of them, so a
client release that drops or renames one fails here, not in a user's export. CI runs it against the
locked client and, in the ``client-latest`` job, against the newest release on PyPI.

The same list, in prose, is what the client's maintainers are asked to keep stable.
"""
from __future__ import annotations

import inspect

import pytest

chemotion_api = pytest.importorskip("chemotion_api")

from chemotion_api import Instance  # noqa: E402
from chemotion_api.collection import Collection, RootCollection  # noqa: E402
from chemotion_api.connection import Connection  # noqa: E402
from chemotion_api.elements.abstract_element import AbstractElement, Analyses, Dataset  # noqa: E402
from chemotion_api.elements.attachments import Attachment  # noqa: E402
from chemotion_api.elements.cell_line import CellLine  # noqa: E402
from chemotion_api.elements.device_description import DEVICE_FIELDS, DeviceDescription  # noqa: E402
from chemotion_api.elements.reaction import Reaction  # noqa: E402
from chemotion_api.elements.research_plan import ResearchPlan  # noqa: E402
from chemotion_api.elements.sample import Sample  # noqa: E402
from chemotion_api.elements.sbmm_sample import SbmmSample  # noqa: E402
from chemotion_api.elements.screen import Screen  # noqa: E402
from chemotion_api.elements.wellplate import Wellplate  # noqa: E402
from chemotion_api.schema_manager import SchemaManager  # noqa: E402
from chemotion_api.utils import normalise_range_value, quill_to_text  # noqa: E402
from chemotion_api.utils.withheld import SERVER_MASK, WITHHELD  # noqa: E402
from chemotion_api.vessels import Vessel, VesselTemplate  # noqa: E402

#: Methods and properties read from each class (``hasattr`` on the class itself).
CLASS_MEMBERS = {
    Instance: ["get_root_collection", "all_element_classes", "host_url", "token"],
    RootCollection: ["children", "sync_root"],
    Collection: ["get_samples", "get_reactions", "get_wellplates", "get_research_plans",
                 "get_device_descriptions", "get_cell_lines", "get_screens", "get_sbmm_samples",
                 "get_vessels",
                 "get_generics_by_name", "children", "label", "id",
                 "parent", "collection_json"],
    AbstractElement: ["properties", "analyses", "attachments", "user_labels_resolved", "segments",
                      "is_restricted", "is_loaded", "load", "id", "name", "element_type",
                      "short_label"],
    Sample: ["molecule", "role_in_reaction", "gram", "mol", "liter"],
    Reaction: ["starting_materials", "reactants", "products", "solvents", "purification_solvents"],
    Wellplate: ["wells"],
    ResearchPlan: ["body"],
    DeviceDescription: ["ontology_terms"],
    CellLine: ["material"],
    Screen: ["wellplate_ids", "research_plan_ids"],
    SbmmSample: ["macromolecule"],
    Attachment: ["iter_file", "load_file", "get"],
    Analyses: ["get"],
    Connection: ["get", "set_bearer_token"],
}

#: Attributes set on each instance in ``__init__`` (checked in the source, as there is no class attribute).
INSTANCE_ATTRIBUTES = {
    AbstractElement: ["json_data", "json_ld", "created_at", "last_update"],
    Dataset: ["id", "name", "json_data", "attachments"],
    Analyses: ["datasets", "id", "type"],
    SchemaManager: ["version_string", "revision"],
}

#: Methods called with these keyword arguments.
CALL_SIGNATURES = [
    (Collection.get_samples, "per_page"),
    (Collection.get_reactions, "per_page"),
    (Collection.get_wellplates, "per_page"),
    (Collection.get_research_plans, "per_page"),
    (Collection.get_device_descriptions, "per_page"),
    (Collection.get_cell_lines, "per_page"),
    (Collection.get_screens, "per_page"),
    (Collection.get_sbmm_samples, "per_page"),
    (Collection.get_generics_by_name, "per_page"),
    (SchemaManager.__init__, "check_version"),
]


@pytest.mark.parametrize("cls,name", [(c, n) for c, names in CLASS_MEMBERS.items() for n in names],
                         ids=lambda v: getattr(v, "__name__", v))
def test_class_member(cls, name):
    assert hasattr(cls, name), f"chemotion-crate reads {cls.__name__}.{name}"


@pytest.mark.parametrize("cls,name", [(c, n) for c, names in INSTANCE_ATTRIBUTES.items()
                                      for n in names],
                         ids=lambda v: getattr(v, "__name__", v))
def test_instance_attribute(cls, name):
    source = "".join(inspect.getsource(k) for k in cls.__mro__ if k.__module__.startswith("chemotion_api"))
    assert f"self.{name}" in source, f"chemotion-crate reads {cls.__name__}().{name}"


@pytest.mark.parametrize("func,keyword", CALL_SIGNATURES, ids=lambda v: getattr(v, "__qualname__", v))
def test_call_signature(func, keyword):
    assert keyword in inspect.signature(func).parameters, \
        f"chemotion-crate calls {func.__qualname__}({keyword}=…)"


def test_helpers():
    assert normalise_range_value("[1.0, 2.0]") is not None
    assert quill_to_text({"ops": [{"insert": "x\n"}]}).strip() == "x"
    assert all(isinstance(f, str) for f in DEVICE_FIELDS)


def test_withheld_values_read_as_the_servers_mask():
    # chemotion-crate recognises a withheld value on a restricted element by this text.
    assert SERVER_MASK == "***"
    assert isinstance(WITHHELD, str) and WITHHELD == SERVER_MASK


@pytest.mark.parametrize("cls,names", [
    (Vessel, ["id", "name", "short_label", "description", "bar_code", "qr_code", "weight_amount",
              "weight_unit", "template", "raw"]),
    (VesselTemplate, ["name", "vessel_type", "material_type", "material_details", "volume_amount",
                      "volume_unit", "details"]),
], ids=["Vessel", "VesselTemplate"])
def test_vessel_fields(cls, names):
    import dataclasses
    fields = {f.name for f in dataclasses.fields(cls)}
    assert set(names) <= fields, f"chemotion-crate reads {cls.__name__}: {set(names) - fields}"


@pytest.mark.xfail(strict=True, reason="chemotion-api 401.1.1: converting a mass to a volume with "
                   "no density and no molarity raises TypeError instead of answering None. "
                   "chemotion-crate copes (extract.py); when the client is fixed this passes and "
                   "the strict xfail fails: then drop the xfail and the workaround.")
def test_an_unconvertible_amount_answers_none():
    from chemotion_api.elements.utils import QuantityUnit, calculate_quantity
    assert calculate_quantity(1.0, QuantityUnit.Gram, QuantityUnit.Liter, molecular_weight=100.0,
                              purity=1.0, density=None, molarity=None) is None
