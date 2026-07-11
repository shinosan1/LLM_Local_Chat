from dataclasses import dataclass
from typing import Any

from resource_monitor import ResourceMonitor, VRAMGuard, WhisperPool
from session_store import SessionStore


@dataclass(frozen=True)
class AppDeps:
    res_monitor: Any
    guard: Any
    whisper_pool: Any
    session_store: SessionStore


def create_app_deps(log_dir: str) -> AppDeps:
    res_monitor = ResourceMonitor()
    guard = VRAMGuard(res_monitor)
    whisper_pool = WhisperPool()
    session_store = SessionStore(log_dir)
    return AppDeps(
        res_monitor=res_monitor,
        guard=guard,
        whisper_pool=whisper_pool,
        session_store=session_store,
    )
