import asyncio, json, sys
from mcp import ClientSession, StdioServerParameters
from mcp.client.streamable_http import streamablehttp_client
from mcp.client.stdio import stdio_client
import os; K=os.environ.get("PAAS_API_KEY") or __import__("json").load(open(os.path.expanduser("~/.aloof.json")))["key"]
async def run(s, app):
    await s.initialize(); print("tools:", [t.name for t in (await s.list_tools()).tools])
    r = await s.call_tool("deploy_app", {"name":app,"image":"paas-examples/hello:3","env":{"GREETING":"namaste from MCP"},"instances":2,"api_key":K})
    print("deploy:", json.loads(r.content[0].text)["status"])
    for _ in range(20):
        await asyncio.sleep(1); st = json.loads((await s.call_tool("get_status", {"name":app,"api_key":K})).content[0].text)
        if st["status"] in ("running","failed"): break
    print("status:", st["status"], st.get("error"), st["url"], len(st["instances"]), "instances")
    print((await s.call_tool("get_logs", {"name":app,"api_key":K,"tail":5})).content[0].text[-300:])
async def main():
    async with streamablehttp_client("http://localhost:8001/mcp", headers={"Authorization": f"Bearer {K}"}) as (r,w,_):
        async with ClientSession(r,w) as s: await run(s, "hello-mcp")
    async with stdio_client(StdioServerParameters(command=".venv/bin/python", args=["mcp_server/server.py"])) as (r,w):
        async with ClientSession(r,w) as s: await run(s, "hello-stdio")
asyncio.run(main())
