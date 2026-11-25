"""Sphinx configuration for the chemotion-crate documentation (https://chemotioncrate.readthedocs.io).

The pages are Markdown (MyST) and reuse README.md, CHANGELOG.md and CONTRIBUTING.md by inclusion,
so the PyPI page and the docs cannot drift apart. The API reference is autodoc over the installed
package: build in the project environment (`just docs`; Read the Docs runs `uv sync --group docs`).
"""
from importlib.metadata import version as _version

# -- Project information -----------------------------------------------------

project = "chemotion-crate"
author = "Shashank S. Harivyasi"
copyright = "2026, Shashank S. Harivyasi"
# From the installed package, so it cannot drift from pyproject.toml.
release = _version("chemotion-crate")
version = release

# -- General configuration ---------------------------------------------------

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx_rtd_theme",
]
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# README links such as `#privacy-and-network-access` are GitHub-style heading anchors.
myst_heading_anchors = 3

# Document members in source order; class docstring and __init__ docstring together. Nothing is
# mocked: autodoc imports the package, and every runtime dependency is installed.
autodoc_member_order = "bysource"
autoclass_content = "both"
autodoc_typehints = "description"

# -- Options for HTML output -------------------------------------------------

html_theme = "sphinx_rtd_theme"
