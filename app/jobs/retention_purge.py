"""
Retention purge - deletes data past its retention window.

Reads retain_days from the retention_policy table (the single source of truth,
defined in the database schema) and deletes rows older than that in each category.

Run on a schedule. Two options:
  1. As a small cron job (Render Cron / GitHub Actions) calling this script.
  2. As pg_cron inside Supabase.

Usage:
    python -m app.jobs.retention_purge          # live
    python -m app.jobs.retention_purge --dry    # report only, delete nothing

Honest note: this enforces the retention CLAIMS in the privacy policy. Until it
runs on a schedule, those claims are not yet true. Verify it on a copy first.
"""
import sys, logging
from datetime import datetime, timedelta, timezone
from app.config import get_admin

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("applyin.retention")

# Map each retention_policy category to the table + timestamp column to purge on.
# Only categories that represent deletable event/cache data are purged here.
# consent_records / dsr_requests / breach_log are retained for accountability and
# are intentionally NOT auto-deleted.
PURGEABLE = {
    "analysis_cache": ("analysis_cache", "created_at"),
    "usage_events":   ("usage_events",   "created_at"),
}


def run(dry: bool = False) -> dict:
    db = get_admin()
    out = {}
    try:
        policy = db.table("retention_policy").select("data_category,retain_days").execute().data or []
    except Exception as e:
        logger.error(f"Cannot read retention_policy: {e}")
        return {"error": str(e)}

    for row in policy:
        cat = row["data_category"]
        days = int(row["retain_days"])
        if cat not in PURGEABLE:
            continue
        table, col = PURGEABLE[cat]
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        try:
            if dry:
                got = db.table(table).select("id").lt(col, cutoff).execute()
                n = len(got.data or [])
                logger.info(f"[DRY] {table}: {n} rows older than {days}d would be deleted")
                out[table] = {"would_delete": n, "cutoff": cutoff}
            else:
                db.table(table).delete().lt(col, cutoff).execute()
                logger.info(f"{table}: purged rows older than {days}d (cutoff {cutoff})")
                out[table] = {"purged": True, "cutoff": cutoff, "retain_days": days}
        except Exception as e:
            logger.error(f"Purge failed for {table}: {e}")
            out[table] = {"error": str(e)}
    return out


if __name__ == "__main__":
    dry = "--dry" in sys.argv
    result = run(dry=dry)
    logger.info(f"Retention purge result: {result}")
