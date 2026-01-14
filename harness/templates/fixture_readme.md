# Webhook Fixture Recording Guide

This directory contains recorded webhook payloads for testing.

## Directory Structure

```
tests/fixtures/webhooks/
├── stripe/
│   ├── payment_intent.succeeded.json
│   ├── payment_intent.failed.json
│   └── customer.subscription.created.json
├── sendgrid/
│   └── email.delivered.json
└── <service>/
    └── <event_type>.json
```

## Recording Payloads

### Stripe

**Option 1: Stripe CLI (Recommended)**
```bash
# Install Stripe CLI
brew install stripe/stripe-cli/stripe

# Login
stripe login

# Forward webhooks to local server and capture
stripe listen --forward-to localhost:3000/api/webhooks/stripe

# In another terminal, trigger events
stripe trigger payment_intent.succeeded
stripe trigger customer.subscription.created
```

**Option 2: Dashboard**
1. Go to Stripe Dashboard > Developers > Webhooks
2. Click on your endpoint
3. Go to "Webhook attempts" tab
4. Click on an event to see the payload
5. Copy the payload JSON

**Option 3: Logging**
Add temporary logging to your webhook handler:
```typescript
app.post('/api/webhooks/stripe', (req, res) => {
  console.log('WEBHOOK PAYLOAD:', JSON.stringify(req.body, null, 2));
  // ... rest of handler
});
```

### SendGrid

1. Set up Event Webhook in SendGrid dashboard
2. Point to a logging endpoint (ngrok or similar)
3. Trigger email events (send test emails)
4. Capture logged payloads

### Auth0

1. Go to Auth0 Dashboard > Monitoring > Logs
2. Set up Log Streams to capture events
3. Trigger authentication events
4. Export log entries as JSON

### Generic Services

1. Use ngrok to expose a local logging endpoint:
   ```bash
   ngrok http 3000
   ```

2. Point the webhook URL to ngrok
3. Trigger events in the service
4. Save captured payloads

## Payload Format

Save payloads exactly as received from the service:

```json
{
  "id": "evt_1234567890",
  "object": "event",
  "type": "payment_intent.succeeded",
  "data": {
    "object": {
      "id": "pi_1234567890",
      "amount": 2000,
      "currency": "usd"
    }
  }
}
```

## Naming Convention

Use the event type as the filename:
- `payment_intent.succeeded.json`
- `customer.subscription.created.json`
- `email.delivered.json`

## Security Notes

1. **Sanitize sensitive data** before committing:
   - Replace real customer emails with `test@example.com`
   - Replace real API keys with `sk_test_xxx`
   - Replace real IDs if needed

2. **Never commit** production webhook secrets

3. **Use test/sandbox** credentials when recording

## Validation

After recording, run the baseline tests:

```bash
npx playwright test tests/webhooks/baseline.spec.ts
```

All fixtures should be accepted by your webhook handlers.
