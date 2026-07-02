"""PDF extraction: ground-truth fixtures + the table/OCR improvements.

Fixtures (tests/fixtures/pdf/, authored by _make_pdf_fixtures.py) reproduce the
real-world failure modes of the old pypdf-only extractor:

  tables.pdf      a real table        -> pypdf flattens it; pdfplumber recovers rows
  scanned.pdf     image-only page      -> pypdf returns ""; OCR recovers text
  two_column.pdf  two-column layout    -> regression guard (ordering)

The baseline tests document what the OLD path loses; the improvement tests
assert the new extract_pdf_text recovers it. Optional-dependency tests skip
cleanly when a backend (pdfplumber / pytesseract+tesseract) is absent.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from graphify.detect import _pdf_text_pypdf, _render_table_markdown, extract_pdf_text

FIXTURES = Path(__file__).parent / "fixtures" / "pdf"


def _load_oracle() -> dict:
    return json.loads((FIXTURES / "expected.json").read_text())


def _has(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None


def _has_tesseract() -> bool:
    if not _has("pytesseract"):
        return False
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


HAS_PDFPLUMBER = _has("pdfplumber")
HAS_PYPDF = _has("pypdf")
HAS_OCR = _has_tesseract() and HAS_PDFPLUMBER

needs_pdfplumber = pytest.mark.skipif(not HAS_PDFPLUMBER, reason="pdfplumber not installed ('pdf' extra)")
needs_pypdf = pytest.mark.skipif(not HAS_PYPDF, reason="pypdf not installed ('pdf' extra)")
needs_ocr = pytest.mark.skipif(not HAS_OCR, reason="OCR toolchain (pytesseract+tesseract+pdfplumber) unavailable")


# ── fixtures present ─────────────────────────────────────────────────────────

def test_fixtures_present() -> None:
    for fname in ("tables.pdf", "scanned.pdf", "two_column.pdf", "expected.json"):
        assert (FIXTURES / fname).exists(), f"missing fixture {fname}"


# ── markdown renderer unit ───────────────────────────────────────────────────

def test_render_table_markdown_handles_none_and_ragged() -> None:
    md = _render_table_markdown([["A", "B"], ["1", None], ["x"]])
    lines = md.splitlines()
    assert lines[0] == "| A | B |"
    assert lines[1] == "| --- | --- |"
    assert lines[2] == "| 1 |  |"  # None -> empty cell
    assert lines[3] == "| x |  |"  # ragged row padded


# ── baseline: what the OLD pypdf-only path loses ─────────────────────────────

class TestBaselineWeakness:
    @needs_pypdf
    def test_pypdf_destroys_table_structure(self) -> None:
        """pypdf emits each cell on its own line — no row/column grouping."""
        raw = _pdf_text_pypdf(FIXTURES / "tables.pdf")
        assert "North" in raw and "100" in raw  # tokens survive
        assert not re.search(r"North\s*\|\s*100\s*\|\s*120\s*\|\s*140", raw), (
            "baseline unexpectedly preserved table rows"
        )

    @needs_pypdf
    def test_pypdf_returns_empty_on_scanned(self) -> None:
        """Image-only PDF has no text layer -> pypdf yields nothing."""
        raw = _pdf_text_pypdf(FIXTURES / "scanned.pdf")
        assert len(raw.strip()) < 8


# ── improvement 1: tables recovered as structured Markdown ───────────────────

class TestTableRecovery:
    @needs_pdfplumber
    def test_table_rows_recovered_as_markdown(self) -> None:
        text = extract_pdf_text(FIXTURES / "tables.pdf")
        oracle = _load_oracle()["tables.pdf"]
        for snippet in oracle["text_contains"]:
            assert snippet in text
        # Every data row appears as a contiguous Markdown row.
        for row in oracle["table_rows"]:
            pattern = r"\s*\|\s*".join(re.escape(c) for c in row)
            assert re.search(pattern, text), f"row not recovered: {row}"


# ── improvement 2: scanned PDF recovered via OCR ─────────────────────────────

class TestOcrFallback:
    @needs_ocr
    def test_scanned_pdf_recovered_via_ocr(self) -> None:
        text = extract_pdf_text(FIXTURES / "scanned.pdf")
        words = _load_oracle()["scanned.pdf"]["ocr_words_contains"]
        hits = [w for w in words if w in text]
        assert len(hits) >= 4, f"OCR recovered only {hits} of {words}"


# ── improvement 3: failures are surfaced, not swallowed ──────────────────────

class TestNonSilentFailures:
    def test_corrupt_pdf_warns_instead_of_silent_empty(self, tmp_path: Path) -> None:
        bad = tmp_path / "broken.pdf"
        bad.write_bytes(b"%PDF-1.4\nthis is not a valid pdf body\n")
        with pytest.warns(UserWarning):
            result = extract_pdf_text(bad)
        assert result == ""  # still returns a string, but loudly


# ── regression guard: two-column ordering (not a claimed fix) ────────────────

class TestTwoColumnOrdering:
    @needs_pdfplumber
    def test_left_column_before_right(self) -> None:
        text = extract_pdf_text(FIXTURES / "two_column.pdf")
        assert "LEFTCOLUMN" in text and "RIGHTCOLUMN" in text
        assert text.index("LEFTCOLUMN") < text.index("RIGHTCOLUMN")
