"""Build limits (pre-launch P1): one build may run at most this long (docker build / Nixpacks / buildpacks)."""
import os
BUILD_TIMEOUT_S = int(os.environ.get("PAAS_BUILD_TIMEOUT_S", "1800"))
