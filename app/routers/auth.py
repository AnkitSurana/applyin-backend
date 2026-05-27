from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel, EmailStr
import time
from app.config import get_supabase, settings
from app.dependencies import get_current_user

router = APIRouter()

class SignupRequest(BaseModel):
    email: EmailStr
    password: str

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

@router.post("/signup")
async def signup(req: SignupRequest):
    if len(req.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    db = get_supabase()
    try:
        res = db.auth.sign_up({"email": req.email, "password": req.password})
        if not res.user:
            raise HTTPException(400, "Signup failed — email may already be registered")

        user_id = res.user.id

        # Wait briefly for auth.users to fully commit before inserting profile
        # (Supabase auth write can be async — retry up to 3 times)
        for attempt in range(3):
            try:
                db.table("user_profiles").upsert({
                    "id": user_id,
                    "email": req.email
                }).execute()
                break
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(0.5)  # wait 500ms before retry

        # Grant free credits (only if not already granted — idempotent check)
        existing = db.table("credit_ledger") \
            .select("id") \
            .eq("user_id", user_id) \
            .eq("reason", "signup_bonus") \
            .execute()

        if not existing.data:
            db.table("credit_ledger").insert({
                "user_id": user_id,
                "delta": settings.FREE_CREDITS_ON_SIGNUP,
                "reason": "signup_bonus",
                "meta": {}
            }).execute()

        return {
            "ok": True,
            "user_id": user_id,
            "email": req.email,
            "access_token": res.session.access_token if res.session else None,
            "refresh_token": res.session.refresh_token if res.session else None,
            "credits": settings.FREE_CREDITS_ON_SIGNUP,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, str(e))

@router.post("/login")
async def login(req: LoginRequest):
    db = get_supabase()
    try:
        res = db.auth.sign_in_with_password({"email": req.email, "password": req.password})
        if not res.user or not res.session:
            raise HTTPException(401, "Invalid email or password")

        # Ensure profile exists (in case trigger didn't fire)
        db.table("user_profiles").upsert({
            "id": res.user.id,
            "email": res.user.email
        }).execute()

        balance = _get_balance(db, res.user.id)
        return {
            "ok": True,
            "user_id": res.user.id,
            "email": res.user.email,
            "access_token": res.session.access_token,
            "refresh_token": res.session.refresh_token,
            "credits": balance,
        }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Invalid email or password")

@router.get("/me")
async def get_me(user=Depends(get_current_user)):
    db = get_supabase()
    balance = _get_balance(db, user.id)
    return {"user_id": user.id, "email": user.email, "credits": balance}

@router.post("/refresh")
async def refresh(body: dict):
    db = get_supabase()
    try:
        res = db.auth.refresh_session(body.get("refresh_token", ""))
        return {
            "access_token": res.session.access_token,
            "refresh_token": res.session.refresh_token
        }
    except Exception:
        raise HTTPException(401, "Token refresh failed — please log in again")

def _get_balance(db, user_id: str) -> int:
    res = db.table("credit_ledger").select("delta").eq("user_id", user_id).execute()
    return max(0, sum(r["delta"] for r in (res.data or [])))
