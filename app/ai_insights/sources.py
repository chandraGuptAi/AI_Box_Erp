"""Rows for one report type from the ERP report file the panel attached.

The panel picks the newest COMPLETED xlsx/csv file of the report type dated in fromDate..toDate and
attaches it. A file is a snapshot (checked on a real Unified First Mile file: the one generated
22-10-2026 13:30 holds the whole open pickup backlog at that moment, each AWB once), so its generation
time is the data's "as of" time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import aggregate
from .report_file import ParsedFile
from .schemas import GenerateRequest

# "<name>_dd-MM-yyyy HH-mm-ss.<ext>"; the ERP also writes "_" between date and time.
_NAME_STAMP = re.compile(r"_(\d{2})-(\d{2})-(\d{4})[ _](\d{2})-(\d{2})-(\d{2})\.[A-Za-z0-9]+$")


class NoCustomerColumn(Exception):
    """A seller filter was asked for but the file cannot be filtered by seller."""


@dataclass
class FileRows:
    rows: list[dict[str, Any]]
    matching: int                    # rows that passed the filters, before the row cap
    read: int                        # data rows read from the file
    notes: list[str] = field(default_factory=list)


def _norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def report_time(name: str | None) -> datetime | None:
    """Generation time from an ERP report name ("..._21-11-2026 13-30-07.xlsx"), IST; None if absent."""
    match = _NAME_STAMP.search(str(name or ""))
    if not match:
        return None
    dd, mm, yyyy, hh, mi, ss = map(int, match.groups())
    try:
        return datetime(yyyy, mm, dd, hh, mi, ss)
    except ValueError:
        return None


def matches(row: dict[str, Any], body: GenerateRequest) -> bool:
    if body.customerCode and str(row.get("customer_code") or "").strip() != body.customerCode:
        return False
    if body.statuses and not any(aggregate.status_matches(s, row.get("status")) for s in body.statuses):
        return False
    day = aggregate.parse_date(row.get("order_date"))
    if day and not (body.fromDate <= day.date() <= body.toDate):
        return False
    return True


def file_rows(parsed: ParsedFile, body: GenerateRequest, cap: int, file_name: str) -> FileRows:
    """Allow-listed fields only (aggregate.pick), request filters applied, repeated AWBs dropped."""
    headers = {_norm(h) for h in parsed.headers}
    if body.customerCode and not headers & set(aggregate.FIELDS["customer_code"]):
        raise NoCustomerColumn(f"the report file has no customer code column, so it cannot be filtered to "
                               f"seller {body.customerCode}")
    out = FileRows(rows=[], matching=0, read=len(parsed.rows))
    if parsed.truncated:
        out.notes.append(f"{file_name} has more than {cap:,} rows; only the first {cap:,} were read.")
    seen: set[tuple[str, str]] = set()
    for raw in parsed.rows:
        row = aggregate.pick(raw)
        if not matches(row, body):
            continue
        key = aggregate.identity(row)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        out.matching += 1
        if len(out.rows) < cap:
            out.rows.append(row)
    return out
