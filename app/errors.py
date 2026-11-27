"""Errors returned to the panel as `{"message": "..."}`; the UI shows `message` in a toast."""
from typing import Any, Sequence

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


_FIELD_LABELS = {"prompt": "Prompt", "ReportCategoryId": "Report type", "statuses": "Shipment status",
                 "fromDate": "From date", "toDate": "To date", "customerCode": "Seller",
                 "verticalId": "Vertical", "request": "Request", "file": "Report file",
                 "sourceReportId": "Source report id", "sourceReportName": "Source report name"}


def readable(errors: Sequence[dict[str, Any]]) -> str:
    """One readable sentence for the first pydantic/FastAPI validation error."""
    error = errors[0]
    loc = [str(p) for p in error.get("loc", ()) if p != "body"]
    if not loc:
        return "The request body must be valid JSON."
    label = _FIELD_LABELS.get(loc[0], loc[0])
    message = str(error.get("msg", "is invalid"))
    message = message.removeprefix("Value error, ")
    if error.get("type") == "missing":
        return f"{label} is required."
    if error.get("type") in ("date_from_datetime_parsing", "date_parsing", "date_type"):
        return f"{label} must be a date in yyyy-MM-dd format."
    if error.get("type") in ("int_parsing", "int_type"):
        return f"{label} must be a number."
    return message if error.get("type") == "value_error" else f"{label}: {message}"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError):
        return JSONResponse({"message": exc.message}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse({"message": readable(exc.errors())}, status_code=422)
