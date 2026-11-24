# chemotion-crate

Export a [Chemotion ELN](https://chemotion.net) collection as an `.eln` file, and read such files back.

An `.eln` file is a ZIP holding an [RO-Crate 1.2](https://w3id.org/ro/crate/1.2) that follows [TheELNFileFormat](https://github.com/TheELNConsortium/TheELNFileFormat) profile `1.2+20260923`. Samples and reactions are typed with [chem-dcat-ap](https://github.com/nfdi-de/chem-dcat-ap). The exporter talks to the ELN only through its REST API, via the [`chemotion-api`](https://github.com/Chemotion/ChemotionAPI) client (== 402.0.0).

## Install

```sh
pip install chemotion-crate                 # or: uv add chemotion-crate
pip install "chemotion-crate[interactive]"  # with tqdm progress bars
```

Requires Python 3.14. Verified against Chemotion ELN v4.0.2.

## Versions

chemotion-crate's version names the ELN line it is built for; the rest is its own. `402.0.0` reads:

| Part | Here | Meaning |
|---|---|---|
| first | `402` | the ELN line, shared with chemotion-api: ELN v4.0.2 (chemotion-api 402.x) |
| second | `0` | chemotion-crate's own minor release: additions, and every move to another chemotion-api release |
| third | `0` | chemotion-crate's own patch release: fixes |

chemotion-api is pinned exactly in the package's dependencies (`chemotion-api==402.0.0` here), so a new chemotion-api release means a new chemotion-crate release, at least a minor one; the changelog names the pin of each release, and every exported `.eln` records the chemotion-api version that made it. A new ELN line starts a new first number.

## Quick start

Export a collection (and every collection below it):

```python
from chemotion_api import Instance
from chemotion_crate import Author, AuthorAffiliation, export

instance = Instance("https://my.chemotion").test_connection().login_token("<token>")
collection = instance.get_root_collection().get_collection("My project")

export(instance, collection, "my_project.eln", authors=[
    Author(name="Ada Lovelace", orcid="0000-0002-1825-0097",
           affiliations=[AuthorAffiliation(name="KIT", ror="https://ror.org/04t3en479")],
           email="ada@example.org"),
])
```

Read it back:

```python
from chemotion_crate import read

with read("my_project.eln") as crate:
    print(crate.root.name, [a.name for a in crate.authors])
    for element in crate.elements:
        print(element.kind, element.name, [c.name for c in element.collections])
        for f in element.files:
            data = f.read()               # payload bytes
            # f.extract("out/")           # or write it to disk (path-traversal safe)
```

## Authors

`authors` is required; authorship is never taken from the logged-in user. Each `Author` has a `name`, an `orcid` (bare `0000-0002-1825-0097` or the full URL), one or more `AuthorAffiliation(name, ror)` and an `email`, which becomes the crate's contact point (RO-Crate asks for one). If privacy is a concern, use `no-reply@eln.invalid`: the `.invalid` domain never delivers. ORCID iDs and ROR ids are validated including their check digits, and a typo raises `ValueError` before anything is fetched. The first author's first affiliation becomes the crate's publisher.

The authors you give are recorded as the authors of the crate and of every element in it: who created each element in the ELN is not exported. The export says so once, as a warning, the first time authors are given.

## `export()` options

`export(instance, collection, file_path, *, authors, ...)` returns `str(file_path)`. All options after `file_path` are keyword-only.

| Option | Default | Effect |
|---|---|---|
| `authors` | required | List of `Author` (see above). |
| `strict` | `False` | Raise `RuntimeError` instead of writing a crate when anything would be left out (an element that could not be loaded, an attachment that could not be downloaded, ...). Nothing is written at `file_path` in that case. |
| `ontology_lookup` | `True` | Annotate analyses with `measurementTechnique` ontology terms. Resolution order: the `CHMO:...` code embedded in the analysis kind, then the instance's own `ols_terms` vocabulary, then an EBI OLS lookup. **Sends data to a third party; see [Privacy and network access](#privacy-and-network-access).** |
| `ols_cache` | `None` | Path of a JSON cache of OLS lookups, read and updated, so each term is looked up once. Saved even when a strict export is refused. |
| `disclose_host` | `True` | Record the ELN's URL in the crate. `False` replaces it, case-insensitively and in free text too, with the sentinel `https://withheld.chemotion.invalid`; the ELN's version is still recorded. |
| `declare_skipped` | `True` | Record what the export left out in the crate itself, as an export manifest (see below). Without it a partial crate reads exactly like a complete one. |
| `progress_callback` | `None` | `callable(processed, total, name)`, called once per element while mapping and once per file while writing. An exception it raises never aborts the export. |
| `interactive` | `False` | Show `tqdm` progress bars when tqdm is installed (the `interactive` extra). |

`export_model(model, file_path, *, authors, ...)` writes a crate from an already extracted `ExportModel` without a server; it takes the same options plus `vocabulary` and `validate` (run py-rocrate over the staged crate before zipping, default `True`). The tests and the conformance check use it.

## Privacy and network access

- **EBI OLS.** With `ontology_lookup=True`, the default, every analysis name that has no embedded `CHMO:` code and no match in the instance's own vocabulary is sent to EBI's Ontology Lookup Service at `www.ebi.ac.uk`. This happens regardless of `disclose_host`. Pass `ontology_lookup=False` to use embedded codes only and make no such request, or `ols_cache=` to look each term up only once.
- **The ELN host.** By default the crate names the ELN's URL (as the source the root dataset `isBasedOn`). `disclose_host=False` withholds it, replacing it in identifiers and in prose.
- **What the crate always contains.** The authors you pass (names, ORCID iDs, affiliations with ROR ids, e-mail if given); the exported collections and their elements with all their exported fields and attachments; the ELN's version string and revision as read from its `/about` page (also with `disclose_host=False`); the versions of `chemotion-crate`, `chemotion-api`, `chem-dcat-ap` and `dcat-ap-plus` that produced it; the export timestamp.
- The export manifest records counts and stable reasons only, never raw error text (which can contain the hostname).

## What a crate contains

- **Root dataset** `./`: the exported collection. Declares conformance to RO-Crate 1.2 and the `.eln` profile, a licence (CC BY 4.0 when the collection specifies none), the authors, the publisher and the source ELN.
- **Elements** with files are Dataset data entities in collection-independent folders: `./samples/<label>_<id>/`, `./reactions/...`, `./wellplates/...`, `./research_plans/...`, `./device_descriptions/...`, and `./generics/<type>/...` for other kinds. An element without files has no folder: it is a Dataset with the local id `#samples/<label>_<id>` (same shape, `#` instead of `./`), because RO-Crate asks a folder to list what it contains. Both kinds are listed from the root. An element that sits in several collections is stored once.
- **chem-dcat-ap typing.** A reaction becomes a `ChemicalReaction`. Each of its materials is a node of its own (`#reactions-<label>_<id>-starting_material-1`, …), typed by its role (`StartingMaterial`, `Reagent`, `ChemicalProduct`, `DissolvingSubstance`) and carrying the amounts used in that reaction; it points to the sample with `isPartOf` when the sample is in the export. A sample is a `SubstanceSample` (or `DissolvingSubstance`) with its own amounts, so a sample used in several reactions keeps each use apart. A sample whose reactions are not in the export is typed by its role there. ELN fields without a chem-dcat-ap home are carried as `PropertyValue` entries.
- **Every field the ELN holds is kept.** What chem-dcat-ap has no place for travels as a `PropertyValue` (`chemotion:<field>`; nested values as JSON): a sample's purity, stereochemistry, solvent composition, target and real amounts, PubChem tag, elemental compositions, residues, mixture components, parent sample, gas-phase data and more; a reaction's pH, vessel size, dangerous products, reaction type, gaseous flag, concentration mode and more; each material's place in its reaction (reference, coefficient, waste, conversion rate, weight percentage, the equivalent of a product or solvent) on the material's node, and, when its sample is not in the export, the sample's own fields there too; purification solvents, as material nodes the reaction `mentions`; and every element's LabIMotion segments as `segment / layer / field`. Values that only restate the ELN's empty default (a flag that is off, "any" stereochemistry, a quantity with only a unit) are left out.
- **Other kinds** (wellplate, research plan, device description, cell line, screen, sequence-based macromolecule sample, vessel, generic LabIMotion elements) are plain `Dataset`s with an ontology placeholder: a `PropertyValue` in `variableMeasured` whose `identifier` can be filled in later (see [Patching ontology terms](#patching-ontology-terms)). Their fields travel as `PropertyValue`s, a quantity with its `unitText`: a cell line's own fields and those of its cell-line material (`material organism`, …), a screen's fields, an SBMM sample's fields and those of its protein (`protein primary accession`, …). A screen `mentions` the wellplates and research plans it links to that are in the export. A vessel carries its own fields and its template's (type, material, volume). Folders: `./cell_lines/`, `./screens/`, `./sbmm_samples/`, `./vessels/`.
- **Collections** are contextual `Collection` entities (`#collection-...`) that the root `mentions`. Membership is recorded both ways: element `isPartOf`, collection `hasPart`. A sub-collection is `isPartOf` its parent, so the tree is kept.
- **Attachments** are `File` entities inside their element's folder, with `contentSize` and `sha256` computed from the bytes actually written, listed from the element, from the analysis they belong to and from the root.
- **Analyses** are `Dataset`s with a `#`-prefixed id, `about` their element and mentioned by it, carrying name, description, status, `measurementTechnique` and their files.
- **Ontology terms** (analysis techniques, reaction types, dataset classes) are `DefinedTerm`s, each recorded once.
- **Export manifest** `#export-manifest`: a `CreativeWork` the root `mentions`, present only when there is something to declare. It separates **omissions** (something is missing from the crate: an element that could not be loaded, a file that could not be downloaded or that the server does not have, fields withheld by a restricted share), which make it read "This crate is incomplete", from **notes** (nothing is missing: metadata the source lacked, an element stored at a `~2` path because its name collided, a collection that appears twice in the tree, a comma in a keyword replaced by `;`). One `PropertyValue` per (kind, reason) with a count: `chemotion:omitted/<kind>` or `chemotion:note/<kind>`. It also credits, in one line, the CC BY 4.0 vocabularies (CHMO, RXNO, ChEBI, …) whose term labels the crate uses (`chemotion:attribution`). A `strict=True` export fails on omissions, not on notes.
- **Names in paths and ids**: element folders, file names and `@id`s keep only `A-Z a-z 0-9 _ -`. Letters from other alphabets are transliterated first (`Benzoesäure` → `Benzoesaure`, `Образец` → `Obrazets`, `苯甲酸` → `BenJiaSuan`), so a name stays readable in a path; everything else becomes `_`. Only paths and ids change: each element's and file's original name is kept in its `name`. The first time a name is transliterated, the export logs a warning saying so.
- **Producer marker**: a `SoftwareApplication` named `chemotion-crate` (`#chemotion-crate`), the `instrument` of the `CreateAction` that created the crate. `read()` uses it to recognise its own crates.

## Reading

`read(path, allow_foreign=False)` opens a `.eln` ZIP or an already extracted crate directory and returns an `ElnCrate` (use it as a context manager).

- `crate.root`, `crate.elements`, `crate.datasets`, `crate.collections`, `crate.files`, `crate.authors`, `crate.source_eln`, `crate.producer`, `crate.date_created`, `crate.get_dataset(id)`.
- `crate.ro_crate_version` (`"1.2"`, from the metadata descriptor's `conformsTo`) and `crate.eln_version` (`"1.2+20260923"`, from the root's `conformsTo`), as declared, including versions this reader does not know, so a caller can warn or refuse; `None` when the crate declares none.
- `ElnDataset`: `id`, `name`, `description`, `types`, `kind`, `source_id` (the element's id in the ELN: an `int`, or a UUID `str` for a vessel; `None` when the crate does not say), `files`, `datasets`, `collections`.
- `ElnCollection`: `id`, `name`, `description`, `parent`, `elements`.
- `ElnFile`: `id`, `name`, `encoding_format`, `content_size`, `read()`, `extract(dest_dir)`.

A crate is treated as untrusted input: payload paths cannot leave the crate root, archive members are capped in size (500 MiB) and compression ratio (200:1) before they are read, and ORCID/ROR identifiers the crate claims are only reported when they validate.

## Patching ontology terms

When an ontology term for an element kind is decided after export, fill the placeholders in place instead of re-exporting:

```python
from chemotion_crate.patch import patch_eln_ontology

patch_eln_ontology("my_project.eln",
                   {"wellplate": "http://purl.obolibrary.org/obo/OBI_0400081"},
                   output_path="my_project.patched.eln")   # omit to overwrite in place
```

Each matching Dataset gets the IRI as the placeholder's `identifier` and as an additional `@type`, written compact (`OBI:0400081`) when the crate declares the prefix, and the term is described once as a `DefinedTerm` that the root `mentions`; this is what an export does for a term set in `ELEMENT_TYPE_IRI`. A `"generic"` entry applies to every LabIMotion type without an entry of its own. It returns the number of nodes patched. `patch_metadata_dict(metadata, mapping)` does the same on a parsed `ro-crate-metadata.json`. To have new exports carry a term, add it to `ELEMENT_TYPE_IRI` in `chemotion_crate/ontology_map.py`.

## Conformance

`just conformance` exports two crates from the committed fixture (`tests/fixtures/mini_model.json`; no server, no network lookups), one of them with `disclose_host=False`, and checks each with `scripts/check_crate_conformance.py`:

- py-rocrate (0.15.1, as TheELNFileFormat uses) must parse it;
- roc-validator must report no REQUIRED issues against the `ro-crate-1.2` profile, run twice: **0.11.3**, the version TheELNFileFormat's CI pins and the stricter one about compact IRIs, and **0.13.0**, which has rules 0.11.3 lacks;
- no absolute API IRIs leak into identifiers, and the root declares an author.

The validator runs on Python 3.13 in a throwaway environment because its dependencies do not build on 3.14.

The test suite validates the chem-dcat-ap parts of the graph against the chem-dcat-ap SHACL shapes (`tests/data/chem_dcat_ap.shacl.ttl`, with pyshacl).

## Development

```sh
just sync      # uv sync --all-extras --group dev
just check     # ruff + mypy + the offline test suite
just conformance
```

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Limitations

- Element kinds without a chem-dcat-ap mapping (everything except samples and reactions) are plain `Dataset`s with an ontology placeholder.
- Attachments that cannot be downloaded are withdrawn from the crate (no partial file, no `File` entity) and declared in the export manifest; with `strict=True` the export fails instead.
- Elements shared with you below full detail level arrive with some fields withheld by the server. Those fields are left out (never written as `***`), the element is kept, and the crate names them: a `chemotion:withheld` `PropertyValue` on the element, and a count in the export manifest.
- `read()` opens only crates written by this package. `.eln` files from other ELNs open only with `allow_foreign=True`, with best-effort generic RO-Crate parsing: element kinds and authors may be missing.
- There is no import into a Chemotion ELN.

## License

MIT, see [LICENSE](LICENSE).
