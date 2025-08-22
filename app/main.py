from fastapi import FastAPI, Request, UploadFile, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import time, json, os

from .config import settings
from .db import cases, checkins, links, logs
from .tokens import make_one_time_token, verify_token
from .storage import upload_photo
from .sms import send_sms
from .geo import ip_to_geo
from .telnyx_tools import router as telnyx_router

# ---- App & mounts ----
app = FastAPI()

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

app.mount("/static", StaticFiles(directory="app/static"), name="static")
tpl = Jinja2Templates(directory="app/templates")

# Telnyx tool webhooks
app.include_router(telnyx_router)

# ---- Helper: IP + UA ----
def client_meta(req: Request):
    ip = req.headers.get("x-forwarded-for", req.client.host)
    ua = req.headers.get("user-agent", "")
    return ip.split(",")[0].strip(), ua

# ---- Helper: unified event logger ----
async def log_event(case_id: str, req: Request, evt_type: str,
                    person_id: str | None = None, notes: str | None = None,
                    tok: str | None = None, extra: dict | None = None):
    ip, ua = client_meta(req)
    geo = await ip_to_geo(ip)
    doc = {
        "ts": int(time.time()),
        "case_id": case_id,
        "person_id": person_id,
        "type": evt_type,
        "ip": ip,
        "user_agent": ua,
        "geo": geo,
        "notes": notes,
        "tok": tok,
    }
    if extra:
        doc.update(extra)
    logs.insert_one(doc)
    return doc

# ---- Beacons (pixel + css) ----
@app.get("/px/{case_id}")
async def px(case_id: str, req: Request, tok: str | None = None):
    await log_event(case_id, req, evt_type="asset_hit", notes="tracking_pixel", tok=tok)
    # 1x1 transparent GIF
    return Response(
        content=(
            b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xFF\xFF\xFF!\xF9\x04"
            b"\x01\x00\x00\x01\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
        ),
        media_type="image/gif"
    )

@app.get("/css/{case_id}.css")
async def css(case_id: str, req: Request, tok: str | None = None):
    await log_event(case_id, req, evt_type="asset_hit", notes="css_beacon", tok=tok)
    return Response("body{visibility:visible}", media_type="text/css")

# ---- Preview page (OG card) ----
@app.get("/p/{case_id}", response_class=HTMLResponse)
async def preview_page(req: Request, case_id: str):
    case = cases.find_one({"case_id": case_id}) or {}

    page_url = f"{settings.BASE_URL}/p/{case_id}"
    # Prefer case-specific image; fall back to a default
    og_candidate = f"{settings.BASE_URL}/static/og/{case_id}.jpg"
    og_default   = f"{settings.BASE_URL}/static/og/default.jpg"
    og_img = og_candidate if os.path.exists("app/static/og/"+case_id+".jpg") else og_default

    action_tok = make_one_time_token(case.get("person_id", "unknown"), case_id, ttl_seconds=3600)
    action_url = f"{settings.BASE_URL}/checkin?tok={action_tok}"

    await log_event(case_id, req, evt_type="preview_view", person_id=case.get("person_id"))

    return tpl.TemplateResponse("preview.html", {
        "request": req,
        "title": "Required Check‑In",
        "og_title": f"Required Check‑In for {case.get('name','Defendant')}",
        "og_desc": "Tap to securely share your location to verify compliance.",
        "og_url": page_url,
        "og_image": og_img,
        "name": case.get("name","Defendant"),
        "case_id": case_id,
        "action_url": action_url,
        "beacon_css": f"{settings.BASE_URL}/css/{case_id}.css?tok={action_tok}",
        "beacon_px":  f"{settings.BASE_URL}/px/{case_id}?tok={action_tok}",
    })

# ---- Check-in page ----
@app.get("/checkin", response_class=HTMLResponse)
async def checkin_page(req: Request, tok: str):
    try:
        payload = verify_token(tok)
    except Exception:
        raise HTTPException(400, "Invalid or expired link")

    case_id = payload.get("case")
    person_id = payload.get("sub")

    await log_event(case_id, req, evt_type="checkin_view", person_id=person_id, tok=tok)

    return tpl.TemplateResponse("checkin.html", {
        "request": req,
        "tok": tok,
        "case_id": case_id
    })

# ---- Receive check-in ----
@app.post("/api/checkin")
async def api_checkin(request: Request, token: str = Form(...), photo: UploadFile | None = None,
                      loc: str = Form(""), loc_error: str = Form("")):
    ip, ua = client_meta(request)

    try:
        payload = verify_token(token)
    except Exception:
        raise HTTPException(400, "Invalid or expired token")

    person_id = payload.get("sub")
    case_id = payload.get("case")

    gps = json.loads(loc) if loc else None

    photo_url = None
    if photo and photo.filename:
        content = await photo.read()
        try:
            photo_url = upload_photo(content, key_prefix=person_id or "unknown")
        except Exception:
            # If S3 is disabled, upload_photo returns None; keep going
            photo_url = None

    checkins.insert_one({
        "person_id": person_id,
        "case_id": case_id,
        "ts": int(time.time()),
        "ip": ip,
        "user_agent": ua,
        "gps": gps,
        "loc_error": loc_error,
        "photo_url": photo_url,
        "outcome": "ok" if gps or photo_url else "partial"
    })

    await log_event(case_id, request, evt_type="checkin_submit", person_id=person_id, tok=token,
                    extra={"had_gps": bool(gps), "had_photo": bool(photo_url)})

    # Queue follow-up (log-only in this starter)
    try:
        case = cases.find_one({"case_id": case_id}) or {}
        to = case.get("phone")
        if to:
            logs.insert_one({"type":"followup_queued","case_id":case_id,"ts":int(time.time())})
    except Exception as e:
        logs.insert_one({"type":"followup_error","case_id":case_id,"err":str(e),"ts":int(time.time())})

    return JSONResponse({"ok": True})

# ---- Record explicit refusal ----
@app.post("/api/refusal")
async def api_refusal(request: Request, tok: str):
    ip, ua = client_meta(request)
    try:
        payload = verify_token(tok)
    except Exception:
        raise HTTPException(400, "Invalid or expired link")

    case_id = payload.get("case")
    person_id = payload.get("sub")

    geo = await ip_to_geo(ip)

    await log_event(case_id, request, evt_type="refusal", person_id=person_id, tok=tok, extra={"geo": geo})

    checkins.insert_one({
        "person_id": person_id,
        "case_id": case_id,
        "ts": int(time.time()),
        "ip": ip,
        "user_agent": ua,
        "gps": None,
        "loc_error": "refused",
        "photo_url": None,
        "outcome": "refused",
        "geo": geo
    })

    return JSONResponse({"ok": True})

# ---- Admin: last-known area (coarse) ----
@app.get("/admin/last_area/{case_id}", response_class=HTMLResponse)
async def admin_last_area(req: Request, case_id: str):
    last = (logs.find_one({"case_id": case_id, "geo": {"$ne": None}}, sort=[("ts", -1)])
            or checkins.find_one({"case_id": case_id, "geo": {"$ne": None}}, sort=[("ts", -1)]))
    if not last or not last.get("geo"):
        return HTMLResponse(f"<h3>No geo on record yet for {case_id}</h3>", status_code=200)

    geo = last["geo"]
    name = (cases.find_one({"case_id": case_id}) or {}).get("name", "Defendant")

    return tpl.TemplateResponse("admin_last_area.html", {
        "request": req,
        "case_id": case_id,
        "name": name,
        "geo": geo,
        "ts": last.get("ts", 0)
    })

# ---- Admin: send link ----
@app.post("/admin/send_link/{case_id}")
async def admin_send_link(case_id: str):
    case = cases.find_one({"case_id": case_id})
    if not case:
        raise HTTPException(404, "Case not found")

    preview_url = f"{settings.BASE_URL}/p/{case_id}"
    action_tok = make_one_time_token(case["person_id"], case_id)
    action_url = f"{settings.BASE_URL}/checkin?tok={action_tok}"

    body = (
        f"{case['name']}, immediate bond compliance check:\n"
        f"{preview_url}\n"
        f"Secure check‑in: {action_url}\n"
        f"Reply STOP to opt out."
    )

    send_sms(case["phone"], body)
    await log_event(
        case_id,
        Request(scope={"type":"http","headers":[],"client":("0.0.0.0",0)}),
        evt_type="sms_sent",
        person_id=case.get("person_id"),
        notes="admin_send_link",
        extra={"to": case["phone"]},
    )
    return {"ok": True}