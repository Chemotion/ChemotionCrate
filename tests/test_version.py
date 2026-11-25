"""The version names the ELN line, and chemotion-api is pinned exactly to a release of that line."""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

PYPROJECT = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())


def test_chemotion_api_is_pinned_exactly_to_a_release_of_the_versions_eln_line():
    version = PYPROJECT["project"]["version"]
    (pin,) = [d for d in PYPROJECT["project"]["dependencies"] if d.startswith("chemotion-api")]
    match = re.fullmatch(r"chemotion-api==(\d+)\.\d+\.\d+", pin.replace(" ", ""))
    assert match, f"chemotion-api must be pinned to one release: {pin}"
    assert version.split(".")[0] == match.group(1), \
        f"version {version} names another ELN line than the pin {pin}"
