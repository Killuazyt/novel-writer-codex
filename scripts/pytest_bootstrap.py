"""Explicit pytest bootstrap loaded before third-party entry-point plugins."""

from __future__ import annotations

import os
import sys
from pathlib import Path


# pytest imports explicit ``-p`` plugins before scanning third-party entry
# points.  This covers direct ``python -m pytest`` runs where CPython does not
# put the working directory on sys.path early enough to auto-import the root
# sitecustomize module.
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from test_isolation import activate_test_isolation, install_network_guard  # noqa: E402


activate_test_isolation(_REPO_ROOT)
install_network_guard()
