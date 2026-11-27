"""The AI Box database (Postgres): saved AI reports, and the log of every report run for billing.

Saved reports: every report has one owner, the ERP login user id that generated it. Every query filters by
that owner, so a user can only list, open or delete their own reports.

Report runs: one row per generate request (success or failure) with who asked, what data, the LLM model and
tokens used, the cost at the configured prices, and columns for billing later (billed_at, invoice_ref).
Rows are never deleted by this service; deleting a saved report keeps its run.

Uses psycopg's synchronous pool in worker threads (psycopg's async mode needs a selector event loop,
which uvicorn does not use on Windows). Tables are created on startup if they do not exist.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.logger import get_logger

logger = get_logger("saved_reports")
_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def _ddl(schema: str) -> str:
    return f"""
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {schema}.saved_reports (
    id            BIGSERIAL PRIMARY KEY,
    user_id       TEXT        NOT NULL,
    vertical_id   TEXT        NOT NULL,
    category_id   INTEGER     NOT NULL,
    category_name TEXT,
    title         TEXT        NOT NULL,
    prompt        TEXT        NOT NULL,
    filters       JSONB       NOT NULL,
    report        JSONB       NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS saved_reports_user_created ON {schema}.saved_reports (user_id, created_at DESC);
-- The first user id seen with an ERP login token owns that token (see security.current_user).
CREATE TABLE IF NOT EXISTS {schema}.token_owners (
    token_hash TEXT        PRIMARY KEY,
    user_id    TEXT        NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now()
);
DELETE FROM {schema}.token_owners WHERE first_seen < now() - interval '30 days';
-- Every generate request, for report history, token use and billing.
CREATE TABLE IF NOT EXISTS {schema}.report_runs (
    id                   BIGSERIAL PRIMARY KEY,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id              TEXT,                       -- X-User-Id of the ERP login (NULL if the panel sent none)
    vertical_id          TEXT        NOT NULL,
    category_id          INTEGER     NOT NULL,
    category_name        TEXT,
    prompt               TEXT        NOT NULL,
    filters              JSONB       NOT NULL,       -- fromDate, toDate, statuses, customerCode
    source               JSONB       NOT NULL,       -- file name/kind/bytes, ReportHistoryId, rows read/matching
    status               TEXT        NOT NULL CHECK (status IN ('ok', 'failed')),
    http_status          INTEGER     NOT NULL,
    error                TEXT,
    report_title         TEXT,
    records_analyzed     INTEGER,
    llm_provider         TEXT,
    llm_model            TEXT,
    llm_calls            INTEGER     NOT NULL DEFAULT 0,  -- 0 when the request failed before the LLM
    prompt_tokens        INTEGER     NOT NULL DEFAULT 0,
    cached_prompt_tokens INTEGER     NOT NULL DEFAULT 0,
    completion_tokens    INTEGER     NOT NULL DEFAULT 0,
    total_tokens         INTEGER     NOT NULL DEFAULT 0,
    cost                 NUMERIC(14, 6),             -- at `prices`; NULL when no prices are configured
    cost_currency        TEXT,
    prices               JSONB,                      -- per-million-token prices used for `cost`
    llm_ms               INTEGER,
    duration_ms          INTEGER     NOT NULL,
    saved_report_id      BIGINT REFERENCES {schema}.saved_reports (id) ON DELETE SET NULL,
    billed_at            TIMESTAMPTZ,                -- for billing: set when the run is invoiced
    invoice_ref          TEXT
);
CREATE INDEX IF NOT EXISTS report_runs_user_created ON {schema}.report_runs (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS report_runs_vertical_created ON {schema}.report_runs (vertical_id, created_at DESC);
CREATE INDEX IF NOT EXISTS report_runs_unbilled ON {schema}.report_runs (created_at)
    WHERE billed_at IS NULL AND llm_calls > 0;
ALTER TABLE {schema}.saved_reports ADD COLUMN IF NOT EXISTS run_id BIGINT;
-- Usage per user and IST month: the starting point for billing.
CREATE OR REPLACE VIEW {schema}.monthly_usage AS
SELECT user_id,
       vertical_id,
       date_trunc('month', created_at AT TIME ZONE 'Asia/Kolkata')::date AS month,
       count(*)                                  AS runs,
       count(*) FILTER (WHERE status = 'ok')     AS reports,
       count(saved_report_id)                    AS saved_reports,
       sum(llm_calls)                            AS llm_calls,
       sum(prompt_tokens)                        AS prompt_tokens,
       sum(cached_prompt_tokens)                 AS cached_prompt_tokens,
       sum(completion_tokens)                    AS completion_tokens,
       sum(total_tokens)                         AS total_tokens,
       sum(cost)                                 AS cost,
       min(cost_currency)                        AS cost_currency,
       count(*) FILTER (WHERE llm_calls > 0 AND billed_at IS NULL) AS unbilled_runs
FROM {schema}.report_runs
GROUP BY 1, 2, 3;
"""

RUN_COLUMNS = ("user_id", "vertical_id", "category_id", "category_name", "prompt", "filters", "source", "status",
               "http_status", "error", "report_title", "records_analyzed", "llm_provider", "llm_model",
               "llm_calls", "prompt_tokens", "cached_prompt_tokens", "completion_tokens", "total_tokens", "cost",
               "cost_currency", "prices", "llm_ms", "duration_ms")
_JSON_COLUMNS = {"filters", "source", "prices"}


def _summary(row: dict[str, Any]) -> dict[str, Any]:
    scope = (row.get("report") or {}).get("scope") or {}
    prompt = row.get("prompt") or ""
    return {
        "id": row["id"],
        "title": row["title"],
        "category_id": row["category_id"],
        "category_name": row.get("category_name"),
        "prompt": prompt if len(prompt) <= 200 else prompt[:199] + "…",
        "period": scope.get("period"),
        "records_analyzed": scope.get("records_analyzed"),
        "created_at": _iso(row["created_at"]),
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else value


class SavedReports:
    def __init__(self, database_url: str, schema: str = "ai_box_erp", max_size: int = 5) -> None:
        if not _SCHEMA_NAME.match(schema):
            raise ValueError("DB_SCHEMA must be a lower-case identifier")
        self.schema = schema
        self._pool = ConnectionPool(database_url, min_size=1, max_size=max_size, open=False,
                                    kwargs={"autocommit": True, "row_factory": dict_row})

    # ---- lifecycle -------------------------------------------------------------------------------
    async def open(self) -> None:
        await asyncio.to_thread(self._open)

    def _open(self) -> None:
        self._pool.open(wait=True, timeout=10)
        with self._pool.connection() as conn:
            conn.execute(_ddl(self.schema))
        logger.info("Saved reports ready in schema %s", self.schema)

    async def close(self) -> None:
        await asyncio.to_thread(self._pool.close)

    def _run(self, sql: str, params: tuple = (), fetch: str = "none"):
        with self._pool.connection() as conn:
            cur = conn.execute(sql, params)
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return cur.rowcount

    # ---- token ownership -------------------------------------------------------------------------
    async def claim_token(self, token_hash: str, user_id: str) -> bool:
        """True when this token belongs to user_id (claiming it if it is new)."""
        s = self.schema
        row = await asyncio.to_thread(
            self._run,
            f"""WITH ins AS (INSERT INTO {s}.token_owners (token_hash, user_id) VALUES (%s, %s)
                             ON CONFLICT (token_hash) DO NOTHING RETURNING user_id)
                SELECT user_id FROM ins UNION ALL
                SELECT user_id FROM {s}.token_owners WHERE token_hash = %s LIMIT 1""",
            (token_hash, user_id, token_hash), "one")
        return bool(row) and row["user_id"] == user_id

    # ---- report runs (billing log) ---------------------------------------------------------------
    async def record_run(self, run: dict[str, Any]) -> int:
        """Stores one generate request (see RUN_COLUMNS) and returns its id."""
        values = tuple(Jsonb(run.get(c)) if c in _JSON_COLUMNS and run.get(c) is not None else run.get(c)
                       for c in RUN_COLUMNS)
        row = await asyncio.to_thread(
            self._run,
            f"""INSERT INTO {self.schema}.report_runs ({", ".join(RUN_COLUMNS)})
                VALUES ({", ".join(["%s"] * len(RUN_COLUMNS))}) RETURNING id""",
            values, "one")
        return int(row["id"])

    # ---- reports ---------------------------------------------------------------------------------
    async def save(self, *, user_id: str, vertical_id: str, category_id: int, category_name: str | None,
                   prompt: str, filters: dict[str, Any], report: dict[str, Any],
                   run_id: int | None = None) -> dict[str, Any]:
        """Saves the report; `run_id` (from the generate response) links it to its run, if the run is the user's."""
        s = self.schema
        row = await asyncio.to_thread(
            self._run,
            f"""WITH saved AS (
                    INSERT INTO {s}.saved_reports
                        (user_id, vertical_id, category_id, category_name, title, prompt, filters, report, run_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                            (SELECT id FROM {s}.report_runs WHERE id = %s AND user_id = %s))
                    RETURNING id, created_at, run_id),
                linked AS (
                    UPDATE {s}.report_runs r SET saved_report_id = saved.id FROM saved
                    WHERE r.id = saved.run_id)
                SELECT id, created_at FROM saved""",
            (user_id, vertical_id, category_id, category_name, report.get("title") or "AI report", prompt,
             Jsonb(filters), Jsonb(report), run_id, user_id), "one")
        return {"id": row["id"], "created_at": _iso(row["created_at"])}

    async def list(self, user_id: str, *, limit: int, offset: int, query: str = "") -> tuple[list[dict], int]:
        s = self.schema
        where, params = "user_id = %s", [user_id]
        if query:
            where += " AND (title ILIKE %s OR prompt ILIKE %s OR category_name ILIKE %s)"
            like = f"%{query}%"
            params += [like, like, like]
        rows = await asyncio.to_thread(
            self._run,
            f"""SELECT id, title, category_id, category_name, prompt, created_at,
                       jsonb_build_object('scope', report->'scope') AS report,
                       count(*) OVER () AS total
                FROM {s}.saved_reports WHERE {where}
                ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s""",
            (*params, limit, offset), "all")
        total = int(rows[0]["total"]) if rows else 0
        if not rows and offset:
            count = await asyncio.to_thread(self._run, f"SELECT count(*) AS n FROM {s}.saved_reports WHERE {where}",
                                            tuple(params), "one")
            total = int(count["n"])
        return [_summary(r) for r in rows], total

    async def get(self, user_id: str, report_id: int) -> dict[str, Any] | None:
        row = await asyncio.to_thread(
            self._run,
            f"""SELECT id, title, category_id, category_name, prompt, filters, report, created_at
                FROM {self.schema}.saved_reports WHERE id = %s AND user_id = %s""",
            (report_id, user_id), "one")
        if not row:
            return None
        return {"id": row["id"], "title": row["title"], "category_id": row["category_id"],
                "category_name": row["category_name"], "prompt": row["prompt"], "filters": row["filters"],
                "created_at": _iso(row["created_at"]), "report": row["report"]}

    async def delete(self, user_id: str, report_id: int) -> bool:
        count = await asyncio.to_thread(
            self._run, f"DELETE FROM {self.schema}.saved_reports WHERE id = %s AND user_id = %s",
            (report_id, user_id))
        return count > 0
