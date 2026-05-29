from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import time, logging
from app.config import get_supabase, settings
from app.dependencies import get_current_user

router = APIRouter()
logger = logging.getLogger("fitly.auth")

class SignupRequest(BaseModel):
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

def _get_balance(db, user_id: str) -> int:
    try:
        res = db.table("credit_ledger").select("delta").eq("user_id", user_id).execute()
        return sum(row["delta"] for row in (res.data or []))
    except Exception as e:
        logger.error(f"Balance check failed for {user_id}: {e}")
        return 0

def _grant_signup_credits(db, user_id: str, email: str) -> int:
    """Grant free credits on signup. Idempotent — safe to call multiple times."""
    try:
        existing = db.table("credit_ledger") \
            .select("id") \
            .eq("user_id", user_id) \
            .eq("reason", "signup_bonus") \
            .execute()
        if existing.data:
            logger.info(f"Signup bonus already granted to {user_id}")
            return _get_balance(db, user_id)

        result = db.table("credit_ledger").insert({
            "user_id": user_id,
            "delta": settings.FREE_CREDITS_ON_SIGNUP,
            "reason": "signup_bonus",
        }).execute()
        # Verify the insert actually landed (RLS can silently block)
        if not result.data:
            logger.error(f"Signup bonus insert returned no data for {user_id} — likely RLS blocking. Check SUPABASE_SERVICE_KEY is service_role.")
            return 0
        logger.info(f"Granted {settings.FREE_CREDITS_ON_SIGNUP} free credits to {user_id}")
        return _get_balance(db, user_id)
    except Exception as e:
        logger.error(f"Failed to grant signup credits to {user_id}: {e}")
        return 0  # be honest — don't claim credits that weren't stored

@router.post("/signup")
async def signup(req: SignupRequest):
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    db = get_supabase()
    try:
        res = db.auth.sign_up({"email": req.email, "password": req.password})
        if not res.user:
            raise HTTPException(400, "Signup failed — email may already be registered")

        user_id = res.user.id
        logger.info(f"New signup: {req.email} ({user_id})")

        # Create profile (retry up to 3x — Supabase auth write can be async)
        for attempt in range(3):
            try:
                db.table("user_profiles").upsert({
                    "id": user_id,
                    "email": req.email
                }).execute()
                break
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"Profile upsert failed after 3 attempts for {user_id}: {e}")
                else:
                    time.sleep(0.5)

        # Grant free credits
        credits = _grant_signup_credits(db, user_id, req.email)

        return {
            "ok": True,
            "user_id": user_id,
            "email": req.email,
            "access_token": res.session.access_token if res.session else None,
            "refresh_token": res.session.refresh_token if res.session else None,
            "credits": credits,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Signup error: {e}")
        raise HTTPException(400, str(e))

@router.post("/login")
async def login(req: LoginRequest):
    db = get_supabase()
    try:
        res = db.auth.sign_in_with_password({"email": req.email, "password": req.password})
        if not res.user or not res.session:
            raise HTTPException(401, "Invalid email or password")

        user_id = res.user.id
        balance = _get_balance(db, user_id)

        # Ensure profile exists (create if missing)
        try:
            db.table("user_profiles").upsert({
                "id": user_id,
                "email": req.email
            }).execute()
        except Exception as e:
            logger.warning(f"Profile upsert on login failed (non-fatal): {e}")

        # Auto-grant signup bonus if somehow not credited (edge case recovery)
        if balance == 0:
            existing = db.table("credit_ledger") \
                .select("id").eq("user_id", user_id) \
                .eq("reason", "signup_bonus").execute()
            if not existing.data:
                logger.warning(f"User {user_id} has 0 credits and no signup bonus — granting now")
                _grant_signup_credits(db, user_id, req.email)
                balance = settings.FREE_CREDITS_ON_SIGNUP

        logger.info(f"Login: {req.email} | balance={balance}")
        return {
            "ok": True,
            "user_id": user_id,
            "email": req.email,
            "access_token": res.session.access_token,
            "refresh_token": res.session.refresh_token,
            "credits": balance,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Login error: {e}")
        raise HTTPException(401, str(e))

@router.get("/me")
async def me(user=Depends(get_current_user)):
    db = get_supabase()
    balance = _get_balance(db, user.id)

    # Recover signup bonus if missing (handles failed signup inserts)
    if balance == 0:
        existing = db.table("credit_ledger") \
            .select("id").eq("user_id", user.id) \
            .eq("reason", "signup_bonus").execute()
        if not existing.data:
            logger.warning(f"/me: user {user.id} has no signup bonus — granting now")
            _grant_signup_credits(db, user.id, user.email)
            balance = _get_balance(db, user.id)

    return {"user_id": user.id, "email": user.email, "credits": balance}

@router.post("/refresh")
async def refresh_token(body: dict):
    db = get_supabase()
    try:
        rt = body.get("refresh_token", "")
        res = db.auth.refresh_session(rt)
        if not res.session:
            raise HTTPException(401, "Invalid refresh token")
        return {
            "access_token": res.session.access_token,
            "refresh_token": res.session.refresh_token,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(401, str(e))
