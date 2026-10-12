"""Load test: N concurrent clients hitting /slow for D seconds, printing instance count every 2s."""
import asyncio, httpx, sys, time, json, os
APP, CONC, DUR = sys.argv[1], int(sys.argv[2]), float(sys.argv[3])
KEY = json.load(open(os.path.expanduser("~/.paas.json")))["key"]
async def main():
    stats = {"ok": 0, "err": 0, "lat": []}; stop = time.time() + DUR
    async with httpx.AsyncClient(timeout=60) as c:
        async def worker():
            while time.time() < stop:
                t = time.time()
                try: r = await c.get(f"http://localhost:8080/_app/{APP}/slow?s=0.5"); stats["ok" if r.status_code == 200 else "err"] += 1
                except Exception: stats["err"] += 1
                stats["lat"].append(time.time() - t)
        async def monitor():
            t0 = time.time()
            while time.time() < stop + 1:
                a = (await c.get(f"http://localhost:8000/v1/apps/{APP}", headers={"Authorization": f"Bearer {KEY}"})).json()
                print(f"t={time.time()-t0:5.1f}s instances={len(a['instances'])} ok={stats['ok']} err={stats['err']}", flush=True); await asyncio.sleep(2)
        await asyncio.gather(monitor(), *[worker() for _ in range(CONC)])
    l = sorted(stats["lat"]); print(f"done: {stats['ok']} ok, {stats['err']} errors, p50={l[len(l)//2]:.2f}s p95={l[int(len(l)*.95)]:.2f}s")
asyncio.run(main())
