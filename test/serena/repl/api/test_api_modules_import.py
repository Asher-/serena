"""Every module of the REPL's API facades imports, so a tool delegating to one of them finds it."""

import importlib
import pkgutil

import pytest

import serena.repl.api

_MODULES = sorted(module.name for module in pkgutil.iter_modules(serena.repl.api.__path__, prefix="serena.repl.api."))


def test_the_api_package_holds_modules() -> None:
    assert len(_MODULES) >= 8, _MODULES


@pytest.mark.parametrize("module_name", _MODULES)
def test_each_api_module_imports(module_name: str) -> None:
    importlib.import_module(module_name)
