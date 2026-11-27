import io
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.ai_insights import aggregate, report_file, sources
from app.ai_insights.schemas import GenerateRequest
from tests.test_ai_insights import (ADDRESS, END, FILE_NAME, FM_HEADERS, FROM, LANDMARK, TO, TODAY, XLSX, fm_row,
                                    llm_parts, make_env, no_other_http, payload, post, xlsx)  # noqa: F401


@pytest.fixture
def env():
    client, llm, app = make_env()
    yield client, llm, app
    client.__exit__(None, None, None)


# ---- parsing --------------------------------------------------------------------------------

def test_parses_xlsx_with_title_row_and_blank_rows():
    wb = Workbook()
    ws = wb.active
    ws.append(["Unified First Mile Report"])
    ws.append([])
    ws.append(FM_HEADERS)
    ws.append(fm_row(1))
    ws.append([None] * 13)
    ws.append(fm_row(2))
    out = io.BytesIO()
    wb.save(out)
    parsed = report_file.parse("Unified_FM_report.xlsx", XLSX, out.getvalue(), max_rows=100)
    assert parsed.headers == FM_HEADERS
    assert [r["orderId"] for r in parsed.rows] == [9001, 9002]
    assert parsed.truncated is False


def test_row_cap_stops_reading():
    parsed = report_file.parse("f.xlsx", XLSX, xlsx(FM_HEADERS, [fm_row(i) for i in range(5)]), max_rows=3)
    assert len(parsed.rows) == 3 and parsed.truncated is True


@pytest.mark.parametrize("encoding", ["utf-8-sig", "cp1252"])
def test_parses_csv_with_bom_or_cp1252(encoding):
    text = "AWB_Number,Courier,Order_Tracking_Status\r\n123,Bluedart Café,Not Picked\r\n\r\n456,Ekart,Pickup Scheduled\r\n"
    parsed = report_file.parse("Unified_FM_report.csv", "text/csv", text.encode(encoding), max_rows=10)
    assert parsed.headers == ["AWB_Number", "Courier", "Order_Tracking_Status"]
    assert parsed.rows == [{"AWB_Number": "123", "Courier": "Bluedart Café", "Order_Tracking_Status": "Not Picked"},
                           {"AWB_Number": "456", "Courier": "Ekart", "Order_Tracking_Status": "Pickup Scheduled"}]


def test_file_type_detection():
    assert report_file.file_kind("a.xlsx") == "xlsx" and report_file.file_kind("a.CSV") == "csv"
    assert report_file.file_kind("a.pdf") == "pdf" and report_file.file_kind("a.docx") == "docx"
    assert report_file.file_kind("a.xls") == "xls" and report_file.file_kind("a.JPG") == "image"
    assert report_file.file_kind("a.doc") is None and report_file.file_kind("a.txt") is None
    assert report_file.file_kind("report", XLSX) == "xlsx"
    assert report_file.file_kind("a.csv", "text/csv", b"PK\x03\x04") == "xlsx"      # bytes win over the label
    assert report_file.file_kind("a.xlsx", XLSX, b"not a zip") is None
    # Bytes prove the type whatever the name says.
    assert report_file.file_kind("scan", "", b"%PDF-1.7 ...") == "pdf"
    assert report_file.file_kind("x.xlsx", XLSX, b"\x89PNG\r\n\x1a\n....") == "image"
    assert report_file.file_kind("x", "", b"\xff\xd8\xff\xe0 jpeg") == "image"
    assert report_file.file_kind("a.xls", "application/vnd.ms-excel", b"\xd0\xcf\x11\xe0 ole") == "xls"
    assert report_file.file_kind("a.doc", "", b"\xd0\xcf\x11\xe0 ole") is None          # old Word is not read
    assert report_file.file_kind("a.docx", "", docx_bytes([["a", "b", "c"]])) == "docx"
    # Text exported with a .xls name is read as CSV, as before.
    assert report_file.file_kind("a.xls", "application/vnd.ms-excel", b"AWB\tCourier\n") == "csv"


def docx_bytes(rows: list[list], tables: int = 1) -> bytes:
    from docx import Document
    document = Document()
    document.add_paragraph("Unified First Mile Report")
    per_table = -(-len(rows) // tables)
    for t in range(tables):
        part = rows[t * per_table:(t + 1) * per_table]
        table = document.add_table(rows=0, cols=len(rows[0]))
        for row in part:
            cells = table.add_row().cells
            for cell, value in zip(cells, row):
                cell.text = "" if value is None else str(value)
    out = io.BytesIO()
    document.save(out)
    return out.getvalue()


def pdf_bytes(rows: list[list], rows_per_page: int = 50) -> bytes:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.platypus import PageBreak, SimpleDocTemplate, Table, TableStyle
    out = io.BytesIO()
    story = []
    header, body = rows[0], rows[1:]
    for start in range(0, len(body), rows_per_page):
        table = Table([header] + body[start:start + rows_per_page])       # the header repeats on every page
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, "black"), ("FONTSIZE", (0, 0), (-1, -1), 6)]))
        story += [table, PageBreak()]
    SimpleDocTemplate(out, pagesize=landscape(A4)).build(story)
    return out.getvalue()


SHORT_HEADERS = ["AWB_Number", "Courier", "Order_Tracking_Status", "Warehouse_City"]
SHORT_ROWS = [[100001, "Delhivery", "Delivered", "DELHI"], [100002, "Xpressbees", "RTO", "MUMBAI"],
              [100003, "Ekart", "In Transit", ""]]


def test_parses_word_tables_split_across_pages():
    parsed = report_file.parse("report.docx", "", docx_bytes([SHORT_HEADERS] + SHORT_ROWS + [SHORT_HEADERS], tables=2),
                               max_rows=10)
    assert parsed.headers == SHORT_HEADERS
    assert [r["AWB_Number"] for r in parsed.rows] == ["100001", "100002", "100003"]   # repeated header skipped
    assert "Warehouse_City" not in parsed.rows[2]                                      # empty cell dropped


def test_word_file_without_a_table():
    from docx import Document
    document = Document()
    document.add_paragraph("No table here")
    out = io.BytesIO()
    document.save(out)
    with pytest.raises(report_file.ReportFileError, match="has no table"):
        report_file.parse("notes.docx", "", out.getvalue(), max_rows=10)


def test_parses_pdf_tables_with_the_header_on_every_page():
    rows = [SHORT_HEADERS] + [[100000 + i, "Delhivery", "Delivered", "DELHI"] for i in range(1, 8)]
    parsed = report_file.parse("report.pdf", "application/pdf", pdf_bytes(rows, rows_per_page=3), max_rows=100)
    assert parsed.headers == SHORT_HEADERS
    assert [r["AWB_Number"] for r in parsed.rows] == [str(100000 + i) for i in range(1, 8)]


def test_pdf_without_a_table_and_a_broken_pdf():
    from reportlab.pdfgen import canvas
    out = io.BytesIO()
    c = canvas.Canvas(out)
    c.drawString(100, 750, "Just a letter, no table.")
    c.save()
    with pytest.raises(report_file.ReportFileError, match="no table was found in the PDF"):
        report_file.parse("letter.pdf", "application/pdf", out.getvalue(), max_rows=10)
    with pytest.raises(report_file.ReportFileError, match="not a readable PDF"):
        report_file.parse("x.pdf", "application/pdf", b"%PDF-1.4 not a pdf", max_rows=10)


def _words(*rows):
    """OCR words: (text, x0) per cell, 30px rows, 20px tall words, 10px per character."""
    return [report_file._Word(x0, x0 + 10 * len(text), 30 * r, 30 * r + 20, text)
            for r, row in enumerate(rows) for text, x0 in row]


def test_ocr_words_become_aligned_table_rows():
    rows = report_file.table_from_words(_words(
        [("Shipment report", 0)],                                                 # title line
        [("AWB", 0), ("Courier", 200), ("Status", 400), ("Pickup", 600), ("Date", 668)],
        [("1001", 0), ("Delhivery", 200), ("Delivered", 400), ("01-11-2026", 600)],
        [("1002", 0), ("RTO", 400)],                                              # courier and date missing
    ))
    assert rows == [["Shipment report"],
                    ["AWB", "Courier", "Status", "Pickup Date"],                 # close words are one cell
                    ["1001", "Delhivery", "Delivered", "01-11-2026"],
                    ["1002", None, "RTO", None]]


def test_parses_a_table_screenshot():
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (1200, 200), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default(size=22)
    lines = [SHORT_HEADERS] + [[str(r[0]), r[1], r[2], r[3]] for r in SHORT_ROWS[:2]]
    for r, line in enumerate(lines):
        for c, text in enumerate(line):
            draw.text((20 + c * 300, 20 + r * 50), text, fill="black", font=font)
    out = io.BytesIO()
    img.save(out, "PNG")
    parsed = report_file.parse("screenshot.png", "image/png", out.getvalue(), max_rows=10)
    # OCR may drop "_"; columns are matched on letters and digits only, so that is fine.
    def letters(names):
        return [re.sub(r"[^a-z0-9]", "", h.lower()) for h in names]
    assert letters(parsed.headers) == letters(SHORT_HEADERS)
    assert len(parsed.rows) == 2
    assert list(parsed.rows[1].values()) == ["100002", "Xpressbees", "RTO", "MUMBAI"]


def test_unreadable_image():
    with pytest.raises(report_file.ReportFileError, match="not a readable image"):
        report_file.parse("x.png", "image/png", b"\x89PNG\r\n\x1a\n broken", max_rows=10)


def test_unreadable_files_raise_readable_errors():
    with pytest.raises(report_file.ReportFileError, match="not a readable Excel file"):
        report_file.parse("a.xlsx", XLSX, b"PK\x03\x04 broken zip", max_rows=10)
    with pytest.raises(report_file.ReportFileError, match="no header row"):
        report_file.parse("a.csv", "text/csv", b"only,two\n", max_rows=10)


def test_report_time_from_the_file_name():
    assert sources.report_time("Unified_FM_report_22-10-2026_13-30-11.xlsx") == datetime(2026, 10, 22, 13, 30, 11)
    assert sources.report_time("Unified_FM_report_21-11-2026 13-30-07.xlsx") == datetime(2026, 11, 21, 13, 30, 7)
    assert sources.report_time("no_date.xlsx") is None and sources.report_time(None) is None


# ---- mapping and filters --------------------------------------------------------------------

def test_first_mile_columns_map_into_the_allow_list_without_pii():
    row = aggregate.pick(dict(zip(FM_HEADERS, fm_row(7, status="Not Picked", city="THANE"))))
    assert row["awb"] == 100007 and row["order_id"] == 9007
    assert row["order_date"] == f"{FROM} 10:00:00" and row["status"] == "Not Picked"
    assert row["courier"] == "Delhivery 10KG" and row["payment_mode"] == "Prepaid"
    assert row["origin_city"] == "THANE" and row["origin_state"] == "DELHI"
    assert ADDRESS not in row.values() and LANDMARK not in row.values()
    assert set(row) == set(aggregate.FIELDS)


@pytest.mark.parametrize("ui, value, expected", [
    ("Pickup Scheduled", "Pickup Scheduled", True), ("Pickup Scheduled", "Not Picked", False),
    ("Not Picked", "Not Picked", True), ("In-Transit", "In Transit", True), ("In-Transit", "RTO In Transit", False),
    ("Out For Delivery", "Out for Delivery", True), ("Delivered", "Delivered", True),
    ("Delivered", "RTO Delivered", False), ("Undelivered", "Undelivered", True),
])
def test_ui_statuses_match_file_status_values(ui, value, expected):
    assert aggregate.status_matches(ui, value) is expected


def test_row_filters_for_seller_status_and_dates():
    body = GenerateRequest(verticalId="2", ReportCategoryId=7, prompt="x", statuses=["Not Picked"],
                           fromDate=FROM, toDate=TO, customerCode="1234567")
    base = {"customer_code": "1234567", "status": "Not Picked", "order_date": f"{TO} 09:00:00"}
    assert sources.matches(base, body)
    assert not sources.matches({**base, "customer_code": "999"}, body)
    assert not sources.matches({**base, "status": "Pickup Scheduled"}, body)
    assert not sources.matches({**base, "order_date": f"{TODAY.isoformat()} 09:00:00"}, body)
    assert sources.matches({**base, "order_date": None}, body)     # rows without a date are kept


def test_file_rows_dedupes_repeated_shipments():
    body = GenerateRequest(verticalId="2", ReportCategoryId=7, prompt="x", fromDate=FROM, toDate=TO)
    # The same shipment (orderId + AWB) twice, as overlapping report rows would repeat it.
    parsed = report_file.parse("f.xlsx", XLSX, xlsx(FM_HEADERS, [fm_row(1, status="Not Picked"), fm_row(2),
                                                                 fm_row(1)]), max_rows=100)
    found = sources.file_rows(parsed, body, cap=100, file_name="f.xlsx")
    assert found.read == 3 and found.matching == 2
    assert [r["status"] for r in found.rows] == ["Not Picked", "Pickup Scheduled"]   # first occurrence kept


# ---- POST with a file -----------------------------------------------------------------------

def test_date_and_status_filters_apply_to_file_rows(env):
    client, llm, _ = env
    out_of_range = (date.fromisoformat(FROM) - timedelta(days=3)).isoformat()
    content = xlsx(FM_HEADERS, [fm_row(1), fm_row(2, status="Not Picked", city="THANE"),
                                fm_row(3, status="Not Picked", day=out_of_range)])
    res = post(client, payload(statuses=["Not Picked"]), file=(FILE_NAME, content, XLSX))
    assert res.status_code == 200
    _, source, data = llm_parts(llm)
    assert source["rows_in_file"] == 3 and source["rows_after_filters"] == 1
    assert data["by_status"] == [{"status": "Not Picked", "shipments": 1, "pct": 100.0}]
    assert data["as_of"] == f"{END.isoformat()} 13:30"
    scope = res.json()["scope"]
    assert scope["records_analyzed"] == 1 and scope["source"] == f"{FILE_NAME} (#4408)"
    assert scope["summary"] == f"Snapshot as of {END:%d %b %Y} 13:30. Statuses: Not Picked. All sellers."


def test_csv_file_with_customer_code_column_filters_to_the_seller(env):
    client, llm, _ = env
    headers = ["AWB_Number", "Customer_Code", "Courier", "Order_Tracking_Status", "AWB_Generation_Date",
               "Consignee_Phone"]
    rows = [[1, "1234567", "Ekart", "Not Picked", f"{FROM} 10:00:00", "9876543210"],
            [2, "7654321", "Ekart", "Not Picked", f"{FROM} 10:00:00", "9876543211"]]
    csv_bytes = "\r\n".join(",".join(map(str, r)) for r in [headers, *rows]).encode()
    name = FILE_NAME.replace(".xlsx", ".csv")
    res = post(client, payload(customerCode="1234567", sourceReportName=name), file=(name, csv_bytes, "text/csv"))
    assert res.status_code == 200
    text, source, data = llm_parts(llm)
    assert source["type"] == "report_file" and data["metrics"]["total_shipments"] == 1
    assert "9876543210" not in text and "1234567" not in text.split("DATA:\n", 1)[1]


def test_seller_filter_on_a_file_without_customer_column(env):
    client, llm, _ = env
    res = post(client, payload(customerCode="1234567"))
    assert res.status_code == 422
    assert res.json() == {"message": "The First Mile report file has no customer code column, so it cannot be "
                                     "filtered to seller 1234567. Turn off the seller filter and try again."}
    assert llm.requests == []


@pytest.mark.parametrize("file, reason", [
    (("report.pdf", b"%PDF-1.4 not a sheet", "application/pdf"), "is not a readable PDF"),
    (("notes.txt", b"just some text", "text/plain"), "is not a supported file"),
    ((FILE_NAME, b"PK\x03\x04 broken", XLSX), "is not a readable Excel file"),
    ((FILE_NAME, b"", XLSX), "The attached report file is empty."),
])
def test_unusable_file_is_422(env, file, reason):
    client, llm, _ = env
    res = post(client, payload(), file=file)
    assert res.status_code == 422 and reason in res.json()["message"]
    assert llm.requests == []


def test_file_too_large_is_413():
    client, llm, _ = make_env(MAX_UPLOAD_MB=0.001)
    try:
        res = post(client, payload(), file=(FILE_NAME, b"PK" + b"x" * 5000, XLSX))
    finally:
        client.__exit__(None, None, None)
    assert res.status_code == 413 and res.json() == {"message": "The report file is larger than 0.001 MB."}
    assert llm.requests == []


def test_real_first_mile_file_if_present(env):
    """Optional: the real export used to design this pipeline, when it is on this machine."""
    path = Path.home() / "Downloads" / "Unified_FM_report_22-10-2026_13-30-11.xlsx"
    if not path.exists():
        pytest.skip("real First Mile export not available")
    client, llm, _ = env
    body = payload(sourceReportName=path.name, fromDate=max(date(2026, 10, 1), TODAY - timedelta(days=150)).isoformat())
    res = post(client, body, file=(path.name, path.read_bytes(), XLSX))
    if res.status_code == 400:
        pytest.skip(res.json()["message"])          # the file's dates are outside the allowed window by now
    assert res.status_code == 200 and res.json()["scope"]["records_analyzed"] > 0
    _, source, data = llm_parts(llm)
    assert source["rows_in_file"] == 6851
    assert {r["status"] for r in data["by_status"]} == {"Pickup Scheduled", "Not Picked"}
