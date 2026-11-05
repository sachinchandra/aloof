"""Single process: control-plane API (:8000) + edge proxy (:8080) + controller loops (autoscale, health)."""
import asyncio, os, uvicorn
from .api import app, proxy
from . import controller, parity, offsite, cron, www
async def main():
    from . import engine
    # Rolling deploys run two copies (paas :8000 and paas-b :8010). Only the one with PAAS_BACKGROUND=1 (default) runs the
    # singleton loops (controller/autoscale/health, log+pg backup, offsite backups, cron, status probes).
    # Startup cleanup (not a loop), in EVERY process incl. paas-b: fail only this process's interrupted builds (owner-stamped).
    try: engine.recover_interrupted()   # builds don't survive a restart: fail them cleanly so Retry works
    except Exception as e: print("recover_interrupted failed:", e)
    if os.environ.get("PAAS_BACKGROUND", "1") == "1":
        controller.start(); parity.start(); offsite.start(); cron.start(); www.start()
    s1 = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(os.environ.get("PAAS_API_PORT", 8000)), log_level="info"))
    s2 = uvicorn.Server(uvicorn.Config(proxy, host="0.0.0.0", port=int(os.environ.get("PAAS_EDGE_PORT", 8080)), log_level="warning", proxy_headers=True))
    await asyncio.gather(s1.serve(), s2.serve())
if __name__ == "__main__": asyncio.run(main())
