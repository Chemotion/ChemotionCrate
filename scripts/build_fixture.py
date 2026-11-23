"""Build tests/fixtures/mini_model.json: a small ExportModel with no server behind it.

Two collections; one sample belongs to both (the case the crate has to be able to state).
Attachment bytes are inlined (base64) so the fixture is self-contained.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chemotion_crate.model import (  # noqa: E402
    AnalysisRecord, AttachmentRecord, CollectionRecord, DatasetRecord, ElementRecord,
    ExportModel, MoleculeRecord, SourceInfo)

HOST = "https://eln.example.org"
JCAMP = b"""##TITLE=Ethanol 1H NMR (synthetic fixture data)
##JCAMP-DX=4.24
##DATA TYPE=NMR SPECTRUM
##ORIGIN=chemotion-crate fixture
##OWNER=public domain
##.OBSERVE NUCLEUS=1H
##XUNITS=HZ
##YUNITS=ARBITRARY UNITS
##FIRSTX=0
##LASTX=4
##NPOINTS=5
##XYDATA=(X++(Y..Y))
0 12 48 31 7
##END=
"""

ethanol = MoleculeRecord(id=11, cano_smiles="CCO", inchikey="LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
                         inchistring="InChI=1S/C2H6O/c1-2-3/h3H,2H2,1H3", iupac_name="ethanol",
                         sum_formular="C2H6O", molecular_weight=46.07, cas=["64-17-5"])
acetaldehyde = MoleculeRecord(id=12, cano_smiles="CC=O", inchikey="IKHGUXGNUITLKF-UHFFFAOYSA-N",
                              inchistring="InChI=1S/C2H4O/c1-2-3/h2H,1H3", iupac_name="acetaldehyde",
                              sum_formular="C2H4O", molecular_weight=44.05, cas=["75-07-0"],
                              names=["acetic aldehyde"])


def sample(id, name, label, mol, **kw):
    return ElementRecord(kind="sample", id=id, name=name, short_label=label,
                         url=f"{HOST}/api/v1/samples/{id}", created_at="2026-01-10T09:00:00+00:00",
                         updated_at="2026-01-11T09:00:00+00:00", molecule=mol, **kw)


sm = sample(1, "Ethanol", "MT-1", ethanol, role="starting_materials", amount_mol=0.1, mass_g=4.607,
            density=0.789, equivalent=1.0, properties={"melting_point": "-114", "boiling_point": "78",
                                                       "sum_formula": "C2H6O", "xref": {"cas": "64-17-5"}})
prod = sample(2, "Acetaldehyde", "MT-2", acetaldehyde, role="products", mass_g=2.0, density=0.784,
              properties={"melting_point": "-123", "boiling_point": "20"})
shared = sample(3, "Shared Sample", "MT-3", ethanol, keywords=["fixture", "shared, comma"],
                description="A sample that lives in two collections.", collection_ids=[1, 2],
                attachments=[AttachmentRecord(filename="notes.txt", identifier="att-notes-uuid", id=7,
                                              filesize=5, mime_type="text/plain",
                                              read=lambda: iter([b"hello"]))])
sm.collection_ids = [1]
prod.collection_ids = [1]

reaction = ElementRecord(
    kind="reaction", id=5, name="Oxidation of ethanol", short_label="MT-R1",
    url=f"{HOST}/api/v1/reactions/5", created_at="2026-01-12T09:00:00+00:00",
    updated_at="2026-01-13T09:00:00+00:00", description="Mild oxidation.",
    observation="Clear solution turned yellow.", status="Successful", duration="3.5 Hour(s)",
    temperature={"valueUnit": "°C", "userText": "25", "data": []}, yield_percent=85.0,
    rxno="RXNO:0000021 | oxidation", collection_ids=[1],
    properties={"rinchi_long_key": "RInChI=1.00.1S/fixture", "purification": ["Extraction", "Filtration"],
                "variations": [{"id": 1, "temperature": {"value": 25, "unit": "°C"}}],
                "tlc_solvents": "EtOAc/hexane 1:4", "rf_value": "0.4"},
    materials={"starting_materials": [sm], "products": [prod]},
    analyses=[AnalysisRecord(
        id=31, name="1H NMR of product",
        kind="CHMO:0000595 | proton nuclear magnetic resonance spectroscopy (1H NMR)",
        status="Confirmed", content="clean spectrum",
        datasets=[DatasetRecord(id=41, name="nmr-data", klass_code="CHMO:0000593",
                                klass_label="NMR spectroscopy",
                                attachments=[AttachmentRecord(
                                    filename="product_1H.jdx", identifier="att-1-uuid", id=1,
                                    filesize=1234, mime_type="chemical/x-jcamp-dx",
                                    read=lambda: iter([JCAMP]))])])])

wellplate = ElementRecord(kind="wellplate", id=8, name="Screening plate", short_label="MT-WP1",
                          url=f"{HOST}/api/v1/wellplates/8", created_at="2026-01-14T09:00:00+00:00",
                          collection_ids=[2], property_values=[{"name": "well A1", "value": "MT-1"}])
plan = ElementRecord(kind="research_plan", id=9, name="Plan", url=f"{HOST}/api/v1/research_plans/9",
                     description="Oxidise ethanol; check by NMR.", collection_ids=[1])
device = ElementRecord(kind="device_description", id=10, name="Bruker 400", short_label="MT-D1",
                       url=f"{HOST}/api/v1/device_descriptions/10", collection_ids=[1],
                       property_values=[{"name": "vendor company name", "value": "Bruker"},
                                        {"name": "operation mode", "value": "manual"}],
                       ontology_terms=[{"iri": "http://purl.obolibrary.org/obo/CHMO_0000613",
                                        "label": "NMR instrument"}])
generic = ElementRecord(kind="lab_notebook_page", id=3, name="Page 3", url=f"{HOST}/api/v1/generic_elements/3",
                        collection_ids=[2], property_values=[{"name": "layer.temperature", "value": 25,
                                                              "unitText": "°C"}])

model = ExportModel(
    root_collection_id=1,
    collections=[CollectionRecord(id=1, label="Mini Test Collection", description="Fixture root"),
                 CollectionRecord(id=2, label="Shared Collection", parent_id=1)],
    elements=[sm, prod, shared, reaction, wellplate, plan, device, generic],
    source=SourceInfo(host=HOST, version="v4.0.1", revision="abc1234"),
)
out = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "mini_model.json"
out.write_text(json.dumps(model.to_json(inline_bytes=True), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("wrote", out, out.stat().st_size, "bytes")
