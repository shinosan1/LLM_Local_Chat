from dataclasses import dataclass
from typing import Any

from resource_monitor import ResourceMonitor, WhisperPool
from session_store import SessionStore


@dataclass(frozen=True)
class AppDeps:
    res_monitor: Any
    whisper_pool: Any
    session_store: SessionStore


def create_app_deps(log_dir: str) -> AppDeps:
    res_monitor = ResourceMonitor()
    try:
        whisper_pool = WhisperPool()
        session_store = SessionStore(log_dir)
    except Exception:
        res_monitor.stop()
        raise
    return AppDeps(
        res_monitor=res_monitor,
        whisper_pool=whisper_pool,
        session_store=session_store,
    )
