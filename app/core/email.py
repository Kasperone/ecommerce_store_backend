import resend
from typing import Optional
from pathlib import Path

from app.core.config import settings

# Configure Resend with API key
resend.api_key = settings.RESEND_API_KEY


def send_email(
    *,
    email_to: str,
    subject: str,
    html_content: str,
    text_content: Optional[str] = None,
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
        
        response = resend.Emails.send(params)
        return response
    
    except Exception as e:
        print(f"Error sending email to {email_to}: {str(e)}")
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
