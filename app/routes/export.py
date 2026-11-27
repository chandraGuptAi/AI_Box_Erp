"""Download an AI report as PDF or Word (.docx), built here from the report JSON.

POST /api/ai-box-erp/export?format=pdf|docx  body {"report": {...}}   any report shown in the panel
GET  /api/ai-box-erp/reports/{id}/export?format=pdf|docx             a saved report (owner only)
"""
import asyncio
import json
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, field_validator

from app import report_export
from app.errors import ApiError
from app.logger import get_logger
from app.routes.saved_reports import MAX_REPORT_BYTES, store
from app.security import current_user, panel_login

logger = get_logger("routes.export")
router = APIRouter(prefix="/api/ai-box-erp", tags=["Export"])
FORMAT = Query("pdf", pattern="^(pdf|docx)$", description="pdf or docx")


class ExportRequest(BaseModel):
    report: dict[str, Any]

    @field_validator("report")
    @classmethod
    def _report(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value.get("title"), str) or not value["title"].strip():
            raise ValueError("The report to export is not a generated AI report.")
        return value


async def _file(report: dict[str, Any], fmt: str) -> Response:
    try:
        content, name, media_type = await asyncio.to_thread(report_export.render, report, fmt)
    except Exception:
        logger.exception("Export to %s failed", fmt)
        raise ApiError(500, f"The {'PDF' if fmt == 'pdf' else 'Word'} file could not be created. Please try again.")
    ascii_name = name.encode("ascii", "ignore").decode() or f"AI_report.{fmt}"
    return Response(content, media_type=media_type, headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}",
        "Cache-Control": "no-store",
    })


@router.post("/export", summary="A report (as shown in the panel) as PDF or Word")
async def export_report(body: ExportRequest, format: str = FORMAT, _login: str = Depends(panel_login)) -> Response:
    if len(json.dumps(body.report, ensure_ascii=False)) > MAX_REPORT_BYTES:
        raise ApiError(413, "This report is too large to export.")
    return await _file(body.report, format)


@router.get("/reports/{report_id}/export", summary="One of the caller's saved reports as PDF or Word")
async def export_saved(report_id: int, request: Request, format: str = FORMAT,
                       user_id: str = Depends(current_user)) -> Response:
    found = await store(request).get(user_id, report_id)
    if not found:
        raise ApiError(404, "This saved report was not found.")
    return await _file(found["report"], format)
