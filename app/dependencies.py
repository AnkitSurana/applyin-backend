from fastapi import HTTPException, Header
from app.config import get_supabase
import logging

logger = logging.getLogger("applyin.auth")

def get_current_user(authorization: str = Header(...)):
    """Validate Supabase JWT and return the user.

    Declared as a plain `def` (not `async`) on purpose: `db.auth.get_user()` is a
    blocking network call, and FastAPI runs sync dependencies in a threadpool, so
    this validation no longer blocks the event loop on every authenticated request.
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid authorization header")

    token = authorization.replace("Bearer ", "")
    try:
        db = get_supabase()
        user = db.auth.get_user(token)
        if not user or not user.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        return user.user
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Auth validation failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid or expired token")
