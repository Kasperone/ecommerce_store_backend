import logging
from decimal import Decimal
from html import escape as html_escape
from typing import TYPE_CHECKING

import resend

from app.core.config import settings
from app.services.brand import get_brand_name

if TYPE_CHECKING:
    from app.models.order import Order

logger = logging.getLogger(__name__)

# Configure Resend with API key
resend.api_key = settings.RESEND_API_KEY


def send_email(
    *,
    email_to: str,
    subject: str,
    html_content: str,
    text_content: str | None = None,
    attachments: list[dict] | None = None,
) -> dict:
    """
    Send email using Resend API

    Args:
        email_to: Recipient email address
        subject: Email subject
        html_content: HTML email content
        text_content: Plain text fallback (optional)

    Returns:
        Response from Resend API

    Raises:
        Exception: If email sending fails
    """
    try:
        brand = get_brand_name()
        params = {
            "from": f"{brand} <{settings.EMAIL_FROM}>",
            "to": [email_to],
            "subject": subject,
            "html": html_content,
        }

        if text_content:
            params["text"] = text_content

        if attachments:
            params["attachments"] = attachments

        response = resend.Emails.send(params)  # pyright: ignore[reportArgumentType] -- resend SDK's SendParams TypedDict is stricter than our plain-dict construction
        return response  # pyright: ignore[reportReturnType] -- resend's SendResponse is dict-shaped; legacy annotation is dict, not the SDK type

    except Exception:
        # Caller-side wrappers in app.api.v1.auth log with user_id context;
        # this layer just records the recipient for triage. exc_info=True
        # carries the traceback through structured logging without
        # interpolating it into the message.
        logger.warning("Email send failed: recipient=%s", email_to, exc_info=True)
        raise


def send_verification_email(
    *,
    email_to: str,
    username: str,
    token: str,
) -> dict:
    """
    Send verification email with token link

    Args:
        email_to: Recipient email address
        username: User's name for personalization
        token: Verification token

    Returns:
        Response from Resend API
    """
    brand = get_brand_name()
    subject = f"{brand} - Verify your email address"

    # Construct verification link
    verification_link = f"{settings.FRONTEND_URL}/verify-email?token={token}"

    # HTML email content
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Verify Your Email</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .container {{
                background-color: #ffffff;
                border-radius: 8px;
                padding: 40px;
                box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
            }}
            .header {{
                text-align: center;
                margin-bottom: 30px;
            }}
            .header h1 {{
                color: #2563eb;
                margin: 0;
                font-size: 28px;
            }}
            .content {{
                margin-bottom: 30px;
            }}
            .button {{
                display: inline-block;
                padding: 14px 32px;
                background-color: #2563eb;
                color: #ffffff !important;
                text-decoration: none;
                border-radius: 6px;
                font-weight: 600;
                text-align: center;
                margin: 20px 0;
            }}
            .button:hover {{
                background-color: #1d4ed8;
            }}
            .footer {{
                margin-top: 40px;
                padding-top: 20px;
                border-top: 1px solid #e5e7eb;
                font-size: 14px;
                color: #6b7280;
                text-align: center;
            }}
            .link {{
                color: #2563eb;
                word-break: break-all;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>🎉 Welcome to {brand}!</h1>
            </div>

            <div class="content">
                <p>Hi {html_escape(username)},</p>

                <p>Thank you for registering with {brand}! We're excited to have you on board.</p>

                <p>To complete your registration and verify your email address, please click the button below:</p>

                <div style="text-align: center;">
                    <a href="{verification_link}" class="button">Verify Email Address</a>
                </div>

                <p>Or copy and paste this link into your browser:</p>
                <p class="link">{verification_link}</p>

                <p><strong>This link will expire in 24 hours.</strong></p>

                <p>If you didn't create an account with us, you can safely ignore this email.</p>
            </div>

            <div class="footer">
                <p>© {brand}. All rights reserved.</p>
                <p>If you have any questions, feel free to contact our support team.</p>
            </div>
        </div>
    </body>
    </html>
    """

    # Plain text fallback
    text_content = f"""
    Welcome to {brand}!

    Hi {username},

    Thank you for registering! To verify your email address, please click the link below:

    {verification_link}

    This link will expire in 24 hours.

    If you didn't create an account with us, you can safely ignore this email.

    © {brand}
    """

    return send_email(
        email_to=email_to,
        subject=subject,
        html_content=html_content,
        text_content=text_content,
    )


def send_welcome_email(
    *,
    email_to: str,
    username: str,
) -> dict:
    """
    Send welcome email after successful verification

    Args:
        email_to: Recipient email address
        username: User's name

    Returns:
        Response from Resend API
    """
    brand = get_brand_name()
    subject = f"Welcome to {brand}!"

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Welcome!</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .container {{
                background-color: #ffffff;
                border-radius: 8px;
                padding: 40px;
                box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
            }}
            h1 {{
                color: #2563eb;
                text-align: center;
            }}
            .button {{
                display: inline-block;
                padding: 14px 32px;
                background-color: #2563eb;
                color: #ffffff !important;
                text-decoration: none;
                border-radius: 6px;
                margin: 20px 0;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>✅ Email Verified!</h1>
            <p>Hi {html_escape(username)},</p>
            <p>Your email has been successfully verified. You can now enjoy all features of {brand}!</p>
            <div style="text-align: center;">
                <a href="{settings.FRONTEND_URL}" class="button">Start Shopping</a>
            </div>
            <p>Happy shopping!</p>
        </div>
    </body>
    </html>
    """

    return send_email(
        email_to=email_to,
        subject=subject,
        html_content=html_content,
    )


def send_company_invite_email(
    *,
    email_to: str,
    company_name: str,
    role: str,
) -> dict:
    """Invite an unregistered address to join a company's B2B team.

    Sent best-effort when a COMPANY_ADMIN invites an email that has no account
    yet — it prompts them to register, at which point the pending membership is
    bound to the new account (``link_pending_invites``).
    """
    brand = get_brand_name()
    register_url = f"{settings.FRONTEND_URL}/register"
    subject = f"You've been invited to {company_name} on {brand}"
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>Team invitation</title></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
        <div style="background:#fff;border-radius:8px;padding:40px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
            <h1 style="color:#2563eb;text-align:center;">You're invited 🎉</h1>
            <p>You've been invited to join <strong>{html_escape(company_name)}</strong> as
               <strong>{html_escape(role)}</strong> on {brand}.</p>
            <p>Create an account with this email address to accept — the invitation is
               linked to your new account automatically.</p>
            <div style="text-align:center;">
                <a href="{register_url}" style="display:inline-block;padding:14px 32px;background:#2563eb;color:#fff !important;text-decoration:none;border-radius:6px;margin:20px 0;">Create your account</a>
            </div>
            <p style="color:#666;font-size:14px;">If you weren't expecting this, you can safely ignore this email.</p>
        </div>
    </body>
    </html>
    """
    return send_email(email_to=email_to, subject=subject, html_content=html_content)


def send_password_reset_email(
    *,
    email_to: str,
    username: str,
    token: str,
) -> dict:
    """Send a password reset email with a one-time link valid for 1 hour."""
    brand = get_brand_name()
    subject = f"{brand} - Reset your password"

    reset_link = f"{settings.FRONTEND_URL}/password-reset/{token}"

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Reset your password</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .container {{
                background-color: #ffffff;
                border-radius: 8px;
                padding: 40px;
                box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
            }}
            h1 {{ color: #2563eb; }}
            .button {{
                display: inline-block;
                padding: 14px 32px;
                background-color: #2563eb;
                color: #ffffff !important;
                text-decoration: none;
                border-radius: 6px;
                font-weight: 600;
                margin: 20px 0;
            }}
            .link {{ color: #2563eb; word-break: break-all; }}
            .footer {{
                margin-top: 40px;
                padding-top: 20px;
                border-top: 1px solid #e5e7eb;
                font-size: 13px;
                color: #6b7280;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Reset your password</h1>
            <p>Hi {html_escape(username)},</p>
            <p>We received a request to reset your password. Click the button below to choose a new one:</p>
            <p style="text-align:center;">
                <a href="{reset_link}" class="button">Reset password</a>
            </p>
            <p>Or copy and paste this link into your browser:</p>
            <p class="link">{reset_link}</p>
            <p><strong>This link expires in 1 hour</strong> and can only be used once.</p>
            <p>If you didn't request a password reset, you can safely ignore this email — your password won't change.</p>
            <div class="footer">
                © {brand}
            </div>
        </div>
    </body>
    </html>
    """

    text_content = (
        f"Hi {username},\n\n"
        f"We received a request to reset your password. Use this link to set a new one:\n\n"
        f"{reset_link}\n\n"
        f"This link expires in 1 hour and can only be used once.\n\n"
        f"If you didn't request a password reset, ignore this email — your password won't change.\n"
    )

    return send_email(
        email_to=email_to,
        subject=subject,
        html_content=html_content,
        text_content=text_content,
    )


def send_account_locked_email(
    *,
    email_to: str,
    username: str,
    lockout_minutes: int,
) -> dict:
    """Notify the account owner that their account was just locked due to
    repeated failed sign-in attempts.

    The lockout state is intentionally **not** revealed in the HTTP response
    (would let attackers confirm the email is valid and probe the lockout
    window). This out-of-band email is the legitimate channel — only the
    real owner sees it, and it points them at the password reset flow which
    also clears the lockout.
    """
    brand = get_brand_name()
    subject = f"{brand} - Account temporarily locked"

    reset_link = f"{settings.FRONTEND_URL}/password-recovery"

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Account temporarily locked</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .container {{
                background-color: #ffffff;
                border-radius: 8px;
                padding: 40px;
                box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
            }}
            h1 {{ color: #b91c1c; }}
            .button {{
                display: inline-block;
                padding: 14px 32px;
                background-color: #2563eb;
                color: #ffffff !important;
                text-decoration: none;
                border-radius: 6px;
                font-weight: 600;
                margin: 20px 0;
            }}
            .link {{ color: #2563eb; word-break: break-all; }}
            .footer {{
                margin-top: 40px;
                padding-top: 20px;
                border-top: 1px solid #e5e7eb;
                font-size: 13px;
                color: #6b7280;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Account temporarily locked</h1>
            <p>Hi {html_escape(username)},</p>
            <p>
                We locked your account for <strong>{lockout_minutes} minutes</strong>
                after several failed sign-in attempts. This is an automatic safeguard.
            </p>
            <p><strong>If this was you</strong> — you can wait it out and try again,
            or reset your password to unlock the account immediately:</p>
            <p style="text-align:center;">
                <a href="{reset_link}" class="button">Reset password</a>
            </p>
            <p class="link">{reset_link}</p>
            <p><strong>If this wasn't you</strong> — someone is trying to guess your
            password. Please reset your password using the link above as soon as
            possible.</p>
            <div class="footer">
                © {brand} — automated security notification.
            </div>
        </div>
    </body>
    </html>
    """

    text_content = (
        f"Hi {username},\n\n"
        f"We locked your account for {lockout_minutes} minutes after several "
        f"failed sign-in attempts.\n\n"
        f"If this was you, you can wait it out and try again, or reset your "
        f"password to unlock the account immediately:\n\n"
        f"{reset_link}\n\n"
        f"If this wasn't you, someone is trying to guess your password — please "
        f"reset it using the link above as soon as possible.\n"
    )

    return send_email(
        email_to=email_to,
        subject=subject,
        html_content=html_content,
        text_content=text_content,
    )


def send_already_registered_email(
    *,
    email_to: str,
    username: str,
) -> dict:
    """
    Notify a user that someone (possibly themselves) attempted to register an
    account with their email. Sent in place of a verification email so that
    /register can return a generic response without leaking account existence.
    """
    brand = get_brand_name()
    subject = f"{brand} - Account already exists"

    login_link = f"{settings.FRONTEND_URL}/login"
    reset_link = f"{settings.FRONTEND_URL}/password-recovery"

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>Account already exists</title>
        <style>
            body {{
                font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                line-height: 1.6;
                color: #333;
                max-width: 600px;
                margin: 0 auto;
                padding: 20px;
            }}
            .container {{
                background-color: #ffffff;
                border-radius: 8px;
                padding: 40px;
                box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
            }}
            h1 {{ color: #2563eb; }}
            .button {{
                display: inline-block;
                padding: 12px 28px;
                background-color: #2563eb;
                color: #ffffff !important;
                text-decoration: none;
                border-radius: 6px;
                margin: 8px 8px 8px 0;
            }}
            .footer {{
                margin-top: 32px;
                padding-top: 16px;
                border-top: 1px solid #e5e7eb;
                font-size: 13px;
                color: #6b7280;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Account already exists</h1>
            <p>Hi {html_escape(username)},</p>
            <p>
                We received a registration request using this email address, but
                an account already exists for it.
            </p>
            <p>
                If this was you, you can sign in or reset your password:
            </p>
            <p>
                <a href="{login_link}" class="button">Sign in</a>
                <a href="{reset_link}" class="button" style="background-color:#6b7280;">Reset password</a>
            </p>
            <p>If you didn't try to register, you can safely ignore this email.</p>
            <div class="footer">
                © {brand}
            </div>
        </div>
    </body>
    </html>
    """

    text_content = (
        f"Hi {username},\n\n"
        f"We received a registration request for this email, but an account "
        f"already exists. If this was you, sign in at {login_link} or reset "
        f"your password at {reset_link}.\n\n"
        f"If you didn't try to register, you can ignore this email.\n"
    )

    return send_email(
        email_to=email_to,
        subject=subject,
        html_content=html_content,
        text_content=text_content,
    )


# Localized labels for the order-email shell (subject/heading/intro are localized
# upstream in the order-email service). Fallback "en" for any unknown locale.
_ORDER_EMAIL_LABELS: dict[str, dict[str, str]] = {
    "en": {
        "order": "Order",
        "subtotal": "Subtotal",
        "shipping": "Shipping",
        "total": "Total",
        "ship_addr": "Shipping address",
    },
    "pl": {
        "order": "Zamówienie",
        "subtotal": "Suma częściowa",
        "shipping": "Dostawa",
        "total": "Razem",
        "ship_addr": "Adres dostawy",
    },
}


def render_order_email_body(
    *, order: "Order", heading: str, intro_html: str, lang: str = "en"
) -> tuple[str, str]:
    """Build the ``(html, text)`` body of an order email.

    Pure — no transport — so the localized shell is unit-testable in isolation.
    Product names + the address are HTML-escaped; ``intro_html`` is trusted markup
    composed upstream. The shell labels render in ``lang`` (EN/PL; subject/heading/
    intro are localized upstream). ``order`` must be loaded (``items`` not lazy).
    """
    brand = get_brand_name()
    currency = order.currency
    labels = _ORDER_EMAIL_LABELS.get(lang, _ORDER_EMAIL_LABELS["en"])

    def money(amount: Decimal) -> str:
        return f"{amount:,.2f} {currency}"

    item_rows = "".join(
        f'<tr><td style="padding:8px 0;border-bottom:1px solid #eee;">'
        f'{html_escape(item.product_name)} <span style="color:#6b7280;">'
        f"&times; {item.quantity}</span></td>"
        f'<td style="padding:8px 0;border-bottom:1px solid #eee;text-align:right;'
        f'white-space:nowrap;">{money(item.total_price)}</td></tr>'
        for item in order.items
    )

    ship_street = html_escape(order.shipping_address or "")
    ship_city = html_escape(
        " ".join(p for p in (order.shipping_postal_code, order.shipping_city) if p)
    )
    ship_country = html_escape(order.shipping_country or "")

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{html_escape(heading)}</title>
    </head>
    <body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
                 line-height:1.6;color:#333;max-width:600px;margin:0 auto;padding:20px;">
        <div style="background:#ffffff;border-radius:8px;padding:40px;
                    box-shadow:0 2px 4px rgba(0,0,0,0.1);">
            <h1 style="color:#2563eb;margin-top:0;font-size:24px;">{html_escape(heading)}</h1>
            {intro_html}
            <h2 style="font-size:16px;margin-top:28px;">{labels['order']} {html_escape(order.order_number)}</h2>
            <table style="width:100%;border-collapse:collapse;font-size:14px;">{item_rows}</table>
            <table style="width:100%;border-collapse:collapse;font-size:14px;margin-top:8px;">
                <tr><td style="padding:2px 0;color:#6b7280;">{labels['subtotal']}</td>
                    <td style="padding:2px 0;text-align:right;">{money(order.subtotal)}</td></tr>
                <tr><td style="padding:2px 0;color:#6b7280;">{labels['shipping']}</td>
                    <td style="padding:2px 0;text-align:right;">{money(order.shipping)}</td></tr>
                <tr><td style="padding:6px 0;font-weight:600;border-top:1px solid #e5e7eb;">{labels['total']}</td>
                    <td style="padding:6px 0;text-align:right;font-weight:600;
                        border-top:1px solid #e5e7eb;">{money(order.total)}</td></tr>
            </table>
            <h2 style="font-size:16px;margin-top:28px;">{labels['ship_addr']}</h2>
            <p style="color:#6b7280;margin:0;">{ship_street}<br>{ship_city}<br>{ship_country}</p>
            <div style="margin-top:40px;padding-top:20px;border-top:1px solid #e5e7eb;
                        font-size:13px;color:#6b7280;">
                &copy; {brand}
            </div>
        </div>
    </body>
    </html>
    """

    text_lines = "".join(
        f"- {item.product_name} x{item.quantity}: {money(item.total_price)}\n"
        for item in order.items
    )
    text_content = (
        f"{heading}\n\n"
        f"{labels['order']} {order.order_number}\n"
        f"{text_lines}\n"
        f"{labels['total']}: {money(order.total)}\n\n"
        f"© {brand}\n"
    )
    return html_content, text_content


def send_order_email(
    *,
    email_to: str,
    subject: str,
    heading: str,
    intro_html: str,
    order: "Order",
    invoice_pdf: bytes | None = None,
    corrective_invoice_pdf: bytes | None = None,
    corrective_invoice_number: str | None = None,
    lang: str = "en",
) -> dict:
    """Send a transactional order email (confirmation / status change / refunded).

    Body built by ``render_order_email_body`` (localized to ``lang``). When
    ``invoice_pdf`` is supplied it rides along as an ``invoice-<order_number>.pdf``
    attachment; the optional ``corrective_invoice_pdf`` (faktura korygująca, sent
    on the ``refunded`` event) is attached as a second file named after
    ``corrective_invoice_number`` so the customer + accountant can tell the two
    apart at a glance. ``order`` must be a loaded instance (``items`` not lazy).
    """
    html_content, text_content = render_order_email_body(
        order=order, heading=heading, intro_html=intro_html, lang=lang
    )

    attachments: list[dict] | None = None
    if invoice_pdf is not None or corrective_invoice_pdf is not None:
        attachments = []
        if invoice_pdf is not None:
            attachments.append(
                {"filename": f"invoice-{order.order_number}.pdf", "content": list(invoice_pdf)}
            )
        if corrective_invoice_pdf is not None:
            # KOR numbers carry "/"; sanitise for the filename (mirror the
            # invoice-download route's behaviour).
            ref = (corrective_invoice_number or "kor").replace("/", "-")
            attachments.append(
                {
                    "filename": f"corrective-invoice-{ref}.pdf",
                    "content": list(corrective_invoice_pdf),
                }
            )

    return send_email(
        email_to=email_to,
        subject=subject,
        html_content=html_content,
        text_content=text_content,
        attachments=attachments,
    )


def send_contact_email(
    *,
    email_to: str,
    name: str,
    reply_to: str,
    subject: str,
    message: str,
) -> dict:
    """Forward a public contact-form submission to the store inbox.

    ``email_to`` is the store inbox (from config); ``reply_to`` / ``name`` are
    the submitter's address + name so staff can reply directly. All
    user-supplied values are HTML-escaped before going into the body. Reuses
    the configured Resend client via ``send_email``; the caller swallows
    provider errors (best-effort, like the other sends).
    """
    full_subject = f"[Contact] {subject}"
    safe_name = html_escape(name)
    safe_email = html_escape(reply_to)
    safe_subject = html_escape(subject)
    safe_message = html_escape(message).replace("\n", "<br>")

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>Contact form submission</title></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
        <div style="background:#fff;border-radius:8px;padding:40px;box-shadow:0 2px 4px rgba(0,0,0,0.1);">
            <h1 style="color:#2563eb;font-size:20px;margin-top:0;">New contact form submission</h1>
            <p style="margin:0;"><strong>From:</strong> {safe_name} &lt;{safe_email}&gt;</p>
            <p style="margin:0;"><strong>Subject:</strong> {safe_subject}</p>
            <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0;">
            <p style="white-space:pre-wrap;">{safe_message}</p>
        </div>
    </body>
    </html>
    """

    text_content = (
        f"New contact form submission\n\n"
        f"From: {name} <{reply_to}>\n"
        f"Subject: {subject}\n\n"
        f"{message}\n"
    )

    return send_email(
        email_to=email_to,
        subject=full_subject,
        html_content=html_content,
        text_content=text_content,
    )
