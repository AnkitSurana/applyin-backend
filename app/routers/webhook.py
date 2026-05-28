from fastapi import APIRouter, HTTPException, Request, Header
import hmac, hashlib, json
from app.config import get_supabase, settings

router = APIRouter()

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
    """Add credits for a completed payment. Idempotent."""
    if not order_id:
        return

    order_res = db.table("credit_orders") \
        .select("*") \
        .eq("razorpay_order_id", order_id) \
        .eq("status", "pending") \
        .maybe_single().execute()

    if not order_res.data:
        return  # Already processed

    order = order_res.data

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

    db.table("credit_orders") \
        .update({"status": "paid", "razorpay_payment_id": payment_id}) \
        .eq("razorpay_order_id", order_id).execute()

    print(f"[Webhook] +{order['credits']} credits → user {order['user_id']}")
