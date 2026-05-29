from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import time, logging
from app.config import get_supabase, get_admin, settings
from app.dependencies import get_current_user

router = APIRouter()
logger = logging.getLogger("fitly.auth")

# IMPORTANT: never call .auth.sign_up / sign_in / get_user on a client that you
# then use for .table() writes. Those auth calls adopt the user's session and
# subsequent DB writes run as the authenticated user (subject to RLS), which
# silently fails credit_ledger inserts. All DB writes here use get_admin() —
# a fresh service-role client that never has .auth.* called on it.

class SignupRequest(BaseModel):
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

def _get_balance(admin, user_id: str) -> int:
    try:
        res = admin.table("credit_ledger").select("delta").eq("user_id", user_id).execute()
        return sum(row["delta"] for row in (res.data or []))
    except Exception as e:
        logger.error(f"Balance check failed for {user_id}: {e}")
        return 0

def _grant_signup_credits(admin, user_id: str) -> int:
    """Grant free credits on signup. Idempotent. Uses service-role admin client."""
    try:
        existing = admin.table("credit_ledger") \
            .select("id") \
            .eq("user_id", user_id) \
            .eq("reason", "signup_bonus") \
            .execute()
        if existing.data:
            logger.info(f"Signup bonus already present for {user_id}")
            return _get_balance(admin, user_id)

        result = admin.table("credit_ledger").insert({
            "user_id": user_id,
            "delta": settings.FREE_CREDITS_ON_SIGNUP,
            "reason": "signup_bonus",
        }).execute()
        if not result.data:
            logger.error(f"Signup bonus insert returned no data for {user_id} — check service-role key / RLS.")
            return 0
        logger.info(f"Granted {settings.FREE_CREDITS_ON_SIGNUP} free credits to {user_id}")
        return _get_balance(admin, user_id)
    except Exception as e:
        logger.error(f"Failed to grant signup credits to {user_id}: {e}")
        return 0

@router.post("/signup")
async def signup(req: SignupRequest):
    if len(req.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    auth_client = get_supabase()   # used ONLY for the auth call
    admin = get_admin()            # fresh service-role client for all DB writes

    try:
        res = auth_client.auth.sign_up({"email": req.email, "password": req.password})
        if not res.user:
            raise HTTPException(400, "Signup failed — email may already be registered")

        user_id = res.user.id
        logger.info(f"New signup: {req.email} ({user_id})")

        # Create profile via admin client (retry — auth.users write can lag)
        for attempt in range(3):
            try:
                admin.table("user_profiles").upsert({"id": user_id, "email": req.email}).execute()
                break
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"Profile upsert failed after 3 attempts for {user_id}: {e}")
                else:
                    time.sleep(0.5)

        credits = _grant_signup_credits(admin, user_id)

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
    auth_client = get_supabase()   # used ONLY for the auth call
    admin = get_admin()            # fresh service-role client for all DB writes

    try:
        res = auth_client.auth.sign_in_with_password({"email": req.email, "password": req.password})
        if not res.user or not res.session:
            raise HTTPException(401, "Invalid email or password")

        user_id = res.user.id

        # Ensure profile exists (admin client)
        try:
            admin.table("user_profiles").upsert({"id": user_id, "email": req.email}).execute()
        except Exception as e:
            logger.warning(f"Profile upsert on login failed (non-fatal): {e}")

        balance = _get_balance(admin, user_id)

        # If the user somehow has no signup bonus yet, grant it now (admin client).
        # This is a genuine safety net for accounts created before the fix — not a
        # workaround for a broken insert path.
        if balance == 0:
            existing = admin.table("credit_ledger") \
                .select("id").eq("user_id", user_id) \
                .eq("reason", "signup_bonus").execute()
            if not existing.data:
                logger.info(f"Login: {user_id} missing signup bonus — granting")
                balance = _grant_signup_credits(admin, user_id)

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
    admin = get_admin()
    balance = _get_balance(admin, user.id)
    return {"user_id": user.id, "email": user.email, "credits": balance}

@router.post("/refresh")
async def refresh_token(body: dict):
    auth_client = get_supabase()
    try:
        rt = body.get("refresh_token", "")
        res = auth_client.auth.refresh_session(rt)
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
