# Email Verification Setup Guide

## Overview
This application uses [Resend](https://resend.com) for sending verification emails during user registration.

## Prerequisites
1. Create a free account at [resend.com](https://resend.com)
2. Get your API key from [API Keys page](https://resend.com/api-keys)

## Configuration

### 1. Backend Environment Variables

Update your `.env` file with the following:

```bash
# Required
RESEND_API_KEY=re_your_actual_api_key_here

# Optional (defaults shown)
EMAIL_FROM=noreply@ecommerce.com
EMAIL_FROM_NAME=E-commerce Store
FRONTEND_URL=http://localhost:3000
```

### 2. Email Domain Setup

#### Option A: Use Resend Test Domain (Development)
For testing, you can use Resend's default sender:
```bash
EMAIL_FROM=onboarding@resend.dev
```

**Note**: Test domain has limits:
- Can only send to your verified email
- Limited daily quota
- For development only

#### Option B: Add Your Own Domain (Production)
1. Go to [Resend Domains](https://resend.com/domains)
2. Click "Add Domain"
3. Enter your domain (e.g., `yourdomain.com`)
4. Add the DNS records shown by Resend:
   - SPF record
   - DKIM records (2 records)
5. Wait for verification (usually a few minutes)
6. Update `.env`:
   ```bash
   EMAIL_FROM=noreply@yourdomain.com
   EMAIL_FROM_NAME=Your Store Name
   FRONTEND_URL=https://yourdomain.com
   ```

## Testing the Flow

### 1. Start Backend
```bash
cd backend
source venv/bin/activate  # or venv\Scripts\activate on Windows
uvicorn app.main:app --reload
```

### 2. Start Frontend
```bash
cd frontend
npm run dev
```

### 3. Test Registration
1. Navigate to `http://localhost:3000/signup`
2. Fill out the registration form
3. Submit the form
4. You should be redirected to `/verify-email-sent`
5. Check your email for the verification link
6. Click the link to verify your account
7. You'll be redirected to login

### 4. Check Backend Logs
Monitor the backend console for:
- Email sending success/failure
- Token generation
- Verification attempts

## Email Templates

The system sends two types of emails:

### 1. Verification Email
- **Sent**: Immediately after registration
- **Contains**: Verification link with unique token
- **Expires**: 24 hours
- **Template**: `app/core/email.py` - `send_verification_email()`

### 2. Welcome Email
- **Sent**: After successful email verification
- **Contains**: Welcome message and getting started info
- **Template**: `app/core/email.py` - `send_welcome_email()`

## Troubleshooting

### Emails not sending
1. **Check API key**: Make sure `RESEND_API_KEY` is set correctly
2. **Check sender email**: Must be from verified domain or `onboarding@resend.dev`
3. **Check logs**: Backend will show detailed error messages
4. **Resend dashboard**: Check [Logs page](https://resend.com/logs) for delivery status

### Token expired
- Tokens expire after 24 hours
- User can request a new verification email from login page or `/verify-email-sent`

### Email in spam
- Check spam folder
- In production, use your own verified domain (improves deliverability)
- Configure SPF, DKIM, and DMARC records properly

### Rate limits
- Free tier: 100 emails/day
- Paid tiers: Higher limits available
- See [Resend Pricing](https://resend.com/pricing)

## API Endpoints

### POST `/api/v1/auth/register`
- Creates user account
- Generates verification token
- Sends verification email
- Returns success message

### POST `/api/v1/auth/verify-email`
```json
{
  "token": "uuid-token-from-email"
}
```
- Validates token
- Marks user as verified
- Sends welcome email
- Returns success message

### POST `/api/v1/auth/resend-verification`
```json
{
  "email": "user@example.com"
}
```
- Generates new token
- Sends new verification email
- Returns success message

### POST `/api/v1/auth/login`
- Checks if user is verified
- Returns 403 if not verified
- Returns tokens if verified

## Database Schema

### verification_tokens table
```sql
CREATE TABLE verification_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token UUID NOT NULL UNIQUE,
    expires_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### users table (updated)
- Added `is_verified` boolean field (default: false)
- Added relationship to `verification_tokens`

## Security Considerations

1. **Token expiration**: Tokens expire after 24 hours
2. **One-time use**: Tokens are deleted after successful verification
3. **Secure transmission**: Tokens sent via email only (HTTPS in production)
4. **Cleanup**: Expired tokens are automatically cleaned up
5. **Rate limiting**: Consider adding rate limits on resend endpoint
6. **Email privacy**: Resend endpoint doesn't reveal if email exists

## Production Checklist

- [ ] Add your own domain to Resend
- [ ] Configure DNS records (SPF, DKIM, DMARC)
- [ ] Update `EMAIL_FROM` to your domain
- [ ] Update `FRONTEND_URL` to production URL
- [ ] Set up monitoring for email delivery
- [ ] Configure rate limiting on resend endpoint
- [ ] Add unsubscribe links (if required by law)
- [ ] Test email deliverability to major providers
- [ ] Set up email analytics in Resend dashboard
- [ ] Configure webhook for delivery events (optional)

## Additional Resources

- [Resend Documentation](https://resend.com/docs)
- [Resend API Reference](https://resend.com/docs/api-reference)
- [Email Best Practices](https://resend.com/docs/knowledge-base/email-best-practices)
- [SPF, DKIM, DMARC Guide](https://resend.com/docs/knowledge-base/authentication)
