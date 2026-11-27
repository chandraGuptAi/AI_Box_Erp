"""POST /api/ai-box-erp/generate: read the ERP report file the panel attached, filter it, ask the LLM once and
return the report as JSON (rendered on screen by the panel) in the same request. This service calls no ERP API: the attached file is the only data."""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.config import Settings
from app.errors import ApiError
from app.logger import get_logger

from . import aggregate, llm, report, report_file, sources
from .schemas import GenerateRequest

logger = get_logger("ai_insights.service")
IST = timezone(timedelta(hours=5, minutes=30))
BUSY_MESSAGE = "The AI is busy with other reports. Please try again in a minute."


def now_ist() -> datetime:
    return datetime.now(IST)


def months_before(day: date, months: int) -> date:
    month_index = day.year * 12 + day.month - 1 - months
    year, month = divmod(month_index, 12)
    month += 1
    next_month = date(year + month // 12, month % 12 + 1, 1)
    return date(year, month, min(day.day, (next_month - timedelta(days=1)).day))


@dataclass
class Upload:
    """The report file the panel attached."""
    name: str
    content_type: str
    content: bytes


def run_cost(settings: Settings, usage: llm.LlmUsage | None) -> tuple[float | None, dict[str, Any] | None]:
    """Cost of the LLM call at the configured per-million-token prices, and those prices; (None, None) when no
    price is configured (the tokens are stored, so the cost can be worked out later)."""
    prices = {"input_per_mtok": settings.LLM_PRICE_INPUT_PER_MTOK,
              "cached_input_per_mtok": settings.LLM_PRICE_CACHED_INPUT_PER_MTOK,
              "output_per_mtok": settings.LLM_PRICE_OUTPUT_PER_MTOK,
              "currency": settings.LLM_PRICE_CURRENCY}
    if not any(prices[k] for k in ("input_per_mtok", "cached_input_per_mtok", "output_per_mtok")):
        return None, None
    if usage is None:
        return 0.0, prices
    fresh = usage.prompt_tokens - usage.cached_prompt_tokens
    cost = (fresh * prices["input_per_mtok"] + usage.cached_prompt_tokens * prices["cached_input_per_mtok"]
            + usage.completion_tokens * prices["output_per_mtok"]) / 1_000_000
    return round(cost, 6), prices


class AiInsightsService:
    def __init__(self, settings: Settings, llm_transport=None, store=None) -> None:
        self.s = settings
        self._llm_transport = llm_transport
        self._slots = asyncio.Semaphore(max(1, settings.MAX_CONCURRENT_JOBS))
        # The database (app.saved_reports.SavedReports) where every run is logged; None = not logged.
        self.store = store

    async def generate(self, body: GenerateRequest, upload: Upload | None,
                       requested_by: str | None = None) -> dict[str, Any]:
        """The report. Every call, successful or not, is logged in report_runs (tokens, cost) for billing;
        the run id is returned in the report as `run_id` so a later save can be linked to it."""
        started = time.perf_counter()
        info: dict[str, Any] = {"rows": None, "source": None, "usage": None}
        outcome = "ok"
        result: dict[str, Any] | None = None
        status, error = 200, None
        try:
            result = await self._generate(body, upload, info)
            outcome = f"ok metrics={len(result['metrics'])} charts={len(result['charts'])}"
        except ApiError as exc:
            status, error = exc.status, exc.message
            outcome = f"HTTP {exc.status} {exc.message}"
            raise
        except Exception:
            status, error = 500, "unexpected error"
            outcome = "HTTP 500 unexpected error"
            logger.exception("AI insights request failed")
            raise
        finally:
            duration = time.perf_counter() - started
            logger.info("AI insights: vertical=%s category=%s file=%r (%s bytes) rows_after_filters=%s "
                        "took=%.1fs user=%s -> %s", body.verticalId, body.ReportCategoryId,
                        upload.name if upload else None, len(upload.content) if upload else 0, info["rows"],
                        duration, requested_by, outcome)
            run_id = await self._log_run(body, upload, requested_by, info, result, status, error, duration)
            if result is not None and run_id is not None:
                result["run_id"] = run_id
        return result

    async def _log_run(self, body: GenerateRequest, upload: Upload | None, user_id: str | None,
                       info: dict[str, Any], result: dict[str, Any] | None, status: int, error: str | None,
                       duration: float) -> int | None:
        """Stores the run in report_runs; never fails the request. When the database is down the full
        record is logged at ERROR level so it can be replayed into report_runs for billing."""
        if self.store is None or not hasattr(self.store, "record_run"):
            return None
        usage: llm.LlmUsage | None = info["usage"]
        cost, prices = run_cost(self.s, usage)
        source = dict(info["source"] or {})
        if upload is not None:
            source.setdefault("file", body.sourceReportName or upload.name)
            source["kind"] = report_file.file_kind(upload.name, upload.content_type, upload.content[:4096])
            source["bytes"] = len(upload.content)
        if body.sourceReportId:
            source["ReportHistoryId"] = body.sourceReportId
        scope = (result or {}).get("scope") or {}
        run = {
            "user_id": user_id, "vertical_id": body.verticalId, "category_id": body.ReportCategoryId,
            "category_name": body.ReportCategoryName, "prompt": body.prompt,
            "filters": {"fromDate": body.fromDate.isoformat(), "toDate": body.toDate.isoformat(),
                        "statuses": body.statuses, "customerCode": body.customerCode},
            "source": source, "status": "ok" if result is not None else "failed", "http_status": status,
            "error": error, "report_title": (result or {}).get("title"),
            "records_analyzed": scope.get("records_analyzed"),
            "llm_provider": usage.provider if usage else None, "llm_model": usage.model if usage else None,
            "llm_calls": usage.calls if usage else 0,
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "cached_prompt_tokens": usage.cached_prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
            "total_tokens": usage.total_tokens if usage else 0,
            "cost": cost, "cost_currency": prices["currency"] if prices else None, "prices": prices,
            "llm_ms": usage.duration_ms if usage else None, "duration_ms": int(duration * 1000),
        }
        try:
            return await self.store.record_run(run)
        except Exception:
            logger.exception("BILLING RECORD NOT SAVED (replay into report_runs): %s",
                             json.dumps(run, ensure_ascii=False, default=str))
            return None

    async def _generate(self, body: GenerateRequest, upload: Upload | None, info: dict[str, Any]) -> dict[str, Any]:
        if not self.s.LLM_API_KEY:
            raise ApiError(503, "AI is not configured on the server.")
        category_name = body.ReportCategoryName or f"Report type {body.ReportCategoryId}"
        self._check_inputs(body, upload, category_name)
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=self.s.SLOT_WAIT_SECONDS)
        except asyncio.TimeoutError:
            raise ApiError(429, BUSY_MESSAGE) from None
        try:
            data, source, as_of = await self._collect(body, upload, category_name)
            info["rows"] = source["rows_after_filters"]
            filters = {"fromDate": body.fromDate.isoformat(), "toDate": body.toDate.isoformat(),
                       "statuses": body.statuses, "seller": body.customerCode}
            info["source"] = {k: source.get(k) for k in ("file", "rows_in_file", "rows_after_filters")}
            try:
                analysis, info["usage"] = await llm.analyse(self.s, prompt=body.prompt, category_name=category_name,
                                                            filters=filters, data=data, source=source,
                                                            transport=self._llm_transport)
            except llm.LlmError as exc:
                info["usage"] = exc.usage      # a failed call may still be charged by the provider
                raise ApiError(502, str(exc)) from None
            return report.build(category_name=category_name, body=body, data=data, analysis=analysis,
                                source=source, as_of=as_of, generated_at=now_ist())
        finally:
            self._slots.release()

    def _check_inputs(self, body: GenerateRequest, upload: Upload | None, category_name: str) -> None:
        if len(body.prompt) > self.s.MAX_PROMPT_CHARS:
            raise ApiError(400, f"The prompt is too long: use at most {self.s.MAX_PROMPT_CHARS} characters.")
        today = now_ist().date()
        if body.fromDate > body.toDate:
            raise ApiError(400, "From date must be on or before To date.")
        if body.toDate > today:
            raise ApiError(400, "To date cannot be in the future.")
        oldest = months_before(today, self.s.MAX_HISTORY_MONTHS)
        if body.fromDate < oldest:
            raise ApiError(400, f"From date cannot be older than {self.s.MAX_HISTORY_MONTHS} months "
                                f"(earliest allowed: {oldest.isoformat()}).")
        if upload is None:
            raise ApiError(422, f"No completed {category_name} report was found for {body.fromDate:%d-%m-%Y} to "
                                f"{body.toDate:%d-%m-%Y}. Generate the {category_name} report in the ERP first, "
                                f"or choose dates that include one.")
        if len(upload.content) > self.s.MAX_UPLOAD_MB * 1024 * 1024:
            raise ApiError(413, f"The report file is larger than {self.s.MAX_UPLOAD_MB:g} MB.")
        if not upload.content:
            raise ApiError(422, "The attached report file is empty.")

    async def _collect(self, body: GenerateRequest, upload: Upload,
                       category_name: str) -> tuple[dict[str, Any], dict[str, Any], datetime | None]:
        """(aggregates, data source info, snapshot time) from the attached report file."""
        name = body.sourceReportName or upload.name or "report file"
        cap = self.s.MAX_FILE_ROWS
        try:
            parsed = await asyncio.to_thread(report_file.parse, name, upload.content_type, upload.content, cap)
        except report_file.ReportFileError as exc:
            raise ApiError(422, f"Report file {name} could not be read: {exc}.") from None
        try:
            found = sources.file_rows(parsed, body, cap, name)
        except sources.NoCustomerColumn:
            raise ApiError(422, f"The {category_name} report file has no customer code column, so it cannot be "
                                f"filtered to seller {body.customerCode}. Turn off the seller filter and "
                                f"try again.") from None
        if not found.matching:
            raise ApiError(422, f"None of the {found.read:,} rows in {name} match the selected dates, statuses "
                                f"and seller. Change the filters and try again.")
        as_of = sources.report_time(name)
        data = aggregate.build(found.rows, total_matching=found.matching, sample_size=self.s.AI_SAMPLE_ROWS,
                               as_of=as_of)
        source = {"type": "report_file", "file": name, "ReportHistoryId": body.sourceReportId,
                  "generated": as_of.strftime("%d-%m-%Y %H:%M") if as_of else None,
                  "rows_in_file": found.read, "rows_after_filters": found.matching, "notes": found.notes}
        return data, source, as_of
