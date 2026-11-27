# Ai Box ERP Service

Microservice behind the `AI_python_backend` gateway for the ERP admin panel's **AI Box** page.
It serves one endpoint, `POST /api/ai-box-erp/generate`: the panel sends a prompt, filters and the ERP
report file (Excel/CSV) of one report type, and gets back the AI report as JSON in the same response; the
panel renders it on screen.

**This service calls no ERP API.** The attached file is the only data. The only outgoing call is the LLM.

```
Angular ERP panel ──► AI_python_backend gateway ──► Ai_Box_Erp ──► LLM (one call per report)
  multipart in, JSON out  /api/ai-box-erp/*       (X-Ai-Box-Service-Token)
```

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env
```

In `.env` set at least:

- `LLM_API_KEY`: **required**. Without it every request answers 503 "AI is not configured on the server."
- `SERVICE_TOKEN`: same value as `AI_BOX_ERP_SERVICE_TOKEN` in the gateway's `.env`.

```bash
python -m app.main              # or: uvicorn app.main:app --port 8200
```

Gateway (`AI_python_backend/.env`): `AI_BOX_ERP_SERVICE_URL=http://127.0.0.1:<PORT>`,
`AI_BOX_ERP_SERVICE_TOKEN=<same as SERVICE_TOKEN>`, `AI_BOX_ERP_SERVICE_TIMEOUT_SECONDS=240` (the LLM runs
inside the request). The gateway allows request bodies up to 51 MB on `/api/ai-box-erp`.
Health: `GET /health` (no token).

## Contract

`POST /api/ai-box-erp/generate`, `multipart/form-data`, header `Authorization: Bearer <ERP token>`
(required to be present; this service does not verify it, see `app/security.py`):

| Field | Required | Content |
|-------|----------|---------|
| `request` | yes | JSON text: `{"verticalId": "2", "ReportCategoryId": 7, "ReportCategoryName": "First Mile", "prompt": "...", "statuses": [], "fromDate": "2026-10-01", "toDate": "2026-10-31", "customerCode": null, "sourceReportId": "4408", "sourceReportName": "Unified_FM_report_21-11-2026 13-30-07.xlsx"}` |
| `file` | yes* | the ERP report file (xlsx or csv, at most `MAX_UPLOAD_MB`), named `sourceReportName` |

\* Without a file the answer is 422 "No completed <type> report was found for <dates>. Generate the
<type> report in the ERP first, or choose dates that include one."

- `ReportCategoryName`: optional display name used in the title and subtitle (else "Report type <id>").
- `statuses`: `Pickup Scheduled`, `Not Picked`, `In-Transit`, `Out For Delivery`, `Delivered`, `Undelivered`; `[]` = all.
- `customerCode`: `null` = all sellers; otherwise the file needs a customer code column (else 422).
- **200**: `application/json`, `Cache-Control: no-store`, the report object:

```json
{
  "title": "First Mile — Pending Pickup Review",
  "subtitle": "First Mile · AI insights",
  "generated_at": "2026-11-21T15:42:10+05:30",
  "scope": {"period": "01 Oct 2026 – 31 Oct 2026", "source": "Unified_FM_report_21-11-2026 13-30-07.xlsx (#4408)",
            "records_analyzed": 6851, "summary": "Snapshot as of 21 Nov 2026 13:30. All statuses. All sellers."},
  "executive_summary": {"paragraphs": ["..."], "highlights": ["..."]},
  "metrics": [{"key": "delivery_pct", "label": "Delivery %", "value": 82.4, "unit": "percent", "tone": "ok", "note": "5,645 delivered"}],
  "charts": [{"id": "status", "title": "Shipments by status", "type": "doughnut", "labels": ["Pickup Scheduled", "Not Picked"],
              "datasets": [{"label": "Shipments", "data": [5874, 977]}], "value_format": {"type": "number"}}],
  "tables": [{"title": "...", "columns": ["..."], "rows": [["..."]], "note": "optional"}],
  "findings": [{"title": "...", "detail": "...", "severity": "high|medium|low|info"}],
  "recommendations": [{"action": "...", "rationale": "...", "priority": "high|medium|low (optional)"}],
  "limitations": ["..."]
}
```

  `metrics`, `charts` and `scope` are computed in Python (`app/ai_insights/report.py`); a metric or chart
  with no data is left out (a First Mile file has no Delivered/RTO, so no Delivery % or RTO %). Charts:
  `status` (doughnut), `courier`, `destination_city`, `origin_city`, `failure_reasons` (horizontal_bar, top
  10), `trend` (line, `wide`), `open_age` (bar), `payment_mode` (pie). Every dataset has one number per
  label. The text parts (`title`, `executive_summary`, `findings`, `recommendations`, `tables`) come from
  the LLM; malformed items are dropped, an unknown severity becomes `info`, an unknown priority is removed.
- **Errors**: JSON `{"message": "<readable text>"}`: 400/422 invalid input, no file, unreadable or not xlsx/csv
  file, no rows matching the filters; 401 no login token; 413 file too large; 429 busy; 502 LLM failure;
  503 LLM not configured.

```bash
curl -F 'request={"verticalId":"2","ReportCategoryId":7,"ReportCategoryName":"First Mile","prompt":"Analyse pickups","statuses":[],"fromDate":"2026-11-01","toDate":"2026-11-20","customerCode":null,"sourceReportId":"4408","sourceReportName":"Unified_FM_report_21-11-2026 13-30-07.xlsx"}' \
     -F 'file=@Unified_FM_report_21-11-2026 13-30-07.xlsx' \
     -H 'Authorization: Bearer <token>' http://localhost:8200/api/ai-box-erp/generate
```

## Flow

The panel picks the newest COMPLETED xlsx/csv file of the report type dated in `fromDate..toDate` from
the ERP's `get-report-history` (skipping `ai_insights_*`), downloads it with `download-report`, and calls
this service once per report type. Then this service, within the one request:

1. Checks `LLM_API_KEY` (503), the request (prompt at most 2000 chars, `fromDate <= toDate`, `toDate` not in
   the future, `fromDate` at most 6 months back, IST) and the file (present, at most `MAX_UPLOAD_MB`).
2. Waits for a free slot (`MAX_CONCURRENT_JOBS`); none within `SLOT_WAIT_SECONDS` → 429.
3. Parses the file (kind from the bytes; header row detected; at most `MAX_FILE_ROWS` rows), keeps only
   allow-listed columns (`aggregate.FIELDS`: never names, phones, emails or addresses), filters rows by
   seller / status / date and drops repeated shipments. The file is a snapshot: the timestamp in its name
   is the data's "as of" time (used for the ageing of open shipments).
4. Computes the aggregates in Python, calls the LLM once (no retries) with the prompt, filters, file name
   and aggregates, and returns the JSON report: numbers from Python, text from the LLM.

One log line per request: vertical, category, file name and size, rows after filters, duration, outcome
(with the number of metrics and charts).
The token and file contents are never logged.

## Structure

```
app/
  main.py               app, lifespan, routers
  config.py             settings from env / .env
  security.py           gateway service token + panel login header
  errors.py             errors as {"message": "..."}
  routes/
    health.py           GET /health
    ai_insights.py      POST /api/ai-box-erp/generate (multipart in, JSON out)
  ai_insights/
    schemas.py          the `request` JSON
    service.py          validation, concurrency slot, data, LLM, report
    sources.py          file rows: filters, dedupe, "as of" time
    report_file.py      xlsx/csv parsing
    aggregate.py        allow-listed fields, aggregates, capped sample (no PII)
    llm.py              one structured LLM call, no retries
    report.py           the JSON report: metrics, charts, scope, limitations + LLM text
tests/
```

## Tests

```bash
pytest
```

## Saved reports (Postgres)

Generating does not save. The user saves a report with the panel's "Save report" button, which calls
`POST /api/ai-box-erp/reports` with `X-User-Id` (the ERP user id from the panel's stored login details). Tables live in schema `DB_SCHEMA` (default `ai_box_erp`) and are created
on startup: `saved_reports` (one owner per row) and `token_owners`.

| Call | Answer |
|------|--------|
| `POST /api/ai-box-erp/reports` JSON `{verticalId, ReportCategoryId, ReportCategoryName, prompt, filters, report}` | `201 {id, created_at}` |
| `GET /api/ai-box-erp/reports?limit=20&offset=0&q=` | `{items: [{id, title, category_name, prompt, period, records_analyzed, created_at}], total}` |
| `GET /api/ai-box-erp/reports/{id}` | `{id, title, category_name, prompt, filters, created_at, report}` |
| `DELETE /api/ai-box-erp/reports/{id}` | `{deleted: true}` |

Every query filters by the caller's user id, so other users' reports are never listed, opened or deleted
(404). The ERP has no API to look a user up from a token, so the id comes from the panel; the first user id
seen with an ERP login token owns that token, and the same token with another id gets 403.
Set `DATABASE_URL` (empty disables saving; the endpoints then answer 503). Postgres test:
`TEST_DATABASE_URL=... pytest tests/test_saved_reports_pg.py`.

## Export (PDF / Word)

`app/report_export.py` builds the files from the report JSON: charts are drawn with matplotlib in the
panel's colours (small charts two per row in the PDF), the PDF uses reportlab with DejaVu fonts (₹ and —
print correctly), and Word is a real `.docx` (python-docx) with tables and chart pictures.

| Call | Answer |
|------|--------|
| `POST /api/ai-box-erp/export?format=pdf\|docx` JSON `{report}` | the file (`Content-Disposition: attachment`) |
| `GET /api/ai-box-erp/reports/{id}/export?format=pdf\|docx` | a saved report's file; owner only (404 otherwise) |
