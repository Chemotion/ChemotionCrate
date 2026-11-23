"""The plain export model and its JSON round trip."""
import json
from pathlib import Path

import pytest

from chemotion_crate.model import (
    AnalysisRecord,
    AttachmentRecord,
    CollectionRecord,
    DatasetRecord,
    ElementRecord,
    ExportModel,
    MoleculeRecord,
    SourceInfo,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mini_model.json"


def _bytes(att):
    return b"".join(att.read()) if att.read else None


def _model():
    own = AttachmentRecord(filename="notes.txt", identifier="u-1", id=1, filesize=5,
                           read=lambda: iter([b"hel", b"lo"]))
    in_ds = AttachmentRecord(filename="spec.jdx", identifier="u-2", read=lambda: iter([b"##"]))
    no_bytes = AttachmentRecord(filename="gone.bin", id=3)
    material_att = AttachmentRecord(filename="m.txt", id=4, read=lambda: iter([b"m"]))
    material = ElementRecord(kind="sample", id=2, role="products", attachments=[material_att],
                             molecule=MoleculeRecord(id=9, cas=["64-17-5"]))
    elem = ElementRecord(
        kind="reaction", id=1, name="R",
        attachments=[own, no_bytes],
        analyses=[AnalysisRecord(id=10, kind="CHMO:0000595 | NMR",
                                 datasets=[DatasetRecord(id=20, attachments=[in_ds])])],
        materials={"products": [material]},
        collection_ids=[1, 2],
        temperature={"valueUnit": "°C", "userText": "25"},
    )
    return ExportModel(
        root_collection_id=1,
        collections=[CollectionRecord(id=1, label="root"), CollectionRecord(id=2, parent_id=1)],
        elements=[elem],
        source=SourceInfo(host="https://eln.example.org", version="v4.0.1", revision="abc"),
        skipped=[{"kind": "sample", "stage": "load", "id": 99, "error": "x"}],
    )


def test_to_json_drops_read_and_is_json_native():
    data = _model().to_json()
    text = json.dumps(data)
    assert '"read"' not in text
    assert "content_b64" not in text
    assert data["elements"][0]["attachments"][0]["filename"] == "notes.txt"


def test_inline_bytes_round_trip():
    original = _model()
    data = json.loads(json.dumps(original.to_json(inline_bytes=True)))
    rebuilt = ExportModel.from_json(data)
    elem = rebuilt.elements[0]
    assert _bytes(elem.attachments[0]) == b"hello"
    assert elem.attachments[1].read is None  # had no bytes; stays that way
    assert _bytes(elem.analyses[0].datasets[0].attachments[0]) == b"##"
    material = elem.materials["products"][0]
    assert _bytes(material.attachments[0]) == b"m"
    assert material.molecule == MoleculeRecord(id=9, cas=["64-17-5"])
    assert rebuilt.to_json() == original.to_json()


def test_round_trip_preserves_everything_but_bytes():
    original = _model()
    rebuilt = ExportModel.from_json(original.to_json())
    assert rebuilt.to_json() == original.to_json()
    assert rebuilt.source == original.source
    assert rebuilt.skipped == original.skipped
    assert rebuilt.elements[0].collection_ids == [1, 2]
    assert rebuilt.elements[0].attachments[0].read is None


def test_path_attachment_reads_relative_to_base_dir(tmp_path):
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "a.txt").write_bytes(b"from disk")
    data = {"root_collection_id": 1, "collections": [{"id": 1}],
            "elements": [{"kind": "sample", "id": 1,
                          "attachments": [{"filename": "a.txt", "path": "files/a.txt",
                                           "unknown_key": "ignored"}]}]}
    model = ExportModel.from_json(data, base_dir=tmp_path)
    att = model.elements[0].attachments[0]
    assert _bytes(att) == b"from disk"
    assert not hasattr(att, "unknown_key")


def test_load_resolves_path_against_the_fixture_dir(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01")
    path = tmp_path / "model.json"
    path.write_text(json.dumps({"root_collection_id": 1, "elements": [
        {"kind": "sample", "id": 1, "attachments": [{"filename": "blob.bin", "path": "blob.bin"}]}]}))
    model = ExportModel.load(path)
    assert _bytes(model.elements[0].attachments[0]) == b"\x00\x01"


def test_keys_and_labels():
    sample = ElementRecord(kind="sample", id=3)
    reaction = ElementRecord(kind="reaction", id=3)
    assert sample.key == ("sample", 3) and sample.key != reaction.key
    assert sample.label == "sample-3"
    assert ElementRecord(kind="sample", id=3, name="N").label == "N"
    assert ElementRecord(kind="sample", id=3, name="N", short_label="S").label == "S"


def test_attachment_key_prefers_identifier_then_id():
    assert AttachmentRecord(filename="a", identifier="u", id=1).key == "u"
    assert AttachmentRecord(filename="a", id=1).key == 1
    bare = AttachmentRecord(filename="a")
    assert bare.key == id(bare)
    assert AttachmentRecord(filename="a").key != bare.key


def test_all_attachments_order():
    a, b, c, d = (AttachmentRecord(filename=n) for n in "abcd")
    elem = ElementRecord(kind="sample", id=1, attachments=[a], analyses=[
        AnalysisRecord(datasets=[DatasetRecord(attachments=[b]), DatasetRecord(attachments=[c])]),
        AnalysisRecord(datasets=[DatasetRecord(attachments=[d])]),
    ])
    assert [x.filename for x in elem.all_attachments()] == ["a", "b", "c", "d"]


def test_root_and_element_lookup():
    model = _model()
    assert model.root.label == "root"
    assert model.element(("reaction", 1)).name == "R"
    assert model.element(("sample", 1)) is None
    with pytest.raises(ValueError, match="no root"):
        _ = ExportModel(root_collection_id=42).root


# -- the committed fixture --------------------------------------------------------------------

def test_load_mini_fixture():
    model = ExportModel.load(FIXTURE)
    assert model.root.label == "Mini Test Collection"
    assert [c.id for c in model.collections] == [1, 2]
    assert model.source == SourceInfo(host="https://eln.example.org", version="v4.0.1",
                                      revision="abc1234")
    kinds = [e.kind for e in model.elements]
    assert kinds.count("sample") == 3 and "reaction" in kinds and "lab_notebook_page" in kinds
    # same id, different kind: two distinct keys
    assert model.element(("sample", 3)) is not model.element(("lab_notebook_page", 3))
    assert model.element(("sample", 3)).collection_ids == [1, 2]
    assert model.element(("sample", 1)).molecule.cano_smiles == "CCO"


def test_mini_fixture_attachment_bytes():
    model = ExportModel.load(FIXTURE)
    (notes,) = model.element(("sample", 3)).attachments
    assert _bytes(notes) == b"hello"
    jdx = list(model.element(("reaction", 5)).all_attachments())
    assert [a.filename for a in jdx] == ["product_1H.jdx"]
    assert _bytes(jdx[0]).startswith(b"##TITLE=")


def test_mini_fixture_round_trips_unchanged():
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert ExportModel.load(FIXTURE).to_json(inline_bytes=True) == raw
