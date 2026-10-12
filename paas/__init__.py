
# Production secrets (Stripe, SMTP, provider keys) come from an env file named by PAAS_ENV_FILE (deploy/golive.sh sets it).
# Existing environment variables win; unfilled "<placeholder>" values are ignored.
import os as _os
def _load_env_file(path):
    for line in open(path):
        line = line.split(" #")[0].strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); v = v.strip().strip('"').strip("'")
        if v and "<" not in v: _os.environ.setdefault(k.strip(), v)
if _os.environ.get("PAAS_ENV_FILE") and _os.path.exists(_os.environ["PAAS_ENV_FILE"]): _load_env_file(_os.environ["PAAS_ENV_FILE"])
