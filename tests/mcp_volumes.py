import asyncio, json, os, time, httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
KEY = json.load(open(os.path.expanduser("~/.aloof.json")))["key"]
J = lambda r: json.loads(r.content[0].text)
async def wait(s, n):
    for _ in range(40):
        await asyncio.sleep(1); a = J(await s.call_tool("get_status", {"name": n}))
        if a["status"] in ("running", "idle", "failed") and a["instances"]: return a
async def main():
    p = StdioServerParameters(command=".venv/bin/python", args=["mcp_server/server.py"], env={**os.environ, "ALOOF_API_KEY": KEY})
    async with stdio_client(p) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print("create_volume:", J(await s.call_tool("create_volume", {"name": "mcp-vol", "size_gb": 1})).get("name"))
            await s.call_tool("deploy_app", {"name": "mcp-db", "image": "paas-examples/hello:3"}); await wait(s, "mcp-db")
            await s.call_tool("attach_volume", {"app": "mcp-db", "volume": "mcp-vol", "path": "/data"}); await asyncio.sleep(2); await wait(s, "mcp-db")
            v = [httpx.get("http://localhost:8080/_app/mcp-db/visit").json()["visits"] for _ in range(3)]
            await s.call_tool("set_env", {"name": "mcp-db", "env": {"X": "1"}}); await asyncio.sleep(2); await wait(s, "mcp-db")
            after = httpx.get("http://localhost:8080/_app/mcp-db/visit").json()
            print("visits before redeploy:", v, "-> after redeploy:", after["visits"], "(new instance", after["instance"] + ")")
            print("list_volumes:", [(x["name"], x["app"], x["mount_path"]) for x in [J(await s.call_tool("list_volumes", {}))] for x in (x if isinstance(x, list) else [x])])
            a = J(await s.call_tool("set_autoscale", {"name": "mcp-db", "min_instances": 0, "max_instances": 2, "idle_seconds": 30}))
            print("set_autoscale:", a["spec"]["autoscale"])
asyncio.run(main())
