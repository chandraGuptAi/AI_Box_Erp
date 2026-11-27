"""AI report → PDF (reportlab) or Word .docx (python-docx), built from the report JSON the panel renders.
Charts are drawn with matplotlib in the panel's colours. Fonts are DejaVu (shipped with matplotlib), so
₹, — and other non-ASCII text print correctly. Pure functions: render(report, fmt) -> (bytes, filename, type).
"""
from __future__ import annotations

import io
import os
import re
from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape

import matplotlib
from matplotlib.figure import Figure
from matplotlib.ticker import FuncFormatter, MaxNLocator

FORMATS = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
PALETTE = ["#5b5fe9", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#2a78d6", "#e34948"]
INK, MUTED, ACCENT = "#1f2937", "#64748b", "#6555df"
_ALIASES = {"column": "bar", "hbar": "horizontal_bar", "donut": "doughnut", "ring": "doughnut", "area_line": "area"}
_KINDS = {"bar", "horizontal_bar", "line", "area", "pie", "doughnut"}
_FONT_DIR = os.path.join(matplotlib.get_data_path(), "fonts", "ttf")


# ---- shared helpers ---------------------------------------------------------------------------------
def file_name(title: str, fmt: str) -> str:
    base = re.sub(r"[\\/:*?\"<>|%#]+", " ", title or "AI report").replace("—", "-").replace("–", "-")
    base = re.sub(r"\s+", "_", base.strip())[:100] or "AI_report"
    return f"{base}.{fmt}"


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _metric_value(m: dict[str, Any]) -> str:
    value, unit = m.get("value"), m.get("unit")
    if isinstance(value, (int, float)) and unit == "number":
        text = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
    else:
        text = _text(value)
    return f"{text}%" if unit == "percent" else text


def _generated(report: dict[str, Any]) -> str:
    raw = report.get("generated_at")
    try:
        return datetime.fromisoformat(str(raw)).strftime("%d %b %Y, %I:%M %p")
    except (TypeError, ValueError):
        return _text(raw)


def _meta_line(report: dict[str, Any]) -> list[str]:
    s = report.get("scope") or {}
    items = []
    if s.get("period"):
        items.append(f"Period: {s['period']}")
    if s.get("records_analyzed") is not None:
        items.append(f"{s['records_analyzed']:,} records analysed")
    if s.get("source"):
        items.append(f"Source: {s['source']}")
    if report.get("generated_at"):
        items.append(f"Generated: {_generated(report)}")
    return items


def _findings(report: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    findings = [f for f in report.get("findings") or [] if isinstance(f, dict)]
    attention = [f for f in findings if f.get("severity") in ("high", "medium")]
    return attention, [f for f in findings if f not in attention]


def _metrics(report: dict[str, Any]) -> list[dict]:
    return [m for m in report.get("metrics") or []
            if isinstance(m, dict) and m.get("label") and m.get("value") not in (None, "")]


# ---- charts -----------------------------------------------------------------------------------------
def _fmt(value: float, value_format: dict | None) -> str:
    kind = (value_format or {}).get("type", "number")
    if kind == "percent":
        return f"{value:.{(value_format or {}).get('decimals', 1)}f}%"
    text = f"{value:,.0f}" if float(value).is_integer() else f"{value:,.2f}"
    return f"₹{text}" if kind == "currency" else text


def chart_kind(chart: dict[str, Any]) -> str:
    kind = _ALIASES.get(str(chart.get("type")), str(chart.get("type")))
    # A trend of a single point is just one bar.
    if kind in ("line", "area") and len(chart.get("labels") or []) < 2:
        return "bar"
    return kind


def is_wide(chart: dict[str, Any]) -> bool:
    """Charts that need the full page width; the rest are laid out two per row."""
    kind = chart_kind(chart)
    labels = chart.get("labels") or []
    return kind in ("line", "area") or (kind == "bar" and len(labels) > 8) or (kind == "horizontal_bar" and len(labels) > 12)


PIE_SLICES = 6


def _pie_slices(labels: list[str], values: list[float]) -> tuple[list[str], list[float]]:
    """The largest PIE_SLICES slices; the rest are summed into "Other" so the legend stays readable."""
    pairs = sorted(zip(labels, values), key=lambda lv: -lv[1])
    if len(pairs) <= PIE_SLICES + 1:
        return [l for l, _ in pairs], [v for _, v in pairs]
    head, tail = pairs[:PIE_SLICES], pairs[PIE_SLICES:]
    return [l for l, _ in head] + [f"Other ({len(tail)})"], [v for _, v in head] + [sum(v for _, v in tail)]


def chart_png(chart: dict[str, Any], compact: bool = False) -> bytes | None:
    """The chart as a PNG (compact = half page width), or None when it cannot be drawn."""
    kind = chart_kind(chart)
    labels = [str(x) for x in chart.get("labels") or []]
    datasets = [d for d in chart.get("datasets") or [] if isinstance(d, dict)]
    if kind not in _KINDS or not labels or not datasets:
        return None
    try:
        series = [[float(v) for v in d.get("data") or []] for d in datasets]
    except (TypeError, ValueError):
        return None
    if any(len(s) != len(labels) for s in series):
        return None
    vf = chart.get("value_format")

    circular = kind in ("pie", "doughnut")
    width = 4.6 if compact else 7.2
    height = 2.9 if compact else 3.0
    if kind == "horizontal_bar":
        per_bar = 0.22 + 0.06 * (len(datasets) - 1)
        height = max(1.9 if compact else 2.2, min(4.2, per_bar * len(labels) + 0.9))
    if circular:
        height = 2.7 if compact else 3.0
    fig = Figure(figsize=(width, height), dpi=170)
    ax = fig.add_subplot(111)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    ax.spines["left"].set_color("#d5d9e2")
    ax.spines["bottom"].set_color("#d5d9e2")
    ax.tick_params(colors="#4b5563", labelsize=8)
    short = lambda s, n: s if len(s) <= n else s[: n - 1] + "…"  # noqa: E731
    tick = FuncFormatter(lambda v, _: _fmt(v, vf) if abs(v) < 1e4 else f"{v / 1e3:,.1f}K")
    # Counts are whole numbers: no 0.25 / 0.50 ticks.
    integer_axis = MaxNLocator(integer=True) if all(float(v).is_integer() for s in series for v in s) else None
    group = 0.8 if len(labels) >= 4 else 0.5      # thinner bars when there are only a few

    if circular:
        labels, values = _pie_slices(labels, series[0])
        wedges, *_ = ax.pie(values, colors=PALETTE * 4, startangle=90, counterclock=False,
                            autopct=lambda p: f"{p:.0f}%" if p >= 4 else "", pctdistance=0.78 if kind == "doughnut" else 0.62,
                            wedgeprops={"width": 0.42 if kind == "doughnut" else 1, "edgecolor": "white", "linewidth": 1.5},
                            textprops={"fontsize": 8, "color": "white", "weight": "bold"})
        ax.set_aspect("equal")
        ax.legend(wedges, [f"{short(l, 26)} ({_fmt(v, vf)})" for l, v in zip(labels, values)], loc="center left",
                  bbox_to_anchor=(1.0, 0.5), frameon=False, fontsize=8)
    elif kind == "horizontal_bar":
        n = len(series)
        h = group / n
        for i, (d, s) in enumerate(zip(datasets, series)):
            pos = [y + i * h - group / 2 + h / 2 for y in range(len(labels))]
            bars = ax.barh(pos, s, height=h * 0.9, color=PALETTE[i % len(PALETTE)], label=_text(d.get("label")))
            if n == 1:
                ax.bar_label(bars, labels=[_fmt(v, vf) for v in s], padding=3, fontsize=7.5, color="#4b5563")
        ax.set_yticks(range(len(labels)), [short(l, 28) for l in labels])
        ax.invert_yaxis()
        if integer_axis:
            ax.xaxis.set_major_locator(integer_axis)
        ax.xaxis.set_major_formatter(tick)
        ax.grid(axis="x", color="#eceef3")
        ax.set_axisbelow(True)
    elif kind == "bar":
        n = len(series)
        w = group / n
        for i, (d, s) in enumerate(zip(datasets, series)):
            pos = [x + i * w - group / 2 + w / 2 for x in range(len(labels))]
            bars = ax.bar(pos, s, width=w * 0.9, color=PALETTE[i % len(PALETTE)], label=_text(d.get("label")))
            if n == 1 and len(labels) <= 12:
                ax.bar_label(bars, labels=[_fmt(v, vf) for v in s], padding=2, fontsize=7.5, color="#4b5563")
        ax.set_xticks(range(len(labels)), [short(l, 14) for l in labels], rotation=35 if len(labels) > 6 else 0,
                      ha="right" if len(labels) > 6 else "center")
        if len(labels) < 4:
            ax.set_xlim(-0.75, len(labels) - 0.25)
        if integer_axis:
            ax.yaxis.set_major_locator(integer_axis)
        ax.yaxis.set_major_formatter(tick)
        ax.grid(axis="y", color="#eceef3")
        ax.set_axisbelow(True)
    else:  # line / area
        x = list(range(len(labels)))
        for i, (d, s) in enumerate(zip(datasets, series)):
            color = PALETTE[i % len(PALETTE)]
            ax.plot(x, s, color=color, linewidth=1.8, marker="o" if len(x) <= 20 else None, markersize=3,
                    label=_text(d.get("label")))
            if kind == "area":
                ax.fill_between(x, s, color=color, alpha=0.15)
        step = max(1, len(labels) // 10)
        ax.set_xticks(x[::step], [short(l, 12) for l in labels[::step]], rotation=35 if len(labels) > 8 else 0,
                      ha="right" if len(labels) > 8 else "center")
        if integer_axis:
            ax.yaxis.set_major_locator(integer_axis)
        ax.yaxis.set_major_formatter(tick)
        ax.grid(axis="y", color="#eceef3")
        ax.set_axisbelow(True)
    if not circular and len(series) > 1:
        # Above the plot, so it never covers bars.
        ax.legend(frameon=False, fontsize=7.5, loc="lower left", bbox_to_anchor=(0, 1.0), ncol=len(series),
                  handlelength=1.2, columnspacing=1.2)

    fig.tight_layout()
    out = io.BytesIO()
    # "tight" keeps a legend placed beside the plot inside the picture.
    fig.savefig(out, format="png", facecolor="white", bbox_inches="tight", pad_inches=0.08)
    return out.getvalue()


def _chart_images(report: dict[str, Any]) -> list[tuple[str, bytes, bool]]:
    """(title, png, wide) for every drawable chart, in report order."""
    images = []
    for chart in report.get("charts") or []:
        if isinstance(chart, dict):
            wide = is_wide(chart)
            png = chart_png(chart, compact=not wide)
            if png:
                images.append((_text(chart.get("title")) or "Chart", png, wide))
    return images


# ---- PDF ---------------------------------------------------------------------------------------------
def build_pdf(report: dict[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (CondPageBreak, HRFlowable, Image, KeepTogether, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)

    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", os.path.join(_FONT_DIR, "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("DejaVu-Bold", os.path.join(_FONT_DIR, "DejaVuSans-Bold.ttf")))
    base = dict(fontName="DejaVu", textColor=colors.HexColor(INK))
    st = {
        "title": ParagraphStyle("t", fontName="DejaVu-Bold", fontSize=18, leading=23, textColor=colors.HexColor("#182039")),
        "sub": ParagraphStyle("s", fontSize=10.5, leading=14, textColor=colors.HexColor("#5a6072"), **{k: v for k, v in base.items() if k != "textColor"}),
        "meta": ParagraphStyle("m", fontSize=8.5, leading=12, textColor=colors.HexColor(MUTED), fontName="DejaVu"),
        "h2": ParagraphStyle("h2", fontName="DejaVu-Bold", fontSize=12.5, leading=16, spaceBefore=12, spaceAfter=6,
                             textColor=colors.HexColor("#202b43")),
        "body": ParagraphStyle("b", fontSize=9.5, leading=14, spaceAfter=5, **base),
        "bold": ParagraphStyle("bb", fontSize=9.5, leading=13, **{**base, "fontName": "DejaVu-Bold"}),
        "small": ParagraphStyle("sm", fontSize=8.5, leading=11.5, textColor=colors.HexColor(MUTED), fontName="DejaVu"),
        "kpi_label": ParagraphStyle("kl", fontSize=7, leading=9, textColor=colors.HexColor(MUTED), fontName="DejaVu-Bold"),
        "kpi_value": ParagraphStyle("kv", fontSize=15, leading=19, fontName="DejaVu-Bold", textColor=colors.HexColor("#0f172a")),
        "cell": ParagraphStyle("c", fontSize=8, leading=10.5, **base),
        "cell_r": ParagraphStyle("cr", fontSize=8, leading=10.5, alignment=TA_RIGHT, **base),
        "cell_h": ParagraphStyle("ch", fontSize=7.5, leading=10, fontName="DejaVu-Bold", textColor=colors.HexColor("#334155")),
    }
    p = lambda text, style="body": Paragraph(escape(_text(text)), st[style])  # noqa: E731

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=18 * mm,
                            bottomMargin=16 * mm, title=_text(report.get("title")), author="Bigship AI Box")
    story: list = [p(report.get("title"), "title")]
    if report.get("subtitle"):
        story.append(p(report["subtitle"], "sub"))
    meta = _meta_line(report)
    if meta:
        story += [Spacer(1, 4), Paragraph("  |  ".join(escape(m) for m in meta), st["meta"])]
    story += [Spacer(1, 6), HRFlowable(width="100%", thickness=1.5, color=colors.HexColor(ACCENT)), Spacer(1, 6)]
    summary_note = (report.get("scope") or {}).get("summary")
    if summary_note:
        box = Table([[p(summary_note, "small")]], colWidths=[doc.width])
        box.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f5f4ff")),
                                 ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 5),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]))
        story += [box, Spacer(1, 8)]

    metrics = _metrics(report)
    if metrics:
        per_row = 4
        tone = {"ok": "#047857", "bad": "#b91c1c", "warning": "#b45309"}
        cells = [[p(m["label"].upper(), "kpi_label"),
                  Paragraph(escape(_metric_value(m)), ParagraphStyle("v", parent=st["kpi_value"],
                                                                     textColor=colors.HexColor(tone.get(m.get("tone"), "#0f172a")))),
                  p(m.get("note") or "", "small")] for m in metrics]
        rows = [cells[i:i + per_row] for i in range(0, len(cells), per_row)]
        rows[-1] += [""] * (per_row - len(rows[-1]))
        grid = Table(rows, colWidths=[doc.width / per_row] * per_row)
        grid.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#e2e8f0")),
                                  ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.HexColor("#e2e8f0")),
                                  ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 7)]))
        story += [grid, Spacer(1, 4)]

    def section(title: str, groups: list[list]) -> None:
        """Each group (a list of flowables) stays on one page, and the heading stays with the first group.
        Groups are never nested KeepTogethers: reportlab sizes a nested one as page-high, which breaks pages."""
        groups = [g for g in groups if g]
        if not groups:
            return
        story.append(KeepTogether([p(title, "h2")] + groups[0]))
        story.extend(KeepTogether(g) if len(g) > 1 else g[0] for g in groups[1:])

    def bullet(text: Any, style: str = "body", mark: str = "•"):
        return Paragraph(escape(_text(text)), ParagraphStyle(f"bl-{style}", parent=st[style], leftIndent=12,
                                                             bulletIndent=0, spaceAfter=3), bulletText=mark)

    es = report.get("executive_summary") or {}
    section("Executive Summary", [[p(x)] for x in es.get("paragraphs") or []] + [[bullet(x)] for x in es.get("highlights") or []])

    images = _chart_images(report)
    if images:
        charts_story: list = []
        gap = 6 * mm
        half = (doc.width - gap) / 2

        def picture(png: bytes, width: float):
            img = Image(io.BytesIO(png))
            img.drawWidth, img.drawHeight = width, width * img.imageHeight / img.imageWidth
            return img

        pending: list = []      # half-width charts waiting for a partner

        def flush():
            if not pending:
                return
            cells = [[p(t, "bold"), Spacer(1, 3), picture(png, half)] for t, png in pending]
            if len(cells) == 1:
                cells.append("")
            row = Table([cells], colWidths=[half + gap / 2, half + gap / 2])
            row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                     ("RIGHTPADDING", (0, 0), (-1, -1), gap / 2),
                                     ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
            charts_story.append([row])
            pending.clear()

        for title, png, wide in images:
            if wide:
                flush()
                charts_story.append([p(title, "bold"), Spacer(1, 3), picture(png, doc.width), Spacer(1, 8)])
            else:
                pending.append((title, png))
                if len(pending) == 2:
                    flush()
        flush()
        section("Charts", charts_story)

    tag = lambda text: f" <font color='#b42318' size='7'>{escape(_text(text).upper())}</font>" if text else ""  # noqa: E731
    attention, others = _findings(report)
    section("What needs your attention?", [
        [Paragraph(escape(_text(f.get("title"))) + tag(f.get("severity")), st["bold"]), p(f.get("detail"))]
        for f in attention])
    section("Key Observations", [[p(f.get("title"), "bold"), p(f.get("detail"))] for f in others])
    recs = [r for r in report.get("recommendations") or [] if isinstance(r, dict)]
    rec_blocks = []
    for i, r in enumerate(recs, 1):
        head = Paragraph(escape(_text(r.get("action"))) + tag(r.get("priority")),
                         ParagraphStyle("rec", parent=st["bold"], leftIndent=16, bulletIndent=0), bulletText=f"{i}.")
        why = [Paragraph(escape(_text(r["rationale"])), ParagraphStyle("why", parent=st["body"], leftIndent=16))] \
            if r.get("rationale") else []
        rec_blocks.append([head] + why + [Spacer(1, 2)])
    section("Recommendations", rec_blocks)

    numeric = re.compile(r"^[₹$]?\s?-?[\d,]+(\.\d+)?\s?%?$")
    for t in report.get("tables") or []:
        if not isinstance(t, dict) or not t.get("columns"):
            continue
        cols = [_text(c) for c in t["columns"]]
        data = [[Paragraph(escape(c), st["cell_h"]) for c in cols]]
        for row in t.get("rows") or []:
            data.append([Paragraph(escape(_text(c)), st["cell_r"] if numeric.match(_text(c).strip()) else st["cell"])
                         for c in list(row)[: len(cols)]])
        table = Table(data, colWidths=[doc.width / len(cols)] * len(cols), repeatRows=1)
        table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2ff")),
                                   ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fafbff")]),
                                   ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e2e8f0")),
                                   ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                                   ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
        # A table may continue on the next page (header row repeats); start it on a new page only when
        # less than a heading and a few rows' space is left, so pages are not left half empty.
        story += [CondPageBreak(40 * mm), p(t.get("title"), "h2"), table]
        if t.get("note"):
            story += [Spacer(1, 3), p(t["note"], "small")]
    section("Data Scope & Limitations", [[bullet(x, "small")] for x in report.get("limitations") or []])

    title = _text(report.get("title"))

    def decorate(canvas, d):
        canvas.saveState()
        canvas.setFont("DejaVu", 7.5)
        canvas.setFillColor(colors.HexColor(MUTED))
        canvas.drawString(d.leftMargin, A4[1] - 11 * mm, title[:110])
        canvas.drawRightString(A4[0] - d.rightMargin, 9 * mm, f"Bigship AI Box  |  Page {d.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()


# ---- Word (.docx) ------------------------------------------------------------------------------------
def build_docx(report: dict[str, Any]) -> bytes:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    for section in doc.sections:
        section.left_margin = section.right_margin = Inches(0.8)

    def muted(paragraph, size=9):
        for run in paragraph.runs:
            run.font.size = Pt(size)
            run.font.color.rgb = RGBColor(0x64, 0x74, 0x8B)
        return paragraph

    def table(headers: list[str], rows: list[list[str]], numeric_cols: set[int] = frozenset()):
        t = doc.add_table(rows=1, cols=len(headers))
        t.style = "Light Grid Accent 1"
        for i, h in enumerate(headers):
            t.rows[0].cells[i].text = h
        for row in rows:
            cells = t.add_row().cells
            for i, value in enumerate(row[: len(headers)]):
                cells[i].text = value
                if i in numeric_cols:
                    cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
        return t

    doc.add_heading(_text(report.get("title")), 0)
    if report.get("subtitle"):
        doc.add_paragraph(_text(report["subtitle"]))
    meta = _meta_line(report)
    if meta:
        muted(doc.add_paragraph("  |  ".join(meta)))
    if (report.get("scope") or {}).get("summary"):
        muted(doc.add_paragraph(_text(report["scope"]["summary"])), 9.5)

    metrics = _metrics(report)
    if metrics:
        doc.add_heading("Key Metrics", 1)
        table(["Metric", "Value", "Note"], [[_text(m["label"]), _metric_value(m), _text(m.get("note"))] for m in metrics], {1})

    es = report.get("executive_summary") or {}
    if es.get("paragraphs") or es.get("highlights"):
        doc.add_heading("Executive Summary", 1)
        for x in es.get("paragraphs") or []:
            doc.add_paragraph(_text(x))
        for x in es.get("highlights") or []:
            doc.add_paragraph(_text(x), style="List Bullet")

    images = _chart_images(report)
    if images:
        doc.add_heading("Charts", 1)
        for title, png, wide in images:
            doc.add_paragraph().add_run(title).bold = True
            doc.add_picture(io.BytesIO(png), width=Inches(6.4 if wide else 4.6))

    attention, others = _findings(report)
    if attention:
        doc.add_heading("What needs your attention?", 1)
        for f in attention:
            para = doc.add_paragraph()
            para.add_run(_text(f.get("title"))).bold = True
            tag = para.add_run(f"  {_text(f.get('severity')).upper()}")
            tag.font.size = Pt(8)
            tag.font.color.rgb = RGBColor(0xB4, 0x23, 0x18)
            doc.add_paragraph(_text(f.get("detail")))
    if others:
        doc.add_heading("Key Observations", 1)
        for f in others:
            doc.add_paragraph().add_run(_text(f.get("title"))).bold = True
            doc.add_paragraph(_text(f.get("detail")))
    recs = [r for r in report.get("recommendations") or [] if isinstance(r, dict)]
    if recs:
        doc.add_heading("Recommendations", 1)
        for r in recs:
            para = doc.add_paragraph(style="List Number")
            para.add_run(_text(r.get("action"))).bold = True
            if r.get("priority"):
                tag = para.add_run(f"  {_text(r['priority']).upper()}")
                tag.font.size = Pt(8)
            if r.get("rationale"):
                para.add_run(f"\n{_text(r['rationale'])}")

    numeric = re.compile(r"^[₹$]?\s?-?[\d,]+(\.\d+)?\s?%?$")
    for t in report.get("tables") or []:
        if not isinstance(t, dict) or not t.get("columns"):
            continue
        rows = [[_text(c) for c in row] for row in t.get("rows") or []]
        nums = {i for i in range(len(t["columns"])) if rows and all(numeric.match(r[i].strip()) for r in rows if i < len(r) and r[i])}
        doc.add_heading(_text(t.get("title")), 1)
        table([_text(c) for c in t["columns"]], rows, nums)
        if t.get("note"):
            muted(doc.add_paragraph(_text(t["note"])))
    if report.get("limitations"):
        doc.add_heading("Data Scope & Limitations", 1)
        for x in report["limitations"]:
            muted(doc.add_paragraph(_text(x), style="List Bullet"), 9.5)

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def render(report: dict[str, Any], fmt: str) -> tuple[bytes, str, str]:
    """(file bytes, file name, media type) for fmt 'pdf' or 'docx'."""
    if fmt not in FORMATS:
        raise ValueError(f"Unknown export format: {fmt}")
    content = build_pdf(report) if fmt == "pdf" else build_docx(report)
    return content, file_name(_text(report.get("title")), fmt), FORMATS[fmt]
