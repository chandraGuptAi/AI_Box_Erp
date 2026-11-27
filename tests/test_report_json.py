import math
from datetime import date, datetime

from app.ai_insights import aggregate, llm, report
from app.ai_insights.schemas import GenerateRequest
from app.ai_insights.service import IST
from tests.test_ai_insights import (FILE_NAME, FM_HEADERS, XLSX, fm_row, make_env, no_other_http, payload,  # noqa: F401
                                    post, xlsx)

UNITS = {"number", "percent", "text"}
TONES = {"ok", "bad", "warning", "normal"}
CHART_TYPES = {"bar", "horizontal_bar", "line", "area", "pie", "doughnut"}
FORMATS = {"number", "percent", "currency"}


def assert_report_shape(r):
    assert set(r) == {"title", "subtitle", "generated_at", "scope", "executive_summary", "metrics", "charts",
                      "tables", "findings", "recommendations", "limitations"}
    assert isinstance(r["title"], str) and r["title"] and isinstance(r["subtitle"], str)
    assert datetime.fromisoformat(r["generated_at"]).utcoffset().total_seconds() == 5.5 * 3600
    assert set(r["scope"]) == {"period", "source", "records_analyzed", "summary"}
    assert isinstance(r["scope"]["records_analyzed"], int)
    assert all(isinstance(p, str) for k in ("paragraphs", "highlights") for p in r["executive_summary"][k])
    for m in r["metrics"]:
        assert {"key", "label", "value", "unit"} <= set(m) and m["unit"] in UNITS
        assert "tone" not in m or m["tone"] in TONES
        assert isinstance(m["value"], (int, float)) and not math.isnan(m["value"])
    for c in r["charts"]:
        assert c["type"] in CHART_TYPES and c["labels"] and all(isinstance(x, str) for x in c["labels"])
        assert c.get("value_format", {"type": "number"})["type"] in FORMATS
        for d in c["datasets"]:
            assert len(d["data"]) == len(c["labels"])
            assert all(isinstance(v, (int, float)) and not isinstance(v, bool) and not math.isnan(v)
                       for v in d["data"])
    for t in r["tables"]:
        assert all(isinstance(cell, str) for row in t["rows"] for cell in row)
        assert all(len(row) == len(t["columns"]) for row in t["rows"])
    assert all(f["severity"] in {"high", "medium", "low", "info"} and f["title"] and f["detail"]
               for f in r["findings"])
    assert all(rec["action"] and rec.get("priority", "high") in {"high", "medium", "low"}
               for rec in r["recommendations"])
    for key in ("metrics", "charts", "tables", "findings", "recommendations", "limitations"):
        assert isinstance(r[key], list) and None not in r[key]


def test_endpoint_returns_the_report_shape():
    client, llm, _ = make_env()
    try:
        content = xlsx(FM_HEADERS, [fm_row(1), fm_row(2, status="Not Picked", city="THANE"),
                                    fm_row(3, courier="Ekart Surface")])
        res = post(client, payload(), file=(FILE_NAME, content, XLSX))
    finally:
        client.__exit__(None, None, None)
    assert res.status_code == 200
    r = res.json()
    assert_report_shape(r)
    # A First Mile file has no delivered / RTO / destination / failure data: those parts are left out.
    assert [m["key"] for m in r["metrics"]] == ["total_shipments", "in_progress"]
    assert {c["id"] for c in r["charts"]} == {"status", "courier", "origin_city", "trend", "open_age",
                                              "payment_mode"}
    courier = next(c for c in r["charts"] if c["id"] == "courier")
    assert courier["type"] == "horizontal_bar" and [d["label"] for d in courier["datasets"]] == ["Shipments"]
    assert courier["labels"] == ["Delhivery 10KG", "Ekart Surface"] and courier["datasets"][0]["data"] == [2, 1]
    trend = next(c for c in r["charts"] if c["id"] == "trend")
    assert trend["wide"] is True and [d["label"] for d in trend["datasets"]] == ["Shipments"]
    assert r["limitations"] == []


# ---- from aggregates (fixed input -> exact values) ---------------------------------------------

def row(status, courier="Delhivery", city="Pune", day="2026-10-02", reason=None, paid="COD",
        picked=None, delivered=None):
    return {"status": status, "courier": courier, "dest_city": city, "dest_state": "Maharashtra",
            "origin_city": None, "order_date": f"{day} 10:00:00", "payment_mode": paid, "remark": reason,
            "cancel_reason": None, "pickup_date": picked, "delivery_date": delivered}


BODY = GenerateRequest(verticalId="2", ReportCategoryId=3, prompt="x", fromDate=date(2026, 10, 1),
                       toDate=date(2026, 10, 31))
SOURCE = {"file": "Forward_report.csv", "ReportHistoryId": "12", "rows_after_filters": 5, "notes": []}
ANALYSIS = llm._clean({"title": "", "summary_paragraphs": ["ok"]})


def build(rows, as_of=None, source=SOURCE, body=BODY):
    data = aggregate.build(rows, total_matching=len(rows), sample_size=5, as_of=as_of)
    return report.build(category_name="Forward", body=body, data=data, analysis=ANALYSIS, source=source,
                        as_of=as_of, generated_at=datetime(2026, 11, 21, 15, 42, 10, tzinfo=IST))


def test_metrics_and_charts_are_computed_from_the_aggregates():
    rows = [row("Delivered", picked="2026-10-02 12:00:00", delivered="2026-10-04 12:00:00"),
            row("Delivered", picked="2026-10-02 12:00:00", delivered="2026-10-06 12:00:00"),
            row("RTO Delivered", courier="Ekart", reason="Customer refused"),
            row("Undelivered", courier="Ekart", city="Nagpur", day="2026-10-03", reason="Address not found"),
            row("In Transit", paid="Prepaid", day="2026-10-03")]
    r = build(rows)
    assert r["title"] == "Forward Report" and r["subtitle"] == "Forward · AI insights"
    assert r["metrics"] == [
        {"key": "total_shipments", "label": "Total Shipments", "value": 5, "unit": "number"},
        {"key": "delivery_pct", "label": "Delivery %", "value": 40.0, "unit": "percent", "tone": "ok",
         "note": "2 delivered"},
        {"key": "rto_pct", "label": "RTO %", "value": 20.0, "unit": "percent", "tone": "bad", "note": "1 RTO"},
        {"key": "undelivered_pct", "label": "Undelivered %", "value": 20.0, "unit": "percent", "tone": "bad",
         "note": "1 undelivered"},
        {"key": "in_progress", "label": "In Progress", "value": 1, "unit": "number", "tone": "warning",
         "note": "not yet delivered, returned, cancelled or lost"},
        {"key": "avg_delivery_days", "label": "Avg Delivery Days", "value": 3.0, "unit": "number",
         "note": "pickup to delivery"},
    ]
    charts = {c["id"]: c for c in r["charts"]}
    assert set(charts) == {"status", "courier", "destination_city", "trend", "payment_mode", "failure_reasons"}
    assert charts["status"]["type"] == "doughnut" and sum(charts["status"]["datasets"][0]["data"]) == 5
    assert charts["courier"]["labels"] == ["Delhivery", "Ekart"]
    assert charts["courier"]["datasets"] == [{"label": "Shipments", "data": [3, 2]},
                                             {"label": "Delivered", "data": [2, 0]},
                                             {"label": "RTO", "data": [0, 1]}]
    assert charts["trend"]["labels"] == ["2026-10-02", "2026-10-03"]
    assert charts["trend"]["datasets"] == [{"label": "Shipments", "data": [3, 2]},
                                           {"label": "Delivered", "data": [2, 0]},
                                           {"label": "RTO", "data": [1, 0]}]
    assert charts["payment_mode"]["type"] == "pie" and charts["payment_mode"]["labels"] == ["COD", "Prepaid"]
    assert sorted(charts["failure_reasons"]["labels"]) == ["Address not found", "Customer refused"]
    assert r["scope"] == {"period": "01 Oct 2026 – 31 Oct 2026", "source": "Forward_report.csv (#12)",
                          "records_analyzed": 5, "summary": "Snapshot time unknown. All statuses. All sellers."}
    assert r["limitations"] == ["The file name has no generation time, so the age of open shipments is not shown."]
    assert_report_shape(r)


def test_limitations_carry_the_file_notes_and_truncation():
    rows = [row("In Transit")]
    data = aggregate.build(rows, total_matching=40, sample_size=5, as_of=datetime(2026, 10, 5))
    notes = ["f.csv has more than 1 rows; only the first 1 were read."]
    r = report.build(category_name="Forward", body=BODY, data=data, analysis=ANALYSIS,
                     source={**SOURCE, "notes": notes}, as_of=datetime(2026, 10, 5),
                     generated_at=datetime(2026, 11, 21, tzinfo=IST))
    assert r["limitations"] == [notes[0], "Only 1 of 40 matching shipments were analysed."]
    assert "open_age" in {c["id"] for c in r["charts"]}


# ---- LLM output mapping --------------------------------------------------------------------------

def test_llm_output_mapping_drops_malformed_items():
    out = llm._clean({
        "title": "T",
        "summary_paragraphs": "one paragraph",
        "highlights": ["a", None, "", {"x": 1}, 5],
        "findings": [{"title": "Backlog", "detail": "412 old", "severity": "HIGH"},
                     {"title": "Odd", "detail": "d", "severity": "critical"},
                     {"title": "No detail"}, "plain string", None],
        "recommendations": [{"action": "Escalate", "rationale": "r", "priority": "high"},
                            {"action": "Watch", "priority": "urgent"},
                            {"rationale": "no action"}, "text"],
        "tables": [{"title": "t", "columns": ["a", "b"], "rows": [["1"], ["2", 3], "bad"], "note": "n"},
                   {"title": "no rows", "columns": ["a"], "rows": []}],
    })
    assert out["summary_paragraphs"] == ["one paragraph"]
    assert out["highlights"] == ["a", "5"]
    assert out["findings"] == [{"title": "Backlog", "detail": "412 old", "severity": "high"},
                               {"title": "Odd", "detail": "d", "severity": "info"}]
    assert out["recommendations"] == [{"action": "Escalate", "rationale": "r", "priority": "high"},
                                      {"action": "Watch", "rationale": ""}]
    assert out["tables"] == [{"title": "t", "columns": ["a", "b"], "rows": [["1", ""], ["2", "3"]], "note": "n"}]


def test_empty_llm_analysis_is_502():
    client, fake, _ = make_env()
    fake.answer = {"title": "x", "summary_paragraphs": [], "findings": [{"title": "no detail"}]}
    try:
        res = post(client, payload())
    finally:
        client.__exit__(None, None, None)
    assert res.status_code == 502 and res.json() == {"message": "The AI model returned an empty analysis."}
