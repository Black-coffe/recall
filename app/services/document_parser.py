"""Document parsing → clean text (+ light structure) for RAG (Phase 16A).

A document is turned into plain text (markdown-like) that is stored in
`transcriptions.transcript_text`. After that the WHOLE lower stack works unchanged:
chunking (embeddings._chunk_from_text), local embeddings (e5-large), FTS5,
Claude enrichment (summary/entities/action items), the graph, directions, the RAG chat.

Parsers are registered by extension. Everything is local and free:
  .md / .markdown / .txt — direct reading (UTF-8, errors=replace)
  .docx                  — python-docx (already in deps): paragraphs + tables → markdown
  .pdf                   — PyMuPDF (fitz): per-page text extraction (+ page blocks)
  .pptx                  — python-pptx: slide text + notes (+ slide blocks)
  .xlsx                  — openpyxl: sheets → markdown tables (+ sheet blocks)
  .csv                   — built-in csv: one table (no dependency)
  .png/.jpg/.tiff/...    — Tesseract OCR (pytesseract): text from an image
  scanned PDF            — per-page OCR fallback (fitz render → tesseract)

Provenance (Phase 16B/16C): PDF/PPTX/XLSX/CSV return `blocks` =
[{text, page, section, ...}], where page is the page/slide/sheet number. This lets
RAG citations point to "p. 3 / slide 5 / sheet 'Budget'". Formats without structure
(md/txt/docx) return blocks=None.

Tables (Phase 16C): a bare cell dump embeds poorly. So besides markdown
(for FTS + the structural vector), Claude generates over a sample of each sheet an
NL description of "what is in the table" (text_polishing.describe_sheets, in documents.py) - it is
mixed into the block text → the table becomes semantically findable. The description is optional:
without ANTHROPIC_API_KEY only the markdown tables remain.

OCR (Phase 16D): images and scanned PDFs (without a text layer) are recognized by
Tesseract (pytesseract is a wrapper over the SYSTEM tesseract.exe, WITHOUT torch/
transformers, so it does not touch the pins - unlike surya/docTR). Requires an
installed tesseract + language packs. ocr_available()=False → images
raise ParserUnavailable, scanned PDFs → needs_ocr, the rest of parsing works.

Lazy guarded imports: the module imports even without PyMuPDF/python-pptx/openpyxl/
pytesseract - those formats then raise ParserUnavailable, while .md/.txt/.docx/.csv
always work (python-docx is in the base deps, csv is built in).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Callable, Optional

from app.core.i18n import tr
from app.utils.files import ALLOWED_DOCUMENT_EXTENSIONS
from app.utils.proc import silence_pytesseract_console_windows


logger = logging.getLogger(__name__)

# Bump when the parsing logic changes → re-parse stale documents (16E).
PARSER_VERSION = 1

# If a PDF text layer has fewer characters per page than this, it is probably a scan without a
# text layer (OCR needed, Phase 16D). We flag it in meta, but still
# return the text (there may be a partial layer).
_PDF_MIN_CHARS_PER_PAGE = 24

# --- OCR (Phase 16D) ---
# Tesseract via pytesseract - a thin wrapper over the system binary, WITHOUT
# torch/transformers (unlike surya/docTR) → does not touch the fragile chain of
# pins. Requires an installed tesseract.exe + language packs in PATH.
_OCR_LANG = os.environ.get("OCR_LANG", "ukr+rus+eng")
_OCR_DPI = int(os.environ.get("OCR_DPI", "200"))      # PDF page render for OCR
_OCR_MAX_PAGES = int(os.environ.get("OCR_MAX_PAGES", "50"))  # cap on synchronous OCR
_ocr_unavailable_reason: Optional[str] = None


class DocumentParseError(Exception):
    """Could not parse the document (empty / corrupt / unsupported)."""


class NoTextFound(DocumentParseError):
    """OCR ran, but there is no text in the image (an ordinary photo). Not a parse failure."""


class ParserUnavailable(DocumentParseError):
    """The parser for the format is unavailable (e.g. PyMuPDF is not installed for PDF)."""


def ext_of(filename: str) -> str:
    if not filename or '.' not in filename:
        return ''
    return filename.rsplit('.', 1)[1].lower()


def is_supported(filename: str) -> bool:
    return ext_of(filename) in ALLOWED_DOCUMENT_EXTENSIONS


def content_hash(text: str) -> str:
    """sha256 of the normalized text - for dedup of repeated uploads (16E)."""
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def _normalize_text(text: str) -> str:
    """Unify line breaks and collapse more than 2 consecutive empty lines."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)        # trailing spaces
    text = re.sub(r"\n{3,}", "\n\n", text)         # ≤1 empty line between blocks
    return text.strip()


# ============================================================
# OCR (Phase 16D) - graceful, like embeddings.is_available()
# ============================================================

_TESSERACT_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def _configure_tesseract() -> None:
    """Find tesseract outside PATH and language packs outside Program Files.

    The UB-Mannheim winget install puts the binary in Program Files, but NOT in PATH, and
    carries only eng/osd; ukr/rus cannot be added there without admin rights. So:
    the binary - ``OCR_TESSERACT_CMD`` or the installer's standard path; language packs -
    ``OCR_TESSDATA_DIR`` or ``%LOCALAPPDATA%\\Recall\\tessdata`` (if the folder exists)."""
    import shutil
    import pytesseract
    cmd = os.environ.get("OCR_TESSERACT_CMD", "")
    if not cmd and not shutil.which("tesseract") and os.path.isfile(_TESSERACT_DEFAULT):
        cmd = _TESSERACT_DEFAULT
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    tessdata = os.environ.get("OCR_TESSDATA_DIR") or os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Recall", "tessdata")
    if tessdata and os.path.isdir(tessdata) and not os.environ.get("TESSDATA_PREFIX"):
        os.environ["TESSDATA_PREFIX"] = tessdata


def ocr_available() -> bool:
    """Whether OCR is available: pytesseract imports AND the system tesseract is present."""
    global _ocr_unavailable_reason
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception as e:  # pragma: no cover
        _ocr_unavailable_reason = tr("svc_media.doc.err.ocr_py_unavailable", error=e)
        return False
    try:
        _configure_tesseract()
    except Exception:
        logger.debug("tesseract path setup failed", exc_info=True)
    try:
        import pytesseract
        # get_tesseract_version() bypasses pytesseract's own subprocess_args()
        # (that is where SW_HIDE is) - without the patch the console flashes under pythonw.exe.
        silence_pytesseract_console_windows()
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        _ocr_unavailable_reason = tr("svc_media.doc.err.tesseract_missing")
        return False


def ocr_unavailability_reason() -> str:
    return _ocr_unavailable_reason or tr("svc_media.doc.err.ocr_unavailable")


def _ocr_image_obj(img) -> str:
    """OCR of a single PIL image (a picture or a scanned PDF page). Tries _OCR_LANG,
    falling back to the default language (if the tesseract language pack is missing it raises
    TesseractError). Passwords and tokens in the recognized text are masked right away
    (ADR-030): OCR is the only point through which text from a screen/scan enters the archive,
    so the mask here covers every consumer (Telegram, UI upload)."""
    import pytesseract
    try:
        text = pytesseract.image_to_string(img, lang=_OCR_LANG)
    except pytesseract.TesseractError:
        try:
            text = pytesseract.image_to_string(img)  # default language (eng)
        except Exception:
            logger.debug("OCR with the default language failed", exc_info=True)
            return ""
    except Exception:
        logger.debug("OCR failed", exc_info=True)
        return ""
    return _mask_secrets(text)


def _mask_secrets(text: str) -> str:
    if not text:
        return text
    from app.services import screen_secrets
    return screen_secrets.mask_text(text, screen_secrets.scan_text(text)) or ""


def _ocr_pdf_page(page) -> str:
    """Render a PDF page to a raster and run it through OCR."""
    try:
        import io
        from PIL import Image
        pix = page.get_pixmap(dpi=_OCR_DPI)
        with Image.open(io.BytesIO(pix.tobytes("png"))) as img:
            return _normalize_text(_ocr_image_obj(img))
    except Exception as e:  # pragma: no cover
        logger.warning("[ocr] page not recognized: %s", e)
        return ""


# ============================================================
# Parsers by format
# ============================================================

def _parse_text(path: str) -> dict:
    """.md / .markdown / .txt - direct reading."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    return {"text": _normalize_text(raw), "page_count": None, "meta": {}}


def _docx_iter_blocks(doc):
    """Iterate over paragraphs and tables in the order they appear in the document."""
    from docx.document import Document as _DocxDocument
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table as _Table
    from docx.text.paragraph import Paragraph as _Paragraph

    body = doc.element.body
    for child in body.iterchildren():
        if isinstance(child, CT_P):
            yield ("p", _Paragraph(child, doc))
        elif isinstance(child, CT_Tbl):
            yield ("table", _Table(child, doc))


def _docx_heading_level(paragraph) -> Optional[int]:
    """Heading level (1..6) if the style is 'Heading N' / 'Заголовок N', otherwise None."""
    try:
        name = (paragraph.style.name or "")
    except Exception:
        return None
    m = re.search(r"(\d+)$", name)
    if m and ("Heading" in name or "Заголовок" in name or "Title" in name):
        return min(int(m.group(1)), 6)
    if name in ("Title", "Заголовок"):
        return 1
    return None


def _docx_table_to_markdown(table) -> str:
    rows = []
    for row in table.rows:
        cells = [" ".join((c.text or "").split()) for c in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
    if not rows:
        return ""
    ncol = len(table.rows[0].cells)
    sep = "| " + " | ".join(["---"] * ncol) + " |"
    return "\n".join([rows[0], sep, *rows[1:]])


def _parse_docx(path: str) -> dict:
    """.docx - paragraphs (with headings) + tables → markdown."""
    try:
        import docx  # python-docx
    except ImportError as e:  # pragma: no cover
        raise ParserUnavailable(tr("svc_media.doc.err.docx_missing", error=e)) from e

    doc = docx.Document(path)
    parts: list[str] = []
    for kind, block in _docx_iter_blocks(doc):
        if kind == "p":
            txt = (block.text or "").strip()
            if not txt:
                continue
            level = _docx_heading_level(block)
            parts.append(("#" * level + " " + txt) if level else txt)
        elif kind == "table":
            md = _docx_table_to_markdown(block)
            if md:
                parts.append(md)
    text = _normalize_text("\n\n".join(parts))
    return {"text": text, "page_count": None, "meta": {}}


def _parse_pdf(path: str) -> dict:
    """.pdf - per-page text extraction via PyMuPDF (fitz)."""
    try:
        import fitz  # PyMuPDF
    except ImportError as e:
        raise ParserUnavailable(
            tr("svc_media.doc.err.pdf_missing")
        ) from e

    ocr_on = ocr_available()  # Phase 16D: fallback for scans without a text layer
    pages: list[str] = []
    blocks: list[dict] = []
    ocr_pages = 0
    with fitz.open(path) as doc:
        page_count = doc.page_count
        for i, page in enumerate(doc):
            ptext = _normalize_text(page.get_text("text") or "")
            # Page with almost no text + OCR available → recognize it as a raster.
            if len(ptext) < _PDF_MIN_CHARS_PER_PAGE and ocr_on and ocr_pages < _OCR_MAX_PAGES:
                otext = _ocr_pdf_page(page)
                if len(otext) > len(ptext):
                    ptext = otext
                    ocr_pages += 1
            pages.append(ptext)
            if ptext:
                # Provenance (Phase 16B): every non-empty page is a separate block.
                blocks.append({"text": ptext, "page": i + 1, "section": None})
    text = _normalize_text("\n\n".join(p for p in pages if p))

    meta = {}
    if ocr_pages:
        meta["ocr_pages"] = ocr_pages
    # Scan without a text layer AND OCR unavailable → flag it (the message is in parse_document).
    if page_count and len(text) < _PDF_MIN_CHARS_PER_PAGE * page_count and not ocr_on:
        meta["needs_ocr"] = True
    return {"text": text, "page_count": page_count, "blocks": blocks, "meta": meta}


def _parse_pptx(path: str) -> dict:
    """.pptx - slide text + speaker notes (Phase 16B). page=slide number,
    section=slide title (for citation provenance)."""
    try:
        from pptx import Presentation  # python-pptx
    except ImportError as e:
        raise ParserUnavailable(
            tr("svc_media.doc.err.pptx_missing")
        ) from e

    prs = Presentation(path)
    slides = list(prs.slides)
    blocks: list[dict] = []
    parts: list[str] = []
    for i, slide in enumerate(slides):
        # Look the title up by shape_id (NOT by `is`: python-pptx hands out new
        # wrappers on every iteration, so an identity comparison does not work).
        title_id = None
        try:
            _ts = slide.shapes.title
            title_id = _ts.shape_id if _ts is not None else None
        except Exception:
            title_id = None
        title: Optional[str] = None
        lines: list[str] = []
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            txt = (shape.text_frame.text or "").strip()
            if not txt:
                continue
            if title is None and title_id is not None:
                try:
                    if shape.shape_id == title_id:
                        title = txt.splitlines()[0][:120]
                except Exception:
                    pass
            lines.append(txt)
        # speaker notes
        notes = ""
        try:
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
        except Exception:
            notes = ""
        slide_text = "\n".join(lines)
        if notes:
            slide_text += f"\n\n[Нотатки доповідача]\n{notes}"
        slide_text = _normalize_text(slide_text)
        if slide_text:
            blocks.append({"text": slide_text, "page": i + 1, "section": title})
            parts.append(slide_text)

    text = _normalize_text("\n\n".join(parts))
    return {"text": text, "page_count": len(slides), "blocks": blocks, "meta": {}}


# ------------------------------------------------------------
# Tables: XLSX / CSV (Phase 16C)
# ------------------------------------------------------------
# Row cap per sheet for the stored markdown - so a giant dump does not bloat
# transcript_text. The description ("what is in the table") is made by Claude (text_polishing.describe_sheets)
# over a sample - so the full volume is not needed for semantics.
_TABLE_MAX_ROWS = 1000


def _md_cell(v) -> str:
    """Cell value → safe markdown text (pipe escaped, no line breaks)."""
    if v is None:
        return ""
    s = str(v).replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return " ".join(s.split())


def _rows_to_markdown(rows: list[list]) -> tuple[str, int, int]:
    """rows (first = header) → markdown table. Returns (md, n_rows, n_cols)."""
    rows = [r for r in rows if r is not None]
    if not rows:
        return "", 0, 0
    ncols = max((len(r) for r in rows), default=0)
    if ncols == 0:
        return "", 0, 0
    norm = [[_md_cell(r[i]) if i < len(r) else "" for i in range(ncols)] for r in rows]
    header = norm[0]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * ncols) + " |",
    ]
    for r in norm[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines), len(rows), ncols


def _parse_xlsx(path: str) -> dict:
    """.xlsx - each sheet → a markdown table as a separate block (page=sheet
    index, section=sheet name). data_only=True → formula values, not formulas."""
    try:
        from openpyxl import load_workbook
    except ImportError as e:
        raise ParserUnavailable(
            tr("svc_media.doc.err.xlsx_missing")
        ) from e

    wb = load_workbook(path, read_only=True, data_only=True)
    blocks: list[dict] = []
    parts: list[str] = []
    try:
        for idx, ws in enumerate(wb.worksheets):
            rows: list[list] = []
            truncated = False
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= _TABLE_MAX_ROWS:
                    truncated = True
                    break
                if row is None or all(c is None for c in row):
                    continue
                rows.append(list(row))
            if not rows:
                continue
            md, nrows, ncols = _rows_to_markdown(rows)
            if not md:
                continue
            if truncated:
                md += f"\n\n_(показано перші {_TABLE_MAX_ROWS} рядків)_"
            title = ws.title or f"Аркуш {idx + 1}"
            sheet_text = _normalize_text(f"## Аркуш «{title}»\n\n{md}")
            blocks.append({"text": sheet_text, "page": idx + 1, "section": title,
                           "rows": nrows, "cols": ncols, "tabular": True})
            parts.append(sheet_text)
    finally:
        wb.close()

    text = _normalize_text("\n\n".join(parts))
    return {"text": text, "page_count": len(blocks), "blocks": blocks, "meta": {}}


def _parse_csv(path: str) -> dict:
    """.csv - one table block (page=1). The delimiter is detected automatically (,/;/tab)."""
    import csv as _csv

    rows: list[list] = []
    truncated = False
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = _csv.Sniffer().sniff(sample, delimiters=",;\t")
        except _csv.Error:
            dialect = _csv.excel
        reader = _csv.reader(f, dialect)
        for i, row in enumerate(reader):
            if i >= _TABLE_MAX_ROWS:
                truncated = True
                break
            rows.append(row)

    if not rows:
        return {"text": "", "page_count": 0, "blocks": [], "meta": {}}
    md, nrows, ncols = _rows_to_markdown(rows)
    if truncated:
        md += f"\n\n_(показано перші {_TABLE_MAX_ROWS} рядків)_"
    md = _normalize_text(md)
    block = {"text": md, "page": 1, "section": None,
             "rows": nrows, "cols": ncols, "tabular": True}
    return {"text": md, "page_count": 1, "blocks": [block], "meta": {}}


# ------------------------------------------------------------
# Images: OCR (Phase 16D)
# ------------------------------------------------------------
_IMAGE_EXTS = ("png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp")


def _parse_image(path: str) -> dict:
    """Image → text via OCR (Tesseract). Without OCR - ParserUnavailable."""
    if not ocr_available():
        raise ParserUnavailable(
            tr("svc_media.doc.err.image_ocr_off", reason=ocr_unavailability_reason())
        )
    try:
        from PIL import Image
    except ImportError as e:  # pragma: no cover
        raise ParserUnavailable(tr("svc_media.doc.err.pillow_missing", error=e)) from e
    try:
        with Image.open(path) as img:
            text = _normalize_text(_ocr_image_obj(img))
    except Exception as e:
        raise DocumentParseError(tr("svc_media.doc.err.image_open", error=e)) from e
    return {"text": text, "page_count": 1, "blocks": None, "meta": {"ocr": True}}


# extension → parser
_PARSERS: dict[str, Callable[[str], dict]] = {
    "md": _parse_text,
    "markdown": _parse_text,
    "txt": _parse_text,
    "docx": _parse_docx,
    "pdf": _parse_pdf,
    "pptx": _parse_pptx,
    "xlsx": _parse_xlsx,
    "csv": _parse_csv,
    **{ext: _parse_image for ext in _IMAGE_EXTS},
}


# ============================================================
# Public API
# ============================================================

def parse_document(path: str, filename: Optional[str] = None) -> dict:
    """Parse a document → text + metadata. The type is determined by the extension of
    filename (if given), otherwise path.

    Returns {text, doc_type, page_count, byte_size, content_hash, char_count,
             parser_version, meta}.
    Raises DocumentParseError if the format is unsupported, the parser is unavailable,
    or the extracted text is empty.
    """
    ext = ext_of(filename or path)
    parser = _PARSERS.get(ext)
    if parser is None:
        raise DocumentParseError(tr("svc_media.doc.err.unsupported_format", ext=ext))

    try:
        byte_size = os.path.getsize(path)
    except OSError:
        byte_size = None

    result = parser(path)
    text = (result.get("text") or "").strip()
    if not text:
        meta = result.get("meta") or {}
        if meta.get("needs_ocr"):
            raise DocumentParseError(
                tr("svc_media.doc.err.pdf_no_text_layer")
            )
        if meta.get("ocr"):
            raise NoTextFound(tr("svc_media.doc.err.ocr_no_text"))
        raise DocumentParseError(tr("svc_media.doc.err.empty"))

    return {
        "text": text,
        "doc_type": ext,
        "page_count": result.get("page_count"),
        "byte_size": byte_size,
        "content_hash": content_hash(text),
        "char_count": len(text),
        "parser_version": PARSER_VERSION,
        # {text, page, section} blocks for provenance (Phase 16B). None for
        # formats without structure (md/txt/docx) → chunking falls back to the text.
        "blocks": result.get("blocks"),
        "meta": result.get("meta") or {},
    }
