"""Support doc bugs 38 (promo codes), 40 (no-scope warning is amber), 41 (access labels for tokens and connected apps)."""
import os, re, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from fastapi.testclient import TestClient
from paas import api
c = TestClient(api.app)

def test_promo_note_matches_checkout_code():
    src = open(os.path.join(ROOT, "paas/stripe_billing.py")).read()
    sub = [l for l in src.splitlines() if '"mode": "subscription"' in l][0]; top = [l for l in src.splitlines() if '"mode": "payment"' in l][0]
    assert '"allow_promotion_codes": True' in sub and '"payment_method_collection": "always"' in sub and "allow_promotion_codes" not in top
    for path in ("/docs/billing", "/pricing"):
        h = c.get(path).text; low = h.lower()
        assert "promo code" in low and "Hobby only" in h and "top-ups" in low and "card" in low, path
        # bug 46: exact FIRST1/EARLY3 terms from docs/billing-promos.md
        for t in ("FIRST1", "EARLY3", "first month of Hobby free", "first three months of Hobby free", "50 redemptions each", "31 December 2026 at 23:59 UTC+4", "first-time customers only"): assert t in h, (path, t)
    b = c.get("/docs/billing").text; note = re.search(r"<p id=promo>(.*?)</p>", b, re.S).group(1).lower()
    assert "tax" not in note and "pay from balance" in note
    assert 'href="/docs/billing#promo"' in c.get("/pricing").text

def test_no_scope_warning_is_amber_not_red():
    sys.path.insert(0, os.path.join(ROOT, "mcp_server")); import oauth
    assert 'class="badge amber"' in oauth.NO_SCOPE_WARN and "full (admin) access" in oauth.NO_SCOPE_WARN
    assert ".amber{" in oauth.CSS and 'class="badge warn"' in oauth.UNVERIFIED_BADGE

def test_connected_apps_report_access_from_real_scopes(monkeypatch):
    from paas import oauth_apps
    toks = {"a": [["paas:read"]], "w": [["paas:read"], ["paas:write"]], "n": [None], "p": [["paas"]], "x": [["paas:admin"]]}
    rows = [("access", f"{cid}{i}", {"client_id": cid, "scopes": s, "expires_at": 100}) for cid, l in toks.items() for i, s in enumerate(l)]
    monkeypatch.setattr(oauth_apps, "_tokens", lambda *a, **k: iter(rows))
    monkeypatch.setattr(oauth_apps, "_client", lambda cid: {"client_name": cid})
    got = {x["client_id"]: (x["access"], x["access_label"]) for x in oauth_apps.list_apps("acc")}
    assert got == {"a": ("read", "Read only"), "w": ("write", "Read and write"), "n": ("admin", "Full access"),
                   "p": ("admin", "Full access"), "x": ("admin", "Full access")}
    sys.path.insert(0, os.path.join(ROOT, "mcp_server")); import oauth
    assert oauth_apps.SCOPE_LEVELS == oauth.LEVELS

def test_dashboard_shows_access_labels():
    d = open(os.path.join(ROOT, "dashboard/index.html")).read()
    assert "<th>Access</th>" in d and "oaccess(x)" in d and "x.access_label" in d
    assert "type:'Full access'" in d and "Can deploy and delete" in d and "Can deploy, no delete" in d
