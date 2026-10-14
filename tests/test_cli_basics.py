import os, re, subprocess, sys, json
CLI = os.path.join(os.path.dirname(__file__), "..", "cli", "aloof")
def run(*a, env=None, home=None):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("ALOOF_", "PAAS_API"))} | {"HOME": home or "/nonexistent"} | (env or {})
    return subprocess.run([sys.executable, CLI, *a], capture_output=True, text=True, env=e, timeout=30)

def test_version_single_constant():
    src = open(CLI).read(); v = re.search(r'^VERSION = "([^"]+)"', src, re.M).group(1)
    assert run("--version").stdout.strip() == f"aloof {v}"
    assert 'f"aloof-cli/{VERSION}' in src and '"User-Agent": UA' in src

def test_help_every_command_described():
    out = run("--help").stdout
    rows = re.findall(r"^    ([a-z][a-z-]*)(?:\s+(.*))?$", out, re.M)
    assert len(rows) > 30
    for name, desc in rows: assert desc and desc.strip() != name, f"{name} has no description"

def test_local_api_warning(tmp_path):
    r = run("plans", env={"ALOOF_API_URL": "http://127.0.0.1:9"})
    assert "warning: using API http://127.0.0.1:9 from env ALOOF_API_URL" in r.stderr and "unset ALOOF_API_URL" in r.stderr and "Traceback" not in r.stderr
    (tmp_path / ".aloof.json").write_text(json.dumps({"api": "http://localhost:1"}))
    r = run("plans", home=str(tmp_path)); assert ".aloof.json" in r.stderr and 'remove "api"' in r.stderr
    r = run("--version", env={"ALOOF_API_URL": "https://api.example.com"}); assert "warning" not in r.stderr
