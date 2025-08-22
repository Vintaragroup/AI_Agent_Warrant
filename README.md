# AI Agent Warrant – FastAPI Starter

This is a ready-to-run FastAPI app that supports:
- SMS link sending (Twilio webhook or HTTP API)
- Consent check-in page (GPS + optional selfie upload to S3)
- Coarse IP-based location fallback (no-consent)
- Admin "last known area" view with MapLibre (no Google Maps key needed)
- Telnyx AI Assistant webhook tools for inmate lookup and bail inquiry intake

## Quick start

1. Create and activate a virtual environment (Python 3.11+ recommended):
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and fill in real values.

3. Run the server:
   ```bash
   uvicorn app.main:app --reload --port 8080
   ```

4. (Optional) Expose locally via ngrok for webhooks:
   ```bash
   ngrok http 8080
   ```

5. Seed a test case in Mongo (via MongoDB Compass or shell) using the `rapid_locate.cases` collection:
   ```json
   {
     "case_id": "CASE123",
     "person_id": "P-001",
     "name": "John A Smith",
     "phone": "+15551234567"
   }
   ```

6. Send a test SMS:
   ```bash
   curl -X POST http://127.0.0.1:8080/admin/send_link/CASE123
   ```

## Telnyx Tools

Endpoints under `/telnyx/*` allow Telnyx AI Assistant to:
- find inmates
- query bail status
- update inmate fields
- create bail inquiries

Set `TELNYX_TOOL_TOKEN` in your `.env` and pass it as a `Bearer` token from Telnyx tools.
See `app/telnyx_tools.py` for payloads and responses.
