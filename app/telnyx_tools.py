# app/telnyx_tools.py
from __future__ import annotations
from fastapi import APIRouter, Request, HTTPException, Depends
from fastapi.security import HTTPBearer
from typing import Any, Dict, Optional
import time, re
from bson import ObjectId

from .config import settings
from .db import persons, custody_events, inquiries, logs, cases  # no 'db' import

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

# ---------- office routing ----------
import json as _json

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