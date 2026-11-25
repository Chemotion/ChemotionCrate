# What a crate contains

An `.eln` file is a ZIP with one top-level folder, named after the file (`my_project.eln/`), holding an RO-Crate 1.2: `ro-crate-metadata.json` and the payload.

## Layout

```text
my_project.eln/
├── ro-crate-metadata.json
├── samples/<label>_<id>/            one folder per element that has files
│   └── spectrum.jdx                 its attachments
├── reactions/<label>_<id>/
├── wellplates/  research_plans/  device_descriptions/
├── cell_lines/  screens/  sbmm_samples/  vessels/
└── generics/<type>/<label>_<id>/    LabIMotion generic elements
```

- **Root dataset** `./`: the exported collection. It declares conformance to RO-Crate 1.2 and to the `.eln` profile, a licence (CC BY 4.0 when the collection names none), the authors, the publisher (the first author's first affiliation) and the source ELN with its version.
- **Elements** live in collection-independent folders, so an element that sits in several collections is stored once. An element without files has no folder: it is a Dataset with a local id of the same shape (`#samples/<label>_<id>`).
- **Collections** are contextual `Collection` entities (`#collection-<id>`) that the root `mentions`. Membership runs both ways (element `isPartOf`, collection `hasPart`), and a sub-collection is `isPartOf` its parent.
- **Attachments** are `File` entities in their element's folder, with `contentSize` and `sha256` of the bytes actually written.
- **Analyses** are Datasets with a `#` id (`#samples-<label>_<id>-analysis-<analysis id>`), `about` their element, carrying `measurementTechnique` and their files.
- **Names in paths and ids** keep only `A-Z a-z 0-9 _ -`; other alphabets are transliterated first (`Benzoesäure` → `Benzoesaure`). The original name stays in `name`.
- **Producer marker**: the `chemotion-crate` `SoftwareApplication` (`#chemotion-crate`), the `instrument` of the `CreateAction` that made the crate, with the versions of the packages that produced it. {func}`chemotion_crate.read` uses it to recognise its own crates.

## Typing

- A **reaction** is a chem-dcat-ap `ChemicalReaction`. Each of its materials is a node of its own (`#reactions-<label>_<id>-starting_material-1`, …), typed by its role (`StartingMaterial`, `Reagent`, `ChemicalProduct`, `DissolvingSubstance`), with the amounts used in that reaction, and `isPartOf` the sample when the sample is in the export.
- A **sample** is a `SubstanceSample` (or `DissolvingSubstance` for a solvent) with its own amounts.
- **Every other kind** (wellplate, research plan, device description, cell line, screen, sequence-based macromolecule sample, vessel, generic LabIMotion element) is a plain `Dataset` with an ontology placeholder: a `PropertyValue` (`propertyID` `chemotion:elementType`) in `variableMeasured` whose `identifier` can be filled in later with {func}`chemotion_crate.patch.patch_eln_ontology`.
- **ELN fields without a vocabulary home** travel as `PropertyValue`s (`chemotion:<field>`, nested values as JSON), including LabIMotion segments as `segment / layer / field`. Values that only restate the ELN's empty default are left out.
- **Ontology terms** (analysis techniques, reaction types, element classes) are `DefinedTerm`s, each recorded once.

## Export manifest

When an export has something to declare, the root `mentions` a `CreativeWork` `#export-manifest`. It holds one `PropertyValue` per kind and reason, with a count:

- **omissions**, `chemotion:omitted/<kind>`: something is missing from the crate (an element that could not be loaded, a file that could not be downloaded or that the server does not have, fields withheld from a restricted share). The manifest then reads "This crate is incomplete", and a `strict=True` export fails instead of writing it.
- **notes**, `chemotion:note/<kind>`: nothing is missing, but something is worth knowing (metadata the source lacked, an element stored at a `~2` path because its name collided, a collection that appears twice in the tree, a comma in a keyword replaced by `;`). Notes never fail a strict export.

Reasons are stable wording, never raw error text, which can carry the hostname. The manifest also credits, in one line (`chemotion:attribution`), the CC BY 4.0 vocabularies whose term labels the crate uses. `declare_skipped=False` leaves the manifest out.

## Withheld fields

An element shared with you below full detail level arrives with some fields masked by the server. Those fields are left out (never written as `***`), the element is kept, and the crate says which: a `chemotion:withheld` `PropertyValue` on the element lists the field names, and the export manifest counts the withheld fields as an omission (`chemotion:omitted/sample field`, …), so the crate reads as incomplete and a `strict=True` export fails.

With `disclose_host=False`, the ELN's URL is replaced everywhere, free text included, by `https://withheld.chemotion.invalid`; the ELN's version is still recorded.
