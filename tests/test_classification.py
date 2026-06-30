import pytest

from classification import classify_document, supported_extension


@pytest.mark.parametrize(
    ("title", "url", "expected"),
    [
        ("Rapport financier annuel 2025", "https://example.test/a.pdf", "annual_financial_report"),
        ("Half-year financial report", "https://example.test/h1.pdf", "half_year_financial_report"),
        ("Document d'enregistrement universel", "https://example.test/deu.pdf", "universal_registration_document"),
        ("Package réglementaire", "https://example.test/report.zip", "esef"),
        ("Présentation investisseurs", "https://example.test/slides.pdf", None),
    ],
)
def test_classification_rules(title: str, url: str, expected: str | None) -> None:
    assert classify_document(title, url) == expected


def test_supported_extensions_are_strict() -> None:
    assert supported_extension("https://example.test/a.pdf") == "pdf"
    assert supported_extension("https://example.test/a", "application/xhtml+xml") == "xhtml"
    assert supported_extension("https://example.test/a.xbri") == "zip"
    assert supported_extension("https://example.test/a.docx") is None
