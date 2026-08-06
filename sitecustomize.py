from __future__ import annotations

import os
import sys
from pathlib import Path


def _looks_like_pytest_process() -> bool:
    if os.environ.get("WEBNOVEL_TEST_ISOLATION") == "1":
        return True

    original = list(getattr(sys, "orig_argv", ()) or ())
    tokens = [str(token) for token in [*original, *sys.argv] if str(token)]
    for index, token in enumerate(tokens):
        name = Path(token).name.casefold()
        if name == "pytest" or name.startswith("pytest."):
            return True
        if token == "-m" and index + 1 < len(tokens):
            if tokens[index + 1].casefold() == "pytest":
                return True
    return False


if _looks_like_pytest_process():
    # Do this before importing the helper so pytest cannot load global plugins
    # or write bytecode outside the repository-owned test session.
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

    repo_root = Path(__file__).resolve().parent
    scripts_dir = repo_root / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    from test_isolation import activate_test_isolation, install_network_guard

    activate_test_isolation(repo_root)
    install_network_guard()
