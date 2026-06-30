"""Payment idempotency: the atomic-claim guard prevents double-crediting.

Simulates the Supabase query builder with a fake that only lets ONE conditional
'pending -> paid' update succeed, exactly like Postgres serialises it."""
import app.routers.webhook as wh


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db, self.table = db, table
        self.op = None
        self.payload = None
        self.filters = {}

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def select(self, *a, **k):
        self.op = "select"
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def execute(self):
        return self.db._execute(self)


class FakeDB:
    """Models credit_orders (one pending order) + credit_ledger (records inserts)."""
    def __init__(self, order):
        self.order = order
        self.ledger_inserts = []
        self._claimed = False

    def table(self, name):
        return _Query(self, name)

    def _execute(self, q):
        if q.table == "credit_orders" and q.op == "update":
            claiming = (q.payload.get("status") == "paid"
                        and q.filters.get("status") == "pending")
            if claiming and not self._claimed:
                self._claimed = True
                return _Result([{**self.order, "status": "paid"}])
            return _Result([])  # already claimed (or a revert) -> no row
        if q.table == "credit_ledger" and q.op == "insert":
            self.ledger_inserts.append(q.payload)
            return _Result([{"id": len(self.ledger_inserts)}])
        return _Result([])


ORDER = {"user_id": "u1", "credits": 60, "package_id": "pro"}


def test_single_payment_grants_once():
    db = FakeDB(dict(ORDER))
    wh._process_payment(db, "order_123", "pay_123")
    assert len(db.ledger_inserts) == 1
    assert db.ledger_inserts[0]["delta"] == 60
    assert db.ledger_inserts[0]["reason"] == "purchase"


def test_webhook_and_callback_double_fire_grants_once():
    # Razorpay fires BOTH the webhook and the redirect callback for one payment.
    db = FakeDB(dict(ORDER))
    wh._process_payment(db, "order_123", "pay_123")  # e.g. webhook
    wh._process_payment(db, "order_123", "pay_123")  # e.g. callback / retry
    assert len(db.ledger_inserts) == 1  # never double-credited


def test_missing_order_id_is_noop():
    db = FakeDB(dict(ORDER))
    wh._process_payment(db, "", "pay_123")
    assert len(db.ledger_inserts) == 0


def test_payment_meta_carries_ids():
    db = FakeDB(dict(ORDER))
    wh._process_payment(db, "order_xyz", "pay_xyz")
    meta = db.ledger_inserts[0]["meta"]
    assert meta["razorpay_order_id"] == "order_xyz"
    assert meta["razorpay_payment_id"] == "pay_xyz"
