from fastapi import APIRouter, HTTPException, Request, Header
import hmac, hashlib, json, logging
from app.config import get_supabase, settings

router = APIRouter()
logger = logging.getLogger("applyin.webhook")

@router.post("/razorpay")
async def razorpay_webhook(request: Request, x_razorpay_signature: str = Header(...)):
    body = await request.body()

    # Verify signature
    expected = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode(),
        body,
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, x_razorpay_signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    payload = json.loads(body)
    event = payload.get("event")

    db = get_supabase()

    if event == "payment.captured":
        payment = payload["payload"]["payment"]["entity"]
        order_id = payment.get("order_id")
        payment_id = payment.get("id")
        _process_payment(db, order_id, payment_id)

    elif event == "payment_link.paid":
        # Payment Link webhook
        payment_link = payload["payload"]["payment_link"]["entity"]
        payment = payload["payload"]["payment"]["entity"]
        link_id = payment_link.get("id")
        payment_id = payment.get("id")
        _process_payment(db, link_id, payment_id)

    elif event in ("payment.failed", "payment_link.cancelled"):
        order_id = (
            payload["payload"].get("payment", {}).get("entity", {}).get("order_id")
            or payload["payload"].get("payment_link", {}).get("entity", {}).get("id")
        )
        if order_id:
            db.table("credit_orders").update({"status": "failed"}) \
                .eq("razorpay_order_id", order_id).execute()

    return {"status": "ok"}


def _process_payment(db, order_id: str, payment_id: str):
    """Add credits for a completed payment. Idempotent and race-safe.

    The status flip pending -> paid is a single conditional UPDATE, which Postgres
    serialises. Razorpay fires BOTH the webhook and the redirect callback for the
    same payment, so without this they could both read 'pending' and credit twice.
    Here, only the call that actually flips the row grants credits; the other sees
    an empty result and does nothing. A payment can never be credited twice.
    """
    if not order_id:
        return

    claim = db.table("credit_orders") \
        .update({"status": "paid", "razorpay_payment_id": payment_id}) \
        .eq("razorpay_order_id", order_id) \
        .eq("status", "pending") \
        .execute()

    if not claim.data:
        return  # already claimed by the callback or a webhook retry - no double grant

    order = claim.data[0]
    try:
        db.table("credit_ledger").insert({
            "user_id": order["user_id"],
            "delta": order["credits"],
            "reason": "purchase",
            "meta": {
                "package_id": order["package_id"],
                "razorpay_order_id": order_id,
                "razorpay_payment_id": payment_id,
            }
        }).execute()
        logger.info(f"Webhook credited +{order['credits']} to user {order['user_id']}")
    except Exception as e:
        # Grant failed after claiming the order; revert so a retry can re-process
        # (avoids a 'paid but not credited' order).
        logger.error(f"Credit grant failed after claim for {order_id}; reverting: {e}")
        db.table("credit_orders").update({"status": "pending"}) \
            .eq("razorpay_order_id", order_id).execute()
        raise
