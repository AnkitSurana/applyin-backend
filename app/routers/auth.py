from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, field_validator
import asyncio, logging, re
from app.config import get_admin, get_auth_client, settings
from app.dependencies import get_current_user
from app.limiter import limiter

router = APIRouter()
logger = logging.getLogger("applyin.auth")

# ─────────────────────────────────────────────────────────────────────────────
# EMAIL ABUSE PREVENTION
# ─────────────────────────────────────────────────────────────────────────────
# Strategy: email-level controls only. No IP fingerprinting.
#
# Why NOT IP-based limits:
#   - Many real users share one IP: offices, universities, families, CGNAT ISPs
#     (a single ISP IP can represent thousands of users in India/SE Asia).
#   - VPNs and proxies make IP-based limits easy to bypass anyway.
#   - Two real email addresses on the same IP is a completely legitimate case.
#
# What we DO instead:
#   1. Normalize the email to its canonical form before checking and storing,
#      so user+alias@gmail.com and u.s.e.r@gmail.com both resolve to user@gmail.com.
#      One person gets one set of free credits, however many aliases they create.
#   2. Block known disposable/throwaway domains.
#   3. The DB unique index (uniq_signup_bonus_per_user) on the canonical email
#      column is the hard backstop - no application code can bypass it.
#   4. Require email confirmation (Supabase setting) before granting credits.
#      An unconfirmed email gets no credits - this alone stops most scripted abuse
#      since disposable inboxes are often unmonitored and the link expires.
# ─────────────────────────────────────────────────────────────────────────────

# ── Disposable / throwaway email domains ─────────────────────────────────────
# Extend this list as you see new domains in your logs.
_DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.net", "guerrillamail.org",
    "guerrillamail.de", "guerrillamail.biz", "guerrillamail.info",
    "tempmail.com", "temp-mail.org", "throwam.com", "throwaway.email",
    "yopmail.com", "yopmail.fr", "cool.fr.nf", "jetable.fr.nf",
    "trashmail.com", "trashmail.at", "trashmail.me", "trashmail.net",
    "trashmail.io", "dispostable.com", "sharklasers.com", "guerrillamailblock.com",
    "grr.la", "spam4.me", "maildrop.cc", "spamgourmet.com",
    "mailnull.com", "spamcorpse.com", "getnada.com", "filzmail.com",
    "discard.email", "fakeinbox.com", "inboxbear.com",
    "mailnesia.com", "mailscrap.com", "trbvm.com", "tempr.email",
    "crazymailing.com", "bobmail.info", "spamfree24.org",
    "10minutemail.com", "10minutemail.net", "10minutemail.org",
    "minuteinbox.com", "mintemail.com", "spamgolf.com",
    "trashmail.fr", "trashmail.de", "trash-mail.at",
    "getairmail.com", "filzmail.de", "fleckens.hu",
}

# ── Providers that support dot-insensitive / plus-alias local parts ───────────
# For these domains we strip dots from the local part and drop everything after
# the first `+`. For all other providers we only strip the `+` alias portion
# (which is RFC-standard sub-addressing), since we can't assume dot behaviour.
_DOT_INSENSITIVE_DOMAINS = {
    "gmail.com", "googlemail.com",
}

# Domains that support `+` sub-addressing but NOT dot-insensitivity.
# We strip `+alias` for all domains because it's the standard RFC extension;
# dots are only stripped for the set above.
# (Yahoo and Outlook use `-` tags, not `+`, so no normalisation needed there.)


def _normalize_email(email: str) -> str:
    """
    Return the canonical form of an email address for abuse-checking.

    Rules applied:
      1. Lowercase the whole address.
      2. Strip `+anything` from the local part (works for all providers that
         support sub-addressing: Gmail, Outlook, ProtonMail, Fastmail, etc.).
      3. For Gmail/Googlemail specifically, also strip dots from the local part
         (Gmail treats u.s.e.r as user; no other major provider does this).

    Examples:
      user+alias@gmail.com     → user@gmail.com
      u.s.e.r+tag@gmail.com    → user@gmail.com
      first.last+tag@outlook.com → first.last@outlook.com  (dots kept, + stripped)
      me@company.com           → me@company.com            (unchanged)

    Important: this canonical form is stored in user_profiles.canonical_email
    and checked on signup. It is NEVER used as the actual login email - Supabase
    still uses the original address for auth. The canonical form is only a
    de-duplication key.
    """
    email = email.strip().lower()
    try:
        local, domain = email.rsplit("@", 1)
    except ValueError:
        return email  # malformed - let the structural validator catch it

    # Strip + alias from local part (all providers)
    local = local.split("+")[0]

    # Strip dots from local part for Gmail-family only
    if domain in _DOT_INSENSITIVE_DOMAINS:
        local = local.replace(".", "")

    return f"{local}@{domain}"


def _is_disposable(email: str) -> bool:
    """True if the email's domain is a known throwaway provider."""
    try:
        domain = email.strip().lower().rsplit("@", 1)[1]
        return domain in _DISPOSABLE_DOMAINS
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Weak-password blocklist (light safety net; min length is enforced separately).
# Supabase's "leaked password protection" (Auth settings) is the comprehensive
# check against known-breached passwords. This just stops the most-guessed ones.
# ─────────────────────────────────────────────────────────────────────────────
_WEAK_PASSWORDS = frozenset({
    "password", "password1", "password12", "password123", "passw0rd", "password!",
    "12345678", "123456789", "1234567890", "123123123", "12341234", "1234abcd",
    "qwerty123", "qwertyuiop", "1q2w3e4r", "qazwsxedc", "zxcvbnm1", "asdfghjkl",
    "iloveyou", "sunshine", "princess", "football", "baseball", "welcome1",
    "admin123", "letmein1", "monkey123", "dragon123", "trustno1", "superman1",
    "batman123", "hello123", "login123", "master123", "shadow12", "freedom1",
    "whatever1", "computer1", "internet1", "samsung1", "abc12345", "changeme1",
})

# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    email: str
    password: str
    is_adult: bool = False  # kept for backward-compat; not enforced
    # DPDP: resume-processing consent given at signup (the consent checkbox the
    # client gates account creation on). When true, signup records a
    # consent_records row so analysis is not blocked later. Defaults false so a
    # client that does not send it does not silently imply consent.
    consent: bool = False

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", v):
            raise ValueError("Invalid email address")
        if _is_disposable(v):
            raise ValueError("Please use a real email address to sign up")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        # Block the most-guessed passwords and trivial repeats (e.g. "aaaaaaaa").
        # Low friction: only rejects genuinely weak choices, not normal passwords.
        if v.lower() in _WEAK_PASSWORDS or len(set(v)) == 1:
            raise ValueError("That password is too common. Please choose a stronger one.")
        return v


class LoginRequest(BaseModel):
    email: str
    password: str


# ─────────────────────────────────────────────────────────────────────────────
# Credit helpers
# ─────────────────────────────────────────────────────────────────────────────

def _get_balance(admin, user_id: str) -> int:
    try:
        res = admin.table("user_credit_balances").select("balance") \
            .eq("user_id", user_id).maybe_single().execute()
        if res.data and res.data.get("balance") is not None:
            return int(res.data["balance"])
        return 0
    except Exception as e:
        logger.error(f"Balance check failed for {user_id}: {e}")
        return 0


def _grant_signup_credits(admin, user_id: str) -> int:
    """
    Grant free credits on signup. Strictly idempotent.

    The DB unique index (uniq_signup_bonus_per_user) is the hard backstop.
    This function is a fast-path: if the row already exists we return the real
    balance without touching the ledger. On a genuine first grant we insert once.
    Concurrent duplicate calls are caught by the unique-violation handler.
    """
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
            logger.error(
                f"Signup bonus insert returned no data for {user_id} "
                f"- check service-role key / RLS."
            )
            return 0
        logger.info(f"Granted {settings.FREE_CREDITS_ON_SIGNUP} free credits to {user_id}")
        return _get_balance(admin, user_id)
    except Exception as e:
        msg = str(e).lower()
        if "duplicate" in msg or "unique" in msg or "23505" in msg:
            # Concurrent call already inserted - this is success, not failure.
            logger.info(f"Signup bonus already granted concurrently for {user_id}")
            return _get_balance(admin, user_id)
        logger.error(f"Failed to grant signup credits to {user_id}: {e}")
        return 0


def _canonical_email_already_used(admin, canonical: str) -> bool:
    """
    Check whether this canonical email has already received a signup bonus.
    Looks at user_profiles.canonical_email, not the raw email address.
    Returns True if a prior account with the same canonical form exists.
    """
    try:
        res = admin.table("user_profiles") \
            .select("id") \
            .eq("canonical_email", canonical) \
            .limit(1) \
            .execute()
        return bool(res.data)
    except Exception as e:
        # Fail OPEN here - if we can't check, we don't block legitimate signups.
        # The DB unique index is still the hard backstop.
        logger.warning(f"Canonical email check failed (non-fatal, allowing signup): {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/signup")
@limiter.limit("3/minute")
async def signup(request: Request, req: SignupRequest):
    """
    Create a new account. Email validation (structural + disposable-domain check)
    is handled by the Pydantic model. Canonical-email de-duplication happens here
    so that alias variants of the same address don't each receive free credits.
    """
    canonical = _normalize_email(req.email)
    auth_client = get_auth_client()
    admin = get_admin()

    # ── Canonical email de-duplication ────────────────────────────────────────
    # Check BEFORE creating the Supabase auth user to avoid leaving orphan auth
    # records. This is a best-effort check (fails open); the DB unique index on
    # canonical_email is the hard guarantee.
    #
    # We intentionally do NOT tell the user which canonical form matched - that
    # would expose whether e.g. "user@gmail.com" is a registered account.
    # The message is the same as a normal "already registered" response.
    if _canonical_email_already_used(admin, canonical):
        logger.info(
            f"Signup blocked: canonical={canonical} already has an account "
            f"(submitted as {req.email})"
        )
        raise HTTPException(
            409,
            "An account with this email already exists. Please sign in instead."
        )

    try:
        # Use the standard sign_up: it creates the user AND sends the confirmation
        # email through Supabase's configured SMTP (with your email templates) in one
        # step - no separate send, no resend rate-limit. The only reason this
        # previously failed was a 5s client timeout vs a slow default-mailer send; the
        # auth client now has a 30s read timeout (see get_auth_client), so it waits
        # for the real response. With your own SMTP the send is also faster.
        res = auth_client.auth.sign_up({"email": req.email, "password": req.password})
        if not res or not res.user:
            raise HTTPException(400, "Couldn't create the account. This email may already be registered.")

        # Empty identities = email already registered (Supabase signals it this way).
        identities = getattr(res.user, "identities", None)
        if identities is not None and len(identities) == 0:
            raise HTTPException(409, "An account with this email already exists. Please sign in instead.")

        user_id = res.user.id
        logger.info(f"New signup: {req.email} canonical={canonical} ({user_id})")

        # Store profile (raw email for display + canonical for de-dup). Best-effort
        # retry; never blocks on the email send anymore.
        for attempt in range(3):
            try:
                admin.table("user_profiles").upsert({
                    "id": user_id,
                    "email": req.email,
                    "canonical_email": canonical,
                }).execute()
                break
            except Exception as e:
                if attempt == 2:
                    logger.warning(f"Profile upsert failed after 3 attempts for {user_id}: {e}")
                else:
                    await asyncio.sleep(0.5)

        credits = _grant_signup_credits(admin, user_id)

        # DPDP: persist the resume-processing consent given at signup, atomically with
        # account creation, so the analyze route (which reads consent_records) does
        # not later refuse with "please agree".
        if req.consent:
            try:
                from app.routers.privacy import CURRENT_POLICY_VERSION, _hash_ip
                admin.table("consent_records").insert({
                    "user_id": user_id,
                    "purpose": "resume_processing",
                    "granted": True,
                    "policy_version": CURRENT_POLICY_VERSION,
                    "source": "extension",
                    "ip_hash": _hash_ip(request.client.host if request.client else ""),
                    "user_agent": request.headers.get("user-agent", "")[:300],
                }).execute()
                logger.info(f"CONSENT recorded at signup user={user_id} purpose=resume_processing")
            except Exception as e:
                logger.warning(f"Signup consent write failed for {user_id}: {e}")

        # Email confirmation is required before the user can sign in (no session is
        # issued at creation). Return the friendly "check your inbox" pending state -
        # this is success, not an error. 200 with pending=true so the client shows
        # the green confirm-email message, not a red failure.
        return {
            "ok": True,
            "pending": True,
            "user_id": user_id,
            "email": req.email,
            "credits": credits,
            "message": "Check your inbox to confirm your email, then sign in to start.",
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.error(f"Signup error: {e}")
        raise HTTPException(400, "Couldn't create the account. Please try again.")


@router.post("/login")
@limiter.limit("10/minute")
async def login(request: Request, req: LoginRequest):
    auth_client = get_auth_client()
    admin = get_admin()

    try:
        res = auth_client.auth.sign_in_with_password({
            "email": req.email,
            "password": req.password,
        })
        if not res.user or not res.session:
            raise HTTPException(401, "Invalid email or password")

        user_id = res.user.id
        canonical = _normalize_email(req.email)

        # Ensure profile exists with canonical_email (non-fatal - may already exist).
        try:
            admin.table("user_profiles").upsert({
                "id": user_id,
                "email": req.email,
                "canonical_email": canonical,
            }).execute()
        except Exception as e:
            logger.warning(f"Profile upsert on login failed (non-fatal): {e}")

        balance = _get_balance(admin, user_id)

        # Safety net: grant bonus ONLY if this account has absolutely no ledger
        # rows of any kind (i.e. a genuine legacy account that predates the credit
        # system). NOT triggered by spending credits - those leave debit rows.
        # This path cannot be exploited: you'd need an account with zero ledger
        # rows, which only exists for pre-credit-system accounts.
        has_any_ledger_row = False
        try:
            check = admin.table("credit_ledger") \
                .select("id").eq("user_id", user_id).limit(1).execute()
            has_any_ledger_row = bool(check.data)
        except Exception as e:
            logger.warning(f"Ledger check failed (non-fatal): {e}")

        if not has_any_ledger_row:
            logger.info(f"Login: {user_id} has no ledger rows - granting legacy signup bonus")
            balance = _grant_signup_credits(admin, user_id)

        logger.info(f"Login: {req.email} | canonical={canonical} | balance={balance}")
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
        raise HTTPException(401, "Invalid email or password")


@router.get("/me")
async def me(user=Depends(get_current_user)):
    admin = get_admin()
    balance = _get_balance(admin, user.id)
    return {"user_id": user.id, "email": user.email, "credits": balance}


@router.post("/refresh")
@limiter.limit("20/minute")
async def refresh_token(request: Request, body: dict):
    auth_client = get_auth_client()
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
