import json
import os
import tempfile


def atomic_write_bytes(path: str, data: bytes) -> None:
    """bytesを同一ディレクトリの一時ファイル経由で置換する。"""
    target = os.path.abspath(path)
    directory = os.path.dirname(target) or os.curdir
    os.makedirs(directory, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=directory,
            prefix=f".{os.path.basename(target)}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = handle.name
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass


def atomic_write_json(path: str, data) -> None:
    """JSONを同一ディレクトリの一時ファイル経由で置換する。"""
    raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    atomic_write_bytes(path, raw)
