"""Reads a report file into rows of {header: value}: Excel (xlsx, xls), CSV, PDF and Word (docx) tables, or a
table in an image (PNG, JPEG, WebP) through OCR.

The header row is the first row with at least MIN_HEADER_CELLS non-empty cells (the ERP files put it on
row 1, but title rows above it are tolerated). Blank rows, and header rows repeated on later pages, are
skipped, and never more than `max_rows` rows are read.
"""
from __future__ import annotations

import csv
import io
import threading
import zipfile
from dataclasses import dataclass
from statistics import median
from typing import Any, Iterable

MIN_HEADER_CELLS = 3
HEADER_SEARCH_ROWS = 20
XLSX_TYPES = {"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
CSV_TYPES = {"text/csv", "application/csv", "text/comma-separated-values", "application/vnd.ms-excel"}
DOCX_TYPES = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
PDF_TYPES = {"application/pdf"}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
EXTENSIONS = {".xlsx": "xlsx", ".xls": "xls", ".csv": "csv", ".pdf": "pdf", ".docx": "docx",
              ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image"}
TEXT_KINDS = {"csv"}
SUPPORTED = "Excel (.xlsx, .xls), CSV, PDF, Word (.docx) or an image (PNG, JPG, WebP)"
MAX_IMAGE_PIXELS = 40_000_000     # bigger images are refused rather than decoded


class ReportFileError(Exception):
    pass


@dataclass
class ParsedFile:
    headers: list[str]
    rows: list[dict[str, Any]]
    truncated: bool          # more data rows than max_rows


def _zip_kind(content: bytes, lower: str) -> str:
    """"docx" or "xlsx" for a zip file; a broken zip goes by its extension (Excel unless it says .docx)."""
    try:
        names = set(zipfile.ZipFile(io.BytesIO(content)).namelist())
    except (zipfile.BadZipFile, ValueError, OSError):
        return "docx" if lower.endswith(".docx") else "xlsx"
    return "docx" if "word/document.xml" in names else "xlsx"


def _sniff(content: bytes, lower: str) -> str | None:
    """The kind the bytes prove, or None when they prove nothing (plain text, or unknown)."""
    if content[:5] == b"%PDF-":
        return "pdf"
    if content[:8] == b"\x89PNG\r\n\x1a\n" or content[:3] == b"\xff\xd8\xff" or \
            (content[:4] == b"RIFF" and content[8:12] == b"WEBP"):
        return "image"
    if content[:4] == b"\xd0\xcf\x11\xe0":
        return None if lower.endswith(".doc") else "xls"     # legacy OLE: .xls is read, old .doc is not
    if content[:2] == b"PK":
        return _zip_kind(content, lower)
    return None


def file_kind(name: str, content_type: str = "", content: bytes = b"") -> str | None:
    """"xlsx", "xls", "csv", "pdf", "docx", "image" or None: the label (Content-Type, then extension), checked
    against the bytes, which win when they prove a type."""
    ctype = (content_type or "").lower()
    lower = (name or "").lower()
    ext = EXTENSIONS.get(lower[lower.rfind("."):]) if "." in lower else None
    if ctype in XLSX_TYPES:
        kind = "xlsx"
    elif ctype in DOCX_TYPES:
        kind = "docx"
    elif ctype in PDF_TYPES:
        kind = "pdf"
    elif ctype in IMAGE_TYPES:
        kind = "image"
    elif ctype in CSV_TYPES and ext in (None, "csv"):
        kind = "csv"
    else:
        kind = ext
    if content:
        proven = _sniff(content, lower)
        if proven:
            return proven
        if kind == "xls":
            return "csv"             # many exports are tab/comma text saved as .xls
        if kind not in TEXT_KINDS:
            return None              # labelled binary, but the bytes are something else
    return kind


def _cell(value: Any) -> Any:
    if isinstance(value, str):
        value = " ".join(value.split())      # PDF and Word cells wrap onto several lines
        return value or None
    return value


def _table(lines: Iterable[Iterable[Any]], max_rows: int) -> ParsedFile:
    headers: list[str] | None = None
    header_values: list[Any] = []
    rows: list[dict[str, Any]] = []
    for index, raw in enumerate(lines):
        values = [_cell(v) for v in raw]
        filled = sum(v is not None for v in values)
        if headers is None:
            if filled >= MIN_HEADER_CELLS:
                header_values = values
                headers = [str(v) if v is not None else f"column_{i + 1}" for i, v in enumerate(values)]
            elif index >= HEADER_SEARCH_ROWS:
                break
            continue
        if not filled or values == header_values:     # blank, or the header repeated on the next page
            continue
        if len(rows) >= max_rows:
            return ParsedFile(headers, rows, truncated=True)
        rows.append({h: v for h, v in zip(headers, values) if v is not None})
    if headers is None:
        raise ReportFileError("no header row was found")
    return ParsedFile(headers, rows, truncated=False)


def _first_sheet_table(sheets: Iterable[Iterable[Iterable[Any]]], max_rows: int) -> ParsedFile:
    """The table of the first sheet that has one (empty or cover sheets are skipped)."""
    last_error = None
    for sheet_rows in sheets:
        try:
            return _table(sheet_rows, max_rows)
        except ReportFileError as exc:
            last_error = exc
    raise last_error or ReportFileError("the workbook has no sheets")


def _xlsx(content: bytes, max_rows: int) -> ParsedFile:
    from openpyxl import load_workbook
    try:
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:     # openpyxl raises many types (BadZipFile, InvalidFileException, KeyError...)
        raise ReportFileError("it is not a readable Excel file") from exc
    try:
        return _first_sheet_table((sheet.iter_rows(values_only=True) for sheet in workbook.worksheets), max_rows)
    finally:
        workbook.close()


def _xls(content: bytes, max_rows: int) -> ParsedFile:
    import xlrd
    try:
        book = xlrd.open_workbook(file_contents=content, on_demand=True)
    except Exception as exc:
        raise ReportFileError("it is not a readable Excel (.xls) file") from exc

    def value(cell: Any) -> Any:
        if cell.ctype == xlrd.XL_CELL_DATE:
            try:
                return xlrd.xldate_as_datetime(cell.value, book.datemode)
            except (ValueError, OverflowError):
                return cell.value
        if cell.ctype == xlrd.XL_CELL_NUMBER and float(cell.value).is_integer():
            return int(cell.value)           # .xls stores every number as a float: 1001.0 is AWB 1001
        if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
            return None
        return cell.value

    def rows(sheet: Any) -> Iterable[list[Any]]:
        for r in range(sheet.nrows):
            yield [value(c) for c in sheet.row(r)]

    try:
        return _first_sheet_table((rows(book.sheet_by_index(i)) for i in range(book.nsheets)), max_rows)
    finally:
        book.release_resources()


def _csv(content: bytes, max_rows: int) -> ParsedFile:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ReportFileError("its text encoding is not supported")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    try:
        return _table(csv.reader(io.StringIO(text, newline=""), dialect), max_rows)
    except csv.Error as exc:
        raise ReportFileError("it is not a readable CSV file") from exc


def _docx(content: bytes, max_rows: int) -> ParsedFile:
    from docx import Document
    try:
        document = Document(io.BytesIO(content))
    except Exception as exc:
        raise ReportFileError("it is not a readable Word (.docx) file") from exc
    if not document.tables:
        raise ReportFileError("the Word file has no table")
    # A table that continues on the next page is often split into several Word tables: read them as one.
    return _table(([cell.text for cell in row.cells] for table in document.tables for row in table.rows), max_rows)


def _pdf(content: bytes, max_rows: int) -> ParsedFile:
    import pdfplumber
    try:
        pdf = pdfplumber.open(io.BytesIO(content))
    except Exception as exc:
        raise ReportFileError("it is not a readable PDF") from exc

    def lines() -> Iterable[list[Any]]:
        for page in pdf.pages:          # read lazily: stops once max_rows rows are read
            for table in page.extract_tables():
                yield from table
            page.close()

    with pdf:
        try:
            return _table(lines(), max_rows)
        except ReportFileError:
            raise ReportFileError("no table was found in the PDF (for a scanned PDF, attach a screenshot "
                                  "or photo of the table instead)") from None
        except Exception as exc:        # pdfminer errors on damaged files
            raise ReportFileError("it is not a readable PDF") from exc


_ocr_engine: Any = None
_ocr_lock = threading.Lock()


def _ocr() -> Any:
    """One RapidOCR engine for the process (loading its models takes a second)."""
    global _ocr_engine
    with _ocr_lock:
        if _ocr_engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _ocr_engine = RapidOCR()
    return _ocr_engine


@dataclass
class _Word:
    x0: float
    x1: float
    y0: float
    y1: float
    text: str

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


def table_from_words(words: list[_Word]) -> list[list[str | None]]:
    """OCR words laid out as table rows. Words on one line (close vertical centres) form a row; words close
    together on a row are one cell. After the header row, each cell goes to the header column it overlaps
    most (or the nearest one), so data rows line up with the header even when cells are empty."""
    if not words:
        return []
    height = median(w.y1 - w.y0 for w in words) or 1.0
    lines: list[list[_Word]] = []
    for word in sorted(words, key=lambda w: w.cy):
        if lines and abs(word.cy - sum(w.cy for w in lines[-1]) / len(lines[-1])) <= height * 0.5:
            lines[-1].append(word)
        else:
            lines.append([word])

    def cells(line: list[_Word]) -> list[_Word]:
        merged: list[_Word] = []
        for word in sorted(line, key=lambda w: w.x0):
            if merged and word.x0 - merged[-1].x1 < height * 0.8:      # a space, not a column gap
                last = merged[-1]
                merged[-1] = _Word(last.x0, max(last.x1, word.x1), min(last.y0, word.y0), max(last.y1, word.y1),
                                   f"{last.text} {word.text}")
            else:
                merged.append(word)
        return merged

    rows: list[list[str | None]] = []
    columns: list[_Word] | None = None
    for line in lines:
        line_cells = cells(line)
        if columns is None:
            rows.append([c.text for c in line_cells])
            if len(line_cells) >= MIN_HEADER_CELLS:
                columns = line_cells
            continue
        row: list[str | None] = [None] * len(columns)
        for cell in line_cells:
            def fit(i: int) -> tuple[float, float]:
                col = columns[i]
                overlap = min(cell.x1, col.x1) - max(cell.x0, col.x0)
                return overlap, -abs((cell.x0 + cell.x1) / 2 - (col.x0 + col.x1) / 2)
            best = max(range(len(columns)), key=fit)
            row[best] = f"{row[best]} {cell.text}" if row[best] else cell.text
        rows.append(row)
    return rows


def _image(content: bytes, max_rows: int) -> ParsedFile:
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(io.BytesIO(content)) as img:
            if img.width * img.height > MAX_IMAGE_PIXELS:
                raise ReportFileError("the image is too large (use a screenshot under 40 megapixels)")
            img.verify()
    except ReportFileError:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ReportFileError("it is not a readable image") from exc
    try:
        result, _ = _ocr()(content)
    except Exception as exc:
        raise ReportFileError("the image could not be read") from exc
    words = [_Word(min(p[0] for p in box), max(p[0] for p in box), min(p[1] for p in box), max(p[1] for p in box),
                   str(text).strip())
             for box, text, _score in (result or []) if str(text).strip()]
    if not words:
        raise ReportFileError("no text was found in the image")
    try:
        return _table(table_from_words(words), max_rows)
    except ReportFileError:
        raise ReportFileError("no table was found in the image (it needs a header row with at least "
                              f"{MIN_HEADER_CELLS} columns)") from None


PARSERS = {"xlsx": _xlsx, "xls": _xls, "csv": _csv, "docx": _docx, "pdf": _pdf, "image": _image}


def parse(name: str, content_type: str, content: bytes, max_rows: int) -> ParsedFile:
    kind = file_kind(name, content_type, content)
    if kind is None:
        raise ReportFileError(f"it is not a supported file: attach {SUPPORTED}")
    return PARSERS[kind](content, max_rows)
