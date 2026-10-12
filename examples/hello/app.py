import os, json, socket, time, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
DATA = os.environ.get("DATA_DIR", "/data"); BOOT = time.time()
def bump():
    os.makedirs(DATA, exist_ok=True); f = os.path.join(DATA, "visits.txt")
    n = int(open(f).read() or 0) + 1 if os.path.exists(f) else 1
    open(f, "w").write(str(n)); return n
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/slow"): time.sleep(float(self.path.split("=")[-1]) if "=" in self.path else 1)
        if self.path.startswith("/burn"):
            end = time.time() + float(self.path.split("=")[-1]) if "=" in self.path else time.time() + 1
            while time.time() < end: pass
        if self.path == "/crash": self.send_response(200); self.end_headers(); self.wfile.write(b"bye"); os._exit(1)
        if self.path == "/sick": open("/tmp/sick","w").write("1")
        if self.path == "/healthz" and os.path.exists("/tmp/sick"): self.send_response(500); self.end_headers(); return
        b = {"hello": os.environ.get("GREETING","world"), "instance": socket.gethostname(), "path": self.path, "uptime_s": round(time.time()-BOOT,1)}
        if self.path == "/visit": b["visits"] = bump()
        b = json.dumps(b).encode(); self.send_response(200); self.send_header("Content-Type","application/json"); self.end_headers(); self.wfile.write(b)
    def log_message(self, f, *a): print("req", self.path, flush=True)
print("listening on", os.environ.get("PORT","8080"), flush=True)
ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT","8080"))), H).serve_forever()
