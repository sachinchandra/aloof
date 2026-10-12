"""Headless-Chrome smoke test: landing/pricing/docs, /dashboard -> /login redirect, magic-link login from the outbox,
every dashboard tab and the app detail panel. Fails on JS errors or failed requests. Screenshots -> data/ui-*.png"""
import re, sys
from playwright.sync_api import sync_playwright
B = "http://localhost:8000"; OUT = "/workspace/paas/data/outbox.log"; errs = []; bad = []
EXPECTED = ("/v1/auth/me",)   # the pre-login 401 that triggers the redirect
with sync_playwright() as p:
    br = p.chromium.launch(channel="chrome", headless=True); pg = br.new_page(viewport={"width": 1300, "height": 900})
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and "status of 40" not in m.text and errs.append(m.text))
    pg.on("response", lambda r: r.status >= 400 and not any(e in r.url for e in EXPECTED) and bad.append(f"{r.status} {r.url}"))
    for path, shot in (("/", "landing"), ("/pricing", "pricing"), ("/docs/gpu", "docs")):
        pg.goto(B + path); assert "Aloof" in pg.title(); pg.screenshot(path=f"/workspace/paas/data/ui-{shot}.png", full_page=True)
    pg.goto(B + "/dashboard"); pg.wait_for_url("**/login", timeout=10000); print("unauthenticated /dashboard -> /login: ok")
    n0 = open(OUT).read().count("token=ml_") if __import__("os").path.exists(OUT) else 0
    pg.fill("#email", "sachin@example.com"); pg.click("text=email me a sign-in link"); pg.wait_for_timeout(1500)
    txt = open(OUT).read(); assert txt.count("token=ml_") == n0 + 1, "magic link not written to outbox"
    pg.goto(f"{B}/auth/magic?token={re.findall(r'token=(ml_[\w-]+)', txt)[-1]}"); pg.wait_for_url("**/dashboard"); pg.wait_for_selector("#apps tr:nth-child(2)")
    print("apps rows:", pg.locator("#apps tr").count() - 1, "| who:", pg.inner_text("#who"))
    pg.click("text=Manage >> nth=0"); pg.wait_for_selector("#detail h3"); pg.wait_for_timeout(1500)
    print("detail:", pg.inner_text("#detail h3")[:40].replace("\n", " ")); pg.screenshot(path="/workspace/paas/data/ui-app.png", full_page=True)
    for t in ("volumes", "billing", "account"):
        pg.click(f"header a[data-t={t}]"); pg.wait_for_timeout(1200); pg.screenshot(path=f"/workspace/paas/data/ui-{t}.png", full_page=True)
    print("balance:", pg.inner_text("#bal"), "|", pg.inner_text("#paynote"))
    bal = pg.text_content("#bal"); print("balance shown:", bal); assert bal.startswith("$") or bal.startswith("−$"), bal
    br.close()
print("JS errors:", errs or "none"); print("failed requests:", bad or "none"); sys.exit(1 if errs or bad else 0)
