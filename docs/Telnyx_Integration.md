# Telnyx Integration Guide

This document explains how the Telnyx AI Assistant integrates with this FastAPI app via the endpoints in `app/telnyx_tools.py`. It covers purpose, configuration, data flow, endpoints, payloads, and database usage.

## Purpose

Provide a minimal set of authenticated API endpoints for a Telnyx AI Assistant to:
- Find a person (inmate) by name and optional DOB.
- Retrieve bail status/amount.
- Create a bail inquiry record (lead intake).
 - Attach caller info to an existing case’s CRM when an inmate is confirmed.

The module is mounted under `/telnyx/*` and is intended for machine-to-machine calls from Telnyx tools, protected by a static Bearer token.

## Where it lives

- Router: `app/telnyx_tools.py`
- Mounted in app: `app.main` → `app.include_router(telnyx_router)`
- Config: `app/config.py` (reads `TELNYX_TOOL_TOKEN`)
  - Optional office routing: `OFFICE_ROUTES_JSON`, `DEFAULT_OFFICE_NUMBER`
  - Optional schedule routing: `OFFICES_SCHEDULE_JSON`, `APP_TZ`, `DIAL_ATTEMPT_TIMEOUT_SEC`
- DB collections: declared in `app/db.py` (MongoDB)

## Authentication

All endpoints require an `Authorization: Bearer <TELNYX_TOOL_TOKEN>` header.
- Token is configured via environment: `TELNYX_TOOL_TOKEN` in `.env`.
 - For transfers, you can configure a county → office phone map using `OFFICE_ROUTES_JSON` and a default with `DEFAULT_OFFICE_NUMBER`.
- Validation is enforced by the `_auth(request)` helper.
- Missing token → 401. Invalid token → 403.

## Data sources and lookup strategy

Two data paths are used:

1) Fast path: simple_* collections
- Collections: `simple_harris`, `simple_brazoria`, `simple_galveston`, `simple_fortbend` (if present in DB)
- Queried by last/first name and optional DOB
- Heuristic scoring prefers: exact last, prefix first, DOB match, recent booking_date
- Returns a synthesized custody/bail view if matched

2) Fallback path: persons + custody_events
- `persons` collection stores canonical person documents
- `custody_events` stores snapshots/events keyed by `person_id`
- Queries by person full name (and optional DOB) or by `_id` when provided
- Uses most recent event by `scraped_at` (or county-filtered if "county" provided)

## Endpoints

Base path: `/telnyx`

### POST /telnyx/find_person
Find a person by full name and optional DOB; returns person and their latest custody snapshot.

Request JSON:
- `full_name` (string, required)
- `dob` (string, optional, e.g., `1990-01-01`)
- `county` (string, optional; used to prefer that county in lookups)

Response JSON examples:
- Found via simple_*:
```json
{
  "found": true,
  "person": { "id": null, "full_name": "DOE, JOHN", "dob": "1990-01-01", "aka": [] },
  "latest_custody": {
    "id": "...",
    "status": "In Custody",
    "facility": "Harris",
    "county": "Harris",
    "booking_number": "...",
    "total_bond": "$2,500.00",
    "arrest_date": "2024-09-14",
    "source_url": null,
    "scraped_at": 1726351200
  }
}
```
- Found via persons/custody_events:
```json
{
  "found": true,
  "person": {
    "id": "6510f...",
    "full_name": "John A Smith",
    "dob": "1988-03-02",
    "aka": []
  },
  "latest_custody": {
    "id": "6510f...",
    "status": "In Custody",
    "facility": "County Jail",
    "county": "Harris",
    "booking_number": "BN123",
    "total_bond": "$5,000.00",
    "arrest_date": "2024-09-14",
    "source_url": "https://...",
    "scraped_at": 1726351200
  }
}
```
- Not found:
```json
{ "found": false }
```

Errors:
- 400 if `full_name` missing
- 401/403 on auth failures

### POST /telnyx/get_bail_status
Return simplified bail eligibility and amount for a given person.

Request JSON:
- Either:
  - `person_id` (Mongo `_id` as string), or
  - `full_name` (string) and optional `dob`
- Optional: `county` to bias simple_* lookup

Response JSON when found:
```json
{
  "found": true,
  "has_custody": true,
  "status": "In Custody",
  "total_bond": "$5,000.00",
  "amount_numeric": 5000.0,
  "eligible": true
}
```
- If person exists but no custody: `{ "found": true, "has_custody": false }`
- If not found at all: `{ "found": false }`

Errors:
- 400 if neither `person_id` nor `full_name` provided
- 400 if `person_id` is not a valid ObjectId

Notes on eligibility:
- Converts `total_bond` string to numeric when possible
- Marks `eligible` False if custody status contains "release" or bond string contains "no bond"
 - For Harris simple_* records, also attempts to parse `bond_label`/`dbg_bond_note` when `bond` and `bond_amount` are missing.
 - Both bail endpoints now include `bond_text` (the human-readable source text) and `needs_human_review` (true when a numeric bond is unavailable and the text implies follow-up like “refer to magistrate”).

### POST /telnyx/create_bail_inquiry
Create a bail inquiry (lead) record. Does not alter custody; writes to `inquiries`.

Request JSON:
- One of: `person_id` or `inmate_name`/`full_name`
- `caller_name` (string, required)
- `caller_phone` (string, E.164, required)
- `relationship` (string, optional)
- `intends_to_post` (bool, optional)
- `notes` (string, optional)

Response JSON:
```json
{ "ok": true, "inquiry_id": "6521a..." }
```

Errors:
- 400 if neither person_id nor full_name
- 400 if name/phone missing or phone not E.164

## Helper functions

- `_auth(request)`: Validates Bearer token.
- `_e164(phone)`: Returns E.164 or None.
- `_objid(s)`: Parses MongoDB ObjectId.
- `_latest_custody(person_id)`: Most recent `custody_events` by `scraped_at`.
- `_parse_bond_str(total_bond)`: Safely parse "$x,xxx.xx" → float.
 - `_parse_bond_label(s)`: Extract numeric bond and simple eligibility from label strings (handles “No Bond”, “PR Bond”, “$5,000.00”).
- `_split_name(full_name)`: Heuristics for "Last, First" and spaced names.
- `_score_simple_hit(...)`: Scores simple_* match candidates.
- `_find_in_simple(full_name, dob, county_hint)`: Executes fast-path search.

### POST /telnyx/transfer_target

Resolve the best transfer phone number (E.164) for an office based on county.

Input JSON:

```
{ "county": "Harris" }
```

Response:

```
{ "ok": true, "phone": "+18324101662" }
```

Configuration:

```
OFFICE_ROUTES_JSON='{"harris":"+18324101662","brazoria":"+18325550123","galveston":"+18325550987","fortbend":"+18325550777"}'
DEFAULT_OFFICE_NUMBER='+18325550000'
```

Notes:
- Match is case-insensitive. Keys are compared on lowercase; a trailing “ county” is tolerated.
- If no county matches, the `DEFAULT_OFFICE_NUMBER` is returned (if set), otherwise `phone` is null.

### POST /telnyx/transfer_plan

Return an ordered list of numbers to dial based on county and time-of-day/day-of-week.

Input JSON:

```
{ "county": "Harris", "lang": "es" }  // lang optional; use for Spanish route if configured
```

Response:

```
{
  "ok": true,
  "numbers": ["+18324100001", "+17130001111"],
  "attempt_timeout_sec": 20
}
```

Configuration:

```
OFFICES_SCHEDULE_JSON='{
  "harris": [
    {"days":["mon","tue","wed","thu","fri"],"start":"08:00","end":"18:00","numbers":["+18324100001","+18324100002"]},
    {"days":["mon","tue","wed","thu","fri"],"start":"18:00","end":"07:59","numbers":["+17130001111","+17130002222"]},
    {"days":["sat","sun"],"start":"00:00","end":"23:59","numbers":["+17130003333","+17130004444"]}
  ],
  "brazoria": [
    {"days":["mon","tue","wed","thu","fri"],"start":"09:00","end":"17:00","numbers":["+18325550101","+18325550102"]},
    {"days":["sat","sun"],"start":"00:00","end":"23:59","numbers":["+17130005555","+17130006666"]}
  ],
  "galveston": [
    {"days":["mon","tue","wed","thu","fri","sat","sun"],"start":"00:00","end":"23:59","numbers":["+18325550987"]}
  ],
  "fortbend": [
    {"days":["mon","tue","wed","thu","fri"],"start":"07:00","end":"19:00","numbers":["+18325550777","+18325550778"]},
    {"days":["mon","tue","wed","thu","fri","sat","sun"],"start":"19:00","end":"06:59","numbers":["+17130007777"]}
  ],
  "default": [
    {"days":["mon","tue","wed","thu","fri","sat","sun"],"start":"00:00","end":"23:59","numbers":["+18325550000"]}
  ]
}'
APP_TZ='America/Chicago'
DIAL_ATTEMPT_TIMEOUT_SEC=20
```

Notes:
 - Harris policy: when matching the `harris` schedule (non‑Spanish), if the first on‑call number doesn’t answer within the attempt timeout (roughly 3 rings), the next attempt will automatically be Alex (Spanish line) before trying any other fallbacks. Set `DIAL_ATTEMPT_TIMEOUT_SEC` to ~18–20 seconds to align with ~3 rings.

Example: Harris county schedule (with real numbers)

```
// People on rota (for your reference only):
// Jennie (Weekdays 8:30 AM–2:59 PM)
// Dylo (Mon/Wed/Fri 3:00 PM–7:59 PM; Sat/Sun 8:30 AM–2:59 PM)
// KG   (Tue/Thu 3:00 PM–7:59 PM; Sat/Sun 3:00 PM–7:59 PM)
// Jerry (Overnight daily 8:00 PM–7:00 AM)
// Alex  (Spanish calls)

OFFICES_SCHEDULE_JSON='{
  "harris": [
    {"days":["mon","tue","wed","thu","fri"],"start":"08:30","end":"14:59","numbers":["+18326101254"]},  // Jennie
    {"days":["mon","wed","fri"],"start":"15:00","end":"19:59","numbers":["+18324339385"]},               // Dylo
    {"days":["tue","thu"],"start":"15:00","end":"19:59","numbers":["+17134464076"]},                      // KG
    {"days":["sat","sun"],"start":"08:30","end":"14:59","numbers":["+18324339385"]},                       // Dylo (weekend midday)
    {"days":["sat","sun"],"start":"15:00","end":"19:59","numbers":["+17134464076"]},                       // KG (weekend afternoon)
    {"days":["mon","tue","wed","thu","fri","sat","sun"],"start":"20:00","end":"07:00","numbers":["+14098536685"]} // Jerry overnight
  ],
  "harris_es": [
    {"days":["mon","tue","wed","thu","fri","sat","sun"],"start":"00:00","end":"23:59","numbers":["+18328732866"]}  // Alex (Spanish)
  ],
  "default": [
    {"days":["mon","tue","wed","thu","fri","sat","sun"],"start":"00:00","end":"23:59","numbers":["+17132252727"]}    // Main number fallback
  ]
}'
```

Replace the +1… placeholders with your actual E.164 numbers.
- `_bail_view_from_simple(doc)`: Shapes a uniform bail response from simple_* docs.

## MongoDB schema expectations (minimal)

- `persons`: `{ _id, full_name, dob?, aka?[] }`
- `custody_events`: `{ _id, person_id, status, facility?, county?, booking_number?, total_bond?, arrest_date?, source_url?, scraped_at }`
- `inquiries`: `{ _id, person_id?, full_name?, caller_name, caller_phone, relationship?, intends_to_post, notes?, created_ts }`
- `simple_*`: lightweight booking records with fields used above: `first_name`, `last_name`, `full_name`, `dob?`, `booking_date?`, `bond` or `bond_amount`, `county`, `booking_number`, `normalized_at`

## Configuration

Set these in `.env`:
- `TELNYX_TOOL_TOKEN` (required)
 - `OFFICE_ROUTES_JSON` and `DEFAULT_OFFICE_NUMBER` (optional for transfer routing)
- Database: `MONGO_URI`, `MONGO_DB`

Optional (elsewhere in the app): S3, IP geo, Twilio.

## Error handling and responses

- Auth failures throw HTTPException 401/403.
- Invalid inputs return HTTP 400 with a short message.
- Lookups return stable JSON shapes with `found` flags to simplify Telnyx tool logic.

## Usage from Telnyx

Configure your Telnyx AI Assistant tool to POST to the relevant endpoint with JSON body and include `Authorization: Bearer <TELNYX_TOOL_TOKEN>`.

Example tool definition (pseudocode):
- name: "find_person"
- method: POST
- url: `${BASE_URL}/telnyx/find_person`
- headers: `{ Authorization: 'Bearer ${TELNYX_TOOL_TOKEN}' }`
- input schema: `{ full_name: string, dob?: string, county?: string }`

Repeat similarly for `get_bail_status` and `create_bail_inquiry`.

See also: `docs/AI_Agent_Voice_Script.md` for the recommended call flow and prompts leveraging `bond_text` and `needs_human_review`.

## Telnyx Portal setup (step-by-step)

1) Assign your phone number to an AI Assistant
- In Telnyx → AI → Assistants → Create (or edit existing)
- Voice: choose ElevenLabs Lina (or your preferred voice)
- Model: Anthropic Claude 3.7 Sonnet (recommended)
- Phone Numbers: assign +1 713-225-2727 to this Assistant

2) Add Tools for your backend endpoints
- Tools → Add HTTP Tool for each endpoint:
  - POST ${BASE_URL}/telnyx/find_person
  - POST ${BASE_URL}/telnyx/get_bail_status
  - POST ${BASE_URL}/telnyx/create_bail_inquiry
  - POST ${BASE_URL}/telnyx/attach_caller
  - POST ${BASE_URL}/telnyx/transfer_plan (primary)
  - POST ${BASE_URL}/telnyx/transfer_target (fallback)
- Headers (for each Tool): Authorization: Bearer ${TELNYX_TOOL_TOKEN}

3) Configure environment in your app
- In your .env:
  - BASE_URL=https://YOUR_DOMAIN
  - TELNYX_TOOL_TOKEN=your-long-random-token
  - OFFICES_SCHEDULE_JSON=... (see Harris example above)
  - APP_TZ=America/Chicago
  - DIAL_ATTEMPT_TIMEOUT_SEC=20

4) Optional: Add webhooks for transcripts and call analytics
- In Telnyx → Assistants → Webhooks → set Event Webhook URL:
  - ${BASE_URL}/telnyx/ai_events
- Optional Shared Secret (recommended): set a random secret; in your .env set:
  - TELNYX_WEBHOOK_SECRET=the-same-secret
- (Optional) Call status webhooks (if you also use Call Control app):
  - Webhook URL: ${BASE_URL}/telnyx/call_events
- (Optional) Recording webhooks (if you enable recording):
  - Webhook URL: ${BASE_URL}/telnyx/recording_ready

Request format Telnyx will POST (examples vary):
```json
{
  "type": "ai.event",
  "data": {
    "session_id": "...",
    "event": "transcript.final",
    "text": "caller: ..."
  }
}
```

Verification:
- Telnyx will include X-Telnyx-Secret: <secret> if configured; the app verifies it.
- These webhook endpoints do not require the Bearer tool token.

### POST /telnyx/attach_caller
Attach caller info to a case CRM if a case exists for the inmate; always records an inquiry.

Request JSON:
- One of: `person_id` or `inmate_name`/`full_name` (+optional `dob`)
- `caller_name` (string, required)
- `caller_phone` (E.164, required)
- `relationship` (string, optional)
- `intends_to_post` (bool, optional)
- `notes` (string, optional)

Response:
```json
{ "ok": true, "inquiry_id": "...", "linked_to_case": true, "case_id": "CASE123" }
```
If the person is unresolved or there is no case, `linked_to_case` will be false, but the inquiry will still be created.

## Notes and limitations

- The fast path only checks a fixed set of counties: Harris, Brazoria, Galveston, Fort Bend, and only if those collections exist.
- County hint biases which collection to check first; it does not hard-filter across all results.
- `eligible` is a heuristic; business rules may require refinement.
- When a person is found only in simple_* data, the `person.id` is null (not persisted in `persons`).
