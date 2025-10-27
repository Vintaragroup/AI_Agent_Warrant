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

## Health and root endpoints

- Root: `GET /` returns `{ ok: true, service, health, docs }` for platform health checks.
- Health: `GET /healthz` reports build info and mounted Telnyx routes.

On Render, the default health probe hits `/` with `HEAD`/`GET`. This repo now serves 200 OK at `/` to avoid noisy 404s in logs. You can also point Render’s Health Check Path to `/healthz` if you prefer.

## Telnyx Tools

Endpoints under `/telnyx/*` allow Telnyx AI Assistant to:
- find inmates
- query bail status
- update inmate fields
- create bail inquiries

Set `TELNYX_TOOL_TOKEN` in your `.env` and pass it as a `Bearer` token from Telnyx tools.
See `app/telnyx_tools.py` for payloads and responses. For a full walkthrough, read `docs/Telnyx_Integration.md`. For the call flow and prompts used by the AI voice agent, see `docs/AI_Agent_Voice_Script.md`.

## Warm transfer and hold music

The warm transfer plan endpoint returns an ordered list of numbers to dial and optional hold music and whisper text for the agent side:

- `POST /telnyx/warm_transfer_plan`
- Response includes: `numbers`, `attempt_timeout_sec`, `whisper_text`, `accept_dtmf`, `decline_dtmf`, `from_caller_id`, and `hold_music_url` if configured.

Hold music setup:
- Place an MP3 in `app/static/hold/` and make it public via the app’s static mount at `/static/hold/<filename>`.
- Set the environment variable `HOLD_MUSIC_URL` to the absolute URL of that file.

Example (Render env):

   HOLD_MUSIC_URL=https://ai-agent-warrant.onrender.com/static/hold/moonlightdrive.mp3

Verification steps:
- Check config: `GET /telnyx/hold_music` (must include your Bearer token) — should return the URL you set.
- Check file headers: HEAD the file URL and confirm `200 OK` and `Content-Type: audio/mpeg`.

Notes:
- Prefer small loopable audio (10–30s, 64–128 kbps mono MP3).
- Avoid Google Drive links; use this app’s `/static` path or a direct object storage/CDN URL (S3/GCS) with correct MIME type and no auth.
