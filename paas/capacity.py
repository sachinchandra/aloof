"""Memory admission control for the control-plane host's own Docker (the local-docker pool).

The sum of the memory limits of running/starting local-docker instances plus the new instance(s) must not exceed
PAAS_LOCAL_MEM_BUDGET_MB (default 6144 MB of the 7.75 GB CX33; 0 disables the check). Over budget, a new instance is
refused with PlatformAtCapacity instead of overcommitting the host until the kernel OOM-kills something.
Retry policy: the API answers 503 with `Retry-After: RETRY_AFTER_S`; clients should retry with backoff
(capacity frees up as idle apps scale to zero or apps are deleted)."""
import os
from .db import q
from .crypto import load_spec
from .plans import PLANS
RETRY_AFTER_S = 30
class PlatformAtCapacity(RuntimeError):
    retry_after = RETRY_AFTER_S
def budget_mb():
    try: return int(os.environ.get("PAAS_LOCAL_MEM_BUDGET_MB", "6144"))
    except ValueError: return 6144
def used_mb():
    """Memory limits (MB) of every running/starting instance on the local node."""
    total = 0
    for r in q("select a.spec from instances i join apps a on a.name=i.app where i.provider='local-docker' and i.status in ('running','starting')"):
        try: total += PLANS[load_spec(r["spec"])["plan"]]["memory_mb"]
        except Exception: pass
    return total
def status():
    b = budget_mb(); u = used_mb()
    return {"budget_mb": b, "used_mb": u, "free_mb": (b - u) if b else None, "retry_after_s": RETRY_AFTER_S}
def admit(plan_id, count=1):
    """Raise PlatformAtCapacity unless `count` more instances of `plan_id` fit in the local memory budget."""
    b = budget_mb()
    if b <= 0 or count <= 0: return
    need = PLANS[plan_id]["memory_mb"] * count; used = used_mb()
    if used + need > b:
        raise PlatformAtCapacity(f"platform at capacity, try again shortly (in about {RETRY_AFTER_S}s): "
                                 f"{need} MB requested, {max(b - used, 0)} MB free of the {b} MB budget")
