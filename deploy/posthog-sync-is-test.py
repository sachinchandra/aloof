"""Backfill: set is_test on the PostHog person of every account (true for is_test=1, false otherwise with --all).
Usage on prod: cd /workspace/paas && set -a && . deploy/golive.env && set +a && PYTHONPATH=. .venv/bin/python deploy/posthog-sync-is-test.py [--all]"""
import sys
from paas.db import q
from paas import analytics, www
if not analytics.enabled(): sys.exit("PostHog not configured")
rows = q("select id from accounts" + ("" if "--all" in sys.argv else " where is_test=1") + " order by id")
www.sync_test_flags([r["id"] for r in rows]); analytics.flush(30)
print(f"synced {len(rows)} accounts; ids: {','.join(str(r['id']) for r in rows)}")
