"""
Every module compiles without a SyntaxWarning, e.g. ``is not ""`` comparing
identity with a literal instead of value (#137).
"""
import pathlib
import warnings

import pytest

import mprov_jobserver

PACKAGE = pathlib.Path(mprov_jobserver.__file__).parent
SOURCES = sorted(PACKAGE.rglob("*.py"))


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(PACKAGE)))
def test_compiles_without_syntax_warnings(path):
  with warnings.catch_warnings():
    warnings.simplefilter("error", SyntaxWarning)
    compile(path.read_text(), str(path), "exec")
