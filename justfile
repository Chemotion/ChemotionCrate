# Task runner for chemotion-crate. `uv` manages Python and dependencies; nothing here needs a
# Chemotion server. Run `just` to list recipes.

# list available recipes
default:
    @just --list

# install/refresh the virtualenv with the extras and the dev group
sync:
    uv sync --all-extras --group dev

# lint (ruff; rules in pyproject.toml)
lint:
    uv run ruff check .

# apply auto-fixable lint findings
lint-fix:
    uv run ruff check . --fix

# static type checking
mypy:
    uv run mypy chemotion_crate

# run the offline test suite (integration tests are deselected in pyproject.toml)
test:
    uv run pytest

# ruff + mypy
static: lint mypy

# the local gate: lint + types + the offline suite
check: lint mypy test

# The validator runs on 3.13 in a throwaway env: its dependencies cannot build on 3.14.
# Two validator versions on purpose. 0.11.3 is what TheELNFileFormat's CI pins, and it is the
# STRICTER of the two here: it rejects a compact IRI whose prefix alone is declared, where 0.13.0
# accepts it. Passing only the newer one let a crate through that their CI rejects. 0.13.0 is kept
# because it carries rules 0.11.3 does not have.
# The second crate is exported with disclose_host=False (--withheld).
#
# export two fixture crates (no server needed) and check them against RO-Crate 1.2
conformance:
    uv run python scripts/export_conformance_crate.py conformance.eln
    uv run python scripts/export_conformance_crate.py --withheld conformance-withheld.eln
    uv run --python 3.13 --no-project --with roc-validator==0.11.3 --with rocrate==0.15.1 \
        python scripts/check_crate_conformance.py conformance.eln
    uv run --python 3.13 --no-project --with roc-validator==0.11.3 --with rocrate==0.15.1 \
        python scripts/check_crate_conformance.py conformance-withheld.eln
    uv run --python 3.13 --no-project --with roc-validator==0.13.0 --with rocrate==0.15.1 \
        python scripts/check_crate_conformance.py conformance.eln
    uv run --python 3.13 --no-project --with roc-validator==0.13.0 --with rocrate==0.15.1 \
        python scripts/check_crate_conformance.py conformance-withheld.eln
    rm -f conformance.eln conformance-withheld.eln

# Writes chemotion_crate/data/ro-crate-context.jsonld from the official RO-Crate 1.2 context
# (fetched from w3id.org unless `source` names a local path or URL).
#
# regenerate the vendored RO-Crate context subset
context source="":
    uv run python scripts/update_context_subset.py {{source}}

# early warning: the client contract and the offline suite against the NEWEST chemotion-api on
# PyPI, ignoring the exact pin. A throwaway environment: the locked dependencies minus the pin, the
# package itself, then the newest client. The project's own environment stays on the pin.
client-latest:
    rm -rf .client-latest && uv venv -q --python 3.14 .client-latest
    uv export -q --frozen --no-hashes --no-emit-project --all-extras --group dev | grep -v '^chemotion-api==' > .client-latest/requirements.txt
    VIRTUAL_ENV=.client-latest uv pip install -q -r .client-latest/requirements.txt
    VIRTUAL_ENV=.client-latest uv pip install -q --no-deps -e .
    VIRTUAL_ENV=.client-latest uv pip install -q --upgrade "chemotion-api<500"
    .client-latest/bin/python -c "import importlib.metadata as m; print('chemotion-api', m.version('chemotion-api'))"
    .client-latest/bin/python -m pytest tests/test_client_contract.py
    .client-latest/bin/python -m pytest
    rm -rf .client-latest
