from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional
import razorpay, hmac, hashlib
from app.config import get_supabase, settings
from app.dependencies import get_current_user
from app.routers.auth import _get_balance

router = APIRouter()

class CreateOrderRequest(BaseModel):
    package_id: str
    currency: str = "INR"

class VerifyPaymentRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str

@router.get("/balance")
async def get_balance(user=Depends(get_current_user)):
    db = get_supabase()
    return {"credits": _get_balance(db, user.id), "user_id": user.id}

@router.get("/packages")
async def get_packages():
    return {"packages": settings.CREDIT_PACKAGES}

@router.post("/create-payment-link")
async def create_payment_link(req: CreateOrderRequest, user=Depends(get_current_user)):
    """
    Creates a Razorpay Payment Link for the selected package.
    Returns a hosted payment URL — no JS SDK needed on the client.
    User pays on Razorpay's own page, webhook fires, credits land.
    """
    pkg = next((p for p in settings.CREDIT_PACKAGES if p["id"] == req.package_id), None)
    if not pkg:
        raise HTTPException(400, "Invalid package ID")

    amount = pkg["inr"] if req.currency == "INR" else pkg["usd"]
    if amount < 100:
        raise HTTPException(400, "Amount must be at least 100 paise")

    try:
        client = razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))

        # Create a Payment Link — hosted by Razorpay, no SDK needed
        link = client.payment_link.create({
            "amount": amount,
            "currency": req.currency,
            "accept_partial": False,
            "description": f"Fitly {pkg['credits']} analysis credits",
            "notes": {
                "user_id": user.id,
                "package_id": pkg["id"],
                "credits": str(pkg["credits"]),
            },
            "notify": {
                "sms": False,
                "email": False,
            },
            "reminder_enable": False,
            "callback_url": f"{settings.APP_URL}/credits/payment-callback",
            "callback_method": "get",
        })
    except Exception as e:
        raise HTTPException(500, f"Could not create payment link: {str(e)}")

    # Log as pending
    db = get_supabase()
    db.table("credit_orders").insert({
        "user_id": user.id,
        "razorpay_order_id": link["id"],  # payment link ID
        "package_id": pkg["id"],
        "credits": pkg["credits"],
        "amount": amount,
        "currency": req.currency,
        "status": "pending",
        "meta": {"type": "payment_link", "short_url": link.get("short_url", "")},
    }).execute()

    return {
        "ok": True,
        "payment_url": link["short_url"],  # e.g. https://rzp.io/l/abc123
        "payment_link_id": link["id"],
        "credits": pkg["credits"],
        "amount": amount,
        "package_label": pkg["label"],
    }

@router.get("/payment-callback")
async def payment_callback(
    razorpay_payment_id: str = "",
    razorpay_payment_link_id: str = "",
    razorpay_payment_link_reference_id: str = "",
    razorpay_payment_link_status: str = "",
    razorpay_signature: str = "",
):
    """
    Razorpay redirects here after payment.
    Verifies signature and adds credits.
    Returns a simple HTML page the user sees briefly before closing.
    """
    from fastapi.responses import HTMLResponse

    if razorpay_payment_link_status != "paid":
        return HTMLResponse(content=_result_page(False, "Payment was not completed."))

    # Verify signature: HMAC(link_id + "|" + ref_id + "|" + payment_id, secret)
    msg = f"{razorpay_payment_link_id}|{razorpay_payment_link_reference_id}|{razorpay_payment_id}".encode()
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        msg,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, razorpay_signature):
        return HTMLResponse(content=_result_page(False, "Payment signature invalid."))

    db = get_supabase()

    # Find the pending order by payment link ID
    order_res = db.table("credit_orders") \
        .select("*") \
        .eq("razorpay_order_id", razorpay_payment_link_id) \
        .eq("status", "pending") \
        .maybe_single().execute()

    if not order_res.data:
        # Already processed
        return HTMLResponse(content=_result_page(True, "Credits already added!", 0))

    order = order_res.data
    credits = order["credits"]
    user_id = order["user_id"]

    # Add credits
    db.table("credit_ledger").insert({
        "user_id": user_id,
        "delta": credits,
        "reason": "purchase",
        "meta": {
            "package_id": order["package_id"],
            "razorpay_payment_link_id": razorpay_payment_link_id,
            "razorpay_payment_id": razorpay_payment_id,
        }
    }).execute()

    # Mark paid
    db.table("credit_orders") \
        .update({"status": "paid", "razorpay_payment_id": razorpay_payment_id}) \
        .eq("razorpay_order_id", razorpay_payment_link_id).execute()

    return HTMLResponse(content=_result_page(True, f"{credits} credits added to your account!", credits))


def _result_page(success: bool, message: str, credits: int = 0) -> str:
    color = "#1e8e3e" if success else "#d93025"
    icon = "✓" if success else "✕"
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Fitly — Payment {"Complete" if success else "Failed"}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Google Sans', system-ui, sans-serif;
      background: linear-gradient(135deg, #e8f0fe, #f8f9fa);
      min-height: 100vh; display: flex; align-items: center; justify-content: center;
    }}
    .card {{
      background: #fff; border-radius: 20px; padding: 40px 32px;
      box-shadow: 0 8px 32px rgba(0,0,0,.12);
      max-width: 360px; width: 90%; text-align: center;
    }}
    .icon {{
      width: 64px; height: 64px; border-radius: 50%;
      background: {"#e6f4ea" if success else "#fce8e6"};
      color: {color}; font-size: 28px; font-weight: 700;
      display: flex; align-items: center; justify-content: center;
      margin: 0 auto 16px;
    }}
    h2 {{ font-size: 20px; color: #202124; margin-bottom: 8px; }}
    p  {{ font-size: 14px; color: #5f6368; line-height: 1.6; margin-bottom: 20px; }}
    .close-btn {{
      background: #1a73e8; color: #fff; border: none; border-radius: 20px;
      font-size: 14px; font-weight: 600; padding: 12px 28px; cursor: pointer;
      font-family: inherit;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="icon">{icon}</div>
    <h2>{"Payment Complete!" if success else "Payment Failed"}</h2>
    <p>{message}{"<br>Return to LinkedIn to continue." if success else ""}</p>
    <button class="close-btn" onclick="window.close()">
      {"Back to Fitly" if success else "Try Again"}
    </button>
  </div>
  <script>
    // Auto-close after 4 seconds on success
    {"setTimeout(() => window.close(), 4000);" if success else ""}
  </script>
</body>
</html>"""


@router.post("/order")
async def create_order(req: CreateOrderRequest, user=Depends(get_current_user)):
    """Legacy endpoint — redirect to payment link approach."""
    return await create_payment_link(req, user)


@router.post("/verify-payment")
async def verify_payment(req: VerifyPaymentRequest, user=Depends(get_current_user)):
    """Legacy standard checkout verification — kept for compatibility."""
    db = get_supabase()
    msg = f"{req.razorpay_order_id}|{req.razorpay_payment_id}".encode()
    expected = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        msg, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, req.razorpay_signature):
        raise HTTPException(400, "Signature mismatch")
    order_res = db.table("credit_orders").select("*") \
        .eq("razorpay_order_id", req.razorpay_order_id) \
        .eq("user_id", user.id).eq("status", "pending").maybe_single().execute()
    if not order_res.data:
        return {"ok": True, "credits_added": 0, "credits_balance": _get_balance(db, user.id), "already_processed": True}
    order = order_res.data
    db.table("credit_ledger").insert({
        "user_id": user.id, "delta": order["credits"], "reason": "purchase",
        "meta": {"razorpay_order_id": req.razorpay_order_id, "razorpay_payment_id": req.razorpay_payment_id}
    }).execute()
    db.table("credit_orders").update({"status": "paid", "razorpay_payment_id": req.razorpay_payment_id}) \
        .eq("razorpay_order_id", req.razorpay_order_id).execute()
    return {"ok": True, "credits_added": order["credits"], "credits_balance": _get_balance(db, user.id)}

@router.get("/history")
async def get_history(user=Depends(get_current_user)):
    db = get_supabase()
    res = db.table("credit_ledger").select("*").eq("user_id", user.id) \
        .order("created_at", desc=True).limit(50).execute()
    return {"balance": _get_balance(db, user.id), "transactions": res.data or []}
