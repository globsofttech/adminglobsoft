# Admin Portal for GlobSoft Inc.

## Gmail newsletter broadcasts

The Newsletter admin page can send a preview or a broadcast through Gmail SMTP.
The backend sends recipients in private BCC batches of 50, so subscribers cannot
see one another's addresses. Gmail quotas still apply.

Configure these environment variables in the AdminPortal deployment:

```text
GMAIL_SENDER=info.globsoft@gmail.com
GMAIL_APP_PASSWORD=...
GMAIL_DAILY_RECIPIENT_LIMIT=400
```

Enable 2-Step Verification for the sender account, then create an App Password
at `https://myaccount.google.com/apppasswords`. Enter the 16-character App
Password in the AdminPortal Vercel environment variables without spaces.
Store it only in Vercel environment variables or the local uncommitted `.env`
file. Never put it in the repository or the admin form.

The broadcast sends at most 50 BCC recipients per SMTP message. The daily
recipient budget is tracked in MongoDB so Vercel instances share one limit.
The default application cap is 400 recipients per UTC day, intentionally below
consumer Gmail's commonly documented daily limit; Gmail can still apply its
own account-specific quota. A campaign that would exceed the remaining budget
is rejected before connecting to SMTP.
