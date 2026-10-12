"""Full MCP OAuth 2.1 flow with the official SDK client: discovery -> DCR -> authorize (PKCE S256) -> simulated
user login/consent -> code exchange -> authenticated MCP tool calls -> refresh rotation -> revocation.
Also negative tests (bad PKCE verifier, code replay, bogus token) and API-key bearer auth."""
import asyncio, json, os, re, sys, base64, hashlib, secrets, httpx
from urllib.parse import urlparse, parse_qs
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientMetadata
MCP = "http://localhost:8001"; KEY = json.load(open(os.path.expanduser("~/.aloof.json")))["key"]
REDIRECT = "http://localhost:3999/callback"; OK = []
def check(cond, label): print(("PASS " if cond else "FAIL ") + label); OK.append(bool(cond))
class Mem(TokenStorage):
    def __init__(self): self.t = self.c = None
    async def get_tokens(self): return self.t
    async def set_tokens(self, t): self.t = t
    async def get_client_info(self): return self.c
    async def set_client_info(self, c): self.c = c
captured = {}
def browser_login(auth_url, api_key=KEY, action="approve"):
    """Plays the user in a browser: follow /authorize to our login page, submit API key, capture redirect."""
    with httpx.Client(follow_redirects=False) as b:
        r = b.get(auth_url); assert r.status_code == 302, r.text
        login = r.headers["location"]; page = b.get(login).text
        req = re.search(r'name="req" value="([^"]+)"', page).group(1)
        r = b.post(login.split("?")[0], data={"req": req, "api_key": api_key, "action": action})
        return r.headers.get("location", ""), page
async def redirect_handler(url):
    check("code_challenge_method=S256" in url and "code_challenge=" in url, "authorize request uses PKCE S256")
    loc, page = browser_login(url); check("Authorize" in page, "consent page rendered")
    qs = parse_qs(urlparse(loc).query); captured["code"], captured["state"] = qs["code"][0], qs.get("state", [None])[0]
async def callback_handler(): return captured["code"], captured["state"]
async def main():
    st = Mem()
    oauth = OAuthClientProvider(server_url=f"{MCP}/mcp", storage=st, redirect_handler=redirect_handler, callback_handler=callback_handler,
        client_metadata=OAuthClientMetadata(client_name="Test Assistant", redirect_uris=[REDIRECT], grant_types=["authorization_code","refresh_token"], response_types=["code"], scope="paas"))
    async with streamablehttp_client(f"{MCP}/mcp", auth=oauth) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize(); tools = [t.name for t in (await s.list_tools()).tools]
            check("deploy_app" in tools and "set_autoscale" in tools, f"OAuth session lists {len(tools)} tools")
            res = await s.call_tool("deploy_app", {"name": "oauth-app", "image": "paas-examples/hello:3"})
            check(json.loads(res.content[0].text).get("provider") == "local-docker", "deploy_app via OAuth token (token -> account API key)")
            for _ in range(30):
                await asyncio.sleep(1); a = json.loads((await s.call_tool("get_status", {"name": "oauth-app"})).content[0].text)
                if a["status"] == "running": break
            check(a["status"] == "running", "oauth-app running")
    check(st.c and st.c.client_id, f"dynamic client registration -> client_id {st.c.client_id[:8]}...")
    at, rt = st.t.access_token, st.t.refresh_token
    check(httpx.get("http://localhost:8080/_app/oauth-app/").status_code == 200, "deployed app responds through edge")
    async with httpx.AsyncClient() as h:
        cid, csec = st.c.client_id, st.c.client_secret
        # refresh-token rotation
        r = await h.post(f"{MCP}/token", data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid, "client_secret": csec or ""})
        check(r.status_code == 200 and r.json()["refresh_token"] != rt, "refresh grant issues new tokens (rotation)")
        r2 = await h.post(f"{MCP}/token", data={"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid, "client_secret": csec or ""})
        check(r2.status_code == 400, "old refresh token rejected after rotation")
        new_at = r.json()["access_token"]
        # manual code flow with WRONG verifier and code replay
        ver = secrets.token_urlsafe(48); chal = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).rstrip(b"=").decode()
        url = f"{MCP}/authorize?" + httpx.QueryParams({"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT, "code_challenge": chal, "code_challenge_method": "S256", "state": "xyz", "scope": "paas"}).__str__()
        loc, _ = browser_login(url); code = parse_qs(urlparse(loc).query)["code"][0]
        check(parse_qs(urlparse(loc).query)["state"][0] == "xyz", "state round-trips")
        tok = lambda v: h.post(f"{MCP}/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": cid, "client_secret": csec or "", "code_verifier": v})
        check((await tok("wrong-" + ver)).status_code == 400, "wrong PKCE verifier rejected")
        good = await tok(ver); check(good.status_code == 200, "correct PKCE verifier accepted")
        check((await tok(ver)).status_code == 400, "authorization code cannot be replayed")
        # deny path
        loc, _ = browser_login(url, action="deny"); check("error=access_denied" in loc, "user denial -> access_denied")
        loc, _ = browser_login(url, api_key="pk_bogus"); check(loc == "", "invalid API key on login page rejected")
        # token checks on the resource
        mcp_post = lambda t: h.post(f"{MCP}/mcp", headers={"Authorization": f"Bearer {t}", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
        check((await mcp_post("mcp_at_bogus")).status_code == 401, "bogus bearer token -> 401")
        check((await mcp_post(new_at)).status_code == 200, "refreshed access token accepted")
        check((await mcp_post(KEY)).status_code == 200, "plain API key (pk_) still accepted as bearer")
        r = await h.post(f"{MCP}/revoke", data={"token": new_at, "client_id": cid, "client_secret": csec or ""})
        check(r.status_code == 200 and (await mcp_post(new_at)).status_code == 401, "revoked token -> 401")
    # API-key auth with the SDK client
    async with streamablehttp_client(f"{MCP}/mcp", headers={"Authorization": f"Bearer {KEY}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize(); res = await s.call_tool("list_apps", {}); apps = [json.loads(c.text) for c in res.content]; apps = apps[0] if len(apps) == 1 and isinstance(apps[0], list) else apps
            check(any(x["name"] == "oauth-app" for x in apps), "API-key MCP session sees same account's apps")
    print(f"\n{sum(OK)}/{len(OK)} checks passed"); sys.exit(0 if all(OK) else 1)
asyncio.run(main())
