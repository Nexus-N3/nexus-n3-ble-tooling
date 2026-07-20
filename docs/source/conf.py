from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(os.environ.get("RS_NEXUS_BLE_TOOLING_ROOT", Path(__file__).resolve().parents[2]))
sys.path.insert(0, os.fspath(ROOT))

project = "Nexus N3 BLE Tooling"
author = "Nexus N3"
copyright = "2026, Nexus N3"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx_wagtail_theme",
]

autosummary_generate = True
autodoc_member_order = "bysource"
autodoc_typehints = "description"
napoleon_google_docstring = False
napoleon_numpy_docstring = False

templates_path = []
exclude_patterns = ["_build"]

html_theme = "sphinx_wagtail_theme"
html_title = project
html_theme_options = {
    "project_name": project,
}

html_static_path = []
