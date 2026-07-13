from __future__ import annotations

import ast
import re
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
CATALOG_PATH = TESTS_DIR / "TEST_CATALOG.md"


def test_catalog_lists_every_web_pytest_case() -> None:
    catalog = CATALOG_PATH.read_text(encoding="utf-8")
    cases: list[str] = []
    for path in sorted(TESTS_DIR.glob("test_web*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        cases.extend(
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )

    assert cases, "Aucun cas pytest web n'a ete trouve"
    missing = sorted(case for case in cases if f"`{case}`" not in catalog)
    assert not missing, (
        "Cas pytest web absents de tests/TEST_CATALOG.md : "
        f"{', '.join(missing)}"
    )


def test_catalog_lists_every_playwright_case() -> None:
    catalog = CATALOG_PATH.read_text(encoding="utf-8")
    pattern = re.compile(
        r"\btest\(\s*(?P<quote>['\"])(?P<title>.*?)(?P=quote)"
    )
    cases: list[str] = []
    for path in sorted((TESTS_DIR / "e2e").glob("*.spec.ts")):
        source = path.read_text(encoding="utf-8")
        cases.extend(match.group("title") for match in pattern.finditer(source))

    assert cases, "Aucun cas Playwright n'a ete trouve"
    missing = sorted(case for case in cases if f"`{case}`" not in catalog)
    assert not missing, (
        "Cas Playwright absents de tests/TEST_CATALOG.md : "
        f"{', '.join(missing)}"
    )
