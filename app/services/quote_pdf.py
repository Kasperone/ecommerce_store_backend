"""B2B quote PDF generation (ReportLab).

Mirrors ``app/services/invoice.py``: same bundled DejaVu fonts (Polish
diacritics), the same ``StoreIdentity`` seller block, and the same EN/PL label
table approach. A quote PDF is NOT a legal document (it's a non-binding price
offer), so it carries no faktura/series number — it bears a stable quote
reference derived from the quote id (``QT-<id>``) plus the company, validity,
line items, subtotal/total, and the current status.

``build_quote_pdf`` is pure — it reads only its arguments (an eager-loaded
``Quote`` with ``items`` + ``company``, a ``StoreIdentity``, and the language).
"""

from __future__ import annotations

import datetime as dt
from io import BytesIO
from typing import TYPE_CHECKING

from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# Reuse the invoice service's font registration + escaping + palette so the two
# documents render identically and the DejaVu fonts are registered once.
from app.services.invoice import (
    _BORDER,
    _FONT_BOLD,
    _FONT_REGULAR,
    _HEAD_BG,
    _MUTED,
    _RULE,
    StoreIdentity,
    _ensure_fonts_registered,
    _esc,
)

if TYPE_CHECKING:
    from app.models.quote import Quote


def derive_quote_number(quote: Quote) -> str:
    """Stable, unique quote reference derived from the quote id (``QT-000123``)."""
    return f"QT-{quote.id:06d}"


_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "title": "QUOTE",
        "quote_no": "Quote no.",
        "issue_date": "Date",
        "valid_until": "Valid until",
        "status": "Status",
        "seller": "Seller",
        "bill_to": "For",
        "tax_id": "Tax ID",
        "item": "Item",
        "qty": "Qty",
        "unit_price": "Unit price",
        "line_total": "Amount",
        "subtotal": "Subtotal",
        "total": "Total",
        "footer": "This quote is a non-binding price offer and is not an invoice.",
        # Status translations.
        "status_draft": "Draft",
        "status_requested": "Requested",
        "status_sent": "Sent",
        "status_accepted": "Accepted",
        "status_rejected": "Rejected",
        "status_expired": "Expired",
    },
    "pl": {
        "title": "OFERTA",
        "quote_no": "Nr oferty",
        "issue_date": "Data",
        "valid_until": "Ważna do",
        "status": "Status",
        "seller": "Sprzedawca",
        "bill_to": "Dla",
        "tax_id": "NIP",
        "item": "Pozycja",
        "qty": "Ilość",
        "unit_price": "Cena jedn.",
        "line_total": "Wartość",
        "subtotal": "Suma częściowa",
        "total": "Razem",
        "footer": "Niniejsza oferta jest niewiążącą propozycją cenową i nie stanowi faktury.",
        # Status translations.
        "status_draft": "Szkic",
        "status_requested": "Zapytanie",
        "status_sent": "Wysłana",
        "status_accepted": "Zaakceptowana",
        "status_rejected": "Odrzucona",
        "status_expired": "Wygasła",
    },
}


def _localized_status(status: str, labels: dict[str, str]) -> str:
    """Translate the raw status value to its EN/PL label, falling back to the value."""
    return labels.get(f"status_{status}", status)


def build_quote_pdf(
    quote: Quote,
    *,
    store: StoreIdentity,
    issued_at: dt.date | None = None,
    lang: str = "en",
) -> bytes:
    """Render a one-page B2B quote PDF and return its bytes.

    Pure — no DB access. ``quote`` must have ``items`` + ``company`` loaded.
    ``issued_at`` defaults to today (a quote has no stamped issue date — it is a
    live offer rendered on demand).
    """
    _ensure_fonts_registered()
    labels = _LABELS.get(lang, _LABELS["en"])
    currency = quote.currency
    issued_at = issued_at or dt.date.today()
    quote_number = derive_quote_number(quote)

    def money(amount: float) -> str:
        return f"{amount:,.2f} {currency}"

    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=18 * mm,
        rightMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=f"{labels['title']} {quote_number}",
    )

    sample = getSampleStyleSheet()
    base = ParagraphStyle(
        "qt_base", parent=sample["Normal"], fontName=_FONT_REGULAR, fontSize=9, leading=13
    )
    title_style = ParagraphStyle(
        "qt_title", parent=base, fontName=_FONT_BOLD, fontSize=22, leading=26
    )
    right = ParagraphStyle("qt_right", parent=base, alignment=TA_RIGHT)
    footer = ParagraphStyle("qt_footer", parent=base, textColor=_MUTED)

    # Header.
    meta_bits = [
        f"<b>{labels['quote_no']}:</b> {_esc(quote_number)}",
        f"<b>{labels['issue_date']}:</b> {issued_at.isoformat()}",
    ]
    if quote.valid_until is not None:
        meta_bits.append(f"<b>{labels['valid_until']}:</b> {quote.valid_until.date().isoformat()}")
    meta_bits.append(f"<b>{labels['status']}:</b> {_esc(_localized_status(quote.status, labels))}")
    meta = "<br/>".join(meta_bits)
    header = Table(
        [[Paragraph(_esc(labels["title"]), title_style), Paragraph(meta, right)]],
        colWidths=[doc.width * 0.55, doc.width * 0.45],
    )
    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    # Seller / company blocks.
    seller = [f"<b>{labels['seller']}</b>", _esc(store.name)]
    seller += [_esc(v) for v in (store.address_line, store.postal_city, store.country) if v]
    if store.tax_id:
        seller.append(f"{labels['tax_id']}: {_esc(store.tax_id)}")
    if store.email:
        seller.append(_esc(store.email))

    company = quote.company
    buyer = [f"<b>{labels['bill_to']}</b>", _esc(company.name if company is not None else "")]
    if company is not None:
        if company.tax_id:
            buyer.append(f"{labels['tax_id']}: {_esc(company.tax_id)}")
        if company.email:
            buyer.append(_esc(company.email))

    parties = Table(
        [[Paragraph("<br/>".join(seller), base), Paragraph("<br/>".join(buyer), base)]],
        colWidths=[doc.width * 0.5, doc.width * 0.5],
    )
    parties.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (0, -1), 0),
                ("RIGHTPADDING", (-1, 0), (-1, -1), 0),
            ]
        )
    )

    # Line items.
    item_rows: list[list] = [
        [labels["item"], labels["qty"], labels["unit_price"], labels["line_total"]]
    ]
    for it in quote.items:
        item_rows.append(
            [
                Paragraph(_esc(it.description), base),
                str(it.quantity),
                money(it.unit_price),
                money(it.line_total),
            ]
        )
    items_tbl = Table(
        item_rows,
        colWidths=[doc.width * 0.52, doc.width * 0.10, doc.width * 0.19, doc.width * 0.19],
        repeatRows=1,
    )
    items_tbl.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), _FONT_REGULAR),
                ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, _BORDER),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, _RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )

    # Totals.
    totals_rows = [
        [labels["subtotal"], money(quote.subtotal)],
        [labels["total"], money(quote.total)],
    ]
    last = len(totals_rows) - 1
    totals_tbl = Table(totals_rows, colWidths=[doc.width * 0.28, doc.width * 0.22])
    totals_tbl.hAlign = "RIGHT"
    totals_tbl.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), _FONT_REGULAR),
                ("FONTNAME", (0, last), (-1, last), _FONT_BOLD),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("LINEABOVE", (0, last), (-1, last), 0.5, _BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]
        )
    )

    doc.build(
        [
            header,
            Spacer(1, 8 * mm),
            parties,
            Spacer(1, 8 * mm),
            items_tbl,
            Spacer(1, 6 * mm),
            totals_tbl,
            Spacer(1, 10 * mm),
            Paragraph(_esc(labels["footer"]), footer),
        ]
    )
    return buf.getvalue()
