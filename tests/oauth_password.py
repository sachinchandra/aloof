"""MCP OAuth with email+password on the consent page (no API key pasted): DCR -> authorize(PKCE) -> password login ->
code -> token -> MCP tool call scoped to that account. Also: the connector key must not revoke the user's CLI key."""
import asyncio, json, re, sys, base64, hashlib, secrets, httpx
from urllib.parse import urlparse, parse_qs
MCP = "http://localhost:8001"; API = "http://localhost:8000"; REDIRECT = "http://localhost:3999/callback"; OK = []
def check(c, l): print(("PASS " if c else "FAIL ") + l); OK.append(bool(c))
email = f"mcp-pw-{secrets.token_hex(3)}@example.in"; pw = "a long password 42"
r = httpx.post(f"{API}/v1/auth/register", json={"email": email, "password": pw}); cli_key = r.json()["api_key"]; check(r.status_code == 200, "registered " + email)
c = httpx.post(f"{MCP}/register", json={"redirect_uris": [REDIRECT], "client_name": "pw-test", "grant_types": ["authorization_code", "refresh_token"],
                                       "response_types": ["code"], "token_endpoint_auth_method": "none"}).json()
ver = secrets.token_urlsafe(48); chal = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).rstrip(b"=").decode()
url = f"{MCP}/authorize?" + str(httpx.QueryParams({"response_type": "code", "client_id": c["client_id"], "redirect_uri": REDIRECT, "code_challenge": chal,
                                                   "code_challenge_method": "S256", "state": "s1", "scope": "paas"}))
def login(**form):
    with httpx.Client(follow_redirects=False) as b:
        r = b.get(url); lp = r.headers["location"]; page = b.get(lp).text
        req = re.search(r'name="req" value="([^"]+)"', page).group(1)
        r = b.post(lp.split("?")[0], data={"req": req, "action": "approve", **form}); return r.headers.get("location", ""), page, r.text
loc, page, _ = login(email=email, password="wrong password!!")
check('type="password"' in page and loc == "", "consent page has password field; wrong password rejected")
loc, _, _ = login(email=email.upper(), password=pw); code = parse_qs(urlparse(loc).query).get("code", [None])[0]
check(bool(code), "email+password login issues authorization code")
t = httpx.post(f"{MCP}/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": c["client_id"], "code_verifier": ver}).json()
at = t.get("access_token"); check(bool(at), "token exchange ok")
H = {"Authorization": f"Bearer {at}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
with httpx.Client(timeout=30) as h:
    r = h.post(f"{MCP}/mcp", headers=H, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    sid = r.headers.get("mcp-session-id"); H2 = {**H, **({"mcp-session-id": sid} if sid else {})}
    h.post(f"{MCP}/mcp", headers=H2, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = h.post(f"{MCP}/mcp", headers=H2, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_usage", "arguments": {}}})
    body = r.text; print("   tool response:", r.status_code, body[:300].replace(chr(10)," ")); check(r.status_code == 200 and '"isError":false' in body and 'credit_inr\\": 500' in body, "MCP tool call as the password-logged-in account")
check(httpx.get(f"{API}/v1/auth/me", headers={"Authorization": f"Bearer {cli_key}"}).status_code == 200, "user's CLI key still valid after connector login")
print(f"\n{sum(OK)}/{len(OK)} checks passed"); sys.exit(0 if all(OK) else 1)
