"""Order invoice PDF generation (ReportLab).

Two document kinds share one renderer. A **faktura** is a numbered, legally-issued
invoice: its sequential gap-free number + issue date live in the ``invoices``
registry, assigned on the order's ``pending → paid`` transition (see
``app/crud/invoice.py``). A **proforma** is the pre-payment document (order
confirmation / pending download); it bears its own order-derived reference
(``derive_proforma_number`` → ``PF-…``) and is **not** part of the legal series.

``build_invoice_pdf`` is pure — the caller passes the resolved number + date +
kind. ``render_invoice_pdf`` is the DB-aware orchestrator that looks up the
registry and picks faktura vs proforma.

Prices are treated as VAT-inclusive (gross), the EU/PL B2C convention, so the
invoice decomposes net/VAT/gross at ``StoreIdentity.vat_rate``. A rate of 0
renders a plain receipt with no VAT block. The rate is applied uniformly across
currencies in v1 — per-currency VAT is a future refinement.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.pdfmetrics import registerFontFamily
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy.orm import Session

from app.crud import corrective_invoice as crud_corrective
from app.crud import invoice as crud_invoice

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.models.order import Order, OrderItem
    from app.models.refund import Refund

_FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
_FONT_REGULAR = "DejaVuSans"
_FONT_BOLD = "DejaVuSans-Bold"
_fonts_registered = False


def _ensure_fonts_registered() -> None:
    """Register the bundled DejaVu fonts once (idempotent).

    The PDF base-14 fonts use WinAnsi encoding, which does not cover Polish
    diacritics (ł/ą/ę/…). DejaVuSans is full-Unicode TrueType. Registering the
    family lets ``<b>`` markup resolve to the bold TTF.
    """
    global _fonts_registered
    if _fonts_registered:
        return
    pdfmetrics.registerFont(TTFont(_FONT_REGULAR, str(_FONTS_DIR / "DejaVuSans.ttf")))
    pdfmetrics.registerFont(TTFont(_FONT_BOLD, str(_FONTS_DIR / "DejaVuSans-Bold.ttf")))
    registerFontFamily(
        _FONT_REGULAR,
        normal=_FONT_REGULAR,
        bold=_FONT_BOLD,
        italic=_FONT_REGULAR,
        boldItalic=_FONT_BOLD,
    )
    _fonts_registered = True


def _esc(value: str) -> str:
    """Escape the characters ReportLab's mini-markup parser treats specially."""
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _item_label(item: OrderItem) -> str:
    """Line label: product name, with the variant title appended when present
    (``"Widget — Red / XL"``). Shared by the invoice and the corrective-invoice
    item tables so variant lines render identically on both documents."""
    if item.variant_title:
        return f"{item.product_name} — {item.variant_title}"
    return item.product_name


def _buyer_lines(order: Order, labels: dict[str, str]) -> list[str]:
    """Buyer ("Bill to") block lines — shared by the invoice and the corrective
    invoice so the two documents can't drift.

    Company details when the user has them, else the person's name; a **guest**
    order (``order.user`` is None) bills to the guest contact email — the only
    buyer identity a guest checkout collects (v1; no guest invoice download
    either, so this renders only on the emailed copy).
    """
    user = order.user
    buyer = [f"<b>{labels['bill_to']}</b>"]
    if user is None:
        if order.guest_email:
            buyer.append(_esc(order.guest_email))
        return buyer
    if user.company_name:
        buyer.append(_esc(user.company_name))
        if user.company_tax_id:
            buyer.append(f"{labels['tax_id']}: {_esc(user.company_tax_id)}")
        if user.company_address_street:
            buyer.append(_esc(user.company_address_street))
        company_city = " ".join(
            p for p in (user.company_address_postal_code, user.company_address_city) if p
        )
        if company_city:
            buyer.append(_esc(company_city))
        if user.company_address_country:
            buyer.append(_esc(user.company_address_country))
    else:
        buyer.append(_esc(user.full_name))
    buyer.append(_esc(user.email))
    return buyer


@dataclass(frozen=True)
class StoreIdentity:
    """Seller identity + VAT rate printed on an invoice (decoupled from Settings)."""

    name: str
    address_line: str = ""
    postal_city: str = ""
    country: str = ""
    tax_id: str = ""
    email: str = ""
    vat_rate: int = 23

    @classmethod
    def from_settings(cls, settings: Settings) -> StoreIdentity:
        return cls(
            name=settings.STORE_NAME,
            address_line=settings.STORE_ADDRESS_LINE,
            postal_city=settings.STORE_POSTAL_CITY,
            country=settings.STORE_COUNTRY,
            tax_id=settings.STORE_TAX_ID,
            email=settings.STORE_EMAIL,
            vat_rate=settings.INVOICE_VAT_RATE,
        )

    @classmethod
    def from_store_config(cls, db: Session, settings: Settings) -> StoreIdentity:
        """Build the identity from the admin-editable ``store_config`` singleton.

        Each text field uses the ``store_config`` value when non-blank
        (``.strip()``), else falls back to the env ``settings.STORE_*``. The
        singleton is seeded blank, so a blank config value must NOT wipe an
        identity that is configured via env — fall back instead of overwriting
        with empty. ``vat_rate`` always comes from the config row
        (``invoice_vat_rate`` has a real ``server_default`` of 23, so it is
        always present).
        """
        # Imported here (not at module scope) to avoid a circular import:
        # app.crud.store_config -> app.schemas.store_config, both of which sit
        # below the services layer.
        from app.crud import store_config as crud_store_config

        config = crud_store_config.get_singleton(db)

        def pick(config_value: str, env_value: str) -> str:
            return config_value if config_value.strip() else env_value

        return cls(
            name=pick(config.store_name, settings.STORE_NAME),
            address_line=pick(config.address_line, settings.STORE_ADDRESS_LINE),
            postal_city=pick(config.postal_city, settings.STORE_POSTAL_CITY),
            country=pick(config.country, settings.STORE_COUNTRY),
            tax_id=pick(config.tax_id, settings.STORE_TAX_ID),
            email=pick(config.email, settings.STORE_EMAIL),
            vat_rate=config.invoice_vat_rate,
        )


def decompose_gross(total_gross: float, vat_rate_pct: int) -> tuple[float, float]:
    """Split a VAT-inclusive (gross) amount into ``(net, vat)`` at the given rate.

    Returns ``(round(total_gross, 2), 0.0)`` when the rate is <= 0. Both
    components are rounded to 2 decimals and sum back to ``total_gross``.
    """
    if vat_rate_pct <= 0:
        return round(total_gross, 2), 0.0
    net = round(total_gross / (1 + vat_rate_pct / 100), 2)
    vat = round(total_gross - net, 2)
    return net, vat


def derive_proforma_number(order: Order) -> str:
    """Proforma reference derived 1:1 from the order number (``ORD-`` → ``PF-``).

    Unique + stable per order (the order number is unique), so a client holding
    proformas for several orders can tell them apart and pay each. Not a legal
    series and intentionally distinct from the gap-free faktura ``FV/…`` numbers —
    a proforma that never gets paid must never consume a faktura number.
    """
    number = order.order_number
    if number.startswith("ORD-"):
        return "PF-" + number[len("ORD-") :]
    return "PF-" + number


_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "invoice": "INVOICE",
        "proforma": "PROFORMA INVOICE",
        "invoice_no": "Invoice no.",
        "proforma_no": "Proforma no.",
        "issue_date": "Issue date",
        "order_no": "Order no.",
        "order_date": "Order date",
        "seller": "Seller",
        "bill_to": "Bill to",
        "ship_to": "Ship to",
        "tax_id": "Tax ID",
        "item": "Item",
        "sku": "SKU",
        "qty": "Qty",
        "unit_price": "Unit price",
        "line_total": "Amount",
        "subtotal": "Subtotal",
        "discount": "Discount",
        "shipping": "Shipping",
        "net": "Net",
        "vat": "VAT",
        "total_due": "Total",
        "payment_method": "Payment method",
        "status": "Status",
        "thank_you": "Thank you for your order.",
        # Corrective-invoice (faktura korygująca) labels.
        "kor_title": "CORRECTIVE INVOICE",
        "kor_no": "Corrective invoice no.",
        "original_ref": "Issued to invoice",
        "original_date": "Original issue date",
        "refund_reason": "Reason for correction",
        "before_correction": "Before correction",
        "after_correction": "After correction",
        "difference": "Difference (refunded)",
        "kor_thank_you": "Refund has been recorded. Funds will be credited via "
        "the original payment method within 5-10 business days.",
        # Refund-reason translations used on the KOR PDF.
        "refund_reason_defective": "Defective product",
        "refund_reason_wrong_item": "Wrong item shipped",
        "refund_reason_customer_request": "Customer request",
        "refund_reason_lost_in_transit": "Lost in transit",
        "refund_reason_damaged_in_transit": "Damaged in transit",
        "refund_reason_other": "Other",
    },
    "pl": {
        "invoice": "FAKTURA",
        "proforma": "FAKTURA PROFORMA",
        "invoice_no": "Nr faktury",
        "proforma_no": "Nr proformy",
        "issue_date": "Data wystawienia",
        "order_no": "Nr zamówienia",
        "order_date": "Data zamówienia",
        "seller": "Sprzedawca",
        "bill_to": "Nabywca",
        "ship_to": "Adres dostawy",
        "tax_id": "NIP",
        "item": "Pozycja",
        "sku": "SKU",
        "qty": "Ilość",
        "unit_price": "Cena jedn.",
        "line_total": "Wartość",
        "subtotal": "Suma częściowa",
        "discount": "Rabat",
        "shipping": "Dostawa",
        "net": "Netto",
        "vat": "VAT",
        "total_due": "Razem do zapłaty",
        "payment_method": "Metoda płatności",
        "status": "Status",
        "thank_you": "Dziękujemy za zamówienie.",
        # Corrective-invoice (faktura korygująca) labels.
        "kor_title": "FAKTURA KORYGUJĄCA",
        "kor_no": "Nr faktury korygującej",
        "original_ref": "Wystawiona do faktury",
        "original_date": "Data wystawienia oryginału",
        "refund_reason": "Przyczyna korekty",
        "before_correction": "Przed korektą",
        "after_correction": "Po korekcie",
        "difference": "Różnica (zwrot)",
        "kor_thank_you": "Zwrot został zarejestrowany. Środki zostaną zwrócone "
        "tą samą metodą płatności w ciągu 5-10 dni roboczych.",
        # Refund-reason translations used on the KOR PDF.
        "refund_reason_defective": "Wadliwy produkt",
        "refund_reason_wrong_item": "Wysłano niewłaściwy produkt",
        "refund_reason_customer_request": "Na życzenie klienta",
        "refund_reason_lost_in_transit": "Zaginął w transporcie",
        "refund_reason_damaged_in_transit": "Uszkodzony w transporcie",
        "refund_reason_other": "Inny",
    },
}

_MUTED = colors.HexColor("#6b7280")
_BORDER = colors.HexColor("#9ca3af")
_RULE = colors.HexColor("#e5e7eb")
_HEAD_BG = colors.HexColor("#f3f4f6")


def build_invoice_pdf(
    order: Order,
    *,
    store: StoreIdentity,
    invoice_number: str,
    issued_at: dt.date,
    is_proforma: bool = False,
    lang: str = "en",
) -> bytes:
    """Render a one-page order invoice PDF and return its bytes.

    Reads only its arguments — ``order`` (eager-loaded ``items`` + ``user``),
    ``store``, and the resolved ``invoice_number``/``issued_at`` — with no DB
    access, so it is unit-testable in isolation. The caller resolves whether this
    is a numbered faktura or a ``is_proforma`` document (see ``render_invoice_pdf``):
    a proforma uses the proforma title + reference and is not part of the legal
    series. ``issued_at`` is the stable issue date (a faktura's is stamped once at
    allocation; a proforma's is the render day).
    """
    _ensure_fonts_registered()
    labels = _LABELS.get(lang, _LABELS["en"])
    title_label = labels["proforma"] if is_proforma else labels["invoice"]
    number_label = labels["proforma_no"] if is_proforma else labels["invoice_no"]
    currency = order.currency

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
        title=f"{title_label} {invoice_number}",
    )

    sample = getSampleStyleSheet()
    base = ParagraphStyle(
        "inv_base", parent=sample["Normal"], fontName=_FONT_REGULAR, fontSize=9, leading=13
    )
    title_style = ParagraphStyle(
        "inv_title", parent=base, fontName=_FONT_BOLD, fontSize=22, leading=26
    )
    right = ParagraphStyle("inv_right", parent=base, alignment=TA_RIGHT)
    footer = ParagraphStyle("inv_footer", parent=base, textColor=_MUTED)

    order_date = order.created_at.date().isoformat() if order.created_at else ""
    meta = "<br/>".join(
        [
            f"<b>{number_label}:</b> {_esc(invoice_number)}",
            f"<b>{labels['issue_date']}:</b> {issued_at.isoformat()}",
            f"<b>{labels['order_no']}:</b> {_esc(order.order_number)}",
            f"<b>{labels['order_date']}:</b> {order_date}",
        ]
    )
    header = Table(
        [[Paragraph(_esc(title_label), title_style), Paragraph(meta, right)]],
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

    # Seller block.
    seller = [f"<b>{labels['seller']}</b>", _esc(store.name)]
    seller += [_esc(v) for v in (store.address_line, store.postal_city, store.country) if v]
    if store.tax_id:
        seller.append(f"{labels['tax_id']}: {_esc(store.tax_id)}")
    if store.email:
        seller.append(_esc(store.email))

    # Buyer block — company details when present, else the person (guest
    # orders bill to the guest email).
    buyer = _buyer_lines(order, labels)

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

    # Ship-to block.
    ship = [f"<b>{labels['ship_to']}</b>", _esc(order.shipping_address or "")]
    ship_city = " ".join(p for p in (order.shipping_postal_code, order.shipping_city) if p)
    if ship_city:
        ship.append(_esc(ship_city))
    if order.shipping_country:
        ship.append(_esc(order.shipping_country))
    ship_para = Paragraph("<br/>".join(ship), base)

    # Line items.
    item_rows: list[list] = [
        [labels["item"], labels["sku"], labels["qty"], labels["unit_price"], labels["line_total"]]
    ]
    for it in order.items:
        item_rows.append(
            [
                Paragraph(_esc(_item_label(it)), base),
                Paragraph(_esc(it.product_sku) if it.product_sku else "—", base),
                str(it.quantity),
                money(it.unit_price),
                money(it.total_price),
            ]
        )
    items_tbl = Table(
        item_rows,
        colWidths=[
            doc.width * 0.42,
            doc.width * 0.16,
            doc.width * 0.08,
            doc.width * 0.17,
            doc.width * 0.17,
        ],
        repeatRows=1,
    )
    items_tbl.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), _FONT_REGULAR),
                ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
                ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, _BORDER),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, _RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )

    # Totals — gross-inclusive VAT decomposition when the rate is set.
    net, vat = decompose_gross(order.total, store.vat_rate)
    totals_rows = [
        [labels["subtotal"], money(order.subtotal)],
    ]
    # Coupon discount line (only when one was applied) — shown as a negative
    # amount between subtotal and shipping. The VAT decomposition above already
    # works off the discounted ``order.total``, so net/VAT stay internally
    # consistent.
    discount = getattr(order, "discount", 0) or 0
    if discount > 0:
        discount_label = labels["discount"]
        if order.coupon_code:
            discount_label = f"{discount_label} ({order.coupon_code})"
        totals_rows.append([discount_label, f"-{money(discount)}"])
    totals_rows.append([labels["shipping"], money(order.shipping)])
    if store.vat_rate > 0:
        totals_rows.append([labels["net"], money(net)])
        totals_rows.append([f"{labels['vat']} ({store.vat_rate}%)", money(vat)])
    totals_rows.append([labels["total_due"], money(order.total)])
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

    # Payment + status.
    status_str = order.status.value if hasattr(order.status, "value") else str(order.status)
    pay_bits = []
    if order.payment_method:
        pay_bits.append(f"<b>{labels['payment_method']}:</b> {_esc(order.payment_method)}")
    pay_bits.append(f"<b>{labels['status']}:</b> {_esc(status_str)}")
    pay_para = Paragraph("<br/>".join(pay_bits), base)

    doc.build(
        [
            header,
            Spacer(1, 8 * mm),
            parties,
            Spacer(1, 5 * mm),
            ship_para,
            Spacer(1, 8 * mm),
            items_tbl,
            Spacer(1, 6 * mm),
            totals_tbl,
            Spacer(1, 8 * mm),
            pay_para,
            Spacer(1, 10 * mm),
            Paragraph(_esc(labels["thank_you"]), footer),
        ]
    )
    return buf.getvalue()


def render_invoice_pdf(
    db: Session, order: Order, *, store: StoreIdentity, lang: str = "en"
) -> bytes:
    """Render the right document for an order: a numbered faktura, else a proforma.

    Looks up the ``invoices`` registry — an order gains a faktura on its
    ``pending → paid`` transition — and feeds the resolved number + stable issue
    date into the pure ``build_invoice_pdf``. Orders without a registry row
    (pending, cancelled) render a proforma bearing the order-derived reference.
    """
    issued = crud_invoice.get_by_order_id(db, order.id)
    if issued is not None:
        return build_invoice_pdf(
            order,
            store=store,
            invoice_number=issued.invoice_number,
            issued_at=issued.issued_at.date(),
            is_proforma=False,
            lang=lang,
        )
    return build_invoice_pdf(
        order,
        store=store,
        invoice_number=derive_proforma_number(order),
        issued_at=dt.date.today(),
        is_proforma=True,
        lang=lang,
    )


# ----------------------------------------------------------------------
# Faktura korygująca (corrective invoice for a refund)
# ----------------------------------------------------------------------


def _localized_refund_reason(reason: str, labels: dict[str, str]) -> str:
    """Translate the raw enum value to its EN/PL label, falling back to the value."""
    return labels.get(f"refund_reason_{reason}", reason)


def build_corrective_invoice_pdf(
    refund: Refund,
    *,
    order: Order,
    store: StoreIdentity,
    kor_number: str,
    issued_at: dt.date,
    original_invoice_number: str,
    original_issued_at: dt.date,
    lang: str = "en",
) -> bytes:
    """Render a one-page faktura korygująca PDF and return its bytes.

    Pure — reads only its arguments (``refund`` + eager-loaded
    ``order.items``/``order.user``, ``store``, the resolved KOR number/date, and
    the original faktura's number + issue date) — no DB access, so it is
    unit-testable in isolation. ``render_corrective_invoice_pdf`` is the
    DB-aware orchestrator that looks up the KOR registry and the original
    ``invoices`` row before calling this.

    Layout (mirrors a real PL faktura korygująca):

    - Header: title + KOR number + issue date.
    - Reference block: original invoice number + its issue date + the refund
      reason (translated).
    - Seller / buyer (same as the original faktura).
    - Line items table (the items being refunded; v1 = all items, full refund).
    - **Before correction** column: net / VAT / gross at the original totals.
    - **After correction** column: net / VAT / gross at the post-refund totals
      (v1 = 0, since the refund is full).
    - **Difference** column: the refunded amounts as negative values (this is
      the legal "wartość korekty").
    """
    _ensure_fonts_registered()
    labels = _LABELS.get(lang, _LABELS["en"])
    currency = order.currency

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
        title=f"{labels['kor_title']} {kor_number}",
    )

    sample = getSampleStyleSheet()
    base = ParagraphStyle(
        "kor_base", parent=sample["Normal"], fontName=_FONT_REGULAR, fontSize=9, leading=13
    )
    title_style = ParagraphStyle(
        "kor_title", parent=base, fontName=_FONT_BOLD, fontSize=22, leading=26
    )
    right = ParagraphStyle("kor_right", parent=base, alignment=TA_RIGHT)
    footer = ParagraphStyle("kor_footer", parent=base, textColor=_MUTED)

    # Header.
    meta = "<br/>".join(
        [
            f"<b>{labels['kor_no']}:</b> {_esc(kor_number)}",
            f"<b>{labels['issue_date']}:</b> {issued_at.isoformat()}",
            f"<b>{labels['order_no']}:</b> {_esc(order.order_number)}",
        ]
    )
    header = Table(
        [[Paragraph(_esc(labels["kor_title"]), title_style), Paragraph(meta, right)]],
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

    # Original-invoice reference + refund reason.
    reason_label = _localized_refund_reason(refund.reason, labels)
    ref_para = Paragraph(
        "<br/>".join(
            [
                f"<b>{labels['original_ref']}:</b> {_esc(original_invoice_number)}",
                f"<b>{labels['original_date']}:</b> {original_issued_at.isoformat()}",
                f"<b>{labels['refund_reason']}:</b> {_esc(reason_label)}",
            ]
        ),
        base,
    )

    # Seller / buyer (same shape as build_invoice_pdf).
    seller = [f"<b>{labels['seller']}</b>", _esc(store.name)]
    seller += [_esc(v) for v in (store.address_line, store.postal_city, store.country) if v]
    if store.tax_id:
        seller.append(f"{labels['tax_id']}: {_esc(store.tax_id)}")
    if store.email:
        seller.append(_esc(store.email))

    buyer = _buyer_lines(order, labels)

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

    # Line items (the items being refunded — v1: all).
    item_rows: list[list] = [
        [labels["item"], labels["sku"], labels["qty"], labels["unit_price"], labels["line_total"]]
    ]
    for it in order.items:
        item_rows.append(
            [
                Paragraph(_esc(_item_label(it)), base),
                Paragraph(_esc(it.product_sku) if it.product_sku else "—", base),
                str(it.quantity),
                money(it.unit_price),
                money(it.total_price),
            ]
        )
    items_tbl = Table(
        item_rows,
        colWidths=[
            doc.width * 0.42,
            doc.width * 0.16,
            doc.width * 0.08,
            doc.width * 0.17,
            doc.width * 0.17,
        ],
        repeatRows=1,
    )
    items_tbl.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), _FONT_REGULAR),
                ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
                ("ALIGN", (2, 0), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, _BORDER),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, _RULE),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )

    # Before / After / Difference totals (the legal "wartość korekty" block).
    # v1 = full refund, so after-correction is the zero baseline and the
    # difference equals the negative of the original gross total. The schema
    # already supports partials (refund.amount can hold any value); the
    # `after = original - amount` math below makes the partial-refund PR a
    # one-line UI relaxation.
    original_gross = round(order.total, 2)
    refunded_gross = round(refund.amount, 2)
    after_gross = round(original_gross - refunded_gross, 2)
    original_net, original_vat = decompose_gross(original_gross, store.vat_rate)
    after_net, after_vat = decompose_gross(after_gross, store.vat_rate)
    diff_net = round(after_net - original_net, 2)
    diff_vat = round(after_vat - original_vat, 2)
    diff_gross = round(after_gross - original_gross, 2)

    totals_rows: list[list[str]] = [
        ["", labels["before_correction"], labels["after_correction"], labels["difference"]],
    ]
    if store.vat_rate > 0:
        totals_rows.append([labels["net"], money(original_net), money(after_net), money(diff_net)])
        totals_rows.append(
            [
                f"{labels['vat']} ({store.vat_rate}%)",
                money(original_vat),
                money(after_vat),
                money(diff_vat),
            ]
        )
    totals_rows.append(
        [labels["total_due"], money(original_gross), money(after_gross), money(diff_gross)]
    )
    last = len(totals_rows) - 1
    totals_tbl = Table(
        totals_rows,
        colWidths=[doc.width * 0.22, doc.width * 0.26, doc.width * 0.26, doc.width * 0.26],
    )
    totals_tbl.hAlign = "RIGHT"
    totals_tbl.setStyle(
        TableStyle(
            [
                ("FONTNAME", (0, 0), (-1, -1), _FONT_REGULAR),
                ("FONTNAME", (0, 0), (-1, 0), _FONT_BOLD),
                ("FONTNAME", (0, last), (-1, last), _FONT_BOLD),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
                ("BACKGROUND", (0, 0), (-1, 0), _HEAD_BG),
                ("LINEBELOW", (0, 0), (-1, 0), 0.5, _BORDER),
                ("LINEABOVE", (0, last), (-1, last), 0.5, _BORDER),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    doc.build(
        [
            header,
            Spacer(1, 6 * mm),
            ref_para,
            Spacer(1, 6 * mm),
            parties,
            Spacer(1, 8 * mm),
            items_tbl,
            Spacer(1, 6 * mm),
            totals_tbl,
            Spacer(1, 10 * mm),
            Paragraph(_esc(labels["kor_thank_you"]), footer),
        ]
    )
    return buf.getvalue()


def render_corrective_invoice_pdf(
    db: Session, refund: Refund, *, store: StoreIdentity, lang: str = "en"
) -> bytes | None:
    """Render the issued faktura korygująca for a refund, or None if none was issued.

    Looks up the ``corrective_invoices`` registry (the KOR is assigned in the
    same transaction as the refund — see ``crud_corrective.issue_for_refund``)
    and feeds the resolved KOR + original faktura references into the pure
    ``build_corrective_invoice_pdf``. Returns ``None`` when the refund has no
    KOR (a refund against an order that never had a faktura — legacy or a
    future "refund without invoice" path).
    """
    kor = crud_corrective.get_by_refund_id(db, refund.id)
    if kor is None:
        return None
    original = kor.original_invoice
    return build_corrective_invoice_pdf(
        refund,
        order=refund.order,
        store=store,
        kor_number=kor.kor_number,
        issued_at=kor.issued_at.date(),
        original_invoice_number=original.invoice_number,
        original_issued_at=original.issued_at.date(),
        lang=lang,
    )
