"""The JSON report the ERP panel renders on screen.

Every number (metrics, charts, scope) comes from aggregate.build() in Python; only the text (title,
summary, findings, recommendations, tables) comes from the LLM (already cleaned by llm._clean).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from .schemas import GenerateRequest

TOP_CHART = 10


def _day(d: date | datetime) -> str:
    return d.strftime("%d %b %Y")


def _metrics(m: dict[str, Any]) -> list[dict[str, Any]]:
    n = m["total_shipments"]
    out: list[dict[str, Any]] = [{"key": "total_shipments", "label": "Total Shipments", "value": n,
                                  "unit": "number"}]
    if m["delivered"]:
        out.append({"key": "delivery_pct", "label": "Delivery %", "value": m["delivery_pct"], "unit": "percent",
                    "tone": "ok", "note": f"{m['delivered']:,} delivered"})
    if m["rto"]:
        out.append({"key": "rto_pct", "label": "RTO %", "value": m["rto_pct"], "unit": "percent",
                    "tone": "bad", "note": f"{m['rto']:,} RTO"})
    if m["undelivered"]:
        out.append({"key": "undelivered_pct", "label": "Undelivered %", "value": m["undelivered_pct"],
                    "unit": "percent", "tone": "bad", "note": f"{m['undelivered']:,} undelivered"})
    if m["in_progress"]:
        out.append({"key": "in_progress", "label": "In Progress", "value": m["in_progress"], "unit": "number",
                    "tone": "warning", "note": "not yet delivered, returned, cancelled or lost"})
    if m["cancelled"]:
        out.append({"key": "cancelled", "label": "Cancelled", "value": m["cancelled"], "unit": "number"})
    if m["avg_delivery_days"] is not None:
        out.append({"key": "avg_delivery_days", "label": "Avg Delivery Days", "value": m["avg_delivery_days"],
                    "unit": "number", "note": "pickup to delivery"})
    return out


def _bar(chart_id: str, title: str, items: list[dict[str, Any]], name_key: str = "name",
         chart_type: str = "horizontal_bar", outcomes: bool = False, **extra: Any) -> dict[str, Any] | None:
    items = [i for i in items if i.get("shipments")][:TOP_CHART]
    if not items:
        return None
    datasets = [{"label": "Shipments", "data": [i["shipments"] for i in items]}]
    if outcomes:
        for key, label in (("delivered", "Delivered"), ("rto", "RTO")):
            values = [i.get(key) or 0 for i in items]
            if any(values):
                datasets.append({"label": label, "data": values})
    return {"id": chart_id, "title": title, "type": chart_type, "labels": [str(i[name_key]) for i in items],
            "datasets": datasets, "value_format": {"type": "number"}, **extra}


def _charts(data: dict[str, Any]) -> list[dict[str, Any]]:
    trend = data["daily_trend"]
    trend_chart = None
    if trend:
        datasets = [{"label": "Shipments", "data": [d["shipments"] for d in trend]}]
        for key, label in (("delivered", "Delivered"), ("rto", "RTO")):
            values = [d[key] for d in trend]
            if any(values):
                datasets.append({"label": label, "data": values})
        trend_chart = {"id": "trend", "title": "Daily trend", "type": "line", "wide": True,
                       "labels": [d["date"] for d in trend], "datasets": datasets,
                       "value_format": {"type": "number"}}
    charts = [
        _bar("status", "Shipments by status", data["by_status"], "status", "doughnut"),
        _bar("courier", "Courier performance", data["by_courier"], outcomes=True,
             note=f"Top {TOP_CHART} by shipments"),
        _bar("destination_city", "Destination cities", data["by_city"], outcomes=True,
             note=f"Top {TOP_CHART} by shipments"),
        _bar("origin_city", "Pickup cities", data["by_origin_city"], note=f"Top {TOP_CHART} by shipments"),
        trend_chart,
        _bar("open_age", "Age of open shipments", data["open_shipment_age"], "age", "bar"),
        _bar("payment_mode", "Payment mode", data["by_payment_mode"], "payment_mode", "pie"),
        _bar("failure_reasons", "Top failure reasons", data["top_failure_reasons"], "reason"),
    ]
    return [c for c in charts if c]


def _scope(body: GenerateRequest, data: dict[str, Any], source: dict[str, Any],
           as_of: datetime | None) -> dict[str, Any]:
    ref = f" (#{source['ReportHistoryId']})" if source.get("ReportHistoryId") else ""
    parts = [f"Snapshot as of {_day(as_of)} {as_of:%H:%M}." if as_of else "Snapshot time unknown.",
             f"Statuses: {', '.join(body.statuses)}." if body.statuses else "All statuses.",
             f"Seller {body.customerCode}." if body.customerCode else "All sellers."]
    return {"period": f"{_day(body.fromDate)} – {_day(body.toDate)}",
            "source": f"{source['file']}{ref}",
            "records_analyzed": source["rows_after_filters"],
            "summary": " ".join(parts)}


def _limitations(data: dict[str, Any], source: dict[str, Any], as_of: datetime | None) -> list[str]:
    out = list(source.get("notes") or [])
    if data["truncated"]:
        out.append(f"Only {data['shipments_analysed']:,} of {data['shipments_matching']:,} matching shipments "
                   f"were analysed.")
    if as_of is None:
        out.append("The file name has no generation time, so the age of open shipments is not shown.")
    return out


def build(*, category_name: str, body: GenerateRequest, data: dict[str, Any], analysis: dict[str, Any],
          source: dict[str, Any], as_of: datetime | None, generated_at: datetime) -> dict[str, Any]:
    return {
        "title": analysis.get("title") or f"{category_name} Report",
        "subtitle": f"{category_name} · AI insights",
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "scope": _scope(body, data, source, as_of),
        "executive_summary": {"paragraphs": analysis["summary_paragraphs"], "highlights": analysis["highlights"]},
        "metrics": _metrics(data["metrics"]),
        "charts": _charts(data),
        "tables": analysis["tables"],
        "findings": analysis["findings"],
        "recommendations": analysis["recommendations"],
        "limitations": _limitations(data, source, as_of),
    }
