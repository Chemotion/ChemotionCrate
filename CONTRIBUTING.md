# Contributing

## Environment

Python 3.14 and [uv](https://docs.astral.sh/uv/); [just](https://just.systems) runs the recipes.

```sh
just sync      # uv sync --all-extras --group dev
```

`chemotion-api` (== 401.1.1) comes from PyPI like every other dependency, and `uv.lock` pins it. To work against an unreleased client, point `[tool.uv.sources]` at a local checkout of [Chemotion/ChemotionAPI](https://github.com/Chemotion/ChemotionAPI) for the session, and do not commit that override.

## Gates

Run before every push:

```sh
just check         # ruff check . + mypy chemotion_crate + pytest (offline)
just conformance   # export the fixture crates and validate them
```

Tests marked `integration` need a live Chemotion instance and are deselected by default (`addopts` in `pyproject.toml`). Run them against a seeded test stack (the chemotion-api repository's `tests/test_config`):

```sh
CHEMOTION_URL=http://127.0.0.1:3579 CHEMOTION_USER=API CHEMOTION_PASSWORD='…' \
CHEMOTION_SECOND_USER=MM CHEMOTION_SECOND_PASSWORD='…' \
    uv run pytest -m integration tests/test_integration_live.py
```

The second user receives a share below full detail level, so the restricted-element path is tested on a real server.

## The chemotion-api contract

chemotion-crate reads the ELN only through chemotion-api, and only through the names in `tests/test_client_contract.py`. Using a new client name means adding it there. chemotion-api is pinned exactly (`==401.1.1`, the release named in the version). `just client-latest` runs the contract and the offline suite against the newest chemotion-api on PyPI in a throwaway environment, ignoring the pin; CI does the same weekly (job `client-latest`) as an early warning that does not gate releases.

## Conformance

`just conformance` exports `conformance.eln` and `conformance-withheld.eln` (`disclose_host=False`) from `tests/fixtures/mini_model.json` with `scripts/export_conformance_crate.py`, then runs `scripts/check_crate_conformance.py` on both with roc-validator **0.11.3** and **0.13.0** (plus rocrate 0.15.1, the version TheELNFileFormat uses) on Python 3.13, and deletes the crates. Keep both validator versions: 0.11.3 is what TheELNFileFormat's CI pins and is stricter about compact IRIs; 0.13.0 has rules 0.11.3 lacks. The validator runs in a throwaway 3.13 environment because its dependencies do not build on 3.14.

## The vendored RO-Crate context subset

`chemotion_crate/data/ro-crate-context.jsonld` is generated, never hand-edited. It holds every prefix declaration of the official RO-Crate 1.2 context plus the terms listed in `CONTEXT_TERMS` (`chemotion_crate/context_terms.py`), copied verbatim by `scripts/update_context_subset.py`. A test fails when the exporter emits a term the subset does not define.

To add a context term:

1. Add it to `CONTEXT_TERMS` in `chemotion_crate/context_terms.py` (it must exist in the official context; the script refuses otherwise).
2. Run `just context` (fetches `https://w3id.org/ro/crate/1.2/context`; `just context path/to/context.jsonld` uses a local copy).
3. Commit both files together, and run `just check` and `just conformance`.

## Changes

Add a line under "Unreleased" in `CHANGELOG.md` for anything a user of the package would notice.
