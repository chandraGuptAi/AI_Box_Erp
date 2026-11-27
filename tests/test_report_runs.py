"""Every generate request is logged in report_runs (tokens, cost) for history and billing; a saved report is
linked to its run. Uses the in-memory store; test_saved_reports_pg.py covers Postgres."""
import httpx
import pytest

from app.ai_insights import llm
from app.ai_insights.service import AiInsightsService
from app.config import Settings
from tests.test_ai_insights import FILE_NAME, USAGE, FakeLlm, TOKEN, make_env, payload, post
from tests.test_saved_reports import MemoryStore

PRICES = {"LLM_PRICE_INPUT_PER_MTOK": 0.27, "LLM_PRICE_CACHED_INPUT_PER_MTOK": 0.07,
          "LLM_PRICE_OUTPUT_PER_MTOK": 1.10}


def runs_env(fail=False, store=None, **settings):
    client, _, app = make_env()
    fake = FakeLlm(fail=fail)
    service = AiInsightsService(Settings(LLM_API_KEY="test-key", SERVICE_TOKEN="", **settings),
                                llm_transport=httpx.MockTransport(fake), store=store or MemoryStore())
    app.state.ai_insights = service
    app.state.saved = service.store
    return client, fake, service.store


def gen(client, user="101", **overrides):
    headers = {"Authorization": TOKEN, "X-User-Id": user}
    body = payload(**overrides)
    import json
    from tests.test_ai_insights import FM_FILE
    return client.post("/api/ai-box-erp/generate", data={"request": json.dumps(body)}, files={"file": FM_FILE},
                       headers=headers)


def test_a_report_is_logged_with_tokens_and_cost():
    client, _, store = runs_env(**PRICES)
    try:
        res = gen(client)
    finally:
        client.__exit__(None, None, None)
    assert res.status_code == 200
    run = store.runs[res.json()["run_id"]]
    assert run["status"] == "ok" and run["http_status"] == 200 and run["error"] is None
    assert run["user_id"] == "101" and run["vertical_id"] == "2" and run["category_id"] == 7
    assert run["prompt"] == "Analyse first mile pickups"
    assert run["source"]["file"] == FILE_NAME and run["source"]["ReportHistoryId"] == "4408"
    assert run["source"]["kind"] == "xlsx" and run["source"]["bytes"] > 0
    assert run["source"]["rows_after_filters"] == 3
    assert run["report_title"] and run["records_analyzed"] == 3
    assert (run["llm_provider"], run["llm_model"], run["llm_calls"]) == ("api.deepseek.com", "deepseek-chat", 1)
    assert (run["prompt_tokens"], run["cached_prompt_tokens"], run["completion_tokens"], run["total_tokens"]) == \
        (12000, 2000, 900, 12900)
    # 10,000 fresh prompt tokens at 0.27 + 2,000 cached at 0.07 + 900 output at 1.10, per million.
    assert run["cost"] == pytest.approx(0.00270 + 0.00014 + 0.00099)
    assert run["cost_currency"] == "USD" and run["prices"]["output_per_mtok"] == 1.10
    assert run["duration_ms"] >= run["llm_ms"] >= 0


def test_without_prices_the_tokens_are_kept_and_the_cost_is_unknown():
    client, _, store = runs_env()
    try:
        res = gen(client)
    finally:
        client.__exit__(None, None, None)
    run = store.runs[res.json()["run_id"]]
    assert run["total_tokens"] == 12900 and run["cost"] is None and run["prices"] is None


def test_failed_llm_call_is_logged_as_a_charged_call():
    client, _, store = runs_env(fail=True, **PRICES)
    try:
        res = gen(client)
    finally:
        client.__exit__(None, None, None)
    assert res.status_code == 502
    [run] = store.runs.values()
    assert run["status"] == "failed" and run["http_status"] == 502 and "HTTP 500" in run["error"]
    assert run["llm_calls"] == 1 and run["report_title"] is None


def test_request_rejected_before_the_llm_is_logged_without_llm_use():
    client, fake, store = runs_env(**PRICES)
    try:
        res = gen(client, statuses=["Delivered"], customerCode="1234567")
    finally:
        client.__exit__(None, None, None)
    assert res.status_code == 422 and fake.requests == []
    [run] = store.runs.values()
    assert run["status"] == "failed" and run["http_status"] == 422
    assert run["llm_calls"] == 0 and run["total_tokens"] == 0 and run["cost"] == 0.0
    assert run["filters"]["customerCode"] == "1234567" and run["filters"]["statuses"] == ["Delivered"]


def test_a_broken_database_never_fails_the_report(caplog):
    class Broken(MemoryStore):
        async def record_run(self, run):
            raise RuntimeError("database down")
    client, _, _ = runs_env(store=Broken())
    try:
        res = gen(client)
    finally:
        client.__exit__(None, None, None)
    assert res.status_code == 200 and "run_id" not in res.json()
    assert "BILLING RECORD NOT SAVED" in caplog.text and '"total_tokens": 12900' in caplog.text


def test_saving_links_the_report_to_its_run_but_not_to_another_users_run():
    client, _, store = runs_env()
    try:
        report = gen(client, user="101").json()
        headers = {"Authorization": TOKEN, "X-User-Id": "101"}
        body = {"verticalId": "2", "ReportCategoryId": 7, "prompt": "p", "filters": {}, "report": report}
        saved = client.post("/api/ai-box-erp/reports", json=body, headers=headers).json()
        assert store.runs[report["run_id"]]["saved_report_id"] == saved["id"]

        other = gen(client, user="202").json()
        client.post("/api/ai-box-erp/reports", json={**body, "report": other}, headers=headers)
        assert store.runs[other["run_id"]]["saved_report_id"] is None
    finally:
        client.__exit__(None, None, None)


def test_usage_formats_of_deepseek_and_openai():
    s = Settings(LLM_BASE_URL="https://api.openai.com/v1", LLM_MODEL="gpt-4o-mini")
    deepseek = llm.read_usage({"model": "deepseek-chat", "usage": USAGE}, s, 1200)
    assert (deepseek.prompt_tokens, deepseek.cached_prompt_tokens, deepseek.completion_tokens) == (12000, 2000, 900)
    openai = llm.read_usage({"usage": {"prompt_tokens": 500, "completion_tokens": 50,
                                       "prompt_tokens_details": {"cached_tokens": 128}}}, s, 10)
    assert (openai.provider, openai.model, openai.cached_prompt_tokens, openai.total_tokens) == \
        ("api.openai.com", "gpt-4o-mini", 128, 550)
    empty = llm.read_usage({"usage": {"prompt_tokens": "x"}}, s, 0)
    assert (empty.prompt_tokens, empty.total_tokens, empty.calls) == (0, 0, 1)
