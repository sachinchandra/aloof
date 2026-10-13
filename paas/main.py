"""Single process: control-plane API (:8000) + edge proxy (:8080) + controller loops (autoscale, health)."""
import asyncio, os, uvicorn
from .api import app, proxy
from . import controller, parity, offsite, cron
async def main():
    controller.start(); parity.start(); offsite.start(); cron.start()
    s1 = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PAAS_API_PORT", 8000)), log_level="info"))
    s2 = uvicorn.Server(uvicorn.Config(proxy, host="0.0.0.0", port=int(os.environ.get("PAAS_EDGE_PORT", 8080)), log_level="warning", proxy_headers=True))
    await asyncio.gather(s1.serve(), s2.serve())
if __name__ == "__main__": asyncio.run(main())
