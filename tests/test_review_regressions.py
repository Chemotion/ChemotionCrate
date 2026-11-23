"""Regressions for the findings of the 2026-11-15 review: each test failed before its fix."""
from __future__ import annotations

import json
import re
import zipfile

import pytest

from chemotion_crate import Author, AuthorAffiliation, export_model, read
from chemotion_crate.model import AttachmentRecord, ExportModel
from chemotion_crate.naming import CrateFileNamer
from chemotion_crate.patch import patch_eln_ontology
from chemotion_crate.passes import replace_host

FIXTURE = "tests/fixtures/mini_model.json"
AUTHORS = [Author(name="A", orcid="0000-0002-1825-0097",
                  affiliations=[AuthorAffiliation(name="KIT", ror="https://ror.org/04t3en479")],
                  email="no-reply@eln.invalid")]


def _metadata(path) -> str:
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.endswith("ro-crate-metadata.json"))
        return z.read(name).decode("utf-8")


def _without_timestamps(text: str) -> str:
    return re.sub(r'"(dateCreated|endTime|datePublished)": "[^"]*"', "", text)


def test_two_exports_of_one_model_are_identical(tmp_path):
    first = export_model(ExportModel.load(FIXTURE), tmp_path / "a.eln", authors=AUTHORS,
                         ontology_lookup=False)
    second = export_model(ExportModel.load(FIXTURE), tmp_path / "b.eln", authors=AUTHORS,
                          ontology_lookup=False)
    a = _without_timestamps(_metadata(first)).replace("a.eln", "X")
    b = _without_timestamps(_metadata(second)).replace("b.eln", "X")
    assert a == b


def test_reexporting_the_same_model_object_does_not_inflate_the_manifest(tmp_path):
    model = ExportModel.load(FIXTURE)
    export_model(model, tmp_path / "a.eln", authors=AUTHORS, ontology_lookup=False)
    export_model(model, tmp_path / "b.eln", authors=AUTHORS, ontology_lookup=False)
    assert model.skipped == []
    manifest = next(n for n in json.loads(_metadata(tmp_path / "b.eln"))["@graph"]
                    if n["@id"] == "#export-manifest")
    assert manifest["description"].startswith("Nothing was omitted. Notes: ")
    assert manifest["description"].count("3 × timestamps") == 1
    assert manifest["description"].count("1 × keyword") == 1


def test_withholding_a_dotless_host_leaves_the_crates_own_names_alone(tmp_path):
    model = ExportModel.load(FIXTURE)
    model.source.host = "http://chemotion"
    for elem in model.elements:
        elem.url = (elem.url or "").replace("https://eln.example.org", "http://chemotion") or None
        for items in elem.materials.values():
            for m in items:
                m.url = m.url.replace("https://eln.example.org", "http://chemotion")
    model.elements[0].short_label, model.elements[0].name = None, "chemotion sample"
    out = export_model(model, tmp_path / "w.eln", authors=AUTHORS, ontology_lookup=False,
                       disclose_host=False)
    text = _metadata(out)
    assert "http://chemotion" not in text
    assert "chemotion:elementType" in text and '"#chemotion-crate"' in text
    with read(out) as crate:
        assert crate.is_chemotion_crate
        assert all(crate.has_file(f.id) for f in crate.files)
        assert crate.get_dataset("#samples/chemotion_sample_1") is not None


def test_replace_host_matches_whole_hosts_case_insensitively_and_skips_ids():
    metadata = {"@graph": [
        {"@id": "./samples/x/", "@type": "Dataset", "description": "see HTTPS://ELN.EXAMPLE.ORG/a and eln.example.org",
         "propertyID": "eln.example.org/x", "note": "myeln.example.org.example stays"},
        {"@id": "https://eln.example.org/api/v1/samples/1", "name": "n"},
    ]}
    replace_host(metadata, "https://eln.example.org", "https://withheld.chemotion.invalid")
    first, second = metadata["@graph"]
    assert first["description"] == ("see https://withheld.chemotion.invalid/a and "
                                    "withheld.chemotion.invalid")
    assert first["propertyID"] == "eln.example.org/x"
    assert first["note"] == "myeln.example.org.example stays"
    assert second["@id"] == "https://withheld.chemotion.invalid/api/v1/samples/1"


def test_a_very_long_element_name_still_exports(tmp_path):
    model = ExportModel.load(FIXTURE)
    plan = next(e for e in model.elements if e.kind == "research_plan")
    plan.name = "p" * 300
    plan.attachments.append(AttachmentRecord(filename="f" * 300 + ".txt", id=99,
                                             read=lambda: iter([b"x"])))
    out = export_model(model, tmp_path / "long.eln", authors=AUTHORS, ontology_lookup=False)
    with read(out) as crate:
        assert any(f.read() == b"x" for f in crate.files)


def test_file_nodes_keep_the_original_filename(tmp_path):
    model = ExportModel.load(FIXTURE)
    shared = next(e for e in model.elements if e.id == 3 and e.kind == "sample")
    shared.attachments[0].filename = "Spektrum (1).jdx"
    out = export_model(model, tmp_path / "n.eln", authors=AUTHORS, ontology_lookup=False)
    with read(out) as crate:
        file = next(f for f in crate.files if f.id.startswith("./samples/MT-3_3/"))
        assert file.id == "./samples/MT-3_3/Spektrum__1_.jdx"
        assert file.name == "Spektrum (1).jdx"


def test_names_differing_only_by_case_do_not_collide():
    namer = CrateFileNamer()
    namer.add(AttachmentRecord(filename="IR.jdx", id=1))
    namer.add(AttachmentRecord(filename="ir.jdx", id=2))
    names = [name for _, name in namer.files]
    assert len({n.casefold() for n in names}) == 2


def test_patch_refuses_an_archive_with_duplicate_member_names(tmp_path):
    bomb = tmp_path / "dup.eln"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("c/ro-crate-metadata.json", json.dumps({"@graph": []}))
        z.writestr("c/x.bin", b"\0" * (4 << 20))
        z.writestr("c/x.bin", b"small")
    with pytest.raises(ValueError, match="more than once"):
        patch_eln_ontology(str(bomb), {"wellplate": "http://x/y"}, output_path=str(tmp_path / "o.eln"))


def test_generic_list_fields_are_not_python_reprs():
    from chemotion_crate.extract import _generic_record

    class Elem:
        id, name, short_label = 1, "g", None
        properties = {"tags": ["a", "b"], "table": [{"x": 1}], "amount": {"value": 2, "unit": "g"}}
        json_data: dict = {}
        json_ld: dict = {}
        created_at = last_update = None
        user_labels_resolved: list = []
        analyses: list = []
        attachments: list = []

    rec = _generic_record(Elem(), "thing", None)
    values = {pv["name"]: pv["value"] for pv in rec.property_values}
    assert values["tags"] == "a, b"
    assert json.loads(values["table"]) == [{"x": 1}]
    assert values["amount"] == 2
    assert not any("[{'" in str(v) or "['" in str(v) for v in values.values())


def test_a_sample_whose_reaction_failed_to_map_keeps_its_chemistry(tmp_path, monkeypatch):
    import chemotion_crate.graph as graph
    import chemotion_crate.nodes as nodes

    real = nodes.reaction_to_dcat

    def broken(rec):
        raise ValueError("boom")

    monkeypatch.setitem(nodes._MAPPERS, "reaction", broken)
    try:
        out = export_model(ExportModel.load(FIXTURE), tmp_path / "r.eln", authors=AUTHORS,
                           ontology_lookup=False)
    finally:
        nodes._MAPPERS["reaction"] = real
    md = json.loads(_metadata(out))
    sample = next(n for n in md["@graph"] if n["@id"] == "#samples/MT-1_1")
    assert "CHEBI:59999" in sample["@type"]
    assert graph  # imported to make the monkeypatched module path explicit



def test_two_exports_in_different_processes_are_identical(tmp_path):
    # rdflib emits multi-valued literals in set order, which follows the process's hash seed: two
    # exports in one process agree even when two processes do not. Seeds 0 and 1 are known to
    # order a shared molecule's two descriptions differently without the fix.
    import os
    import subprocess
    import sys
    script = (
        "import sys\n"
        "from chemotion_crate import Author, AuthorAffiliation, export_model\n"
        "from chemotion_crate.model import ExportModel\n"
        "a = [Author(name='A', orcid='0000-0002-1825-0097', email='no-reply@eln.invalid',\n"
        "            affiliations=[AuthorAffiliation(name='KIT', ror='https://ror.org/04t3en479')])]\n"
        f"export_model(ExportModel.load({FIXTURE!r}), sys.argv[1], authors=a, ontology_lookup=False)\n"
    )
    texts = []
    for seed in ("0", "1"):
        out = tmp_path / f"seed{seed}.eln"
        subprocess.run([sys.executable, "-c", script, str(out)], check=True,
                       env={**os.environ, "PYTHONHASHSEED": seed})
        texts.append(_without_timestamps(_metadata(out)))
    assert texts[0] == texts[1]


def test_export_refuses_a_collection_of_another_instance(tmp_path):
    from types import SimpleNamespace

    from chemotion_crate import export

    def col(id, *children):
        return SimpleNamespace(id=id, label=f"c{id}", children=list(children))

    shared = SimpleNamespace(children=[col(40)])
    root = SimpleNamespace(children=[col(1, col(2))], sync_root=shared)
    instance = SimpleNamespace(get_root_collection=lambda: root, host_url="https://a.example")
    with pytest.raises(ValueError, match="not one of this instance's collections"):
        export(instance, col(99), tmp_path / "x.eln", authors=AUTHORS)
    assert not list(tmp_path.iterdir()), "nothing is written"

    from chemotion_crate import _check_collection_belongs
    _check_collection_belongs(instance, col(2))   # a sub-collection
    _check_collection_belongs(instance, col(40))  # a collection shared with the user
    _check_collection_belongs(instance, col(None))  # no id: cannot be checked


def test_the_collection_check_matches_label_too_and_looks_at_own_collections_first():
    from types import SimpleNamespace

    from chemotion_crate import _check_collection_belongs

    class Root:
        children = [SimpleNamespace(id=2, label="Local", children=[])]

        @property
        def sync_root(self):
            raise AssertionError("shared collections fetched although the collection is the user's own")

    instance = SimpleNamespace(get_root_collection=lambda: Root(), host_url="https://a.example")
    _check_collection_belongs(instance, SimpleNamespace(id=2, label="Local"))
    with pytest.raises(ValueError):
        _check_collection_belongs(instance, SimpleNamespace(id=2, label="Foreign"))
