from dataclasses import dataclass
@dataclass
class Instance:
    id: str; host: str; port: int; status: str = "running"
class Driver:
    """Provider driver contract. A driver turns an app spec into running instances
    reachable at host:port. Control plane handles state, routing, billing."""
    name = "base"; supports_gpu = False; regions = []
    def available(self) -> tuple[bool, str]: return False, "not implemented"
    def build(self, app: str, source: dict, log) -> str:
        """Return an image ref runnable by this driver (may push to a registry)."""
        raise NotImplementedError
    def start_instance(self, app: str, image: str, spec: dict, index: int) -> Instance: raise NotImplementedError
    def stop_instance(self, instance_id: str): raise NotImplementedError
    def logs(self, instance_id: str, tail: int = 200) -> str: raise NotImplementedError
    def inspect(self, instance_id: str) -> str: return "unknown"
