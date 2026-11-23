"""Collection walk -> ExportModel, from fake client objects (no server)."""
import logging
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from chemotion_crate import extract as ex
from chemotion_crate.model import SourceInfo

HOST = "https://eln.example.org"
SOURCE = SourceInfo(host=HOST, version="v4.0.1")


# -- fakes ------------------------------------------------------------------------------------

class FakeAttachment(dict):
    def __init__(self, data=b"bytes", **fields):
        super().__init__(fields)
        self._data = data

    def iter_file(self):
        yield self._data


class FakeDataset(dict):
    def __init__(self, id, name, json_data=None, attachments=()):
        super().__init__()
        self.id, self.name = id, name
        self.json_data = json_data or {}
        self.attachments = list(attachments)


class FakeAnalysis(dict):
    def __init__(self, id, datasets=(), **fields):
        super().__init__(fields)
        self.id = id
        self.datasets = list(datasets)


class FakeMolecule(dict):
    def __init__(self, id, **fields):
        super().__init__(id=id, **fields)
        self.id = id


class FakeElement:
    element_type = "sample"

    def __init__(self, id, name=None, properties=None, json_data=None, loaded=True,
                 load_error=None, **attrs):
        self.id = id
        self.name = name
        self.short_label = attrs.pop("short_label", None)
        self.properties = properties if properties is not None else {}
        self.json_data = json_data if json_data is not None else {}
        self.json_ld = attrs.pop("json_ld", {})
        self.created_at = attrs.pop("created_at", datetime(2026, 1, 10, 9, tzinfo=UTC))
        self.last_update = attrs.pop("last_update", datetime(2026, 1, 11, 9, tzinfo=UTC))
        self.user_labels_resolved = attrs.pop("user_labels_resolved", [])
        self.analyses = attrs.pop("analyses", [])
        self.segments = attrs.pop("segments", {})
        self.attachments = attrs.pop("attachments", [])
        self.is_loaded = loaded
        self.load_calls = 0
        self._load_error = load_error
        for key, value in attrs.items():
            setattr(self, key, value)

    def load(self):
        self.load_calls += 1
        if self._load_error:
            raise self._load_error
        self.is_loaded = True


class FakeSample(FakeElement):
    element_type = "sample"

    def __init__(self, id, **kw):
        kw.setdefault("role_in_reaction", None)
        kw.setdefault("gram", None)
        kw.setdefault("mol", None)
        kw.setdefault("liter", None)
        kw.setdefault("molecule", None)
        super().__init__(id, **kw)


class FakeCollection:
    def __init__(self, id, label, children=(), parent=None, description=None, **elements):
        self.id = id
        self.label = label
        self.collection_json = {"description": description} if description else {}
        self.parent = parent
        self.children = list(children)
        for child in self.children:
            child.parent = self
        self._elements = elements
        self.calls = []

    def _get(self, kind, per_page):
        self.calls.append((kind, per_page))
        items = self._elements.get(kind, [])
        if isinstance(items, Exception):
            raise items
        return list(items)

    def get_samples(self, per_page):
        return self._get("sample", per_page)

    def get_reactions(self, per_page):
        return self._get("reaction", per_page)

    def get_wellplates(self, per_page):
        return self._get("wellplate", per_page)

    def get_research_plans(self, per_page):
        return self._get("research_plan", per_page)

    def get_device_descriptions(self, per_page):
        return self._get("device_description", per_page)

    def get_cell_lines(self, per_page):
        return self._get("cell_line", per_page)

    def get_screens(self, per_page):
        return self._get("screen", per_page)

    def get_sbmm_samples(self, per_page):
        return self._get("sequence_based_macromolecule_sample", per_page)

    def get_vessels(self):
        return self._get("vessel", None)

    def get_generics_by_name(self, name, per_page):
        return self._get(name, per_page)


def _instance(generic=()):
    classes = {k: {} for k in ("sample", "reaction", "wellplate", "research_plan",
                               "device_description", *generic)}
    return SimpleNamespace(host_url=HOST + "/", token="t", all_element_classes=classes)


def _extract(collection, generic=()):
    return ex.extract(_instance(generic), collection, source=SOURCE)


# -- the walk ---------------------------------------------------------------------------------

def test_element_in_two_collections_is_one_record_loaded_once():
    shared = FakeSample(1, name="Ethanol", loaded=False)
    child = FakeCollection(2, "Child", sample=[shared])
    root = FakeCollection(1, "Root", children=[child], description="the root", sample=[shared])
    model = _extract(root)
    assert [e.key for e in model.elements] == [("sample", 1)]
    assert model.elements[0].collection_ids == [1, 2]
    assert shared.load_calls == 1
    assert [(c.id, c.label, c.parent_id) for c in model.collections] == [
        (1, "Root", None), (2, "Child", 1)]
    assert model.collections[0].description == "the root"
    assert model.root_collection_id == 1
    assert model.source is SOURCE


def test_same_id_different_kind_are_two_records():
    root = FakeCollection(1, "Root", sample=[FakeSample(3)],
                          reaction=[FakeElement(3, name="R", element_type="reaction")])
    assert {e.key for e in _extract(root).elements} == {("sample", 3), ("reaction", 3)}


def test_every_kind_is_listed_with_per_page():
    root = FakeCollection(1, "Root")
    _extract(root, generic=("lab_notebook_page",))
    assert {kind for kind, _ in root.calls} == {
        "sample", "reaction", "wellplate", "research_plan", "device_description",
        "cell_line", "screen", "sequence_based_macromolecule_sample", "vessel", "lab_notebook_page"}
    assert all(per_page == ex.PER_PAGE for kind, per_page in root.calls if kind != "vessel")


def test_loop_in_tree_is_skipped_and_recorded():
    root = FakeCollection(1, "Root")
    child = FakeCollection(2, "Child", children=[root])
    root.children = [child]
    child.parent = root
    model = _extract(root)
    assert [c.id for c in model.collections] == [1, 2]
    assert model.skipped == [{"kind": "collection", "stage": "index", "id": 1,
                              "error": "collection tree contains a loop"}]


def test_parent_outside_the_export_is_not_named():
    outside = FakeCollection(99, "Outside")
    root = FakeCollection(1, "Root", parent=outside)
    assert _extract(root).collections[0].parent_id is None


def test_retrieve_failure_is_recorded():
    root = FakeCollection(1, "Root", reaction=RuntimeError("HTTP 500"), sample=[FakeSample(1)])
    model = _extract(root)
    assert [e.key for e in model.elements] == [("sample", 1)]
    assert model.skipped == [{"kind": "reaction", "stage": "retrieve", "collection": 1,
                              "error": "HTTP 500"}]


def test_load_failure_is_recorded():
    broken = FakeSample(4, loaded=False, load_error=RuntimeError("gone"))
    root = FakeCollection(1, "Root", sample=[broken, FakeSample(5)])
    model = _extract(root)
    assert [e.key for e in model.elements] == [("sample", 5)]
    assert model.skipped == [{"kind": "sample", "stage": "load", "id": 4, "error": "gone"}]


def test_element_classes_failure_skips_generics_only():
    class _Instance:
        host_url = HOST

        @property
        def all_element_classes(self):
            raise RuntimeError("no klasses")

    root = FakeCollection(1, "Root", sample=[FakeSample(1)])
    model = ex.extract(_Instance(), root, source=SOURCE)
    assert [e.key for e in model.elements] == [("sample", 1)]


# -- base record ------------------------------------------------------------------------------

def test_base_record_fields():
    att = FakeAttachment(b"abc", filename="notes.txt", identifier="uuid-1", id=7, filesize="3",
                         content_type="text/plain")
    ds = FakeDataset(20, "ds", json_data={"dataset": {"klass_ols": "CHMO:0000595",
                                                      "klass_label": "13C NMR"}},
                     attachments=[FakeAttachment(b"##", filename="s.jdx", id=8)])
    ana = FakeAnalysis(10, datasets=[ds], name="NMR", kind="CHMO:0000595 | NMR",
                       status="Confirmed", content={"ops": [{"insert": "Looks right.\n"}]})
    elem = FakeSample(
        1, name="Ethanol", short_label="S-1",
        properties={"description": {"ops": [{"insert": "rich "}, {"insert": "text\n"}]},
                    "observation": "  seen  "},
        user_labels_resolved=[SimpleNamespace(title="urgent"), SimpleNamespace(title="")],
        analyses=[ana], attachments=[att],
    )
    rec = _extract(FakeCollection(1, "Root", sample=[elem])).elements[0]
    assert rec.url == f"{HOST}/api/v1/samples/1"
    assert rec.name == "Ethanol" and rec.short_label == "S-1"
    assert rec.created_at == "2026-01-10T09:00:00+00:00"
    assert rec.updated_at == "2026-01-11T09:00:00+00:00"
    assert rec.description == "rich text"
    assert rec.observation == "seen"
    assert rec.keywords == ["urgent"]
    (a,) = rec.attachments
    assert (a.filename, a.identifier, a.id, a.filesize, a.mime_type) == (
        "notes.txt", "uuid-1", 7, 3, "text/plain")
    assert b"".join(a.read()) == b"abc"
    (analysis,) = rec.analyses
    assert (analysis.id, analysis.name, analysis.kind, analysis.status, analysis.content) == (
        10, "NMR", "CHMO:0000595 | NMR", "Confirmed", "Looks right.")
    (dataset,) = analysis.datasets
    assert (dataset.id, dataset.name, dataset.klass_code, dataset.klass_label) == (
        20, "ds", "CHMO:0000595", "13C NMR")
    assert [x.filename for x in rec.all_attachments()] == ["notes.txt", "s.jdx"]


def test_json_ld_id_is_preferred_for_the_url():
    elem = FakeSample(1, json_ld={"@id": "https://eln.example.org/x/1"})
    assert _extract(FakeCollection(1, "R", sample=[elem])).elements[0].url == \
        "https://eln.example.org/x/1"


def test_user_label_failure_drops_labels_not_the_element():
    class _Labels(FakeSample):
        @property
        def user_labels_resolved(self):
            raise RuntimeError("labels endpoint down")

        @user_labels_resolved.setter
        def user_labels_resolved(self, value):
            pass

    rec = _extract(FakeCollection(1, "R", sample=[_Labels(1)])).elements[0]
    assert rec.keywords == []


def test_bad_filesize_becomes_none():
    elem = FakeSample(1, attachments=[FakeAttachment(filename="a", filesize="lots")])
    assert _extract(FakeCollection(1, "R", sample=[elem])).elements[0].attachments[0].filesize is None


# -- samples ----------------------------------------------------------------------------------

def test_sample_record():
    elem = FakeSample(
        1, gram=0.5, mol="0.01", liter=None, role_in_reaction="products",
        properties={"density": "0.79", "equivalent": 2, "sum_formula": "C2H6O", "location": "",
                    "molarity": {"value": "0.5", "unit": "M"},
                    "melting_point_lowerbound": -114, "melting_point_upperbound": -114,
                    "boiling_point_lowerbound": 70, "boiling_point_upperbound": 80},
        molecule=FakeMolecule(11, cano_smiles="CCO", inchikey="K", molecular_weight="46.07",
                              cas="64-17-5", names="ethanol"),
    )
    rec = _extract(FakeCollection(1, "R", sample=[elem])).elements[0]
    assert (rec.mass_g, rec.amount_mol, rec.volume_l) == (0.5, 0.01, None)
    assert rec.role == "products"
    assert rec.is_solvent is False
    assert (rec.density, rec.equivalent) == (0.79, 2.0)
    assert (rec.molarity_value, rec.molarity_unit) == (0.5, "M")
    assert rec.properties == {"sum_formula": "C2H6O", "melting_point": "-114",
                              "boiling_point": "70-80"}
    mol = rec.molecule
    assert (mol.id, mol.cano_smiles, mol.inchikey, mol.molecular_weight) == (11, "CCO", "K", 46.07)
    assert mol.cas == ["64-17-5"] and mol.names == ["ethanol"]


def test_sample_cas_list_and_raw_ranges():
    elem = FakeSample(1, json_data={"melting_point": "(,)", "boiling_point": "78...78"},
                      molecule=FakeMolecule(2, cas=["64-17-5", " ", "1-1-1"], names=["a", ""]))
    rec = _extract(FakeCollection(1, "R", sample=[elem])).elements[0]
    assert rec.molecule.cas == ["64-17-5", "1-1-1"]
    assert rec.molecule.names == ["a"]
    assert "melting_point" not in rec.properties  # unset Ruby range
    assert rec.properties["boiling_point"] == "78"


def test_unreadable_sample_amount_is_omitted():
    class _Sample(FakeSample):
        @property
        def gram(self):
            raise ValueError("unit conversion failed")

        @gram.setter
        def gram(self, value):
            pass

    rec = _extract(FakeCollection(1, "R", sample=[_Sample(1, mol=0.1)])).elements[0]
    assert rec.mass_g is None and rec.amount_mol == 0.1


# -- reactions --------------------------------------------------------------------------------

def test_reaction_record():
    sm = FakeSample(11, gram=1.0, properties={"equivalent": 1})
    solvent = FakeSample(12)  # a solvent because it is in the solvent list, nothing else
    props = {
        "duration": "3.5 Hour(s)", "yield": "85", "status": "Successful", "rxno": " RXNO:1 | x ",
        "timestamp_start": "2026-01-01T10:00:00", "timestamp_stop": "",
        "temperature": {"valueUnit": "°C", "userText": "25", "data": []},
        "reaction_description": {"ops": [{"insert": "Mild.\n"}]},
        "rinchi_long_key": "", "conditions": "reflux", "variations": [],
    }
    reaction = FakeElement(5, name="Ox", properties=props,
                           json_data={"rinchi_long_key": "Long-Key"},
                           starting_materials=[sm], reactants=[], products=[], solvents=[solvent])
    rec = _extract(FakeCollection(1, "R", reaction=[reaction])).elements[0]
    assert rec.kind == "reaction" and rec.url == f"{HOST}/api/v1/reactions/5"
    assert rec.description == "Mild."
    assert rec.duration == "3.5 Hour(s)"
    assert rec.timestamp_start == "2026-01-01T10:00:00" and rec.timestamp_stop is None
    assert rec.yield_percent == 85.0
    assert rec.status == "Successful"
    assert rec.rxno == "RXNO:1 | x"
    assert rec.temperature == {"valueUnit": "°C", "userText": "25", "data": []}
    assert rec.properties["rinchi_long_key"] == "Long-Key"  # raw fallback
    assert rec.properties["conditions"] == "reflux"
    assert "variations" not in rec.properties
    (material,) = rec.materials["starting_materials"]
    assert material.role == "starting_materials" and material.mass_g == 1.0
    assert material.is_solvent is False
    assert material.equivalent == 1.0
    (solv,) = rec.materials["solvents"]
    assert solv.role == "solvents" and solv.is_solvent is True
    assert rec.materials["reactants"] == [] and rec.materials["products"] == []


def test_reaction_role_list_failure_is_tolerated():
    class _Reaction(FakeElement):
        @property
        def products(self):
            raise RuntimeError("boom")

    reaction = _Reaction(5, starting_materials=[], reactants=[], solvents=[])
    rec = _extract(FakeCollection(1, "R", reaction=[reaction])).elements[0]
    assert "products" not in rec.materials and "reactants" in rec.materials


# -- other kinds ------------------------------------------------------------------------------

def test_wellplate_wells():
    occupied = FakeSample(1, properties={"short_label": "S-1"})
    by_smiles = FakeSample(2, properties={}, molecule={"cano_smiles": "CCO"})
    plate = FakeElement(8, name="Plate", wells=[{"A": occupied, "B": None}, {"A": by_smiles}])
    rec = _extract(FakeCollection(1, "R", wellplate=[plate])).elements[0]
    assert rec.property_values == [{"name": "well A1", "value": "S-1"},
                                   {"name": "well A2", "value": "CCO"}]


def test_research_plan_body_becomes_description():
    plan = FakeElement(9, name="Plan", body=[
        {"type": "richtext", "value": {"ops": [{"insert": "Step one\n"}]}},
        {"type": "image", "value": None},
        SimpleNamespace(value="Step two"),
    ])
    rec = _extract(FakeCollection(1, "R", research_plan=[plan])).elements[0]
    assert rec.description == "Step one\nStep two"


def test_device_description_fields_and_terms():
    device = FakeElement(10, name="NMR", properties={
        "vendor_company_name": "Bruker", "serial_number": "", "device_class": ["a", None, "b"],
        "description": "skipped"},
        ontology_terms=lambda: [{"iri": "http://purl.obolibrary.org/obo/CHMO_1", "label": "NMR"}])
    rec = _extract(FakeCollection(1, "R", device_description=[device])).elements[0]
    assert {"name": "vendor company name", "value": "Bruker"} in rec.property_values
    assert {"name": "device class", "value": "a, b"} in rec.property_values
    assert not any(pv["name"] in ("serial number", "description") for pv in rec.property_values)
    assert rec.ontology_terms == [{"iri": "http://purl.obolibrary.org/obo/CHMO_1", "label": "NMR"}]


def test_generic_element_property_values():
    page = FakeElement(3, name="Page", properties={
        "temp": {"value": 25, "unit": "°C"}, "empty": {"value": "", "unit": "K"},
        "nested": {"a": 1, "b": ""}, "flag": True, "none": None})
    rec = _extract(FakeCollection(1, "R", lab_notebook_page=[page]),
                   generic=("lab_notebook_page",)).elements[0]
    assert rec.kind == "lab_notebook_page"
    assert rec.url == f"{HOST}/api/v1/lab_notebook_pages/3"
    assert rec.property_values == [{"name": "temp", "value": 25, "unitText": "°C"},
                                   {"name": "nested.a", "value": 1},
                                   {"name": "flag", "value": True}]


# -- source info ------------------------------------------------------------------------------

class _Response:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self, response=None, error=None):
        self._response, self._error = response, error
        self.calls = []

    def get(self, path, **kwargs):
        self.calls.append(path)
        if self._error:
            raise self._error
        return self._response


def test_read_source_info_parses_about():
    body = ("<html><h4>Version: v4.0.1-rc-260818</h4>"
            "<p>Current revision: abc1234\n</p></html>")
    session = _Session(_Response(body))
    info = ex.read_source_info(SimpleNamespace(host_url=HOST + "/"), session=session)
    assert session.calls == ["/about"]
    assert info == SourceInfo(host=HOST, version="v4.0.1-rc-260818", revision="abc1234")


def test_read_source_info_accepts_other_majors():
    info = ex.read_source_info(SimpleNamespace(host_url=HOST),
                               session=_Session(_Response("Version: v3.1.2")))
    assert info.version == "v3.1.2" and info.revision is None


@pytest.mark.parametrize("session", [
    _Session(_Response("<html>not chemotion</html>")),
    _Session(_Response("Version: v4.0.1", status_code=503)),
    _Session(error=ConnectionError("down")),
])
def test_read_source_info_failure_gives_no_version(session, caplog):
    with caplog.at_level(logging.WARNING, logger="chemotion_crate.extract"):
        info = ex.read_source_info(SimpleNamespace(host_url=HOST), session=session)
    assert info == SourceInfo(host=HOST, version=None, revision=None)
    assert caplog.records


def test_read_source_info_without_host_asks_nothing():
    session = _Session(_Response("Version: v4.0.1"))
    assert ex.read_source_info(SimpleNamespace(host_url=""), session=session) == SourceInfo()
    assert session.calls == []


# -- plain ------------------------------------------------------------------------------------

class _PropertyDict(dict):
    def to_dict(self):
        return {"value": self["v"], "unit": "mg", "when": datetime(2026, 1, 1, tzinfo=UTC)}


class _BrokenToDict(dict):
    def to_dict(self):
        raise RuntimeError("no")


@pytest.mark.parametrize("value,expected", [
    (None, None), ("s", "s"), (1, 1), (1.5, 1.5), (True, True),
    (datetime(2026, 1, 1, tzinfo=UTC), "2026-01-01T00:00:00+00:00"),
    ({"a": (1, 2), 3: [None]}, {"a": [1, 2], "3": [None]}),
])
def test_plain_scalars_and_containers(value, expected):
    assert ex.plain(value) == expected


def test_plain_uses_to_dict():
    assert ex.plain(_PropertyDict(v=5)) == {"value": 5, "unit": "mg",
                                            "when": "2026-01-01T00:00:00+00:00"}


def test_plain_falls_back_to_mapping_when_to_dict_fails():
    assert ex.plain(_BrokenToDict(a=1)) == {"a": 1}


@pytest.mark.parametrize("value", [object(), SimpleNamespace(x=1), FakeSample(1)])
def test_plain_unknown_objects_are_none_never_a_repr(value):
    assert ex.plain(value) is None
    assert ex.plain([value]) == [None]


# -- elements shared below full detail level ---------------------------------------------------
# ELN v4 masks each withheld field with '***' and sets is_restricted; the client turns the mask
# into WITHHELD, a str reading '***' that raises WithheldValueError (a PermissionError) when used
# as a number.

from chemotion_api.utils.withheld import WITHHELD, WithheldValueError  # noqa: E402


class _WithheldAmount:
    def __get__(self, obj, objtype=None):
        raise WithheldValueError("withheld")


class RestrictedSample(FakeSample):
    gram = _WithheldAmount()

    def __init__(self, id, **kw):
        super().__init__(id, is_restricted=True, **kw)
        del self.__dict__["gram"]


def _restricted_sample(id=1):
    return RestrictedSample(
        id, name=WITHHELD, short_label=WITHHELD, mol=0.01,
        properties={"density": WITHHELD, "equivalent": 2, "location": WITHHELD,
                    "sum_formula": "C2H6O", "description": WITHHELD,
                    "molarity": {"value": WITHHELD, "unit": WITHHELD},
                    "boiling_point_lowerbound": WITHHELD},
        json_data={"is_restricted": True, "boiling_point": "***"},
    )


def test_a_restricted_sample_is_kept_without_its_withheld_fields():
    model = _extract(FakeCollection(1, "R", sample=[_restricted_sample()]))
    assert len(model.elements) == 1, "a withheld value must not drop the element"
    rec = model.elements[0]
    assert rec.name is None and rec.short_label is None and rec.label == "sample-1"
    assert rec.mass_g is None and rec.amount_mol == 0.01
    assert rec.density is None and rec.equivalent == 2.0
    assert rec.molarity_value is None and rec.molarity_unit is None
    assert rec.description is None
    assert rec.properties == {"sum_formula": "C2H6O"}
    assert set(rec.withheld) == {"name", "short_label", "amount", "density", "location",
                                 "description", "molarity", "molarity.unit",
                                 "boiling_point"}
    assert {"kind": "sample field", "stage": "withheld", "id": 1,
            "count": len(rec.withheld)} in model.skipped
    assert "***" not in repr(rec.__dict__), "the mask never becomes a value"


def test_a_restricted_reaction_keeps_its_materials_and_names_their_withheld_fields():
    material = _restricted_sample(11)
    reaction = FakeElement(5, name=WITHHELD, is_restricted=True,
                           properties={"duration": WITHHELD, "yield": WITHHELD, "status": WITHHELD,
                                       "rxno": WITHHELD, "timestamp_start": WITHHELD,
                                       "conditions": WITHHELD, "temperature": {"userText": "25"}},
                           starting_materials=[material], reactants=[], products=[], solvents=[])
    rec = _extract(FakeCollection(1, "R", reaction=[reaction])).elements[0]
    assert rec.kind == "reaction" and rec.name is None
    assert (rec.duration, rec.yield_percent, rec.status, rec.rxno, rec.timestamp_start) == \
        (None, None, None, None, None)
    assert "conditions" not in rec.properties and rec.temperature == {"userText": "25"}
    assert [m.id for m in rec.materials["starting_materials"]] == [11]
    assert rec.materials["starting_materials"][0].density is None
    assert {"name", "duration", "yield", "status", "rxno", "timestamp_start", "conditions",
            "starting_materials sample 11: density"} <= set(rec.withheld)


def test_three_asterisks_are_ordinary_text_on_a_full_detail_element():
    elem = FakeSample(1, name="***", properties={"description": "***"})
    rec = _extract(FakeCollection(1, "R", sample=[elem])).elements[0]
    assert rec.name == "***" and rec.description == "***" and rec.withheld == []


# -- cell lines, screens, SBMM samples ---------------------------------------------------------

def test_cell_line_record_carries_sample_and_material_fields():
    line = FakeElement(4, name="HeLa sample", element_type="cell_line",
                       properties={"amount": 1000000, "unit": "cells", "passage": 3,
                                   "contamination": "", "description": "stock"},
                       material={"name": "HeLa", "source": "ATCC", "organism": "Homo sapiens",
                                 "tissue": None, "growth_medium": "DMEM"})
    rec = _extract(FakeCollection(1, "R", cell_line=[line])).elements[0]
    assert rec.kind == "cell_line" and rec.description == "stock"
    values = {pv["name"]: pv for pv in rec.property_values}
    assert values["amount"] == {"name": "amount", "value": 1000000, "unitText": "cells"}
    assert values["passage"]["value"] == 3 and "contamination" not in values
    assert values["material name"]["value"] == "HeLa"
    assert values["material organism"]["value"] == "Homo sapiens"
    assert "material tissue" not in values


def test_screen_record_carries_its_fields_and_links():
    screen = FakeElement(6, name="Kinase panel", element_type="screen",
                         properties={"collaborator": "Lab B", "conditions": "37 °C",
                                     "requirements": "", "result": None},
                         wellplate_ids=[8], research_plan_ids=[9])
    rec = _extract(FakeCollection(1, "R", screen=[screen])).elements[0]
    assert rec.kind == "screen"
    assert {pv["name"]: pv["value"] for pv in rec.property_values} == {
        "collaborator": "Lab B", "conditions": "37 °C"}
    assert rec.links == [["wellplate", 8], ["research_plan", 9]]


def test_sbmm_record_carries_quantities_and_the_protein():
    sbmm = FakeElement(7, name="Hb", element_type="sequence_based_macromolecule_sample",
                       properties={"purity": 0.95, "supplier": "",
                                   "concentration": {"value": 2.5, "unit": "mg/ml"},
                                   "molarity": {"value": None, "unit": "mol/l"}},
                       macromolecule={"primary_accession": "P69905",
                                      "short_name": "Hemoglobin subunit alpha",
                                      "organism": "Homo sapiens", "ec_numbers": []})
    rec = _extract(FakeCollection(1, "R", sequence_based_macromolecule_sample=[sbmm])).elements[0]
    values = {pv["name"]: pv for pv in rec.property_values}
    assert values["purity"]["value"] == 0.95 and "supplier" not in values
    assert values["concentration"] == {"name": "concentration", "value": 2.5, "unitText": "mg/ml"}
    assert "molarity" not in values, "a quantity without a value is left out"
    assert values["protein primary accession"]["value"] == "P69905"
    assert "protein ec numbers" not in values


def test_withheld_fields_of_the_new_kinds_are_left_out():
    line = FakeElement(4, name=WITHHELD, element_type="cell_line", is_restricted=True,
                       properties={"amount": WITHHELD, "unit": "cells", "passage": WITHHELD},
                       material={"name": "HeLa", "organism": WITHHELD})
    rec = _extract(FakeCollection(1, "R", cell_line=[line])).elements[0]
    assert "***" not in repr(rec.property_values) and rec.name is None
    assert {"name", "amount", "passage", "material.organism"} <= set(rec.withheld)


# -- fields that are not chemistry-typed, but must not be lost --------------------------------

def test_sample_keeps_every_scientific_field_the_eln_holds():
    from enum import Enum

    class GasType(Enum):
        CATALYST = "catalyst"

    elem = FakeSample(1, properties={
        "purity": 0.98, "stereo": {"abs": "rac", "rel": "any"}, "decoupled": True,
        "is_top_secret": False, "solvent": [{"label": "Water", "smiles": "O", "ratio": 1}],
        "target_amount": {"value": 1.5, "unit": "g"}, "real_amount": {"value": 1.4, "unit": "g"},
        "gas_type": GasType.CATALYST, "gas_phase_data": {"time": {"value": 1, "unit": "h"}}},
        json_data={"sample_type": "Mixture"})
    rec = _extract(FakeCollection(1, "R", sample=[elem])).elements[0]
    props = rec.properties
    assert props["purity"] == 0.98 and props["stereo"] == {"abs": "rac", "rel": "any"}
    assert props["decoupled"] is True and "is_top_secret" not in props, "a flag that is off is left out"
    assert props["solvent"] == [{"label": "Water", "smiles": "O", "ratio": 1}]
    assert props["target_amount"] == {"value": 1.5, "unit": "g"}
    assert props["real_amount"] == {"value": 1.4, "unit": "g"}
    assert props["gas_type"] == "catalyst", "an enum is written as the ELN's own text"
    assert props["gas_phase_data"] == {"time": {"value": 1, "unit": "h"}}
    assert props["sample_type"] == "Mixture"


def test_a_material_keeps_its_place_in_the_reaction():
    reference = FakeSample(11, properties={"reference": True, "coefficient": 2, "waste": False})
    product = FakeSample(12, properties={"equivalent": 0.85, "waste": True})
    reaction = FakeElement(5, name="Ox", properties={"vessel_size": {"amount": 10, "unit": "ml"},
                                                     "dangerous_products": ["explosive"],
                                                     "gas_schema": False,
                                                     "concentration_mode": "combined"},
                           starting_materials=[reference], reactants=[], products=[product],
                           solvents=[])
    rec = _extract(FakeCollection(1, "R", reaction=[reaction])).elements[0]
    (sm,), (prod,) = rec.materials["starting_materials"], rec.materials["products"]
    assert sm.properties["reference"] is True and sm.properties["coefficient"] == 2
    assert "waste" not in sm.properties, "a flag that is off is not recorded"
    assert prod.properties["waste"] is True and prod.equivalent == 0.85
    assert rec.properties["vessel_size"] == {"amount": 10, "unit": "ml"}
    assert rec.properties["dangerous_products"] == ["explosive"]
    assert rec.properties["concentration_mode"] == "combined"


def test_vessel_record_carries_its_fields_and_template():
    template = SimpleNamespace(name="Round-bottom flask 100 ml", vessel_type="flask",
                               material_type="glass", material_details=None, details="",
                               volume_amount=100, volume_unit="ml")
    vessel = SimpleNamespace(id="3f2a-uuid", name="Flask A", short_label="AB-V1",
                             description="for reflux", bar_code="12345", qr_code=None,
                             weight_amount=52.1, weight_unit="g", template=template,
                             raw={"created_at": "2026-01-10T09:00:00Z"})
    rec = _extract(FakeCollection(1, "R", vessel=[vessel])).elements[0]
    assert (rec.kind, rec.id, rec.label, rec.description) == ("vessel", "3f2a-uuid", "AB-V1", "for reflux")
    assert rec.url == f"{HOST}/api/v1/vessels/3f2a-uuid" and rec.created_at == "2026-01-10T09:00:00Z"
    values = {pv["name"]: pv for pv in rec.property_values}
    assert values["bar code"]["value"] == "12345" and "qr code" not in values
    assert values["weight"] == {"name": "weight", "value": 52.1, "unitText": "g"}
    assert values["template vessel type"]["value"] == "flask"
    assert values["template volume"] == {"name": "template volume", "value": 100, "unitText": "ml"}
    assert "template details" not in values


def test_fields_only_the_raw_payload_carries_are_kept():
    # Checked against the ELN's entities (v4.0.2): these come from the server but not through the
    # client's properties.
    sample = FakeSample(1, json_data={"pubchem_tag": {"pubchem_cid": 702}, "inventory_sample": True,
                                      "elemental_compositions": [{"composition_type": "found",
                                                                  "data": {"C": 52.1}}],
                                      "components": [], "parent_id": 7, "sample_details": None})
    rec = _extract(FakeCollection(1, "R", sample=[sample])).elements[0]
    assert rec.properties["pubchem_tag"] == {"pubchem_cid": 702}
    assert rec.properties["inventory_sample"] is True and rec.properties["parent_id"] == 7
    assert rec.properties["elemental_compositions"][0]["data"] == {"C": 52.1}
    assert "components" not in rec.properties and "sample_details" not in rec.properties

    material = FakeSample(11, json_data={"conversion_rate": 87.5, "weight_percentage": None})
    washer = FakeSample(12)
    reaction = FakeElement(5, name="Ox", properties={"gaseous": True},
                           json_data={"ph_value": 7.4, "ph_operator": "=", "origin": None,
                                      "role": "gp", "lock_reaction_volume": False},
                           starting_materials=[], reactants=[], products=[material], solvents=[],
                           purification_solvents=[washer])
    rec = _extract(FakeCollection(1, "R", reaction=[reaction])).elements[0]
    assert rec.properties["gaseous"] is True
    assert (rec.properties["ph_value"], rec.properties["ph_operator"], rec.properties["reaction_role"]) == (7.4, "=", "gp")
    assert "origin" not in rec.properties and "lock_reaction_volume" not in rec.properties
    assert rec.materials["products"][0].properties["conversion_rate"] == 87.5
    assert [m.id for m in rec.materials["purification_solvents"]] == [12]


def test_segments_the_element_has_are_kept_and_none_is_added():
    class Segments(dict):
        def get(self, *a, **k):  # the client's get() adds a segment: must never be called
            raise AssertionError("segments.get() would add a segment to the element")

    elem = FakeSample(1, segments=Segments({
        "Properties": {"x": {"y": 1}}, "Analyses": {"x": {"y": 1}},
        "Electrochemistry": {"Cell": {"Electrode type": "traditional",
                                      "Temperature": {"value": 25, "unit": "C"}, "Empty": None}},
        "Not added": None}))
    rec = _extract(FakeCollection(1, "R", sample=[elem])).elements[0]
    values = {pv["name"]: pv for pv in rec.property_values}
    assert values["Electrochemistry / Cell / Electrode type"]["value"] == "traditional"
    assert values["Electrochemistry / Cell / Temperature"] == {
        "name": "Electrochemistry / Cell / Temperature", "value": 25, "unitText": "C"}
    assert not any(n.startswith(("Properties", "Analyses", "Not added")) for n in values)



def test_eln_defaults_that_mean_not_set_are_left_out():
    elem = FakeSample(1, properties={"gas_type": "off", "stereo": {"abs": "any", "rel": "any"},
                                     "gas_phase_data": {"time": {"value": None, "unit": "h"}},
                                     "purity": 0})
    reaction = FakeElement(5, name="Ox", properties={"vessel_size": {"amount": None, "unit": "ml"}},
                           json_data={"ph_operator": "="}, starting_materials=[], reactants=[],
                           products=[], solvents=[])
    model = _extract(FakeCollection(1, "R", sample=[elem], reaction=[reaction]))
    sample = next(e for e in model.elements if e.kind == "sample")
    assert sample.properties == {"purity": 0}, "a zero is data; 'off', 'any' and empty values are not"
    reaction_rec = next(e for e in model.elements if e.kind == "reaction")
    assert "vessel_size" not in reaction_rec.properties and "ph_operator" not in reaction_rec.properties


def test_a_zero_in_a_material_field_is_kept():
    material = FakeSample(11, properties={"coefficient": 0}, json_data={"conversion_rate": 0})
    reaction = FakeElement(5, name="Ox", starting_materials=[material], reactants=[], products=[],
                           solvents=[])
    (sm,) = _extract(FakeCollection(1, "R", reaction=[reaction])).elements[0].materials["starting_materials"]
    assert sm.properties["coefficient"] == 0 and sm.properties["conversion_rate"] == 0


def test_structured_segment_values_travel_as_json_not_python_repr():
    import json as _json
    elem = FakeSample(1, segments={"Measure": {"Run": {
        "Table": [{"col": 1, "unit": "g"}, {"col": 2, "unit": "g"}],
        "Picked": {"label": "A", "id": 5},
        "Tags": ["a", "b"],
        "Nested quantity": {"value": {"x": 1}, "unit": "g"}}}})
    rec = _extract(FakeCollection(1, "R", sample=[elem])).elements[0]
    values = {pv["name"]: pv["value"] for pv in rec.property_values}
    assert _json.loads(values["Measure / Run / Table"]) == [{"col": 1, "unit": "g"}, {"col": 2, "unit": "g"}]
    assert _json.loads(values["Measure / Run / Picked"]) == {"label": "A", "id": 5}
    assert values["Measure / Run / Tags"] == "a, b"
    assert _json.loads(values["Measure / Run / Nested quantity"]) == {"x": 1}


def test_an_unconvertible_amount_is_quiet_not_a_warning(caplog):
    class Solid(FakeSample):
        @property
        def liter(self):  # chemotion-api 401.1.1: TypeError with no density and no molarity
            raise TypeError("unsupported operand type(s) for /: 'float' and 'NoneType'")

        @liter.setter
        def liter(self, _value):  # the fake's constructor sets it; the property ignores that
            pass

    elem = Solid(1, gram=1.0)
    with caplog.at_level("WARNING"):
        rec = _extract(FakeCollection(1, "R", sample=[elem])).elements[0]
    assert rec.mass_g == 1.0 and rec.volume_l is None
    assert not [r for r in caplog.records if "could not read" in r.getMessage()]


def test_the_clients_synthetic_generic_element_class_is_not_queried():
    root = FakeCollection(1, "Root")
    instance = _instance(("lab_notebook_page",))
    instance.all_element_classes["generic_element"] = {"is_generic": True, "name": "generic_element"}
    instance.all_element_classes["some_builtin"] = {"is_generic": False, "name": "some_builtin"}
    ex.extract(instance, root, source=SOURCE)
    kinds = {kind for kind, _ in root.calls}
    assert "lab_notebook_page" in kinds
    assert "generic_element" not in kinds and "some_builtin" not in kinds


def test_one_withheld_amount_is_counted_once():
    sample = _restricted_sample()
    sample.properties["target_amount"] = {"value": WITHHELD, "unit": "g"}
    model = _extract(FakeCollection(1, "R", sample=[sample]))
    withheld = model.elements[0].withheld
    assert "target_amount" in withheld
    assert not {"gram", "mol", "liter", "target_amount.value"} & set(withheld), withheld


def test_collections_without_ids_are_numbered_afresh_by_each_extraction():
    first = _extract(FakeCollection(None, "A"))
    second = _extract(FakeCollection(None, "B"))
    assert first.root_collection_id == second.root_collection_id == "local-1"


@pytest.mark.parametrize("duration,expected", [
    ({"value": 3.5, "unit": "Hour(s)"}, "3.5 Hour(s)"),
    ({"value": None, "unit": None}, None),
    ("3.5 Hour(s)", "3.5 Hour(s)"),
])
def test_a_duration_given_as_value_and_unit_is_read(duration, expected):
    reaction = FakeElement(5, name="Ox", properties={"duration": duration},
                           starting_materials=[], reactants=[], products=[], solvents=[])
    assert _extract(FakeCollection(1, "R", reaction=[reaction])).elements[0].duration == expected
