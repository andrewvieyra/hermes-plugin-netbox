"""Import the plugin as the package name Hermes uses (``hermes_plugins.netbox``) from the repo root."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "hermes_plugins.netbox"


def load_plugin():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []  # type: ignore[attr-defined]
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        PACKAGE, REPO_ROOT / "__init__.py", submodule_search_locations=[str(REPO_ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def submodule(name: str):
    load_plugin()
    import importlib

    return importlib.import_module(f"{PACKAGE}.{name}")
