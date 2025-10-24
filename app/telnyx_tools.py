# app/telnyx_tools.py
from __future__ import annotations
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.security import HTTPBearer
from typing import Any, Dict, Optional, List
import time, re
from bson import ObjectId

from .config import settings
from .db import persons, custody_events, inquiries, logs, cases  # no 'db' import
from .sms import send_sms

# Expose Bearer auth in OpenAPI/Swagger; _auth below still validates the exact token
router = APIRouter(
    prefix="/telnyx",
    tags=["telnyx-tools"],
    dependencies=[Depends(HTTPBearer())]
)

# Get the Database handle from an existing collection
_db = persons.database  # <-- fixes ImportError on Render

# --------- helpers ---------
def _auth(req: Request) -> None:
    auth = req.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = auth.split(" ", 1)[1]
    if token != settings.TELNYX_TOOL_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid token")

def _verify_webhook_secret(req: Request) -> None:
    """Optional lightweight verification for webhook posts via a shared secret header.
    If TELNYX_WEBHOOK_SECRET is set in settings, require header 'X-Telnyx-Secret' to match.
    """
    secret = getattr(settings, "TELNYX_WEBHOOK_SECRET", None)
    if not secret:
        return
    hdr = req.headers.get("x-telnyx-secret")
    if not hdr or hdr != secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

def _e164(phone: str | None) -> Optional[str]:
    if not phone:
        return None
    phone = phone.strip()
    return phone if re.fullmatch(r"\+\d{10,15}", phone) else None

def _objid(s: str | None) -> Optional[ObjectId]:
    if not s:
        return None
    try:
        return ObjectId(s)
    except Exception:
        return None

def _latest_custody(person_id: str) -> Optional[dict]:
    return custody_events.find_one({"person_id": person_id}, sort=[("scraped_at", -1)])

def _parse_bond_str(total_bond: str | None) -> Optional[float]:
    if not total_bond:
        return None
    try:
        return float(total_bond.replace("$", "").replace(",", ""))
    except Exception:
        return None

def _parse_bond_label(s: str | None) -> tuple[Optional[float], Optional[bool]]:
    """Parse assorted bond label strings.
    Returns (amount_numeric, eligible) where eligible may be None if unknown.
    Examples handled: "$5,000.00", "5000", "No Bond", "PR Bond".
    """
    if not s:
        return None, None
    txt = s.strip().lower()
    if "no bond" in txt:
        return None, False
    if "pr bond" in txt or "personal recognizance" in txt:
        # Treat PR as bond amount 0 but eligible False for posting
        return 0.0, False
    # Extract first money-like token
    m = re.search(r"\$?\s*([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{2})?|[0-9]+)", s)
    if m:
        try:
            val = float(m.group(1).replace(",", ""))
            return val, (val > 0)
        except Exception:
            return None, None
    return None, None

def _needs_human_review(text: Optional[str], status: Optional[str]) -> bool:
    """Heuristic to flag when a human should review/advise caller.
    Triggers on phrases like 'refer to magistrate', 'pending', 'see judge', etc.
    """
    blob = f"{text or ''} {status or ''}".lower()
    return bool(re.search(r"refer|magistrate|see\s+(?:the\s+)?judge|pending|tbd|set by|to be|not available|call|contact", blob))

def _compose_agent_sms(
    *,
    county: Optional[str],
    inmate: Optional[dict],
    bail: Optional[dict],
    caller: Optional[dict],
    summary: Optional[str]
) -> str:
    """Build a concise SMS (<= 320 chars) for the on-call agent.
    inmate: { full_name?, dob? }
    bail: { total_bond?, amount_numeric?, eligible?, bond_text?, needs_human_review? }
    caller: { name?, phone?, relationship?, intends_to_post? }
    """
    parts: list[str] = []
    if county:
        parts.append(f"New transfer ({county.title()}):")
    else:
        parts.append("New transfer:")

    if inmate:
        nm = inmate.get("full_name") or inmate.get("name")
        dob = inmate.get("dob")
        if nm and dob:
            parts.append(f"Inmate {nm} (DOB {dob})")
        elif nm:
            parts.append(f"Inmate {nm}")

    if bail:
        tb = bail.get("total_bond") or bail.get("bond_text")
        elig = bail.get("eligible")
        need = bail.get("needs_human_review")
        if tb:
            parts.append(f"Bail {tb}")
        if elig is True:
            parts.append("Eligible")
        elif elig is False:
            parts.append("Not eligible")
        elif need:
            parts.append("Needs review")

    if caller:
        cname = caller.get("name")
        cph = caller.get("phone")
        rel = caller.get("relationship")
        intends = caller.get("intends_to_post")
        if cname and cph:
            parts.append(f"Caller {cname} {cph}")
        elif cname:
            parts.append(f"Caller {cname}")
        elif cph:
            parts.append(f"Caller {cph}")
        if rel:
            parts.append(rel)
        if intends is True:
            parts.append("intends to post")

    if summary:
        parts.append(f"Summary: {summary}")

    msg = " | ".join([p for p in parts if p])
    return (msg[:317] + "...") if len(msg) > 320 else msg

# ---------- office routing ----------
import json as _json
from datetime import datetime, time as dtime
try:
    import zoneinfo  # Python 3.9+
except Exception:  # pragma: no cover
    zoneinfo = None

def _office_phone_for_county(county: Optional[str]) -> Optional[str]:
    """Return E.164 office phone for a county using settings routes, else default.
    Match is case-insensitive; uses normalized county key.
    """
    try:
        routes = (_json.loads(settings.OFFICE_ROUTES_JSON) if settings.OFFICE_ROUTES_JSON else {})
    except Exception:
        routes = {}
    key = (county or "").strip().lower()
    if key in routes:
        return routes.get(key)
    # try simple normalization (drop ' county')
    if key.endswith(" county") and key[:-7] in routes:
        return routes.get(key[:-7])
    return settings.DEFAULT_OFFICE_NUMBER

def _transfer_numbers_by_schedule(county: Optional[str], lang: Optional[str] = None) -> list[str]:
    """Return an ordered list of phone numbers based on OFFICES_SCHEDULE_JSON.
    If no schedule match, fall back to OFFICE_ROUTES_JSON or default.
    """
    numbers: list[str] = []
    # Parse schedule JSON
    try:
        sched = _json.loads(settings.OFFICES_SCHEDULE_JSON) if settings.OFFICES_SCHEDULE_JSON else {}
    except Exception:
        sched = {}
    tzname = settings.APP_TZ or "UTC"
    tz = zoneinfo.ZoneInfo(tzname) if zoneinfo else None
    now = datetime.now(tz) if tz else datetime.utcnow()
    dow = ["mon","tue","wed","thu","fri","sat","sun"][now.weekday()]
    cur_min = now.hour * 60 + now.minute

    key = (county or "").strip().lower()
    langkey = f"{key}_{(lang or '').strip().lower()}" if lang else None
    # candidates: county+lang, county-specific, normalized without ' county', then default
    selected_key: Optional[str] = None
    for k in [langkey, key, key[:-7] if key.endswith(" county") else None, "default"]:
        if not k:
            continue
        rules = sched.get(k)
        if not rules:
            continue
        for r in rules:
            days = [d.lower() for d in r.get("days", [])]
            s = r.get("start","00:00")
            e = r.get("end","23:59")
            try:
                sh, sm = [int(x) for x in s.split(":",1)]
                eh, em = [int(x) for x in e.split(":",1)]
            except Exception:
                sh, sm, eh, em = 0, 0, 23, 59
            start_min = sh*60 + sm
            end_min = eh*60 + em
            in_window = False
            if dow in days:
                if start_min <= end_min:
                    in_window = (start_min <= cur_min <= end_min)
                else:
                    # window wraps past midnight
                    in_window = (cur_min >= start_min or cur_min <= end_min)
            if in_window:
                arr = [p for p in r.get("numbers", []) if isinstance(p, str) and p.startswith("+")]
                numbers.extend(arr)
        if numbers:
            selected_key = k
            break

    # Dedupe while preserving order
    seen = set()
    deduped: list[str] = []
    for n in numbers:
        if n not in seen:
            deduped.append(n)
            seen.add(n)
    numbers = deduped

    # Harris-specific policy: if we matched Harris (non-Spanish) schedule, insert Alex (Spanish fallback) as the next attempt.
    try:
        if selected_key == "harris" and (not lang or lang.lower() != "es"):
            # find Alex from the 'harris_es' schedule (first available number)
            r_es = sched.get("harris_es") or []
            alex_num: Optional[str] = None
            for rr in r_es:
                arr = [p for p in rr.get("numbers", []) if isinstance(p, str) and p.startswith("+")]
                if arr:
                    alex_num = arr[0]
                    break
            if alex_num and alex_num not in numbers:
                if numbers:
                    numbers.insert(1, alex_num)
                else:
                    numbers.append(alex_num)
    except Exception:
        pass

    # If still empty, apply Harris-specific gap fallback to Alex (Spanish line)
    if not numbers:
        try:
            # Normalize county key used earlier
            base_key = (county or "").strip().lower()
            if base_key.endswith(" county"):
                base_key = base_key[:-7]
            if base_key == "harris":
                r_es = sched.get("harris_es") or []
                alex_num: Optional[str] = None
                for rr in r_es:
                    arr = [p for p in rr.get("numbers", []) if isinstance(p, str) and p.startswith("+")]
                    if arr:
                        alex_num = arr[0]
                        break
                if alex_num:
                    numbers.append(alex_num)
        except Exception:
            pass

    # if still empty, fallback to single route
    if not numbers:
        one = _office_phone_for_county(county)
        if one:
            numbers.append(one)
    # final fallback: none
    return numbers

# ---------- FAST PATH over simple_* collections ----------
SIMPLE_COUNTIES = ["harris", "brazoria", "galveston", "fortbend"]

def _list_simple_cols():
    existing = set(_db.list_collection_names())
    return [_db[f"simple_{c}"] for c in SIMPLE_COUNTIES if f"simple_{c}" in existing]

def _split_name(full_name: str) -> tuple[Optional[str], Optional[str]]:
    if not full_name:
        return None, None
    s = full_name.strip()
    if "," in s:
        last, first = s.split(",", 1)
        return first.strip() or None, last.strip() or None
    parts = s.split()
    if len(parts) >= 2:
        return " ".join(parts[:-1]), parts[-1]
    return s, None

def _score_simple_hit(doc: dict, want_last: Optional[str], want_first: Optional[str], dob: Optional[str]) -> int:
    sc = 0
    try:
        if want_last and doc.get("last_name"):
            if doc["last_name"].upper() == want_last.upper():
                sc += 4
            elif doc["last_name"].upper().startswith(want_last.upper()):
                sc += 2
        if want_first and doc.get("first_name"):
            if doc["first_name"].upper().startswith((want_first or "").upper()):
                sc += 2
        if dob and doc.get("dob") and str(doc["dob"]) == dob:
            sc += 3
        if doc.get("booking_date"):
            sc += 1
    except Exception:
        pass
    return sc

def _find_in_simple(full_name: str, *, dob: Optional[str]=None, county_hint: Optional[str]=None) -> Optional[dict]:
    first, last = _split_name(full_name)
    if not last and first:
        last, first = first, None  # single token -> treat as last name

    q: Dict[str, Any] = {}
    if last:
        q["last_name"] = {"$regex": f"^{re.escape(last)}$", "$options": "i"}
    if first:
        q["first_name"] = {"$regex": f"^{re.escape(first)}", "$options": "i"}
    if dob:
        q["dob"] = dob

    cols = _list_simple_cols()
    if county_hint:
        hint = county_hint.lower()
        cols = sorted(cols, key=lambda c: 0 if c.name.endswith(hint) else 1)

    best = None
    best_score = -1
    for col in cols:
        cur = col.find(q).sort([("booking_date", -1)]).limit(5)
        for d in cur:
            sc = _score_simple_hit(d, last, first, dob)
            if sc > best_score:
                best, best_score = d, sc
    return best

def _bail_view_from_simple(d: dict) -> dict:
    # Prefer explicit numeric bond_amount when present
    amount = None
    if d.get("bond_amount") is not None:
        try:
            amount = float(d["bond_amount"])
        except Exception:
            amount = None

    total_bond_str = d.get("bond")
    eligible = None if amount is None else (amount > 0)

    # Harris often uses bond_label instead of bond/bond_amount
    if amount is None and (not total_bond_str):
        amt2, elig2 = _parse_bond_label(d.get("bond_label") or d.get("dbg_bond_note"))
        if amt2 is not None:
            amount = amt2
            total_bond_str = f"${int(amt2):,}.00"
            eligible = elig2 if elig2 is not None else (amt2 > 0)
        elif elig2 is not None:
            # we know it's no/PR bond, but no numeric
            eligible = elig2

    # If we had amount but no string, format a default string
    if (not total_bond_str) and (amount is not None):
        total_bond_str = f"${int(amount):,}.00"

    bond_text = d.get("bond") or d.get("bond_label") or d.get("dbg_bond_note")
    needs_review = (amount is None) and _needs_human_review(bond_text, d.get("category") or d.get("status"))

    return {
        "found": True,
        "has_custody": True,
        "status": d.get("category") or "In Custody",
        "total_bond": total_bond_str,
        "amount_numeric": amount,
        "eligible": eligible,
        "bond_text": bond_text,
        "needs_human_review": needs_review
    }

def _resolve_person_id_from_payload(person_id: str | None, full_name: str | None, dob: str | None) -> Optional[str]:
    """Resolve and return person_id (as string) if possible.
    Does not create new persons; only matches existing.
    """
    if person_id:
        oid = _objid(person_id)
        if not oid:
            return None
        pdoc = persons.find_one({"_id": oid})
        return str(pdoc["_id"]) if pdoc else None
    if full_name:
        q: Dict[str, Any] = {"full_name": full_name}
        if dob:
            q["dob"] = dob
        pdoc = persons.find_one(q)
        if pdoc:
            return str(pdoc["_id"])  # type: ignore[index]
    return None

# --------- endpoints ---------

@router.post("/find_person")
async def find_person(payload: Dict[str, Any], request: Request):
    """
    Find a person by full name (required) and optional date-of-birth.
    Returns the person and their latest custody snapshot (if any).
    """
    _auth(request)
    full_name = (payload.get("full_name") or "").strip()
    dob       = (payload.get("dob") or "").strip() or None
    county    = (payload.get("county") or "").strip() or None

    if not full_name:
        raise HTTPException(400, "Provide 'full_name'")

    # FAST PATH: simple_* collections
    s = _find_in_simple(full_name, dob=dob, county_hint=county)
    if s:
        latest = {
            "id": str(s.get("_id")),
            "status": "In Custody",
            "facility": (s.get("county") or "").title(),
            "county": (s.get("county") or "").title(),
            "booking_number": s.get("booking_number"),
            "total_bond": s.get("bond") or (f"${int(s.get('bond_amount',0)):,}.00" if s.get("bond_amount") else None),
            "arrest_date": s.get("booking_date"),
            "source_url": None,
            "scraped_at": s.get("normalized_at"),
        }
        person_out = {
            "id": None,
            "full_name": s.get("full_name"),
            "dob": s.get("dob"),
            "aka": [],
        }
        return {"found": True, "person": person_out, "latest_custody": latest}

    # FALLBACK: persons + custody_events
    q: Dict[str, Any] = {"full_name": full_name}
    if dob:
        q["dob"] = dob

    pdoc = persons.find_one(q)
    if not pdoc:
        return {"found": False}

    pid = str(pdoc.get("_id"))
    latest = _latest_custody(pid)

    if county and latest and (latest.get("county") or "").lower() != county.lower():
        latest = custody_events.find_one(
            {"person_id": pid, "county": {"$regex": f"^{re.escape(county)}$", "$options": "i"}},
            sort=[("scraped_at", -1)]
        ) or latest

    custody = None
    if latest:
        custody = {
            "id": str(latest.get("_id")),
            "status": latest.get("status"),
            "facility": latest.get("facility"),
            "county": latest.get("county"),
            "booking_number": latest.get("booking_number"),
            "total_bond": latest.get("total_bond"),
            "arrest_date": latest.get("arrest_date"),
            "source_url": latest.get("source_url"),
            "scraped_at": latest.get("scraped_at"),
        }

    return {
        "found": True,
        "person": {
            "id": pid,
            "full_name": pdoc.get("full_name"),
            "dob": pdoc.get("dob"),
            "aka": pdoc.get("aka", []),
        },
        "latest_custody": custody
    }

@router.post("/get_bail_status")
async def get_bail_status(payload: Dict[str, Any], request: Request):
    """
    Get simple bail status/amount. Accepts person_id OR full_name(+optional dob).
    Tries simple_* first, then persons/custody_events.
    """
    _auth(request)

    person_id = (payload.get("person_id") or "").strip()
    full_name = (payload.get("full_name") or "").strip()
    dob       = (payload.get("dob") or "").strip() or None
    county    = (payload.get("county") or "").strip() or None

    # FAST PATH: name lookup in simple_* first
    if full_name:
        s = _find_in_simple(full_name, dob=dob, county_hint=county)
        if s:
            return _bail_view_from_simple(s)

    # FALLBACK: persons + custody_events
    pdoc = None
    if person_id:
        oid = _objid(person_id)
        if not oid:
            raise HTTPException(400, "Invalid person_id")
        pdoc = persons.find_one({"_id": oid})
    elif full_name:
        q: Dict[str, Any] = {"full_name": full_name}
        if dob:
            q["dob"] = dob
        pdoc = persons.find_one(q)
    else:
        raise HTTPException(400, "Provide person_id OR full_name")

    if not pdoc:
        return {"found": False}

    pid = str(pdoc.get("_id"))
    c = _latest_custody(pid)
    if not c:
        return {"found": True, "has_custody": False}

    amount_num = _parse_bond_str(c.get("total_bond"))
    eligible = None
    if amount_num is not None:
        status = (c.get("status") or "").lower()
        eligible = not ("release" in status or "no bond" in (c.get("total_bond") or "").lower())

    bond_text = c.get("total_bond")
    needs_review = (amount_num is None) and _needs_human_review(bond_text, c.get("status"))

    return {
        "found": True,
        "has_custody": True,
        "status": c.get("status"),
        "total_bond": c.get("total_bond"),
        "amount_numeric": amount_num,
        "eligible": eligible,
        "bond_text": bond_text,
        "needs_human_review": needs_review
    }

@router.post("/create_bail_inquiry")
async def create_bail_inquiry(payload: Dict[str, Any], request: Request):
    """
    Create an inquiry record from a caller. This does NOT modify custody_events;
    it creates/records the lead in your 'inquiries' collection.
    """
    _auth(request)
    person_id   = (payload.get("person_id") or "").strip()
    full_name   = (payload.get("inmate_name") or payload.get("full_name") or "").strip()
    caller_name = (payload.get("caller_name") or "").strip()
    caller_ph   = _e164(payload.get("caller_phone"))
    relation    = (payload.get("relationship") or "").strip()
    intends     = bool(payload.get("intends_to_post", False))
    notes       = (payload.get("notes") or "").strip()

    if not person_id and not full_name:
        raise HTTPException(400, "person_id or full_name required")
    if not caller_name or not caller_ph:
        raise HTTPException(400, "Valid caller_name and caller_phone (E.164) required")

    doc = {
        "person_id": person_id or None,
        "full_name": full_name or None,
        "caller_name": caller_name,
        "caller_phone": caller_ph,
        "relationship": relation or None,
        "intends_to_post": intends,
        "notes": notes or None,
        "created_ts": int(time.time())
    }
    res = inquiries.insert_one(doc)

    logs.insert_one({"type":"telnyx_create_inquiry","inquiry_id":str(res.inserted_id),"ts":int(time.time())})
    return {"ok": True, "inquiry_id": str(res.inserted_id)}

@router.post("/attach_caller")
async def attach_caller_to_case(payload: Dict[str, Any], request: Request):
    """
    Attach caller info to an existing case CRM (if a case exists for the inmate),
    and persist an inquiry record regardless. This is intended to be called after
    the agent confirms the inmate and bail exists.

    Input JSON:
    - person_id OR full_name (+optional dob)
    - caller_name (required)
    - caller_phone (E.164, required)
    - relationship (optional)
    - intends_to_post (bool, optional)
    - notes (optional)
    Returns: { ok, inquiry_id, linked_to_case, case_id? }
    """
    _auth(request)

    person_id   = (payload.get("person_id") or "").strip()
    full_name   = (payload.get("inmate_name") or payload.get("full_name") or "").strip()
    dob         = (payload.get("dob") or "").strip() or None
    caller_name = (payload.get("caller_name") or "").strip()
    caller_ph   = _e164(payload.get("caller_phone"))
    relationship= (payload.get("relationship") or "").strip() or None
    intends     = bool(payload.get("intends_to_post", False))
    notes       = (payload.get("notes") or "").strip() or None

    if not person_id and not full_name:
        raise HTTPException(400, "person_id or full_name required")
    if not caller_name or not caller_ph:
        raise HTTPException(400, "Valid caller_name and caller_phone (E.164) required")

    # First, persist the inquiry record
    inquiry_doc = {
        "person_id": person_id or None,
        "full_name": full_name or None,
        "caller_name": caller_name,
        "caller_phone": caller_ph,
        "relationship": relationship,
        "intends_to_post": intends,
        "notes": notes,
        "created_ts": int(time.time())
    }
    res = inquiries.insert_one(inquiry_doc)

    # Attempt to resolve person_id to link to a case
    pid = _resolve_person_id_from_payload(person_id or None, full_name or None, dob)
    linked = False
    case_id_out: Optional[str] = None
    if pid:
        case_doc = cases.find_one({"person_id": pid})
        if case_doc:
            case_id_out = case_doc.get("case_id") or str(case_doc.get("_id"))
            # Upsert CRM contact into the case document
            cases.update_one(
                {"_id": case_doc["_id"]},
                {"$push": {"crm_contacts": {
                    "ts": int(time.time()),
                    "inquiry_id": str(res.inserted_id),
                    "caller_name": caller_name,
                    "caller_phone": caller_ph,
                    "relationship": relationship,
                    "intends_to_post": intends,
                    "notes": notes,
                }},
                 "$set": {"crm_updated_ts": int(time.time())}}
            )
            linked = True
        else:
            logs.insert_one({
                "type": "telnyx_attach_no_case",
                "person_id": pid,
                "inquiry_id": str(res.inserted_id),
                "ts": int(time.time())
            })
    else:
        logs.insert_one({
            "type": "telnyx_attach_unresolved_person",
            "full_name": full_name,
            "inquiry_id": str(res.inserted_id),
            "ts": int(time.time())
        })

    return {
        "ok": True,
        "inquiry_id": str(res.inserted_id),
        "linked_to_case": linked,
        "case_id": case_id_out
    }

@router.post("/transfer_target")
async def transfer_target(payload: Dict[str, Any], request: Request):
    """
    Resolve the best transfer phone number (E.164) for an office based on county.
    Input JSON: { county?: string }
    Returns: { ok: true, phone?: "+1..." }
    If no county match and no default is configured, phone will be null.
    """
    _auth(request)
    county = (payload.get("county") or "").strip() or None
    phone = _office_phone_for_county(county)
    return {"ok": True, "phone": phone}

@router.post("/transfer_plan")
async def transfer_plan(payload: Dict[str, Any], request: Request):
    """
    Return an ordered list of phone numbers to try, based on county and schedule.
    Input JSON: { county?: string }
    Response: { ok: true, numbers: ["+1...", "+1..."], attempt_timeout_sec: number }
    """
    _auth(request)
    county = (payload.get("county") or "").strip() or None
    lang = (payload.get("lang") or "").strip() or None
    numbers = _transfer_numbers_by_schedule(county, lang)
    return {
        "ok": True,
        "numbers": numbers,
        "attempt_timeout_sec": int(settings.DIAL_ATTEMPT_TIMEOUT_SEC or 20)
    }

@router.get("/schedule_status")
async def schedule_status(request: Request, county: Optional[str] = None, lang: Optional[str] = None):
    """Diagnostic endpoint to verify OFFICES_SCHEDULE_JSON parsing and matching.
    Requires Bearer token. Returns tz, time, parsed keys, and a sample plan.
    """
    _auth(request)
    # Parse schedule
    try:
        sched = _json.loads(settings.OFFICES_SCHEDULE_JSON) if settings.OFFICES_SCHEDULE_JSON else {}
        parse_ok = True
    except Exception as e:
        sched = {}
        parse_ok = False
    keys = list(sched.keys())
    tzname = settings.APP_TZ or "UTC"
    tz = zoneinfo.ZoneInfo(tzname) if zoneinfo else None
    now = datetime.now(tz) if tz else datetime.utcnow()
    dow = ["mon","tue","wed","thu","fri","sat","sun"][now.weekday()]
    plan = _transfer_numbers_by_schedule(county or "Harris", lang)
    fallback = _office_phone_for_county(county or "Harris")
    return {
        "parse_ok": parse_ok,
        "tz": tzname,
        "now_local": now.isoformat(),
        "weekday": dow,
        "parsed_keys": keys,
        "has_default_list": isinstance(sched.get("default"), list),
        "sample_input": {"county": county or "Harris", "lang": lang},
        "sample_plan": plan,
        "fallback_via_routes": fallback
    }

@router.post("/notify_agent")
async def notify_agent(payload: Dict[str, Any], request: Request):
    """Send an SMS summary to the on-call agent before/while transferring.
    Input JSON:
    - to_phone (E.164, required)
    - county (optional)
    - inmate (object with full_name?, dob?)
    - bail (object with total_bond?, amount_numeric?, eligible?, bond_text?, needs_human_review?)
    - caller (object with name?, phone?, relationship?, intends_to_post?)
    - summary (string, optional freeform notes/summary)
    Returns: { ok: true }
    """
    _auth(request)
    to = _e164(payload.get("to_phone"))
    if not to:
        raise HTTPException(400, "Valid E.164 'to_phone' required")
    county = (payload.get("county") or "").strip() or None
    inmate = payload.get("inmate") or {}
    bail = payload.get("bail") or {}
    caller = payload.get("caller") or {}
    summary = (payload.get("summary") or "").strip() or None

    body = _compose_agent_sms(county=county, inmate=inmate, bail=bail, caller=caller, summary=summary)
    try:
        res = send_sms(to, body)
        logs.insert_one({"type":"notify_agent_sms","to":to,"body":body,"ts":int(time.time()),"res":res})
        return {"ok": True, "provider_response": res}
    except Exception as e:
        logs.insert_one({"type":"notify_agent_sms_error","to":to,"err":str(e),"ts":int(time.time())})
        raise HTTPException(500, "Failed to send SMS")

@router.post("/notify_group")
async def notify_group(payload: Dict[str, Any], request: Request):
    """Send an SMS/WhatsApp summary to multiple recipients.
    Input JSON:
    - to_phones: array of E.164 strings (required)
    - county, inmate, bail, caller, summary: same as /notify_agent (optional)
    Returns: { ok: true, sent: [...], failed: [...]} with per-recipient provider responses.
    """
    _auth(request)
    tos_raw = payload.get("to_phones") or []
    if not isinstance(tos_raw, list) or not tos_raw:
        raise HTTPException(400, "Provide non-empty 'to_phones' array of E.164 numbers")
    to_list: List[str] = []
    for p in tos_raw:
        e = _e164(str(p))
        if e:
            to_list.append(e)
    if not to_list:
        raise HTTPException(400, "No valid E.164 numbers in 'to_phones'")

    county = (payload.get("county") or "").strip() or None
    inmate = payload.get("inmate") or {}
    bail = payload.get("bail") or {}
    caller = payload.get("caller") or {}
    summary = (payload.get("summary") or "").strip() or None

    body = _compose_agent_sms(county=county, inmate=inmate, bail=bail, caller=caller, summary=summary)
    sent: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for to in to_list:
        try:
            res = send_sms(to, body)
            logs.insert_one({"type":"notify_group_sms","to":to,"body":body,"ts":int(time.time()),"res":res})
            sent.append({"to": to, "provider_response": res})
        except Exception as e:
            logs.insert_one({"type":"notify_group_sms_error","to":to,"err":str(e),"ts":int(time.time())})
            failed.append({"to": to, "error": str(e)})
    return {"ok": True, "sent": sent, "failed": failed, "count": {"sent": len(sent), "failed": len(failed)}}

# ---------- optional webhooks ----------
@router.post("/ai_events")
async def telnyx_ai_events(payload: Dict[str, Any], request: Request):
    """Receive Telnyx AI Assistant events (session start/end, tool calls, transcripts).
    Secured with optional X-Telnyx-Secret if configured. Does not require Bearer token.
    """
    _verify_webhook_secret(request)
    logs.insert_one({
        "type": "telnyx_ai_events",
        "ts": int(time.time()),
        "payload": payload
    })
    return {"ok": True}

@router.post("/call_events")
async def telnyx_call_events(payload: Dict[str, Any], request: Request):
    """Receive telephony-level call status events if configured in Telnyx.
    Secured with optional X-Telnyx-Secret.
    """
    _verify_webhook_secret(request)
    logs.insert_one({
        "type": "telnyx_call_events",
        "ts": int(time.time()),
        "payload": payload
    })
    return {"ok": True}

# ---------- diagnostics ----------
@router.get("/sms_status")
async def sms_status(request: Request):
    """Return which SMS providers are configured so we can debug notify_agent delivery.
    Does not send any messages. Requires Bearer token.
    """
    _auth(request)
    try:
        from .config import settings as _s
    except Exception:
        raise HTTPException(500, "Config load error")

    telnyx_ready = bool(_s.TELNYX_API_KEY and (_s.TELNYX_MESSAGING_FROM_NUMBER or _s.TELNYX_MESSAGING_PROFILE_ID))
    twilio_ready = bool(
        _s.TWILIO_ACCOUNT_SID and _s.TWILIO_AUTH_TOKEN and (
            getattr(_s, "TWILIO_MESSAGING_SERVICE_SID", None) or _s.TWILIO_FROM_NUMBER
        )
    )

    return {
        "ok": True,
        "telnyx": {
            "api_key": bool(_s.TELNYX_API_KEY),
            "from_number": bool(_s.TELNYX_MESSAGING_FROM_NUMBER),
            "messaging_profile_id": bool(_s.TELNYX_MESSAGING_PROFILE_ID),
            "configured": telnyx_ready
        },
        "twilio": {
            "account_sid": bool(_s.TWILIO_ACCOUNT_SID),
            "from_number": bool(_s.TWILIO_FROM_NUMBER),
            "messaging_service_sid": bool(getattr(_s, "TWILIO_MESSAGING_SERVICE_SID", None)),
            "use_whatsapp": bool(getattr(_s, "TWILIO_USE_WHATSAPP", False)),
            "whatsapp_from": bool(getattr(_s, "TWILIO_WHATSAPP_FROM", None)),
            "configured": twilio_ready
        },
        "send_order": ["twilio", "telnyx", "dev-noop"]
    }

@router.post("/recording_ready")
async def telnyx_recording_ready(payload: Dict[str, Any], request: Request):
    """Receive recording availability notifications (URL) when enabled in Telnyx.
    Secured with optional X-Telnyx-Secret.
    """
    _verify_webhook_secret(request)
    logs.insert_one({
        "type": "telnyx_recording_ready",
        "ts": int(time.time()),
        "payload": payload
    })
    return {"ok": True}