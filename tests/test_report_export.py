"""PDF / Word export of AI reports: built from a real generated report (fake LLM), owner-only for saved reports."""
import io
import json

import pytest
from docx import Document

from app import report_export
from tests.test_ai_insights import FM_FILE, TOKEN, make_env, payload
from tests.test_saved_reports import MemoryStore, headers


@pytest.fixture
def env():
    client, llm, app = make_env()
    app.state.saved = MemoryStore()
    yield client, app.state.saved
    client.__exit__(None, None, None)


@pytest.fixture
def report(env):
    client, _ = env
    res = client.post("/api/ai-box-erp/generate", data={"request": json.dumps(payload())},
                      files={"file": FM_FILE}, headers={"Authorization": TOKEN})
    assert res.status_code == 200, res.text
    body = res.json()
    body["title"] = "RTO % by City — 21-11-2026 ₹"   # non-ASCII must survive both formats
    return body


@pytest.mark.parametrize("kind", ["bar", "horizontal_bar", "line", "area", "pie", "doughnut", "donut"])
def test_every_chart_type_draws_a_png(kind):
    chart = {"title": "T", "type": kind, "labels": ["A", "B", "C"],
             "datasets": [{"label": "Shipments", "data": [3, 1, 2]}, {"label": "RTO", "data": [1, 0, 1]}]}
    png = report_export.chart_png(chart)
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("chart", [
    {"type": "radar", "labels": ["A"], "datasets": [{"data": [1]}]},
    {"type": "bar", "labels": ["A", "B"], "datasets": [{"data": [1]}]},       # length mismatch
    {"type": "bar", "labels": ["A"], "datasets": [{"data": ["x"]}]},          # not numbers
    {"type": "bar", "labels": [], "datasets": []},
])
def test_undrawable_charts_are_skipped(chart):
    assert report_export.chart_png(chart) is None


def test_file_name():
    assert report_export.file_name("RTO % by City — 21-11-2026", "pdf") == "RTO_by_City_-_21-11-2026.pdf"
    assert report_export.file_name("", "docx") == "AI_report.docx"


def test_pdf_is_built_from_the_report(report):
    content, name, media = report_export.render(report, "pdf")
    assert content[:5] == b"%PDF-" and len(content) > 5000
    assert media == "application/pdf" and name.endswith(".pdf")


def test_docx_has_the_report_sections_tables_and_charts(report):
    content, name, media = report_export.render(report, "docx")
    assert content[:2] == b"PK" and name.endswith(".docx")
    doc = Document(io.BytesIO(content))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert report["title"] in text
    assert "Key Metrics" in text and "Executive Summary" in text and "Recommendations" in text
    charts = [c for c in report.get("charts") or [] if report_export.chart_png(c)]
    assert len(doc.inline_shapes) == len(charts) > 0
    metric_table = doc.tables[0]
    assert [c.text for c in metric_table.rows[0].cells] == ["Metric", "Value", "Note"]
    assert len(metric_table.rows) == len(report_export._metrics(report)) + 1


@pytest.mark.parametrize("fmt, media, magic", [("pdf", "application/pdf", b"%PDF-"), ("docx", report_export.FORMATS["docx"], b"PK")])
def test_export_endpoint_downloads_the_file(env, report, fmt, media, magic):
    client, _ = env
    res = client.post(f"/api/ai-box-erp/export?format={fmt}", json={"report": report}, headers={"Authorization": TOKEN})
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == media
    disposition = res.headers["content-disposition"]
    assert disposition.startswith("attachment;") and f".{fmt}" in disposition and "filename*=UTF-8''" in disposition
    assert res.content[: len(magic)] == magic


@pytest.mark.parametrize("url, body, status", [
    ("/api/ai-box-erp/export?format=xls", {"report": {"title": "T"}}, 422),
    ("/api/ai-box-erp/export?format=pdf", {"report": {"x": 1}}, 422),
])
def test_export_rejects_bad_requests(env, url, body, status):
    client, _ = env
    res = client.post(url, json=body, headers={"Authorization": TOKEN})
    assert res.status_code == status and res.json()["message"]


def test_export_needs_login(env, report):
    client, _ = env
    assert client.post("/api/ai-box-erp/export?format=pdf", json={"report": report}).status_code == 401


def test_saved_report_export_is_owner_only(env, report):
    client, _ = env
    body = {"verticalId": "2", "ReportCategoryId": 7, "ReportCategoryName": "First Mile", "prompt": "x",
            "filters": {}, "report": report}
    saved = client.post("/api/ai-box-erp/reports", json=body, headers=headers("101", "Bearer token-a")).json()
    url = f"/api/ai-box-erp/reports/{saved['id']}/export?format=docx"
    assert client.get(url, headers=headers("202", "Bearer token-b")).status_code == 404
    own = client.get(url, headers=headers("101", "Bearer token-a"))
    assert own.status_code == 200 and own.content[:2] == b"PK"
