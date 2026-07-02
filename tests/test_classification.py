import pytest

from classification import classify_document, supported_extension


@pytest.mark.parametrize(
    ("title", "url", "expected"),
    [
        ("Rapport financier annuel 2025", "https://example.test/a.pdf", "annual_financial_report"),
        ("Half-year financial report", "https://example.test/h1.pdf", "half_year_financial_report"),
        ("Document d'enregistrement universel", "https://example.test/deu.pdf", "universal_registration_document"),
        ("Document d'enregistrement universel 2025 - Annual financial report", "https://example.test/deu.pdf", "universal_registration_document"),
        ("Package réglementaire", "https://example.test/report.zip", "esef"),
        ("Présentation investisseurs", "https://example.test/slides.pdf", None),
        ("Press Release: Availability of the aide-mémoire for Q2 2026 results", "https://example.test/q2.pdf", None),
        ("REPORT DE LA PUBLICATION DU RAPPORT FINANCIER ANNUEL 2025", "https://example.test/postponement.pdf", None),
        ("Informe financiero anual (ACCIONA, S.A.)", "https://example.test/a.pdf", "annual_financial_report"),
        ("Informe semestral 2025", "https://example.test/h.pdf", "half_year_financial_report"),
        ("NPRO: 2Q 2026 - Strong letting quarter", "https://example.test/q2.pdf", "quarterly_financial_report"),
    ],
)
def test_classification_rules(title: str, url: str, expected: str | None) -> None:
    assert classify_document(title, url) == expected


def test_supported_extensions_are_strict() -> None:
    assert supported_extension("https://example.test/a.pdf") == "pdf"
    assert supported_extension("https://example.test/a", "application/xhtml+xml") == "xhtml"
    assert supported_extension("https://example.test/a.xbri") == "zip"
    assert supported_extension("https://example.test/a.docx") is None
