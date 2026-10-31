"""Author the PDF ground-truth fixtures + their expected-output oracle.

Generates three small PDFs that reproduce the real-world failure modes of the
current pypdf-only extractor (detect.extract_pdf_text), plus expected.json —
the oracle, written from the SAME constants used to draw the PDFs, so the
fixtures and their expected output cannot drift apart.

  tables.pdf       a real table        -> pypdf flattens it; pdfplumber recovers rows
  scanned.pdf      image-only page      -> pypdf returns ""; OCR (tesseract) recovers
  two_column.pdf   two-column layout    -> documented hard case (column ordering)

Regenerate (deps are dev/test extras):

    uv pip install reportlab pypdf pdfplumber pillow pytesseract
    uv run python tests/fixtures/pdf/_make_pdf_fixtures.py

Writes the three PDFs and expected.json into this directory.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).parent

# ── source-of-truth content (oracle is derived from these) ──────────────────

TABLE_TITLE = "Quarterly Report"
TABLE_INTRO = "Revenue by region for fiscal year 2025."
TABLE_ROWS = [
    ["Region", "Q1", "Q2", "Q3"],
    ["North", "100", "120", "140"],
    ["South", "90", "95", "99"],
    ["East", "70", "80", "85"],
]

# Distinct, OCR-friendly tokens so we can assert recovery without exact match.
SCANNED_LINES = [
    "Invoice 4815",
    "Customer Northwind Traders",
    "Amount Due 2380 USD",
    "Status PAID",
]

TWO_COL_LEFT = [
    "LEFTCOLUMN begins here.",
    "It discusses ingestion pipelines",
    "and how documents become nodes.",
]
TWO_COL_RIGHT = [
    "RIGHTCOLUMN begins here.",
    "It covers clustering and the",
    "community detection stage.",
]


def _make_tables_pdf(path: Path) -> None:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=letter)
    table = Table(TABLE_ROWS)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                ("FONTSIZE", (0, 0), (-1, -1), 12),
            ]
        )
    )
    doc.build(
        [
            Paragraph(TABLE_TITLE, styles["Title"]),
            Paragraph(TABLE_INTRO, styles["BodyText"]),
            Spacer(1, 18),
            table,
        ]
    )


def _make_two_column_pdf(path: Path) -> None:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (  # noqa: F401
        BaseDocTemplate,
        Frame,
        FrameBreak,
        NextPageTemplate,
        PageTemplate,
        Paragraph,
    )

    styles = getSampleStyleSheet()
    width, height = letter
    gap = 24
    col_w = (width - 2 * 72 - gap) / 2
    left = Frame(72, 72, col_w, height - 144, id="left")
    right = Frame(72 + col_w + gap, 72, col_w, height - 144, id="right")
    doc = BaseDocTemplate(str(path), pagesize=letter)
    doc.addPageTemplates([PageTemplate(id="twocol", frames=[left, right])])
    story = [Paragraph(line, styles["BodyText"]) for line in TWO_COL_LEFT]
    story.append(FrameBreak())
    story += [Paragraph(line, styles["BodyText"]) for line in TWO_COL_RIGHT]
    doc.build(story)


def _make_scanned_pdf(path: Path) -> None:
    """Render text to a raster image and embed it with NO text layer."""
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas

    img = Image.new("RGB", (1240, 1754), "white")  # ~150dpi letter
    draw = ImageDraw.Draw(img)
    font = None
    for candidate in (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        try:
            font = ImageFont.truetype(candidate, 48)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    y = 120
    for line in SCANNED_LINES:
        draw.text((120, y), line, fill="black", font=font)
        y += 110
    png = path.with_suffix(".source.png")
    img.save(png)
    c = canvas.Canvas(str(path), pagesize=letter)
    c.drawImage(str(png), 0, 0, width=letter[0], height=letter[1])
    c.save()
    png.unlink()  # keep only the PDF


def _write_oracle(path: Path) -> None:
    oracle = {
        "_comment": "Expected extraction output, derived from the same constants "
        "that draw the PDFs. See _make_pdf_fixtures.py.",
        "tables.pdf": {
            "text_contains": [TABLE_TITLE, TABLE_INTRO],
            "table_rows": TABLE_ROWS,
            "baseline_pypdf": "flattens the table into ungridded text; cells are "
            "present as tokens but row/column structure is lost.",
        },
        "scanned.pdf": {
            "text_layer_is_empty": True,
            "ocr_words_contains": ["Invoice", "4815", "Northwind", "PAID", "2380"],
            "baseline_pypdf": "returns empty string (no text layer).",
        },
        "two_column.pdf": {
            "text_contains": ["LEFTCOLUMN", "RIGHTCOLUMN"],
            "left_before_right": True,
            "note": "Column-correct ordering is a known hard case; documented, "
            "not yet guaranteed.",
        },
    }
    path.write_text(json.dumps(oracle, indent=2) + "\n")


def main() -> int:
    _make_tables_pdf(HERE / "tables.pdf")
    _make_two_column_pdf(HERE / "two_column.pdf")
    _make_scanned_pdf(HERE / "scanned.pdf")
    _write_oracle(HERE / "expected.json")
    print("wrote tables.pdf, two_column.pdf, scanned.pdf, expected.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
