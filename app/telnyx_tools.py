# app/telnyx_tools.py
from __future__ import annotations
from fastapi import APIRouter, Request, HTTPException
from typing import Any, Dict, Optional
import time, re
from bson import ObjectId

from .config import settings
from .db import persons, custody_events, inquiries, logs  # <-- use your actual collections

router = APIRouter(prefix="/telnyx", tags=["telnyx-tools"])

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
    """Return the most recent custody_event doc for a person_id, if any."""
    return custody_events.find_one(
        {"person_id": person_id},
        sort=[("scraped_at", -1)]  # ISO time string sorts fine
    )

def _parse_bond_str(total_bond: str | None) -> Optional[float]:
    """From strings like '$101,500.00' -> 101500.0. Returns None if missing."""
    if not total_bond:
        return None
    try:
        return float(total_bond.replace("$", "").replace(",", ""))
    except Exception:
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
    dob       = (payload.get("dob") or "").strip() or None  # may be null in DB
    county    = (payload.get("county") or "").strip()       # optional hint

    if not full_name:
        raise HTTPException(400, "Provide 'full_name'")

    q: Dict[str, Any] = {"full_name": full_name}
    # Only add DOB to the query if the caller actually provided one and
    # you store DOB values (may be null in many records)
    if dob:
        q["dob"] = dob

    pdoc = persons.find_one(q)
    if not pdoc:
        return {"found": False}

    pid = str(pdoc.get("_id"))
    latest = _latest_custody(pid)

    # Optionally filter by county hint (if provided and latest doesn't match)
    if county and latest and (latest.get("county") or "").lower() != county.lower():
        # Try to find one that matches the county hint
        latest = custody_events.find_one(
            {"person_id": pid, "county": {"$regex": f"^{re.escape(county)}$", "$options": "i"}},
            sort=[("scraped_at", -1)]
        ) or latest

    # Build response
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
    """
    _auth(request)

    person_id = (payload.get("person_id") or "").strip()
    full_name = (payload.get("full_name") or "").strip()
    dob       = (payload.get("dob") or "").strip() or None

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
    # Heuristic: eligible if there is a numeric total_bond and status is not released
    if amount_num is not None:
        status = (c.get("status") or "").lower()
        eligible = not ("release" in status or "no bond" in (c.get("total_bond") or "").lower())

    return {
        "found": True,
        "has_custody": True,
        "status": c.get("status"),
        "total_bond": c.get("total_bond"),
        "amount_numeric": amount_num,
        "eligible": eligible
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