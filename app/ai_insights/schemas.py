from datetime import date

from pydantic import BaseModel, ConfigDict, field_validator

# Shipment statuses the panel can select; matched to file values by aggregate.status_matches.
STATUS_TYPES = ("Pickup Scheduled", "Not Picked", "In-Transit", "Out For Delivery", "Delivered", "Undelivered")


class GenerateRequest(BaseModel):
    """The `request` form field of POST /api/ai-box-erp/generate (one report type per call)."""
    model_config = ConfigDict(extra="ignore")

    verticalId: str
    ReportCategoryId: int
    # Display name of the report type ("First Mile"); used in the report title and subtitle.
    ReportCategoryName: str | None = None
    prompt: str
    statuses: list[str] = []
    fromDate: date
    toDate: date
    customerCode: str | None = None
    # The ERP report file the panel attached (both null when no file is attached).
    sourceReportId: str | None = None
    sourceReportName: str | None = None

    @field_validator("verticalId", mode="before")
    @classmethod
    def _vertical(cls, value):
        value = str(value).strip() if value is not None else ""
        if not value:
            raise ValueError("Vertical is required.")
        return value

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Please write a prompt for the AI.")
        return value

    @field_validator("statuses", mode="before")
    @classmethod
    def _statuses(cls, value):
        value = value or []
        if not isinstance(value, list):
            raise ValueError("Shipment status must be a list.")
        unknown = [str(s) for s in value if s not in STATUS_TYPES]
        if unknown:
            raise ValueError(f"Unknown shipment status: {', '.join(unknown)}. "
                             f"Use one of: {', '.join(STATUS_TYPES)}.")
        return list(dict.fromkeys(value))

    @field_validator("customerCode", "sourceReportId", "sourceReportName", "ReportCategoryName", mode="before")
    @classmethod
    def _optional_text(cls, value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None
