# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the preprocessing Lambda processors module."""

import io
import logging
import pathlib
import sys
import time
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

for _mod_name in (
    "unstructured",
    "unstructured.partition",
):
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = ModuleType(_mod_name)

# These need a callable attribute so @patch can target it
for _mod_name, _attr in [
    ("unstructured.partition.docx", "partition_docx"),
    ("unstructured.partition.pdf", "partition_pdf"),
]:
    if _mod_name not in sys.modules:
        mod = ModuleType(_mod_name)
        setattr(mod, _attr, None)
        sys.modules[_mod_name] = mod

from coa_sources.documents.preprocessing.processors import (
    _PROCESSORS,
    _SCANNED_PDF_THRESHOLD,
    _blocks_to_markdown,
    elements_to_markdown,
    get_page_count,
    process_docx,
    process_md,
    process_pdf,
    process_pdf_textract,
    process_pdf_textract_tables,
    process_txt,
)


@pytest.mark.unit
class TestProcessTxt:
    def test_decodes_utf8(self):
        text, ext = process_txt(b"hello world", "file.txt")
        assert text == "hello world"
        assert ext == ".txt"

    def test_preserves_newlines(self):
        text, _ = process_txt(b"line1\nline2\nline3", "file.txt")
        assert text == "line1\nline2\nline3"

    def test_replaces_invalid_bytes(self):
        text, ext = process_txt(b"hello \xff world", "file.txt")
        assert "hello" in text
        assert "world" in text
        assert ext == ".txt"

    def test_empty_file(self):
        text, ext = process_txt(b"", "empty.txt")
        assert text == ""
        assert ext == ".txt"


@pytest.mark.unit
class TestProcessMd:
    def test_decodes_utf8(self):
        text, ext = process_md(b"# Heading\n\nParagraph", "doc.md")
        assert text == "# Heading\n\nParagraph"
        assert ext == ".md"

    def test_replaces_invalid_bytes(self):
        text, _ = process_md(b"# Title\xff", "doc.md")
        assert "Title" in text

    def test_empty_file(self):
        text, ext = process_md(b"", "empty.md")
        assert text == ""
        assert ext == ".md"


@pytest.mark.unit
class TestProcessDocx:
    @patch("unstructured.partition.docx.partition_docx")
    def test_calls_partition_and_returns_markdown(self, mock_partition):
        el = MagicMock()
        el.category = "NarrativeText"
        el.__str__ = MagicMock(return_value="Some paragraph text")
        mock_partition.return_value = [el]

        text, ext = process_docx(b"fake-docx-bytes", "doc.docx")
        assert ext == ".md"
        assert "Some paragraph text" in text
        mock_partition.assert_called_once()

    @patch("unstructured.partition.docx.partition_docx")
    def test_empty_docx(self, mock_partition):
        mock_partition.return_value = []
        text, ext = process_docx(b"fake", "empty.docx")
        assert text == ""
        assert ext == ".md"


def _make_pdf(pages_text: list[str]) -> bytes:
    """Build a minimal valid PDF (US-Letter, Helvetica 12pt, one text line per page).

    Real bytes, parsed by the real PDF library — no mocks. pdfium extracts a
    single-line ``Tj`` string verbatim, so char-count-sensitive tests (the
    scanned-PDF threshold) are exact.
    """
    objects: list[bytes] = []
    n_pages = len(pages_text)
    page_obj_nums = [4 + 2 * i for i in range(n_pages)]
    kids = " ".join(f"{n} 0 R" for n in page_obj_nums)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")  # obj 1
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())  # obj 2
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")  # obj 3
    for i, text in enumerate(pages_text):
        content_num = page_obj_nums[i] + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_num} 0 R >>"
            ).encode()
        )
        esc = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({esc}) Tj ET".encode()
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for num, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{num} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode())
    return out.getvalue()


@pytest.mark.unit
class TestIsScannedPdf:
    def test_text_heavy_pdf_not_scanned(self):
        from coa_sources.documents.preprocessing.processors import is_scanned_pdf

        assert is_scanned_pdf(_make_pdf(["a" * 500, "b" * 600])) is False

    def test_image_only_pdf_is_scanned(self):
        from coa_sources.documents.preprocessing.processors import is_scanned_pdf

        assert is_scanned_pdf(_make_pdf(["", ""])) is True

    def test_below_threshold_is_scanned(self):
        from coa_sources.documents.preprocessing.processors import is_scanned_pdf

        assert is_scanned_pdf(_make_pdf(["x" * (_SCANNED_PDF_THRESHOLD - 1)])) is True

    def test_at_threshold_not_scanned(self):
        from coa_sources.documents.preprocessing.processors import is_scanned_pdf

        assert is_scanned_pdf(_make_pdf(["x" * _SCANNED_PDF_THRESHOLD])) is False

    def test_corrupt_pdf_treated_as_scanned(self):
        """Unopenable bytes must not raise — report scanned and log the reason."""
        from coa_sources.documents.preprocessing.processors import is_scanned_pdf

        assert is_scanned_pdf(b"%PDF-1.4\nnot actually a pdf") is True

    def test_truncated_pdf_treated_as_scanned(self):
        """The repo's truncated.pdf edge-case fixture must not raise."""
        from coa_sources.documents.preprocessing.processors import is_scanned_pdf

        fixture = (
            pathlib.Path(__file__).parents[6] / "tests/cdk/scripts/preprocessing-fixtures/edge-cases/truncated.pdf"
        )
        if not fixture.is_file():  # fixture lives at repo root; skip if packaged alone
            pytest.skip(f"fixture not present: {fixture}")
        assert is_scanned_pdf(fixture.read_bytes()) is True

    def test_empty_bytes_treated_as_scanned(self):
        from coa_sources.documents.preprocessing.processors import is_scanned_pdf

        assert is_scanned_pdf(b"") is True

    def test_corrupt_pdf_logs_reason(self, caplog):
        from coa_sources.documents.preprocessing.processors import is_scanned_pdf

        with caplog.at_level(logging.WARNING):
            is_scanned_pdf(b"garbage")
        assert any("treating as scanned" in r.getMessage() for r in caplog.records)


@pytest.mark.unit
class TestProcessPdfTextract:
    def test_single_page_calls_textract_with_real_png(self):
        textract = MagicMock()
        textract.detect_document_text.return_value = {
            "Blocks": [
                {"BlockType": "LINE", "Text": "Hello from Textract"},
                {"BlockType": "WORD", "Text": "ignored"},
            ]
        }

        result = process_pdf_textract(_make_pdf(["hello"]), "scan.pdf", textract)
        assert result == "Hello from Textract"
        textract.detect_document_text.assert_called_once()

        # The bytes handed to Textract are a real PNG at 300 DPI:
        # US-Letter 612x792 pt -> 2550x3300 px (pdfium may round up by 1px).
        png_bytes = textract.detect_document_text.call_args.kwargs["Document"]["Bytes"]
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
        from PIL import Image

        width, height = Image.open(io.BytesIO(png_bytes)).size
        assert abs(width - 2550) <= 1 and abs(height - 3300) <= 1

    def test_multi_page_joins_with_double_newline(self):
        pdf_bytes = _make_pdf(["alpha", "beta", "gamma"])

        # Pre-render each page with the same library/scale to build a
        # bytes -> page-index map (rendering is deterministic), so the Textract
        # stub can identify pages by content, not call order.
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_bytes)
        expected_pngs: dict[bytes, int] = {}
        try:
            for i in range(len(doc)):
                buf = io.BytesIO()
                doc[i].render(scale=300 / 72).to_pil().save(buf, format="PNG")
                expected_pngs[buf.getvalue()] = i
        finally:
            doc.close()
        assert len(expected_pngs) == 3, "pages must render to distinct PNGs"

        # Pages are OCR'd concurrently, so the stub keys off page bytes (not call
        # order) and sleeps in reverse page order to catch completion-order reassembly.
        def _detect(Document):  # noqa: N803 - matches the boto3 kwarg name
            idx = expected_pngs[Document["Bytes"]]
            time.sleep(0.05 * (3 - idx))
            return {"Blocks": [{"BlockType": "LINE", "Text": f"Page {idx + 1}"}]}

        textract = MagicMock()
        textract.detect_document_text.side_effect = _detect

        result = process_pdf_textract(pdf_bytes, "multi.pdf", textract)
        assert result == "Page 1\n\nPage 2\n\nPage 3"
        assert textract.detect_document_text.call_count == 3

    def test_empty_textract_response(self):
        textract = MagicMock()
        textract.detect_document_text.return_value = {"Blocks": []}

        result = process_pdf_textract(_make_pdf(["scan"]), "empty.pdf", textract)
        assert result == ""

    def test_corrupt_pdf_returns_empty_without_calling_textract(self):
        """An unopenable PDF must not raise a PDFium error nor bill Textract."""
        textract = MagicMock()
        assert process_pdf_textract(b"%PDF-1.4 garbage", "bad.pdf", textract) == ""
        textract.detect_document_text.assert_not_called()

    def test_page_count_limit_enforced_before_rendering(self):
        """Over-long PDFs are rejected with a clear message, before any render."""
        from coa_sources.documents.preprocessing import processors

        textract = MagicMock()
        with (
            patch.object(processors, "_MAX_PDF_PAGES", 2),
            pytest.raises(ValueError, match="exceeding the 2-page limit"),
        ):
            processors.process_pdf_textract(_make_pdf(["a", "b", "c"]), "long.pdf", textract)
        textract.detect_document_text.assert_not_called()

    def test_oversized_page_rejected_before_rendering(self):
        """A page too large to rasterize safely is rejected, not rendered."""
        from coa_sources.documents.preprocessing import processors

        textract = MagicMock()
        # US-Letter at 300 DPI is ~8.4 MP; a 1 MP cap rejects it without allocating.
        with (
            patch.object(processors, "_MAX_RENDER_MEGAPIXELS", 1),
            pytest.raises(ValueError, match="exceeding the 1 MP per-page limit"),
        ):
            processors.process_pdf_textract(_make_pdf(["big"]), "big.pdf", textract)
        textract.detect_document_text.assert_not_called()

    def test_within_page_limit_still_processes(self):
        """The cap must not reject documents at or under the limit."""
        from coa_sources.documents.preprocessing import processors

        textract = MagicMock()
        textract.detect_document_text.return_value = {"Blocks": [{"BlockType": "LINE", "Text": "ok"}]}
        with patch.object(processors, "_MAX_PDF_PAGES", 2):
            result = processors.process_pdf_textract(_make_pdf(["a", "b"]), "two.pdf", textract)
        assert result == "ok\n\nok"


@pytest.mark.unit
class TestProcessPdfTextractTables:
    """Cover process_pdf_textract_tables end-to-end with real rendered PNGs."""

    @staticmethod
    def _table_blocks(header: str, cell: str) -> list[dict]:
        """A minimal AnalyzeDocument(TABLES) block graph: one 1x1+header table."""
        return [
            {"BlockType": "TABLE", "Id": "t1", "Relationships": [{"Type": "CHILD", "Ids": ["c1", "c2"]}]},
            {
                "BlockType": "CELL",
                "Id": "c1",
                "RowIndex": 1,
                "ColumnIndex": 1,
                "EntityTypes": ["COLUMN_HEADER"],
                "Relationships": [{"Type": "CHILD", "Ids": ["w1"]}],
            },
            {
                "BlockType": "CELL",
                "Id": "c2",
                "RowIndex": 2,
                "ColumnIndex": 1,
                "Relationships": [{"Type": "CHILD", "Ids": ["w2"]}],
            },
            {"BlockType": "WORD", "Id": "w1", "Text": header},
            {"BlockType": "WORD", "Id": "w2", "Text": cell},
        ]

    def test_single_page_calls_analyze_document_with_real_png(self):
        textract = MagicMock()
        textract.analyze_document.return_value = {"Blocks": self._table_blocks("Policy", "P-100")}

        result = process_pdf_textract_tables(_make_pdf(["table page"]), "policy.pdf", textract)

        textract.analyze_document.assert_called_once()
        assert textract.analyze_document.call_args.kwargs["FeatureTypes"] == ["TABLES"]
        assert "Policy" in result and "P-100" in result
        assert "|" in result

        png_bytes = textract.analyze_document.call_args.kwargs["Document"]["Bytes"]
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"

    def test_multi_page_joins_with_double_newline(self):
        pdf_bytes = _make_pdf(["alpha", "beta", "gamma"])

        # Key the stub off page bytes, not call order (pages OCR concurrently) —
        # same rationale as TestProcessPdfTextract.test_multi_page.
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_bytes)
        page_index: dict[bytes, int] = {}
        try:
            for i in range(len(doc)):
                buf = io.BytesIO()
                doc[i].render(scale=300 / 72).to_pil().save(buf, format="PNG")
                page_index[buf.getvalue()] = i
        finally:
            doc.close()
        assert len(page_index) == 3, "pages must render to distinct PNGs"

        def _analyze(Document, FeatureTypes):  # noqa: N803 - boto3 kwarg names
            idx = page_index[Document["Bytes"]]
            time.sleep(0.05 * (3 - idx))  # finish in reverse to catch order bugs
            return {"Blocks": [{"BlockType": "LINE", "Id": f"l{idx}", "Text": f"Page {idx + 1}"}]}

        textract = MagicMock()
        textract.analyze_document.side_effect = _analyze

        result = process_pdf_textract_tables(pdf_bytes, "multi.pdf", textract)
        assert result == "Page 1\n\nPage 2\n\nPage 3"
        assert textract.analyze_document.call_count == 3

    def test_empty_analyze_response_yields_empty(self):
        textract = MagicMock()
        textract.analyze_document.return_value = {"Blocks": []}
        assert process_pdf_textract_tables(_make_pdf(["x"]), "empty.pdf", textract) == ""

    def test_textract_error_on_page_yields_empty_not_crash(self):
        """A Textract API error on a page is isolated to that page: it returns empty
        for it and never aborts the whole document."""
        textract = MagicMock()
        textract.analyze_document.side_effect = Exception("ThrottlingException")
        # single page -> whole result is empty, but no exception escapes
        assert process_pdf_textract_tables(_make_pdf(["x"]), "throttled.pdf", textract) == ""
        textract.analyze_document.assert_called_once()

    def test_textract_partial_failure_keeps_good_pages(self):
        """When one page fails but others succeed, the good pages still return —
        a single transient per-page fault must not zero out the document."""
        pdf_bytes = _make_pdf(["alpha", "beta", "gamma"])
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(pdf_bytes)
        page_index: dict[bytes, int] = {}
        try:
            for i in range(len(doc)):
                buf = io.BytesIO()
                doc[i].render(scale=300 / 72).to_pil().save(buf, format="PNG")
                page_index[buf.getvalue()] = i
        finally:
            doc.close()
        assert len(page_index) == 3

        def _analyze(Document, FeatureTypes):  # noqa: N803 - boto3 kwarg names
            idx = page_index[Document["Bytes"]]
            if idx == 1:  # middle page fails
                raise Exception("InvalidParameterException")
            return {"Blocks": [{"BlockType": "LINE", "Id": f"l{idx}", "Text": f"Page {idx + 1}"}]}

        textract = MagicMock()
        textract.analyze_document.side_effect = _analyze

        result = process_pdf_textract_tables(pdf_bytes, "partial.pdf", textract)
        # page 2 is empty; pages 1 and 3 survive, positions preserved.
        assert result == "Page 1\n\n\n\nPage 3"
        assert textract.analyze_document.call_count == 3

    def test_corrupt_pdf_returns_empty_without_calling_textract(self):
        textract = MagicMock()
        assert process_pdf_textract_tables(b"%PDF-1.4 garbage", "bad.pdf", textract) == ""
        textract.analyze_document.assert_not_called()

    def test_page_count_limit_enforced_before_rendering(self):
        from coa_sources.documents.preprocessing import processors

        textract = MagicMock()
        with (
            patch.object(processors, "_MAX_PDF_PAGES", 2),
            pytest.raises(ValueError, match="exceeding the 2-page limit"),
        ):
            processors.process_pdf_textract_tables(_make_pdf(["a", "b", "c"]), "long.pdf", textract)
        textract.analyze_document.assert_not_called()

    def test_oversized_page_rejected_before_rendering(self):
        from coa_sources.documents.preprocessing import processors

        textract = MagicMock()
        with (
            patch.object(processors, "_MAX_RENDER_MEGAPIXELS", 1),
            pytest.raises(ValueError, match="exceeding the 1 MP per-page limit"),
        ):
            processors.process_pdf_textract_tables(_make_pdf(["big"]), "big.pdf", textract)
        textract.analyze_document.assert_not_called()

    def test_page_render_failure_returns_empty_without_calling_textract(self):
        """If a page fails to rasterize mid-loop, bail with "" — don't bill
        Textract for a partial document."""
        import pypdfium2 as pdfium
        from coa_sources.documents.preprocessing import processors

        textract = MagicMock()
        pdf_bytes = _make_pdf(["x"])

        real_page = pdfium.PdfDocument(pdf_bytes)[0]

        def _boom(*a, **k):
            raise pdfium.PdfiumError("render exploded")

        with patch.object(type(real_page), "render", _boom):
            result = processors.process_pdf_textract_tables(pdf_bytes, "boom.pdf", textract)
        assert result == ""
        textract.analyze_document.assert_not_called()


@pytest.mark.unit
class TestProcessPdf:
    @patch("unstructured.partition.pdf.partition_pdf")
    @patch("coa_sources.documents.preprocessing.processors.is_scanned_pdf", return_value=False)
    def test_text_native_uses_unstructured(self, mock_scanned, mock_partition):
        el = MagicMock()
        el.category = "NarrativeText"
        el.__str__ = MagicMock(return_value="PDF text content")
        mock_partition.return_value = [el]

        text, ext = process_pdf(b"pdf-bytes", "doc.pdf", MagicMock())
        assert ext == ".md"
        assert "PDF text content" in text
        mock_partition.assert_called_once()

    @patch(
        "coa_sources.documents.preprocessing.processors.process_pdf_textract",
        return_value="Textract output",
    )
    @patch("coa_sources.documents.preprocessing.processors.is_scanned_pdf", return_value=True)
    def test_scanned_uses_textract(self, mock_scanned, mock_textract):
        textract_client = MagicMock()
        text, ext = process_pdf(b"pdf-bytes", "scan.pdf", textract_client)
        assert ext == ".txt"
        assert text == "Textract output"
        mock_textract.assert_called_once_with(b"pdf-bytes", "scan.pdf", textract_client)

    @patch("unstructured.partition.pdf.partition_pdf")
    @patch("coa_sources.documents.preprocessing.processors.is_scanned_pdf", return_value=False)
    def test_empty_result_logs_at_warning(self, mock_scanned, mock_partition, caplog):
        """An extraction that found nothing must not log like one that worked.

        `unstructured` abandons drawing-heavy pages and returns no elements without
        raising. At INFO, char_count=0 and char_count=3000 were indistinguishable to
        log-based alerting.
        """
        mock_partition.return_value = []

        with caplog.at_level(logging.INFO):
            text, ext = process_pdf(b"pdf-bytes", "drawing.pdf", MagicMock())

        assert text == ""
        records = [r for r in caplog.records if r.getMessage() == "PDF processed"]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert records[0].char_count == 0

    @patch("unstructured.partition.pdf.partition_pdf")
    @patch("coa_sources.documents.preprocessing.processors.is_scanned_pdf", return_value=False)
    def test_non_empty_result_stays_at_info(self, mock_scanned, mock_partition, caplog):
        el = MagicMock()
        el.category = "NarrativeText"
        el.__str__ = MagicMock(return_value="Real content")
        mock_partition.return_value = [el]

        with caplog.at_level(logging.INFO):
            process_pdf(b"pdf-bytes", "doc.pdf", MagicMock())

        records = [r for r in caplog.records if r.getMessage() == "PDF processed"]
        assert len(records) == 1
        assert records[0].levelno == logging.INFO

    @patch(
        "coa_sources.documents.preprocessing.processors.process_pdf_textract_tables",
        return_value="| Header |\n| --- |\n| Cell |",
    )
    @patch("coa_sources.documents.preprocessing.processors.is_scanned_pdf")
    def test_enable_table_extraction_bypasses_scanned_check(self, mock_scanned, mock_tables):
        """With enable_table_extraction=True, ALL PDFs (text-native or
        scanned) route through Textract AnalyzeDocument(TABLES). is_scanned_pdf
        is not even called — the flag is a routing override."""
        textract_client = MagicMock()
        text, ext = process_pdf(b"pdf-bytes", "policy.pdf", textract_client, enable_table_extraction=True)
        assert ext == ".md"
        assert "| Header |" in text
        mock_tables.assert_called_once_with(b"pdf-bytes", "policy.pdf", textract_client)
        mock_scanned.assert_not_called()

    @patch("unstructured.partition.pdf.partition_pdf")
    @patch(
        "coa_sources.documents.preprocessing.processors.process_pdf_textract_tables",
    )
    @patch("coa_sources.documents.preprocessing.processors.is_scanned_pdf", return_value=False)
    def test_enable_table_extraction_false_preserves_legacy_routing(self, mock_scanned, mock_tables, mock_partition):
        """Regression guard: with the flag off, the existing scanned/text-native
        routing is unchanged — prose-dominant corpora don't silently pay the
        AnalyzeDocument premium."""
        el = MagicMock()
        el.category = "NarrativeText"
        el.__str__ = MagicMock(return_value="Prose only")
        mock_partition.return_value = [el]

        process_pdf(b"pdf-bytes", "prose.pdf", MagicMock(), enable_table_extraction=False)
        mock_tables.assert_not_called()
        mock_partition.assert_called_once()


@pytest.mark.unit
class TestElementsToMarkdown:
    def _make_element(self, category: str, text: str) -> MagicMock:
        el = MagicMock()
        el.category = category
        el.__str__ = MagicMock(return_value=text)
        return el

    def test_title_becomes_h2(self):
        result = elements_to_markdown([self._make_element("Title", "My Title")])
        assert result == "## My Title"

    def test_header_becomes_h1(self):
        result = elements_to_markdown([self._make_element("Header", "My Header")])
        assert result == "# My Header"

    def test_list_item_becomes_bullet(self):
        result = elements_to_markdown([self._make_element("ListItem", "First item")])
        assert result == "- First item"

    def test_table_becomes_code_block(self):
        result = elements_to_markdown([self._make_element("Table", "col1 | col2")])
        assert result == "```\ncol1 | col2\n```"

    def test_narrative_text_plain(self):
        result = elements_to_markdown([self._make_element("NarrativeText", "Paragraph text")])
        assert result == "Paragraph text"

    def test_empty_text_skipped(self):
        result = elements_to_markdown([self._make_element("NarrativeText", "   ")])
        assert result == ""

    def test_mixed_elements(self):
        els = [
            self._make_element("Header", "Doc Title"),
            self._make_element("NarrativeText", "Introduction paragraph"),
            self._make_element("Title", "Section 1"),
            self._make_element("ListItem", "Item A"),
            self._make_element("ListItem", "Item B"),
        ]
        result = elements_to_markdown(els)
        lines = result.split("\n\n")
        assert lines[0] == "# Doc Title"
        assert lines[1] == "Introduction paragraph"
        assert lines[2] == "## Section 1"
        assert lines[3] == "- Item A"
        assert lines[4] == "- Item B"

    def test_empty_list(self):
        assert elements_to_markdown([]) == ""

    def test_no_category_attribute(self):
        """Element without a .category attribute falls through to plain text."""

        class BareElement:
            """An element with no .category attribute."""

            def __str__(self):
                return "plain text"

        el = BareElement()
        result = elements_to_markdown([el])
        assert result == "plain text"


@pytest.mark.unit
class TestGetPageCount:
    def test_pdf_returns_count(self):
        assert get_page_count(_make_pdf(["p1", "p2", "p3", "p4", "p5"]), ".pdf") == 5

    def test_malformed_pdf_returns_zero(self):
        """A malformed PDF reports 0 pages rather than raising a PDFium error."""
        assert get_page_count(b"%PDF-1.4 not a pdf", ".pdf") == 0

    def test_empty_bytes_returns_zero(self):
        assert get_page_count(b"", ".pdf") == 0

    def test_txt_returns_zero(self):
        assert get_page_count(b"text", ".txt") == 0

    def test_md_returns_zero(self):
        assert get_page_count(b"# heading", ".md") == 0

    def test_docx_returns_zero(self):
        assert get_page_count(b"docx-bytes", ".docx") == 0


@pytest.mark.unit
class TestProcessorDispatchTable:
    def test_has_txt(self):
        assert ".txt" in _PROCESSORS
        assert _PROCESSORS[".txt"] is process_txt

    def test_has_md(self):
        assert ".md" in _PROCESSORS
        assert _PROCESSORS[".md"] is process_md

    def test_has_docx(self):
        assert ".docx" in _PROCESSORS
        assert _PROCESSORS[".docx"] is process_docx

    def test_no_unexpected_keys(self):
        assert set(_PROCESSORS.keys()) == {".txt", ".md", ".docx"}


def _word(wid: str, text: str) -> dict:
    return {"Id": wid, "BlockType": "WORD", "Text": text}


def _line(lid: str, text: str, word_ids: list[str]) -> dict:
    return {
        "Id": lid,
        "BlockType": "LINE",
        "Text": text,
        "Relationships": [{"Type": "CHILD", "Ids": word_ids}],
    }


def _cell(cid: str, row: int, col: int, word_ids: list[str], is_header: bool = False) -> dict:
    b: dict = {
        "Id": cid,
        "BlockType": "CELL",
        "RowIndex": row,
        "ColumnIndex": col,
        "Relationships": [{"Type": "CHILD", "Ids": word_ids}],
    }
    if is_header:
        b["EntityTypes"] = ["COLUMN_HEADER"]
    return b


def _table(tid: str, cell_ids: list[str]) -> dict:
    return {
        "Id": tid,
        "BlockType": "TABLE",
        "Relationships": [{"Type": "CHILD", "Ids": cell_ids}],
    }


@pytest.mark.unit
class TestBlocksToMarkdown:
    def test_table_with_marked_headers_emits_pipe_table(self):
        """AnalyzeDocument marks column headers as EntityTypes=[COLUMN_HEADER];
        emit them as the Markdown header row."""
        blocks = [
            _word("w1", "Policy"),
            _word("w2", "Premium"),
            _word("w3", "P-001"),
            _word("w4", "1200"),
            _word("w5", "P-002"),
            _word("w6", "1500"),
            _cell("c1", 1, 1, ["w1"], is_header=True),
            _cell("c2", 1, 2, ["w2"], is_header=True),
            _cell("c3", 2, 1, ["w3"]),
            _cell("c4", 2, 2, ["w4"]),
            _cell("c5", 3, 1, ["w5"]),
            _cell("c6", 3, 2, ["w6"]),
            _table("t1", ["c1", "c2", "c3", "c4", "c5", "c6"]),
        ]
        md = _blocks_to_markdown(blocks)
        assert "| Policy | Premium |" in md
        assert "| --- | --- |" in md
        assert "| P-001 | 1200 |" in md
        assert "| P-002 | 1500 |" in md

    def test_table_without_header_marker_treats_row_zero_as_header(self):
        """If Textract doesn't tag any COLUMN_HEADER, use row 1 — a pipe-table
        without a header separator line is not valid Markdown."""
        blocks = [
            _word("w1", "A"),
            _word("w2", "B"),
            _word("w3", "1"),
            _word("w4", "2"),
            _cell("c1", 1, 1, ["w1"]),
            _cell("c2", 1, 2, ["w2"]),
            _cell("c3", 2, 1, ["w3"]),
            _cell("c4", 2, 2, ["w4"]),
            _table("t1", ["c1", "c2", "c3", "c4"]),
        ]
        md = _blocks_to_markdown(blocks)
        assert md.split("\n")[0] == "| A | B |"
        assert md.split("\n")[1] == "| --- | --- |"
        assert md.split("\n")[2] == "| 1 | 2 |"

    def test_prose_and_table_both_emitted(self):
        """Prose LINEs and table content coexist on a real page — both should
        survive, and neither should double-emit table words as prose."""
        blocks = [
            _word("wt1", "Header"),
            _word("wt2", "Value"),
            _word("wt3", "Row1"),
            _word("wt4", "10"),
            _word("wp1", "Intro"),
            _word("wp2", "paragraph"),
            _line("L1", "Intro paragraph", ["wp1", "wp2"]),
            _line("L2", "Header Value", ["wt1", "wt2"]),  # inside-table line
            _line("L3", "Row1 10", ["wt3", "wt4"]),  # inside-table line
            _cell("c1", 1, 1, ["wt1"], is_header=True),
            _cell("c2", 1, 2, ["wt2"], is_header=True),
            _cell("c3", 2, 1, ["wt3"]),
            _cell("c4", 2, 2, ["wt4"]),
            _table("t1", ["c1", "c2", "c3", "c4"]),
        ]
        md = _blocks_to_markdown(blocks)
        assert "Intro paragraph" in md
        # Inside-table LINEs are not emitted as prose (would double-count).
        assert "Header Value" not in md
        assert "Row1 10" not in md
        assert "| Header | Value |" in md
        assert "| Row1 | 10 |" in md

    def test_cell_text_with_pipe_escaped(self):
        """A raw pipe in a cell must be escaped so it doesn't break the
        Markdown grid alignment."""
        blocks = [
            _word("w1", "a|b"),
            _cell("c1", 1, 1, ["w1"], is_header=True),
            _table("t1", ["c1"]),
        ]
        md = _blocks_to_markdown(blocks)
        assert "a\\|b" in md

    def test_prose_only_no_tables(self):
        """AnalyzeDocument called on a table-less page — same as
        DetectDocumentText for the LINEs, no table section."""
        blocks = [
            _word("w1", "Just"),
            _word("w2", "prose"),
            _line("L1", "Just prose", ["w1", "w2"]),
        ]
        md = _blocks_to_markdown(blocks)
        assert md == "Just prose"

    def test_block_without_id_is_skipped_not_crashed(self):
        """A block missing "Id" is skipped by the by_id map; a well-formed LINE still emits."""
        blocks = [
            {"BlockType": "PAGE"},  # no "Id"
            _line("L1", "survives", ["w1"]),
            _word("w1", "survives"),
        ]
        md = _blocks_to_markdown(blocks)
        assert md == "survives"
