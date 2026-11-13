# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""File-type-specific document processors for the preprocessing Lambda.

Each processor converts a file's raw bytes into clean text suitable for
the GraphRAG Toolkit.  Processors use lazy imports so that heavy
dependencies (``unstructured``, ``pypdfium2``) are only loaded when the
corresponding code path is hit, keeping cold-start time low for
pass-through formats.
"""

from __future__ import annotations

import io
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

# Chars-per-page threshold below which a PDF is considered scanned (image-based).
# PDFs with fewer than this many characters per page on average are routed to Textract.
_SCANNED_PDF_THRESHOLD = 50

# DPI used when rendering PDF pages to PNG for Textract OCR.
# 300 DPI is the recommended minimum for accurate OCR results.
_TEXTRACT_RENDER_DPI = 300

# Hard caps on what we will rasterize. A PDF is untrusted input: a few thousand
# pages, or a single page with absurd dimensions, compresses to almost nothing on
# disk but expands to gigabytes of bitmap once rendered at 300 DPI — enough to OOM
# the task. Exceeding either cap raises a clear error (reported per-file by the
# caller) instead of being silently truncated.
_MAX_PDF_PAGES = 1000

# Per-page pixel budget at the render DPI. US-Letter at 300 DPI is ~8.4 MP, so 50 MP
# leaves generous headroom for large-format pages while rejecting pathological ones
# (a 100000x100000 pt page would be ~173,000 MP).
_MAX_RENDER_MEGAPIXELS = 50

# Concurrent Textract DetectDocumentText calls per document. Keep <= the account
# Textract TPS quota (default ~10) to avoid throttling.
_TEXTRACT_CONCURRENCY = int(os.environ.get("TEXTRACT_CONCURRENCY", "10"))


def process_txt(content_bytes: bytes, filename: str) -> tuple[str, str]:
    """Pass through ``.txt`` files (UTF-8 decode only)."""
    return content_bytes.decode("utf-8", errors="replace"), ".txt"


def process_md(content_bytes: bytes, filename: str) -> tuple[str, str]:
    """Pass through ``.md`` files (UTF-8 decode only)."""
    return content_bytes.decode("utf-8", errors="replace"), ".md"


def process_docx(content_bytes: bytes, filename: str) -> tuple[str, str]:
    """Convert ``.docx`` to Markdown via ``unstructured.partition_docx``."""
    from unstructured.partition.docx import partition_docx  # lazy import

    logger.info(
        "Processing DOCX with unstructured",
        extra={"doc_filename": filename},
    )
    elements = partition_docx(file=io.BytesIO(content_bytes))
    text = elements_to_markdown(elements)
    # WARNING on an empty result — see the note in process_pdf.
    log = logger.warning if not text.strip() else logger.info
    log(
        "DOCX processed",
        extra={"doc_filename": filename, "elements": len(elements), "char_count": len(text)},
    )
    return text, ".md"


def process_pdf(
    content_bytes: bytes,
    filename: str,
    textract_client: Any,
    *,
    enable_table_extraction: bool = False,
) -> tuple[str, str]:
    """Process a PDF — text-native via unstructured, scanned via Textract.

    When ``enable_table_extraction`` is True, ALL PDFs (text-native or
    scanned) route through Textract's ``AnalyzeDocument(TABLES)`` instead of
    ``unstructured.partition_pdf(strategy="fast")``. The ``fast`` strategy
    uses pdfminer only, which does not detect tables — a statistical table
    arrives at graphrag as flattened prose with rows severed from headers,
    losing the very structure that made it worth extracting.
    """
    if enable_table_extraction:
        logger.info(
            "PDF routed to Textract AnalyzeDocument(TABLES) — table structure preserved",
            extra={"doc_filename": filename},
        )
        text = process_pdf_textract_tables(content_bytes, filename, textract_client)
        return text, ".md"

    if is_scanned_pdf(content_bytes):
        logger.info(
            "PDF detected as scanned, routing to Textract",
            extra={"doc_filename": filename},
        )
        text = process_pdf_textract(content_bytes, filename, textract_client)
        return text, ".txt"

    logger.info(
        "PDF detected as text-native, using unstructured",
        extra={"doc_filename": filename},
    )
    from unstructured.partition.pdf import partition_pdf  # lazy import

    # strategy="fast" uses pdfminer only — no layout detection models (torch/detectron2)
    elements = partition_pdf(file=io.BytesIO(content_bytes), strategy="fast")
    text = elements_to_markdown(elements)
    # WARNING, not INFO, on an empty result: `unstructured` gives up on
    # drawing-heavy pages and returns zero characters without raising, so an
    # extraction that found nothing and one that worked used to be the same
    # severity — log-based alerting could not tell them apart.
    log = logger.warning if not text.strip() else logger.info
    log(
        "PDF processed",
        extra={"doc_filename": filename, "elements": len(elements), "char_count": len(text)},
    )
    return text, ".md"


def is_scanned_pdf(pdf_bytes: bytes) -> bool:
    """Return True if the PDF appears to be scanned (< 50 chars/page avg).

    A PDF that cannot be opened or whose text cannot be extracted is reported as
    scanned rather than raising, so the caller gets a usable verdict and a logged
    reason instead of a PDFium traceback.
    """
    import pypdfium2 as pdfium  # lazy import

    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except pdfium.PdfiumError as exc:
        logger.warning(
            "Could not open PDF for scanned detection; treating as scanned",
            extra={"reason": str(exc)},
        )
        return True

    try:
        num_pages = len(doc)
        if num_pages == 0:
            return True

        total_chars = 0
        for page_num in range(num_pages):
            # Close each textpage explicitly: it wraps a native PDFium handle, and
            # relying on GC to reclaim them leaks native memory across a long
            # multi-page document.
            textpage = doc[page_num].get_textpage()
            try:
                total_chars += len(textpage.get_text_range())
            finally:
                textpage.close()
        avg_chars = total_chars / num_pages
    except pdfium.PdfiumError as exc:
        logger.warning(
            "Could not extract PDF text for scanned detection; treating as scanned",
            extra={"reason": str(exc)},
        )
        return True
    finally:
        doc.close()

    logger.info(
        "Scanned PDF detection",
        extra={
            "total_chars": total_chars,
            "avg_chars_per_page": round(avg_chars, 1),
            "threshold": _SCANNED_PDF_THRESHOLD,
            "page_count": num_pages,
        },
    )
    return avg_chars < _SCANNED_PDF_THRESHOLD


def process_pdf_textract(
    pdf_bytes: bytes,
    filename: str,
    textract_client: Any,
) -> str:
    """Extract text from a scanned PDF using Amazon Textract.

    Renders each page to a 300-DPI PNG via pypdfium2, then OCRs the pages with
    Textract sync ``DetectDocumentText``. Rendering is done sequentially first
    (PDFium is NOT thread-safe), then the per-page Textract calls are fanned
    out over a thread pool (``_TEXTRACT_CONCURRENCY``) since boto3 clients are
    thread-safe and Textract accepts concurrent requests. Results are
    reassembled in page order.

    Raises:
        ValueError: if the PDF exceeds ``_MAX_PDF_PAGES`` or a page would rasterize
            to more than ``_MAX_RENDER_MEGAPIXELS`` at the render DPI. Both are
            resource guards against untrusted input; the message is safe to surface.
    """
    import pypdfium2 as pdfium  # lazy import

    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except pdfium.PdfiumError as exc:
        logger.warning(
            "Could not open PDF for Textract rendering",
            extra={"doc_filename": filename, "reason": str(exc)},
        )
        return ""

    try:
        num_pages = len(doc)
        # Check the page count immediately after opening: rendering is what costs
        # memory, so reject before the first bitmap is allocated.
        if num_pages > _MAX_PDF_PAGES:
            raise ValueError(f"PDF has {num_pages} pages, exceeding the {_MAX_PDF_PAGES}-page limit for OCR")

        scale = _TEXTRACT_RENDER_DPI / 72
        max_pixels = _MAX_RENDER_MEGAPIXELS * 1_000_000

        # 1. Render all pages to PNG sequentially (local CPU; PDFium not
        #    thread-safe), keeping page order. scale 1.0 == 72 DPI.
        page_pngs: list[bytes] = []
        for page_num in range(num_pages):
            page = doc[page_num]

            # Validate dimensions BEFORE rendering — the bitmap is allocated inside
            # render(), so an oversized page must be rejected up front.
            width_pt, height_pt = page.get_size()
            pixels = (width_pt * scale) * (height_pt * scale)
            if pixels > max_pixels:
                raise ValueError(
                    f"PDF page {page_num + 1} is {width_pt:.0f}x{height_pt:.0f} pt, which renders to "
                    f"{pixels / 1_000_000:.0f} MP at {_TEXTRACT_RENDER_DPI} DPI, "
                    f"exceeding the {_MAX_RENDER_MEGAPIXELS} MP per-page limit"
                )

            try:
                bitmap = page.render(scale=scale)
            except pdfium.PdfiumError as exc:
                logger.warning(
                    "Could not render PDF page for Textract",
                    extra={"doc_filename": filename, "page": page_num + 1, "reason": str(exc)},
                )
                return ""
            try:
                buf = io.BytesIO()
                # to_pil() views the bitmap buffer, so serialize before releasing it.
                bitmap.to_pil().save(buf, format="PNG")
                page_pngs.append(buf.getvalue())
            finally:
                # Release the native bitmap now rather than waiting for GC; at
                # 300 DPI each page is tens of MB and they would otherwise
                # accumulate across the whole document.
                bitmap.close()
    finally:
        doc.close()

    total_pages = len(page_pngs)

    def _ocr_page(idx_png: tuple[int, bytes]) -> tuple[int, str]:
        idx, png_bytes = idx_png
        logger.info(
            "Calling Textract for page",
            extra={"doc_filename": filename, "page": idx + 1, "total_pages": total_pages},
        )
        resp = textract_client.detect_document_text(Document={"Bytes": png_bytes})
        lines = [block["Text"] for block in resp.get("Blocks", []) if block["BlockType"] == "LINE"]
        return idx, "\n".join(lines)

    # 2. OCR pages concurrently (I/O-bound remote calls); reassemble in order.
    pages_text: list[str] = [""] * total_pages
    if total_pages:
        workers = min(_TEXTRACT_CONCURRENCY, total_pages)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for idx, text in pool.map(_ocr_page, enumerate(page_pngs)):
                pages_text[idx] = text

    return "\n\n".join(pages_text)


def process_pdf_textract_tables(
    pdf_bytes: bytes,
    filename: str,
    textract_client: Any,
) -> str:
    """Extract text + tables from a PDF using Textract ``AnalyzeDocument``.

    Renders each page to a 300-DPI PNG (same as
    :func:`process_pdf_textract`), then calls ``AnalyzeDocument`` with
    ``FeatureTypes=['TABLES']`` per page. Reassembles each page into
    Markdown: prose LINEs first, then any TABLE as a Markdown pipe-table.
    Preserves row/column structure so a statistical table arrives at
    graphrag as facts, not prose.

    Cost note: ``AnalyzeDocument`` is materially more expensive per page
    than ``DetectDocumentText``. Opt in per source via
    ``ENABLE_TABLE_EXTRACTION=true`` — do not enable globally on
    prose-dominant corpora.

    Raises:
        ValueError: on the same page-count / page-size guards as
            :func:`process_pdf_textract`.
    """
    import pypdfium2 as pdfium  # lazy import

    try:
        doc = pdfium.PdfDocument(pdf_bytes)
    except pdfium.PdfiumError as exc:
        logger.warning(
            "Could not open PDF for Textract TABLES rendering",
            extra={"doc_filename": filename, "reason": str(exc)},
        )
        return ""

    try:
        num_pages = len(doc)
        if num_pages > _MAX_PDF_PAGES:
            raise ValueError(f"PDF has {num_pages} pages, exceeding the {_MAX_PDF_PAGES}-page limit for OCR")

        scale = _TEXTRACT_RENDER_DPI / 72
        max_pixels = _MAX_RENDER_MEGAPIXELS * 1_000_000

        page_pngs: list[bytes] = []
        for page_num in range(num_pages):
            page = doc[page_num]
            width_pt, height_pt = page.get_size()
            pixels = (width_pt * scale) * (height_pt * scale)
            if pixels > max_pixels:
                raise ValueError(
                    f"PDF page {page_num + 1} is {width_pt:.0f}x{height_pt:.0f} pt, which renders to "
                    f"{pixels / 1_000_000:.0f} MP at {_TEXTRACT_RENDER_DPI} DPI, "
                    f"exceeding the {_MAX_RENDER_MEGAPIXELS} MP per-page limit"
                )

            try:
                bitmap = page.render(scale=scale)
            except pdfium.PdfiumError as exc:
                logger.warning(
                    "Could not render PDF page for Textract TABLES",
                    extra={"doc_filename": filename, "page": page_num + 1, "reason": str(exc)},
                )
                return ""
            try:
                buf = io.BytesIO()
                bitmap.to_pil().save(buf, format="PNG")
                page_pngs.append(buf.getvalue())
            finally:
                bitmap.close()
    finally:
        doc.close()

    total_pages = len(page_pngs)

    def _analyze_page(idx_png: tuple[int, bytes]) -> tuple[int, str]:
        idx, png_bytes = idx_png
        logger.info(
            "Calling Textract AnalyzeDocument(TABLES) for page",
            extra={"doc_filename": filename, "page": idx + 1, "total_pages": total_pages},
        )
        # Runs inside ThreadPoolExecutor.map; an unhandled exception here
        # (ThrottlingException, ProvisionedThroughputExceeded, a malformed page,
        # etc.) propagates out of pool.map and aborts EVERY page, turning one
        # transient per-page fault into a whole-document failure. Isolate it:
        # log and yield an empty page so the rest of the document still returns.
        try:
            resp = textract_client.analyze_document(
                Document={"Bytes": png_bytes},
                FeatureTypes=["TABLES"],
            )
            return idx, _blocks_to_markdown(resp.get("Blocks", []))
        except Exception as exc:  # noqa: BLE001 - isolate per-page failure
            logger.error(
                "Textract AnalyzeDocument(TABLES) failed for page",
                extra={
                    "doc_filename": filename,
                    "page": idx + 1,
                    "total_pages": total_pages,
                    "error": str(exc),
                },
            )
            return idx, ""

    pages_md: list[str] = [""] * total_pages
    if total_pages:
        workers = min(_TEXTRACT_CONCURRENCY, total_pages)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for idx, md in pool.map(_analyze_page, enumerate(page_pngs)):
                pages_md[idx] = md

    return "\n\n".join(pages_md)


def _blocks_to_markdown(blocks: list[dict]) -> str:
    """Reassemble Textract ``AnalyzeDocument`` blocks as Markdown.

    LINE blocks that are NOT inside any TABLE cell are emitted as prose,
    in reading order. TABLE blocks are emitted as pipe-tables, cells
    filled by walking ``CELL → CHILD → WORD`` and reconstructing per-row
    text. Header cells (``EntityTypes == ["COLUMN_HEADER"]``) become the
    first row; if none are marked, the first row of cells is treated as
    the header so the pipe-table stays valid.
    """
    by_id = {b["Id"]: b for b in blocks if "Id" in b}

    # 1. Collect every WORD id that participates in some TABLE cell, so the
    #    prose pass can skip them and we don't emit table content twice.
    table_word_ids: set[str] = set()
    tables: list[dict] = []
    for b in blocks:
        if b.get("BlockType") == "TABLE":
            tables.append(b)
            for rel in b.get("Relationships") or []:
                if rel.get("Type") != "CHILD":
                    continue
                for cell_id in rel.get("Ids", []):
                    cell = by_id.get(cell_id, {})
                    for cell_rel in cell.get("Relationships") or []:
                        if cell_rel.get("Type") != "CHILD":
                            continue
                        table_word_ids.update(cell_rel.get("Ids", []))

    def _cell_text(cell: dict) -> str:
        parts: list[str] = []
        for rel in cell.get("Relationships") or []:
            if rel.get("Type") != "CHILD":
                continue
            for wid in rel.get("Ids", []):
                w = by_id.get(wid)
                if w and w.get("BlockType") == "WORD":
                    parts.append(w.get("Text", ""))
        return " ".join(p for p in parts if p).replace("|", "\\|").strip()

    # 2. Build tables. Textract gives per-cell RowIndex / ColumnIndex, both
    #    1-indexed. Fill a grid, header-detect, emit a pipe-table.
    md_tables: list[str] = []
    for tbl in tables:
        cells: list[dict] = []
        for rel in tbl.get("Relationships") or []:
            if rel.get("Type") != "CHILD":
                continue
            for cid in rel.get("Ids", []):
                c = by_id.get(cid)
                if c and c.get("BlockType") == "CELL":
                    cells.append(c)
        if not cells:
            continue
        max_row = max(c.get("RowIndex", 1) for c in cells)
        max_col = max(c.get("ColumnIndex", 1) for c in cells)
        grid: list[list[str]] = [["" for _ in range(max_col)] for _ in range(max_row)]
        header_row_idx: int | None = None
        for c in cells:
            r = c.get("RowIndex", 1) - 1
            col = c.get("ColumnIndex", 1) - 1
            grid[r][col] = _cell_text(c)
            if "COLUMN_HEADER" in (c.get("EntityTypes") or []) and header_row_idx is None:
                header_row_idx = r
        # If Textract didn't tag any header, treat row 0 as the header — a
        # pipe-table without a header line is not valid Markdown.
        if header_row_idx is None:
            header_row_idx = 0
        header = grid[header_row_idx]
        body_rows = [row for i, row in enumerate(grid) if i != header_row_idx]

        lines = [
            "| " + " | ".join(header) + " |",
            "| " + " | ".join("---" for _ in header) + " |",
            *("| " + " | ".join(row) + " |" for row in body_rows),
        ]
        md_tables.append("\n".join(lines))

    # 3. Prose lines, skipping any LINE composed entirely of table words.
    prose: list[str] = []
    for b in blocks:
        if b.get("BlockType") != "LINE":
            continue
        line_word_ids: list[str] = []
        for rel in b.get("Relationships") or []:
            if rel.get("Type") == "CHILD":
                line_word_ids.extend(rel.get("Ids", []))
        if line_word_ids and all(wid in table_word_ids for wid in line_word_ids):
            continue
        text = b.get("Text", "").strip()
        if text:
            prose.append(text)

    parts: list[str] = []
    if prose:
        parts.append("\n\n".join(prose))
    parts.extend(md_tables)
    return "\n\n".join(parts)


def elements_to_markdown(elements: list) -> str:
    """Convert unstructured ``Element`` objects to a Markdown string."""
    lines: list[str] = []

    for el in elements:
        category = el.category if hasattr(el, "category") else ""
        text = str(el)

        if category == "Title":
            lines.append(f"## {text}")
        elif category == "Header":
            lines.append(f"# {text}")
        elif category == "ListItem":
            lines.append(f"- {text}")
        elif category == "Table":
            lines.append(f"```\n{text}\n```")
        elif text.strip():
            lines.append(text)

    return "\n\n".join(lines)


def get_page_count(content_bytes: bytes, ext: str) -> int:
    """Return the page count for PDFs; 0 for other formats."""
    if ext != ".pdf":
        return 0
    import pypdfium2 as pdfium  # lazy import

    try:
        doc = pdfium.PdfDocument(content_bytes)
    except pdfium.PdfiumError as exc:
        # Page count is metadata, not the extraction itself — a malformed PDF
        # reports 0 pages here and is handled by the processing path, which
        # surfaces the failure with a filename-scoped message.
        logger.warning("Could not read PDF page count", extra={"reason": str(exc)})
        return 0

    try:
        return len(doc)
    finally:
        doc.close()


_PROCESSORS: dict[str, Any] = {
    ".txt": process_txt,
    ".md": process_md,
    ".docx": process_docx,
    # .pdf is handled separately in handler.py (needs textract_client)
}
