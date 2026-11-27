"""SavedReports against a real Postgres. Skipped unless TEST_DATABASE_URL is set, e.g.
TEST_DATABASE_URL=postgresql://ai_box:...@127.0.0.1:5434/ai_box pytest tests/test_saved_reports_pg.py
Uses its own schema and drops it afterwards."""
import asyncio
import os
import uuid

import pytest

from app.saved_reports import SavedReports

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL is not set")

REPORT = {"title": "First Mile Review", "generated_at": "2026-11-21T10:00:00+05:30",
          "scope": {"period": "01 Oct 2026 – 31 Oct 2026", "records_analyzed": 6851}, "metrics": []}


def test_reports_are_scoped_to_their_owner():
    schema = f"ai_box_erp_test_{uuid.uuid4().hex[:8]}"
    store = SavedReports(URL, schema)

    async def scenario():
        await store.open()
        try:
            a = await store.save(user_id="101", vertical_id="2", category_id=3, category_name="First Mile",
                                 prompt="Pending pickups", filters={"fromDate": "2026-10-01"}, report=REPORT)
            await store.save(user_id="202", vertical_id="2", category_id=3, category_name="First Mile",
                             prompt="Other user", filters={}, report=REPORT)

            items, total = await store.list("101", limit=10, offset=0)
            assert total == 1 and items[0]["id"] == a["id"]
            assert items[0]["period"] == "01 Oct 2026 – 31 Oct 2026" and items[0]["records_analyzed"] == 6851
            assert (await store.list("101", limit=10, offset=0, query="pickups"))[1] == 1
            assert (await store.list("101", limit=10, offset=0, query="nothing"))[1] == 0

            assert (await store.get("101", a["id"]))["report"] == REPORT
            assert await store.get("202", a["id"]) is None
            assert await store.delete("202", a["id"]) is False
            assert await store.delete("101", a["id"]) is True

            run = {"user_id": "101", "vertical_id": "2", "category_id": 3, "category_name": "First Mile",
                   "prompt": "Pending pickups", "filters": {"fromDate": "2026-10-01"}, "source": {"file": "FM.xlsx"},
                   "status": "ok", "http_status": 200, "error": None, "report_title": "First Mile Review",
                   "records_analyzed": 6851, "llm_provider": "api.deepseek.com", "llm_model": "deepseek-chat",
                   "llm_calls": 1, "prompt_tokens": 12000, "cached_prompt_tokens": 2000, "completion_tokens": 900,
                   "total_tokens": 12900, "cost": 0.00383, "cost_currency": "USD",
                   "prices": {"input_per_mtok": 0.27}, "llm_ms": 4200, "duration_ms": 5100}
            run_id = await store.record_run(run)
            other_run = await store.record_run({**run, "user_id": "202"})
            linked = await store.save(user_id="101", vertical_id="2", category_id=3, category_name="First Mile",
                                      prompt="p", filters={}, report=REPORT, run_id=run_id)
            await store.save(user_id="101", vertical_id="2", category_id=3, category_name="First Mile",
                             prompt="p", filters={}, report=REPORT, run_id=other_run)     # not theirs: not linked
            rows = await asyncio.to_thread(store._run, f"SELECT id, saved_report_id FROM {schema}.report_runs "
                                                       "ORDER BY id", (), "all")
            assert [(r["id"], r["saved_report_id"]) for r in rows] == [(run_id, linked["id"]), (other_run, None)]
            usage = await asyncio.to_thread(store._run, f"SELECT * FROM {schema}.monthly_usage WHERE user_id = '101'",
                                            (), "one")
            assert (usage["runs"], usage["reports"], usage["saved_reports"], usage["total_tokens"]) == (1, 1, 1, 12900)
            assert float(usage["cost"]) == pytest.approx(0.00383) and usage["unbilled_runs"] == 1

            assert await store.claim_token("hash-1", "101") is True
            assert await store.claim_token("hash-1", "101") is True
            assert await store.claim_token("hash-1", "202") is False
        finally:
            await asyncio.to_thread(store._run, f"DROP SCHEMA {schema} CASCADE")
            await store.close()

    asyncio.run(scenario())
