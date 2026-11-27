"""One LLM call per report, on an OpenAI-compatible chat completions API. No retries: a retry could be
charged twice."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings

SYSTEM_PROMPT = """You are a logistics analyst for Bigship, an Indian shipping aggregator.
You write the text of an on-screen report for an ERP admin. The screen already shows the metrics and
charts computed from DATA; your job is to explain them.

Rules:
- Answer ONLY from the DATA provided. Never invent, estimate or extrapolate numbers.
- Use the numbers exactly as given (delivery_pct, rto_pct, counts, ...). If the data cannot answer part
  of the question, say so plainly instead of guessing.
- If "truncated" is true, say that the figures cover only the shipments analysed, not every match.
- DATA SOURCE names the ERP report file the data came from. A report file is a snapshot taken at its
  generation time ("as_of"): describe statuses and ageing as of that time.
- Focus on what the user asked and on the report type. Be concise and specific; name couriers, cities
  and dates when the data supports it.
- Reply with ONE JSON object and nothing else, in this shape:
{
  "title": "short report title",
  "summary_paragraphs": ["executive summary, 1-3 short paragraphs"],
  "highlights": ["one-line highlight with its number"],
  "findings": [{"title": "short finding title", "detail": "the finding with the supporting numbers",
                "severity": "high|medium|low|info"}],
  "recommendations": [{"action": "what to do", "rationale": "why, citing the data",
                       "priority": "high|medium|low"}],
  "tables": [{"title": "table title", "columns": ["col1", "col2"], "rows": [["a", "b"]],
              "note": "optional"}]
}
Keep at most 3 summary_paragraphs, 6 highlights, 8 findings, 6 recommendations, 3 tables of at most
15 rows each. Table cells are display strings."""

SEVERITIES = ("high", "medium", "low", "info")
PRIORITIES = ("high", "medium", "low")


@dataclass
class LlmUsage:
    """What one LLM call used, for the report run log and billing. Tokens are 0 when the provider sent none."""
    provider: str                 # host of LLM_BASE_URL, e.g. api.deepseek.com
    model: str                    # the model the provider says answered (else LLM_MODEL)
    prompt_tokens: int = 0
    cached_prompt_tokens: int = 0  # prompt tokens served from the provider's cache (cheaper)
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    calls: int = 1                # requests sent to the provider (charged or not)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_usage(payload: Any, settings: Settings, duration_ms: int) -> LlmUsage:
    """Token counts from an OpenAI-compatible response: `usage.prompt_tokens` etc.; cached prompt tokens are
    `prompt_cache_hit_tokens` (DeepSeek) or `prompt_tokens_details.cached_tokens` (OpenAI)."""
    payload = payload if isinstance(payload, dict) else {}
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}

    def count(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    prompt, completion = count(usage.get("prompt_tokens")), count(usage.get("completion_tokens"))
    cached = usage.get("prompt_cache_hit_tokens")
    if cached is None:
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")             if isinstance(usage.get("prompt_tokens_details"), dict) else None
    return LlmUsage(provider=urlparse(settings.LLM_BASE_URL).hostname or settings.LLM_BASE_URL,
                    model=str(payload.get("model") or settings.LLM_MODEL),
                    prompt_tokens=prompt, cached_prompt_tokens=min(count(cached), prompt),
                    completion_tokens=completion, total_tokens=count(usage.get("total_tokens")) or prompt + completion,
                    duration_ms=duration_ms)


class LlmError(Exception):
    """The LLM call failed. `usage` is set when the provider was reached: it may still charge for the call."""

    def __init__(self, message: str, usage: LlmUsage | None = None) -> None:
        super().__init__(message)
        self.usage = usage


def _text(value: Any, limit: int = 2000) -> str:
    return str(value).strip()[:limit] if value not in (None, "") else ""


def _clean(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce the model's JSON into the report's text parts; drop anything malformed (never None in lists)."""
    def items(key, limit):
        value = raw.get(key)
        if isinstance(value, str):          # a single paragraph / highlight instead of a list
            value = [value]
        return value[:limit] if isinstance(value, list) else []

    def strings(key, limit, size):
        return [_text(v, size) for v in items(key, limit) if isinstance(v, (str, int, float)) and _text(v)]

    tables = []
    for t in items("tables", 3):
        if not isinstance(t, dict) or not isinstance(t.get("columns"), list):
            continue
        columns = [_text(c, 60) for c in t["columns"]][:8]
        rows = [[_text(cell, 120) for cell in row][:len(columns)]
                for row in (t.get("rows") or [])[:15] if isinstance(row, list)]
        rows = [row + [""] * (len(columns) - len(row)) for row in rows]
        if columns and rows:
            table = {"title": _text(t.get("title"), 120), "columns": columns, "rows": rows}
            if _text(t.get("note")):
                table["note"] = _text(t.get("note"), 200)
            tables.append(table)

    findings = []
    for f in items("findings", 8):
        if not isinstance(f, dict) or not _text(f.get("title")) or not _text(f.get("detail")):
            continue
        severity = _text(f.get("severity"), 20).lower()
        findings.append({"title": _text(f["title"], 150), "detail": _text(f["detail"], 800),
                         "severity": severity if severity in SEVERITIES else "info"})

    recommendations = []
    for r in items("recommendations", 6):
        if not isinstance(r, dict) or not _text(r.get("action")):
            continue
        rec = {"action": _text(r["action"], 300), "rationale": _text(r.get("rationale"), 800)}
        priority = _text(r.get("priority"), 20).lower()
        if priority in PRIORITIES:
            rec["priority"] = priority
        recommendations.append(rec)

    paragraphs = strings("summary_paragraphs", 3, 1500) or strings("summary", 3, 1500)
    return {
        "title": _text(raw.get("title"), 150),
        "summary_paragraphs": paragraphs,
        "highlights": strings("highlights", 6, 300),
        "findings": findings,
        "recommendations": recommendations,
        "tables": tables,
    }


async def analyse(settings: Settings, *, prompt: str, category_name: str, filters: dict[str, Any],
                  data: dict[str, Any], source: dict[str, Any] | None = None,
                  transport: httpx.AsyncBaseTransport | None = None) -> tuple[dict[str, Any], LlmUsage]:
    """The report text from one LLM call, and what that call used."""
    if not settings.LLM_API_KEY:
        raise LlmError("The AI model is not configured on the server (LLM_API_KEY).")
    user_message = (f"REPORT TYPE: {category_name}\n"
                    f"FILTERS: {json.dumps(filters, ensure_ascii=False)}\n"
                    f"DATA SOURCE: {json.dumps(source or {}, ensure_ascii=False, default=str)}\n"
                    f"USER QUESTION: {prompt}\n\n"
                    f"DATA:\n{json.dumps(data, ensure_ascii=False, default=str)}")
    body = {"model": settings.LLM_MODEL, "temperature": settings.LLM_TEMPERATURE,
            "max_tokens": settings.LLM_MAX_OUTPUT_TOKENS, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user_message}]}
    started = time.perf_counter()

    def elapsed() -> int:
        return int((time.perf_counter() - started) * 1000)

    def no_answer() -> LlmUsage:
        # Sent, but no usable answer (timeout, connection lost): nothing counted by us, still one call.
        return read_usage({}, settings, elapsed())

    try:
        async with httpx.AsyncClient(base_url=settings.LLM_BASE_URL.rstrip("/"), transport=transport,
                                     timeout=httpx.Timeout(settings.LLM_TIMEOUT_SECONDS, connect=10.0),
                                     trust_env=False) as client:
            res = await client.post("/chat/completions", json=body,
                                    headers={"Authorization": f"Bearer {settings.LLM_API_KEY}"})
    except httpx.TimeoutException:
        raise LlmError("The AI model took too long to respond.", no_answer()) from None
    except httpx.RequestError:
        raise LlmError("The AI model could not be reached.", no_answer()) from None
    try:
        payload = res.json()
    except ValueError:
        payload = {}
    usage = read_usage(payload, settings, elapsed())
    if res.status_code >= 400:
        raise LlmError(f"The AI model returned HTTP {res.status_code}.", usage)
    try:
        raw = json.loads(payload["choices"][0]["message"]["content"])
    except (ValueError, KeyError, IndexError, TypeError):
        raise LlmError("The AI model returned an unreadable answer.", usage) from None
    if not isinstance(raw, dict):
        raise LlmError("The AI model returned an unreadable answer.", usage)
    result = _clean(raw)
    if not result["summary_paragraphs"] and not result["findings"]:
        raise LlmError("The AI model returned an empty analysis.", usage)
    return result, usage
