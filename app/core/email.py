import logging

import resend

from app.core.config import settings

logger = logging.getLogger(__name__)

# Configure Resend with API key
resend.api_key = settings.RESEND_API_KEY


def send_email(
    *,
    email_to: str,
    subject: str,
    html_content: str,
    text_content: str | None = None,
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
        params = {
            "from": f"{settings.EMAIL_FROM_NAME} <{settings.EMAIL_FROM}>",
            "to": [email_to],
            "subject": subject,
            "html": html_content,
        }

        if text_content:
            params["text"] = text_content

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
    subject = f"{settings.EMAIL_FROM_NAME} - Verify your email address"

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
                <h1>🎉 Welcome to {settings.EMAIL_FROM_NAME}!</h1>
            </div>

            <div class="content">
                <p>Hi {username},</p>

                <p>Thank you for registering with {settings.EMAIL_FROM_NAME}! We're excited to have you on board.</p>

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
                <p>© 2024 {settings.EMAIL_FROM_NAME}. All rights reserved.</p>
                <p>If you have any questions, feel free to contact our support team.</p>
            </div>
        </div>
    </body>
    </html>
    """

    # Plain text fallback
    text_content = f"""
    Welcome to {settings.EMAIL_FROM_NAME}!

    Hi {username},

    Thank you for registering! To verify your email address, please click the link below:

    {verification_link}

    This link will expire in 24 hours.

    If you didn't create an account with us, you can safely ignore this email.

    © 2024 {settings.EMAIL_FROM_NAME}
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
    subject = f"Welcome to {settings.EMAIL_FROM_NAME}!"

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
            <p>Hi {username},</p>
            <p>Your email has been successfully verified. You can now enjoy all features of {settings.EMAIL_FROM_NAME}!</p>
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


def send_password_reset_email(
    *,
    email_to: str,
    username: str,
    token: str,
) -> dict:
    """Send a password reset email with a one-time link valid for 1 hour."""
    subject = f"{settings.EMAIL_FROM_NAME} - Reset your password"

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
            <p>Hi {username},</p>
            <p>We received a request to reset your password. Click the button below to choose a new one:</p>
            <p style="text-align:center;">
                <a href="{reset_link}" class="button">Reset password</a>
            </p>
            <p>Or copy and paste this link into your browser:</p>
            <p class="link">{reset_link}</p>
            <p><strong>This link expires in 1 hour</strong> and can only be used once.</p>
            <p>If you didn't request a password reset, you can safely ignore this email — your password won't change.</p>
            <div class="footer">
                © {settings.EMAIL_FROM_NAME}
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
    subject = f"{settings.EMAIL_FROM_NAME} - Account temporarily locked"

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
            <p>Hi {username},</p>
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
                © {settings.EMAIL_FROM_NAME} — automated security notification.
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
    subject = f"{settings.EMAIL_FROM_NAME} - Account already exists"

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
            <p>Hi {username},</p>
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
                © {settings.EMAIL_FROM_NAME}
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
