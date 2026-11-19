"""PDF-смета (коммерческое предложение). Структура повторяет образец «Quotation»:
шапка с реквизитами → данные клиента и исполнителя → таблицы по изделиям (секциям)
с размерами, количеством, ценой и суммой → итоги → сумма прописью."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Image, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

from app.core.fmt import amount_in_words, date_str, money

FONT_DIR = Path(__file__).parent / "fonts"
_fonts_ready = False

INK = colors.HexColor("#1F1D1B")
MUTED = colors.HexColor("#6F6A64")
ACCENT = colors.HexColor("#A9876F")
ACCENT_SOFT = colors.HexColor("#F3ECE6")
LINE = colors.HexColor("#DDD6CF")
HEAD_BG = colors.HexColor("#2A2725")


def _ensure_fonts():
    global _fonts_ready
    if not _fonts_ready:
        pdfmetrics.registerFont(TTFont("Sans", str(FONT_DIR / "DejaVuSans.ttf")))
        pdfmetrics.registerFont(TTFont("Sans-Bold", str(FONT_DIR / "DejaVuSans-Bold.ttf")))
        pdfmetrics.registerFontFamily("Sans", normal="Sans", bold="Sans-Bold")
        _fonts_ready = True


@dataclass
class PdfLine:
    name: str
    size: str
    pieces: str
    quantity: str
    unit: str
    unit_price: Decimal
    total: Decimal


@dataclass
class PdfData:
    uid: str
    created_at: datetime
    company_name: str
    company_phone: str | None
    company_address: str | None
    company_email: str | None
    company_tax_id: str | None
    logo_path: Path | None
    order_number: int
    order_title: str
    client_name: str
    client_phone: str | None
    client_address: str | None
    sections: list[tuple[str, list[PdfLine]]]
    extra_label: str
    extra_amount: Decimal
    total: Decimal
    comment: str | None = None


def _styles():
    return {
        "h1": ParagraphStyle("h1", fontName="Sans-Bold", fontSize=18, leading=22, textColor=INK),
        "h2": ParagraphStyle("h2", fontName="Sans-Bold", fontSize=10, leading=13, textColor=INK),
        "small": ParagraphStyle("small", fontName="Sans", fontSize=8, leading=10.5, textColor=MUTED),
        "body": ParagraphStyle("body", fontName="Sans", fontSize=9, leading=12, textColor=INK),
        "cell": ParagraphStyle("cell", fontName="Sans", fontSize=8.5, leading=10.5, textColor=INK),
        "cell_r": ParagraphStyle("cell_r", fontName="Sans", fontSize=8.5, leading=10.5, textColor=INK,
                                 alignment=TA_RIGHT),
        "th": ParagraphStyle("th", fontName="Sans-Bold", fontSize=7.5, leading=9, textColor=colors.white),
        "th_r": ParagraphStyle("th_r", fontName="Sans-Bold", fontSize=7.5, leading=9, textColor=colors.white,
                               alignment=TA_RIGHT),
        "right": ParagraphStyle("right", fontName="Sans", fontSize=9, leading=12, textColor=INK, alignment=TA_RIGHT),
        "label": ParagraphStyle("label", fontName="Sans", fontSize=7.5, leading=10, textColor=MUTED, alignment=TA_LEFT),
    }


def _esc(s: str | None) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(data: PdfData, out: Path) -> Path:
    _ensure_fonts()
    st = _styles()
    out.parent.mkdir(parents=True, exist_ok=True)
    W = A4[0] - 30 * mm

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFont("Sans", 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(15 * mm, 10 * mm,
                          f"Смета {data.uid} · сформирована {data.created_at.strftime('%d.%m.%Y %H:%M')} · Furniture Estimate")
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Стр. {doc.page}")
        canvas.setStrokeColor(ACCENT)
        canvas.setLineWidth(2)
        canvas.line(15 * mm, A4[1] - 10 * mm, A4[0] - 15 * mm, A4[1] - 10 * mm)
        canvas.restoreState()

    doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=16 * mm, bottomMargin=18 * mm,
                            title=f"Смета {data.uid}", author=data.company_name)
    story = []

    # ---- шапка
    left = []
    if data.logo_path and data.logo_path.exists():
        try:
            img = Image(str(data.logo_path))
            ratio = img.imageWidth / float(img.imageHeight or 1)
            h = 16 * mm
            img.drawHeight, img.drawWidth = h, min(h * ratio, 50 * mm)
            left.append(img)
            left.append(Spacer(1, 2 * mm))
        except Exception:
            pass
    left.append(Paragraph(_esc(data.company_name), st["h2"]))
    contacts = " · ".join(x for x in [data.company_phone, data.company_email] if x)
    if contacts:
        left.append(Paragraph(_esc(contacts), st["small"]))
    if data.company_address:
        left.append(Paragraph(_esc(data.company_address), st["small"]))
    if data.company_tax_id:
        left.append(Paragraph(f"ИНН: {_esc(data.company_tax_id)}", st["small"]))
    right = [
        Paragraph("СМЕТА", ParagraphStyle("t", parent=st["h1"], alignment=TA_RIGHT)),
        Paragraph(f"№ {_esc(data.uid)}", st["right"]),
        Paragraph(f"от {date_str(data.created_at)}", ParagraphStyle("r2", parent=st["small"], alignment=TA_RIGHT)),
    ]
    head = Table([[left, right]], colWidths=[W * 0.6, W * 0.4])
    head.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                              ("RIGHTPADDING", (0, 0), (-1, -1), 0)]))
    story += [head, Spacer(1, 6 * mm)]

    # ---- клиент / заказ
    def info(label, value):
        return [Paragraph(label, st["label"]), Paragraph(_esc(value) or "—", st["body"])]

    client = [Paragraph("ЗАКАЗЧИК", st["label"]), Paragraph(_esc(data.client_name) or "—", st["h2"])]
    if data.client_phone:
        client.append(Paragraph(_esc(data.client_phone), st["body"]))
    if data.client_address:
        client.append(Paragraph(_esc(data.client_address), st["small"]))
    order = info("ЗАКАЗ", f"№ {data.order_number}" + (f" — {data.order_title}" if data.order_title else ""))
    box = Table([[client, order]], colWidths=[W * 0.55, W * 0.45])
    box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT_SOFT), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 4 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 3 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
    ]))
    story += [box, Spacer(1, 6 * mm)]

    # ---- таблицы по изделиям
    cw = [8 * mm, None, 22 * mm, 11 * mm, 16 * mm, 16 * mm, 24 * mm, 26 * mm]
    fixed = sum(c for c in cw if c)
    cw[1] = W - fixed
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for idx, (section, lines) in enumerate(data.sections):
        rows = [[Paragraph(h, st["th_r"] if i >= 3 else st["th"]) for i, h in enumerate(
            ["№", "Наименование", "Размер, мм", "Шт.", "Кол-во", "Ед.", "Цена", "Сумма"])]]
        subtotal = Decimal(0)
        for n, ln in enumerate(lines, 1):
            rows.append([Paragraph(str(n), st["cell"]), Paragraph(_esc(ln.name), st["cell"]),
                         Paragraph(_esc(ln.size), st["cell"]), Paragraph(ln.pieces, st["cell_r"]),
                         Paragraph(ln.quantity, st["cell_r"]), Paragraph(_esc(ln.unit), st["cell"]),
                         Paragraph(money(ln.unit_price, ""), st["cell_r"]),
                         Paragraph(money(ln.total, ""), st["cell_r"])])
            subtotal += ln.total
        rows.append(["", Paragraph("<b>Итого по разделу</b>", st["cell"]), "", "", "", "", "",
                     Paragraph(f"<b>{money(subtotal, '')}</b>", st["cell_r"])])
        t = Table(rows, colWidths=cw, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HEAD_BG),
            ("LINEBELOW", (0, 1), (-1, -2), 0.4, LINE),
            ("BACKGROUND", (0, -1), (-1, -1), ACCENT_SOFT),
            ("SPAN", (1, -1), (6, -1)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 2.2 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2 * mm),
            ("LEFTPADDING", (0, 0), (-1, -1), 1.6 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 1.6 * mm),
        ]))
        label = letters[idx] if idx < len(letters) else str(idx + 1)
        story.append(KeepTogether([Paragraph(f"Раздел {label} · {_esc(section)}", st["h2"]), Spacer(1, 2 * mm), t]))
        story.append(Spacer(1, 5 * mm))

    # ---- итоги
    tot_rows = []
    if data.extra_amount and data.extra_amount > 0:
        tot_rows.append([Paragraph(_esc(data.extra_label), st["body"]), Paragraph(money(data.extra_amount), st["right"])])
    tot_rows.append([Paragraph("<b>ИТОГО К ОПЛАТЕ</b>", ParagraphStyle("tt", parent=st["body"], textColor=colors.white)),
                     Paragraph(f"<b>{money(data.total)}</b>",
                               ParagraphStyle("tv", parent=st["right"], textColor=colors.white, fontSize=11))])
    tt = Table(tot_rows, colWidths=[W * 0.25, W * 0.25], hAlign="RIGHT")
    tt.setStyle(TableStyle([
        ("BACKGROUND", (0, -1), (-1, -1), ACCENT),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, LINE),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5 * mm), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5 * mm),
        ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm), ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
    ]))
    story += [tt, Spacer(1, 4 * mm),
              Paragraph(f"Сумма прописью: {_esc(amount_in_words(data.total))}", st["body"])]
    if data.comment:
        story += [Spacer(1, 4 * mm), Paragraph(f"Примечание: {_esc(data.comment)}", st["small"])]
    story += [Spacer(1, 10 * mm),
              Paragraph("Документ является коммерческим предложением (сметой) и не является фискальным кассовым чеком. "
                        "Окончательная стоимость может быть уточнена после замера и согласования проекта.", st["small"])]
    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)
    return out
