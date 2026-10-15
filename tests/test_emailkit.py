"""Customer emails: shared layout renders, carries the link and the real expiry, has no violet, no tracking pixel, and a text part."""
import re, pytest
from paas import emailkit, auth, billing

URL = "https://api.aloof.si/auth/magic?token=ml_abc123"
import colorsys
def _violet(htm):
    """Any hex colour with a blue-violet/purple hue (240-320 deg) and real saturation, or a violet word."""
    bad = [w for w in ("violet", "purple", "indigo") if w in htm.lower()]
    for h in set(re.findall(r"#([0-9a-fA-F]{6})\b", htm)):
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)); hh, l, sat = colorsys.rgb_to_hls(r, g, b)
        if 240 <= hh * 360 <= 320 and sat > 0.15: bad.append("#" + h)
    return bad
def _all():
    rw = {"credit_usd": 4.2, "burn_usd_day": 1.5, "threshold_usd": 5}
    out = {"magic": emailkit.magic_link(URL, auth.MAGIC_TTL // 60), "welcome": emailkit.welcome("ak_live_xyz", "https://api.aloof.si/dashboard")}
    for k in ("72h", "24h", "custom", "suspended"): out["lowbal_" + k] = emailkit.low_balance(k, "$4.20", "$1.50/day", "about 3 days", "https://api.aloof.si/dashboard#billing", 5)
    for k in ("gpu", "region", "design_partner"):
        subj, txt, htm = ("x",) + emailkit.waitlist(k, "the Aloof waitlist", "We'll email you once.", "https://www.aloof.si/waitlist/unsubscribe?id=1&t=abc")
        out["waitlist_" + k] = (subj, txt, htm)
    return out
@pytest.mark.parametrize("name", list(_all()))
def test_renders(name):
    subj, txt, htm = _all()[name]
    assert subj and txt.strip() and htm.startswith("<!doctype html>")
    assert "https://www.aloof.si/email-logo.png" in htm and 'max-width:560px' in htm and "color-scheme" in htm and "Enigmatic IT Solutions FZE" in htm
    assert "<img" in htm and htm.count("<img") == 1          # logo only: no tracking pixel
    assert not _violet(htm), _violet(htm)
    assert "babysit" not in (txt + htm).lower()
def test_magic_link_content():
    subj, txt, htm = emailkit.magic_link(URL, auth.MAGIC_TTL // 60)
    assert txt.count(URL) == 1 and htm.count(URL) >= 2        # button + plain fallback
    for s in (txt, htm): assert "15 minutes" in s and "works once" in s and "didn't ask" in s
def test_magic_link_sent_through_outbox(monkeypatch):
    sent = []; monkeypatch.setattr(auth, "send_mail", lambda to, subj, body, html=None, headers=None: sent.append((to, subj, body, html)))
    auth.start_magic("someone@example.com", "http://api.aloof.test")
    to, subj, body, html = sent[0]
    assert subj == "Your Aloof sign-in link" and "/auth/magic?token=ml_" in body and html and "/auth/magic?token=ml_" in html
def test_lowbal_mail_tuple():
    subj, txt, htm = billing._lowbal_mail("a@b.co", "24h", {"credit_usd": 3.0, "burn_usd_day": 2.0, "hours_left": 20.0, "threshold_usd": None})
    assert "24 hours" in subj and "$3.00" in txt and "$3.00" in htm
def test_email_logo_served():
    from fastapi.testclient import TestClient; from paas.api import app
    r = TestClient(app).get("/email-logo.png"); assert r.status_code == 200 and r.headers["content-type"] == "image/png"
