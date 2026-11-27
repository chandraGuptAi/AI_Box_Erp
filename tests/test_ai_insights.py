import io
import json
import re
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from app.ai_insights.service import AiInsightsService, months_before, now_ist
from app.config import Settings
from app.main import create_app

TOKEN = "Bearer user-token"
TODAY = now_ist().date()
FROM, TO = (TODAY - timedelta(days=10)).isoformat(), (TODAY - timedelta(days=1)).isoformat()
END = date.fromisoformat(TO)
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# Headers of the real Unified First Mile report (Unified_FM_report_22-10-2026_13-30-11.xlsx).
FM_HEADERS = ["UnifiedFMReportId", "orderId", "Pickup_Warehouse_Id", "Payment_Mode", "Courier", "AWB_Number",
              "AWB_Generation_Date", "Order_Tracking_Status", "Warehouse_Address", "Warehouse_City",
              "Warehouse_State", "Warehouse_Pincode", "Warehouse_Landmark"]
ADDRESS, LANDMARK = "Plot 12, Industrial Area Phase 2", "Near Old Water Tank"
FILE_NAME = f"Unified_FM_report_{END:%d-%m-%Y} 13-30-07.xlsx"


def fm_row(i: int, status: str = "Pickup Scheduled", day: str = FROM, city: str = "DELHI",
           courier: str = "Delhivery 10KG"):
    return [i, 9000 + i, f"WH{i}", "Prepaid", courier, 100000 + i, f"{day} 10:00:00", status,
            ADDRESS, city, "DELHI", 110001, LANDMARK]


def xlsx(headers, rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for row in rows:
        ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


FM_FILE = (FILE_NAME, xlsx(FM_HEADERS, [fm_row(1), fm_row(2, status="Not Picked", city="THANE"),
                                        fm_row(3, courier="Ekart Surface")]), XLSX)


ANSWER = {"title": "First Mile pickups",
          "summary_paragraphs": ["2 of 3 shipments are pickup scheduled."],
          "highlights": ["1 shipment not picked"],
          "findings": [{"title": "Thane backlog", "detail": "Thane has the only Not Picked shipment.",
                        "severity": "medium"}],
          "recommendations": [{"action": "Follow up pickups in Thane.", "rationale": "1 Not Picked",
                               "priority": "high"}],
          "tables": [{"title": "Couriers", "columns": ["Courier", "Shipments"],
                      "rows": [["Delhivery 10KG", "2"], ["Ekart Surface", "1"]]}]}


# Token usage as DeepSeek reports it (prompt_cache_hit_tokens = prompt tokens served from its cache).
USAGE = {"prompt_tokens": 12000, "completion_tokens": 900, "total_tokens": 12900,
         "prompt_cache_hit_tokens": 2000, "prompt_cache_miss_tokens": 10000}


class FakeLlm:
    def __init__(self, fail=False):
        self.fail, self.requests, self.answer = fail, [], dict(ANSWER)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        if self.fail:
            return httpx.Response(500, json={"error": "overloaded"})
        return httpx.Response(200, json={"model": "deepseek-chat", "usage": USAGE,
                                         "choices": [{"message": {"content": json.dumps(self.answer)}}]})


def make_env(**overrides):
    llm = FakeLlm()
    settings = Settings(**{"LLM_API_KEY": "test-key", "SERVICE_TOKEN": "", **overrides})
    app = create_app()
    client = TestClient(app)
    client.__enter__()
    app.state.ai_insights = AiInsightsService(settings, llm_transport=httpx.MockTransport(llm))
    return client, llm, app


@pytest.fixture
def env():
    client, llm, app = make_env()
    yield client, llm, app
    client.__exit__(None, None, None)


@pytest.fixture(autouse=True)
def no_other_http(monkeypatch):
    """This service calls no ERP API: the only outgoing HTTP request allowed is the (mocked) LLM call."""
    real_send = httpx.AsyncClient.send

    async def guarded(self, request, *args, **kwargs):
        assert request.url.path == "/chat/completions", f"unexpected outgoing request: {request.url}"
        return await real_send(self, request, *args, **kwargs)
    monkeypatch.setattr(httpx.AsyncClient, "send", guarded)


def payload(**overrides):
    body = {"verticalId": "2", "ReportCategoryId": 7, "ReportCategoryName": "First Mile",
            "prompt": "  Analyse first mile pickups  ", "statuses": [], "fromDate": FROM, "toDate": TO,
            "customerCode": None, "sourceReportId": "4408", "sourceReportName": FILE_NAME}
    body.update(overrides)
    return body


def post(client, body, file=FM_FILE, token=TOKEN, raw_request=None):
    """multipart/form-data like the panel: `request` (JSON text) and the optional `file` (name, bytes, type)."""
    headers = {"Authorization": token} if token else {}
    data = {"request": raw_request if raw_request is not None else json.dumps(body)}
    files = {"file": file} if file else None
    return client.post("/api/ai-box-erp/generate", data=data, files=files, headers=headers)


def llm_parts(llm):
    text = llm.requests[0]["messages"][1]["content"]
    source = json.loads(text.split("DATA SOURCE: ", 1)[1].split("\n", 1)[0])
    return text, source, json.loads(text.split("DATA:\n", 1)[1])


# ---- validation ------------------------------------------------------------------------------

@pytest.mark.parametrize("overrides, status, message", [
    ({"prompt": "   "}, 422, "Please write a prompt for the AI."),
    ({"prompt": "x" * 2001}, 400, "at most 2000 characters"),
    ({"ReportCategoryId": "abc"}, 422, "Report type must be a number."),
    ({"statuses": ["Shipped"]}, 422, "Unknown shipment status: Shipped"),
    ({"fromDate": TO, "toDate": FROM}, 400, "From date must be on or before To date."),
    ({"toDate": (TODAY + timedelta(days=1)).isoformat()}, 400, "To date cannot be in the future."),
    ({"fromDate": (months_before(TODAY, 6) - timedelta(days=1)).isoformat()}, 400, "older than 6 months"),
    ({"fromDate": "01-10-2026"}, 422, "From date must be a date in yyyy-MM-dd format."),
    ({"customerCode": "1234567"}, 422, "has no customer code column"),
])
def test_rejects_bad_input_with_readable_message(env, overrides, status, message):
    client, llm, _ = env
    res = post(client, payload(**overrides))
    assert res.status_code == status
    assert message in res.json()["message"]
    assert llm.requests == []


@pytest.mark.parametrize("raw, message", [
    ("{not json", "The request field must be valid JSON."),
    ("[1, 2]", "The request field must be a JSON object."),
])
def test_rejects_bad_request_json(env, raw, message):
    client, _, _ = env
    res = post(client, None, raw_request=raw)
    assert res.status_code == 422 and res.json() == {"message": message}


def test_missing_fields_are_required(env):
    client, _, _ = env
    body = payload()
    del body["prompt"]
    assert post(client, body).json() == {"message": "Prompt is required."}
    res = client.post("/api/ai-box-erp/generate", data={}, headers={"Authorization": TOKEN})
    assert res.status_code == 422 and res.json() == {"message": "Request is required."}


def test_requires_panel_login(env):
    client, llm, _ = env
    res = post(client, payload(), token=None)
    assert res.status_code == 401 and "log in" in res.json()["message"]
    assert llm.requests == []


def test_without_a_file_says_what_to_do(env):
    client, llm, _ = env
    res = post(client, payload(sourceReportId=None, sourceReportName=None), file=None)
    from_, to = (f"{d[8:10]}-{d[5:7]}-{d[:4]}" for d in (FROM, TO))
    assert res.status_code == 422
    assert res.json() == {"message": f"No completed First Mile report was found for {from_} to {to}. Generate the "
                                     f"First Mile report in the ERP first, or choose dates that include one."}
    assert llm.requests == []


def test_no_rows_matching_the_filters(env):
    client, llm, _ = env
    res = post(client, payload(statuses=["Delivered"]))
    assert res.status_code == 422
    assert res.json()["message"].startswith(f"None of the 3 rows in {FILE_NAME} match")
    assert llm.requests == []


def test_oldest_allowed_from_date_is_accepted(env):
    client, _, _ = env
    assert post(client, payload(fromDate=months_before(TODAY, 6).isoformat())).status_code == 200


def test_llm_key_missing_is_503_before_any_work():
    client, llm, _ = make_env(LLM_API_KEY="")
    try:
        res = post(client, payload())
    finally:
        client.__exit__(None, None, None)
    assert res.status_code == 503 and res.json() == {"message": "AI is not configured on the server."}
    assert llm.requests == []


def test_llm_failure_is_502_without_retry(env):
    client, llm, _ = env
    llm.fail = True
    res = post(client, payload())
    assert res.status_code == 502 and "AI model" in res.json()["message"]
    assert len(llm.requests) == 1


def test_busy_when_no_slot_frees_up():
    client, llm, app = make_env(MAX_CONCURRENT_JOBS=1, SLOT_WAIT_SECONDS=0.05)
    try:
        service = app.state.ai_insights
        client.portal.call(service._slots.acquire)           # another report holds the only slot
        res = post(client, payload())
        client.portal.call(service._slots.release)
        after = post(client, payload())
    finally:
        client.__exit__(None, None, None)
    assert res.status_code == 429
    assert res.json() == {"message": "The AI is busy with other reports. Please try again in a minute."}
    assert after.status_code == 200 and len(llm.requests) == 1


# ---- success ---------------------------------------------------------------------------------

def test_returns_the_json_report_built_from_the_attached_file(env):
    client, llm, _ = env
    res = post(client, payload())

    assert res.status_code == 200
    assert res.headers["content-type"] == "application/json"
    assert res.headers["cache-control"] == "no-store"
    report = res.json()
    assert report["title"] == "First Mile pickups"
    assert report["subtitle"] == "First Mile · AI insights"
    assert report["scope"]["source"] == f"{FILE_NAME} (#4408)"
    assert report["scope"]["records_analyzed"] == 3
    assert report["executive_summary"] == {"paragraphs": ["2 of 3 shipments are pickup scheduled."],
                                           "highlights": ["1 shipment not picked"]}
    assert report["findings"][0]["severity"] == "medium" and report["recommendations"][0]["priority"] == "high"
    assert report["tables"][0]["title"] == "Couriers"
    text, source, data = llm_parts(llm)
    assert source == {"type": "report_file", "file": FILE_NAME, "ReportHistoryId": "4408",
                      "generated": f"{END:%d-%m-%Y} 13:30", "rows_in_file": 3, "rows_after_filters": 3,
                      "notes": []}
    assert data["metrics"]["total_shipments"] == 3
    assert {r["status"]: r["shipments"] for r in data["by_status"]} == {"Pickup Scheduled": 2, "Not Picked": 1}
    assert {r["name"] for r in data["by_origin_city"]} == {"Delhi", "Thane"}
    assert ADDRESS not in text and LANDMARK not in text and ADDRESS not in res.text
    request = llm.requests[0]
    assert request["response_format"] == {"type": "json_object"}
    assert "never invent" in request["messages"][0]["content"].lower()
    assert "REPORT TYPE: First Mile" in text


def test_category_name_falls_back_to_the_id(env):
    client, llm, _ = env
    llm.answer = {**ANSWER, "title": ""}
    res = post(client, payload(ReportCategoryName=None))
    assert res.status_code == 200
    assert res.json()["title"] == "Report type 7 Report"
    assert res.json()["subtitle"] == "Report type 7 · AI insights"
    assert "REPORT TYPE: Report type 7" in llm.requests[0]["messages"][1]["content"]
