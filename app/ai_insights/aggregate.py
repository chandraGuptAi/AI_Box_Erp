"""Shrinks the shipment rows into what the LLM and the on-screen report need.

Only the fields in FIELDS are ever read (an allow-list), so customer names, phones, emails, addresses
and documents never leave this module. Every number is computed here in Python; the LLM only explains
them.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

# Logical field -> accepted keys (compared lower-cased, without non-alphanumerics), from ERP order-list
# rows (aliases shared with AI_python_backend app/services/shipment_insights.py) and from ERP report file
# headers (e.g. Unified First Mile: orderId, AWB_Number, AWB_Generation_Date, Order_Tracking_Status,
# Courier, Payment_Mode, Warehouse_City, Warehouse_State). Name, phone, email and address columns
# (Warehouse_Address, Warehouse_Landmark, consignee_*) are deliberately absent.
FIELDS: dict[str, tuple[str, ...]] = {
    "order_id": ("mastercustomorderid", "orderid", "ordernumber", "referencenumber"),
    "awb": ("awbnumber", "awbno", "awb", "lrnumber", "lrn", "trackingnumber"),
    "order_date": ("manifestdatetime", "awbgenerationdate", "awbgenerateddate", "manifestdate", "orderdate",
                   "createdat", "ordercreateddate", "bookingdate", "shipmentdate"),
    "courier": ("couriername", "courier", "carriername", "courierpartner"),
    "dest_city": ("consigneecity", "destinationcity", "destcity", "deliverycity", "city"),
    "dest_state": ("consigneestate", "destinationstate", "deststate", "deliverystate", "state"),
    "origin_city": ("warehousecity", "pickupcity", "origincity", "sourcecity", "pickuplocationcity"),
    "origin_state": ("warehousestate", "pickupstate", "originstate", "sourcestate", "pickuplocationstate"),
    # Used only to filter by seller; never sent to the LLM (not in SAMPLE_FIELDS).
    "customer_code": ("customercode", "custcode", "customerid", "custid", "sellercode", "sellerid", "clientcode"),
    "status": ("status", "ordertrackingstatus", "trackingstatus", "orderstatus", "shipmentstatus",
               "currentstatus"),
    "payment_mode": ("paymentmode", "paymenttype", "paymentmethod"),
    "weight": ("weightkg", "totalweight", "chargeableweight", "chargedweight", "weight"),
    "pickup_date": ("domestictransitstatsdatepickup", "pickupdate", "pickeddate", "pickedupdate", "pickupdatetime"),
    "delivery_date": ("domestictransitstatsdatedelivery", "deliverydate", "delivereddate", "deliveredat",
                      "deliverydatetime"),
    "remark": ("domestictransitstatscourierremarks", "courierremark", "courierremarks", "lastremark",
               "trackingremark", "statusremark", "ndrreason", "undeliveredreason", "rtoreason",
               "failurereason", "nonpickupreason", "pickupfailurereason"),
    "cancel_reason": ("ordercancelreason", "cancelreason", "cancellationreason"),
}
# Sent to the LLM as sample rows (identifiers excluded: the analysis does not need them).
SAMPLE_FIELDS = ("order_date", "courier", "origin_city", "origin_state", "dest_city", "dest_state", "status",
                 "payment_mode", "weight", "pickup_date", "delivery_date", "remark")
AGE_BUCKETS = ((1, "0-1 days"), (3, "2-3 days"), (7, "4-7 days"), (15, "8-15 days"), (None, "over 15 days"))
TOP_N = 15


def _norm(key: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _flatten(row: dict[str, Any], out: dict[str, Any] | None = None) -> dict[str, Any]:
    out = {} if out is None else out
    for key, value in row.items():
        if isinstance(value, dict):
            _flatten(value, out)
        elif not isinstance(value, list):
            out.setdefault(_norm(key), value)
    return out


def pick(row: dict[str, Any]) -> dict[str, Any]:
    """Only the allow-listed fields of one order-list row, under logical names."""
    flat = _flatten(row)
    picked = {}
    for field, keys in FIELDS.items():
        value = next((flat[k] for k in keys if flat.get(k) not in (None, "")), None)
        picked[field] = value.strip() if isinstance(value, str) else value
    return picked


def identity(row: dict[str, Any]) -> tuple[str, str] | None:
    order_id, awb = str(row.get("order_id") or ""), str(row.get("awb") or "")
    return (order_id, awb) if order_id or awb else None


def _parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("T", " ").rstrip("Z")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return datetime.strptime(text[:19] if "%S" in fmt else text[:10], fmt)
        except ValueError:
            continue
    return None


def status_group(status: Any) -> str:
    s = _norm(status)
    if not s:
        return "unknown"
    if "rto" in s or "return" in s:
        return "rto"
    if "undeliver" in s or "ndr" in s or "failed" in s:
        return "undelivered"
    if "deliver" in s and "outfor" not in s:
        return "delivered"
    if "cancel" in s:
        return "cancelled"
    if "lost" in s:
        return "lost"
    return "in_progress"


def status_matches(ui_status: str, value: Any) -> bool:
    """Whether a row's status (order list or report file wording) is one of the 6 UI statuses."""
    s, group = _norm(value), status_group(value)
    if ui_status == "Delivered":
        return group == "delivered"
    if ui_status == "Undelivered":
        return group == "undelivered"
    if group in ("rto", "delivered", "undelivered", "cancelled", "lost"):
        return False
    if ui_status == "Pickup Scheduled":
        return "pickupscheduled" in s or s in ("manifested", "pickuppending", "readytoship")
    if ui_status == "Not Picked":
        return "notpicked" in s or "pickupfailed" in s or "pickupcancel" in s
    if ui_status == "In-Transit":
        return "intransit" in s or s in ("pickedup", "shipped", "dispatched", "reachedatdestinationhub")
    if ui_status == "Out For Delivery":
        return "outfordelivery" in s
    return False


def parse_date(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    return _parse_date(value)


def _pct(part: int, whole: int) -> float:
    return round(part * 100.0 / whole, 1) if whole else 0.0


def _label(value: Any, default: str = "Unknown") -> str:
    text = str(value).strip() if value not in (None, "") else ""
    if not text.isupper():
        return text or default
    # "DELHI" -> "Delhi", but short acronyms (COD, RTO, NDR) stay as they are.
    return " ".join(w if len(w) <= 3 and w.isalpha() else w.title() for w in text.split())


def build(rows: list[dict[str, Any]], *, total_matching: int, sample_size: int,
          as_of: datetime | None = None) -> dict[str, Any]:
    """Aggregates over `rows` (already reduced with pick()) plus a capped sample.

    `as_of` (when the data was taken, e.g. the report file's time) dates the open-shipment ageing.
    """
    n = len(rows)
    groups = Counter(status_group(r["status"]) for r in rows)
    statuses = Counter(_label(r["status"]) for r in rows)

    per_courier: dict[str, Counter] = defaultdict(Counter)
    per_city: dict[str, Counter] = defaultdict(Counter)
    per_state: dict[str, Counter] = defaultdict(Counter)
    per_origin: dict[str, Counter] = defaultdict(Counter)
    per_day: dict[str, Counter] = defaultdict(Counter)
    ageing = Counter()
    payment = Counter()
    reasons = Counter()
    delivery_days: list[float] = []
    for r in rows:
        g = status_group(r["status"])
        buckets = [(per_courier, _label(r["courier"]))]
        if r.get("dest_city") or r.get("dest_state"):
            buckets += [(per_city, _label(r["dest_city"])), (per_state, _label(r["dest_state"]))]
        if r.get("origin_city"):
            buckets.append((per_origin, _label(r["origin_city"])))
        for bucket, key in buckets:
            bucket[key]["total"] += 1
            bucket[key][g] += 1
            bucket[key]["status:" + _label(r["status"])] += 1
        day = parse_date(r["order_date"])
        if day and as_of and g == "in_progress":
            age = max(0.0, (as_of - day).total_seconds() / 86400)
            ageing[next(label for limit, label in AGE_BUCKETS if limit is None or age <= limit)] += 1
        if day:
            per_day[day.strftime("%Y-%m-%d")]["total"] += 1
            per_day[day.strftime("%Y-%m-%d")][g] += 1
            per_day[day.strftime("%Y-%m-%d")]["status:" + _label(r["status"])] += 1
        payment[_label(r["payment_mode"])] += 1
        if g in ("undelivered", "rto", "cancelled", "lost"):
            reason = r["remark"] if g != "cancelled" else (r["cancel_reason"] or r["remark"])
            if reason:
                reasons[re.sub(r"\s+", " ", str(reason)).strip()[:120]] += 1
        picked, delivered = parse_date(r["pickup_date"]), parse_date(r["delivery_date"])
        if g == "delivered" and picked and delivered and delivered >= picked:
            delivery_days.append((delivered - picked).total_seconds() / 86400)

    top_statuses = [s for s, _ in statuses.most_common(4)]

    def table(bucket: dict[str, Counter], limit: int | None = TOP_N) -> list[dict[str, Any]]:
        items = sorted(bucket.items(), key=lambda kv: (-kv[1]["total"], kv[0]))
        return [{"name": name, "shipments": c["total"], "delivered": c["delivered"], "rto": c["rto"],
                 "undelivered": c["undelivered"], "delivery_pct": _pct(c["delivered"], c["total"]),
                 "rto_pct": _pct(c["rto"], c["total"]),
                 "statuses": {s: c["status:" + s] for s in top_statuses}} for name, c in items[:limit]]

    return {
        "definitions": {
            "delivery_pct": "delivered shipments / all shipments analysed x 100",
            "rto_pct": "RTO (return to origin) shipments / all shipments analysed x 100",
            "avg_delivery_days": "average days from pickup to delivery, delivered shipments only",
            "open_shipment_age": "days from order/AWB date to the data's as-of time, shipments not yet "
                                 "delivered, returned, cancelled or lost",
            "by_origin_city": "pickup (warehouse) city",
        },
        "as_of": as_of.strftime("%Y-%m-%d %H:%M") if as_of else None,
        "shipments_analysed": n,
        "shipments_matching": total_matching,
        "truncated": total_matching > n,
        "metrics": {
            "total_shipments": n,
            "delivered": groups["delivered"],
            "undelivered": groups["undelivered"],
            "rto": groups["rto"],
            "in_progress": groups["in_progress"],
            "cancelled": groups["cancelled"],
            "lost": groups["lost"],
            "delivery_pct": _pct(groups["delivered"], n),
            "rto_pct": _pct(groups["rto"], n),
            "undelivered_pct": _pct(groups["undelivered"], n),
            "avg_delivery_days": round(sum(delivery_days) / len(delivery_days), 1) if delivery_days else None,
        },
        "by_status": [{"status": s, "shipments": c, "pct": _pct(c, n)} for s, c in statuses.most_common()],
        "by_courier": table(per_courier),
        "by_city": table(per_city),
        "by_state": table(per_state, limit=10),
        "by_origin_city": table(per_origin),
        "open_shipment_age": ([{"age": label, "shipments": ageing[label],
                                "pct": _pct(ageing[label], groups["in_progress"])}
                               for _, label in AGE_BUCKETS] if ageing else []),
        "by_payment_mode": [{"payment_mode": p, "shipments": c, "pct": _pct(c, n)} for p, c in payment.most_common()],
        "top_failure_reasons": [{"reason": r, "shipments": c} for r, c in reasons.most_common(10)],
        "daily_trend": [{"date": d, "shipments": c["total"], "delivered": c["delivered"], "rto": c["rto"],
                         "statuses": {s: c["status:" + s] for s in top_statuses}}
                        for d, c in sorted(per_day.items())],
        "sample_rows": [{f: r[f] for f in SAMPLE_FIELDS if r.get(f) not in (None, "")}
                        for r in (rows[::max(1, n // sample_size)][:sample_size] if sample_size > 0 else [])],
    }
