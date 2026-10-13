from .base import Driver, Instance
from .local_docker import LocalDockerDriver
from .providers import PROVISIONERS
REGISTRY = {}
def get_drivers():
    if not REGISTRY:
        from .remote import RemoteDriver
        REGISTRY["local-docker"] = LocalDockerDriver()
        for pool in PROVISIONERS: REGISTRY[pool] = RemoteDriver(pool)
        from .runpod import RunpodDriver
        REGISTRY["runpod"] = RunpodDriver()
    return REGISTRY
