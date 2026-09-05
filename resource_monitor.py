# -*- coding: utf-8 -*-
"""
resource_monitor.py  —  VRAM安全フィルタ

設計思想: 「VRAMを管理する」ではなく「VRAMから逃げる」
  - 予約・帳簿・状態管理は持たない。即時の観測値だけで判断する。
  - OOM完全防止は不可能。「クラッシュではなく劣化」で済ませることが目標。
  - 唯一残す状態: Whisper の GPU/CPU ヒステリシス（WhisperController のみ）

既知の限界:
  - pynvml は allocator 後スナップショット（TOCTOU あり）
  - 他プロセス分も総使用量には反映されるが、内訳や将来の確保は予測できない
  - この設計は確率的削減が目的であり、OOM の完全防止は保証しない
"""

import gc
import logging
import os
import re
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

MIN_HARD_RESERVE_MB = 512
HARD_RESERVE_RATIO = 0.06
INFERENCE_SOFT_LIMIT_MB = 1536
WHISPER_GPU_MEDIUM_MIN_FREE_MB = 4096
WHISPER_GPU_SMALL_MIN_FREE_MB = 2048
WHISPER_MODES = ("auto", "gpu_small", "gpu_medium", "cpu_small")
LLM_GPU_OFFLOAD_MODES = ("auto", "full", "75", "50", "25", "cpu")
LLM_STARTUP_RESERVE_MB = 1024
LLM_STARTUP_RESERVE_RATIO = 0.12
MAX_GGUF_BLOCK_COUNT = 4096
_GGUF_ARCHITECTURE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
_GGUF_BLOCK_COUNT_PATTERN = re.compile(r"[1-9][0-9]*")
_PARTIAL_OFFLOAD_FRACTIONS = (
    ("partial_75", 3, 4),
    ("partial_50", 1, 2),
    ("partial_25", 1, 4),
)


def normalize_llm_gpu_offload_mode(value) -> str:
    """保存値が欠落・破損していても従来どおりautoへ戻す。"""
    return (
        value
        if isinstance(value, str) and value in LLM_GPU_OFFLOAD_MODES
        else "auto"
    )


def hard_reserve_mb(total_mb: int) -> int:
    """GPU全体容量に応じた、推論中に残す最低空き容量。"""
    return max(MIN_HARD_RESERVE_MB, int(total_mb * HARD_RESERVE_RATIO))


def startup_reserve_mb(total_mb: int) -> int:
    """LLMロード前に残す既存の保守的予約量。"""
    return max(
        LLM_STARTUP_RESERVE_MB,
        int(max(0, total_mb) * LLM_STARTUP_RESERVE_RATIO),
    )


def _parse_gguf_block_count(value) -> int | None:
    if not isinstance(value, str) or not _GGUF_BLOCK_COUNT_PATTERN.fullmatch(value):
        return None
    layers = int(value)
    return layers if 1 <= layers <= MAX_GGUF_BLOCK_COUNT else None


def read_gguf_total_layers(model_path: str) -> int | None:
    """GGUFをweightsなしで開き、metadataから総ブロック数を取得する。"""
    model = None
    total_layers = None
    try:
        from llama_cpp import _internals, llama_cpp

        params = llama_cpp.llama_model_default_params()
        params.vocab_only = True
        params.n_gpu_layers = 0
        params.use_mmap = True
        model = _internals.LlamaModel(
            path_model=model_path,
            params=params,
            verbose=False,
        )
        metadata = model.metadata()
        if not isinstance(metadata, dict):
            return None

        architecture = metadata.get("general.architecture")
        if architecture is not None:
            if (
                not isinstance(architecture, str)
                or not _GGUF_ARCHITECTURE_PATTERN.fullmatch(architecture)
            ):
                return None
            total_layers = _parse_gguf_block_count(
                metadata.get(f"{architecture}.block_count")
            )
        else:
            candidates = [
                parsed
                for key, value in metadata.items()
                if isinstance(key, str)
                and key.endswith(".block_count")
                and (parsed := _parse_gguf_block_count(value)) is not None
            ]
            if len(candidates) == 1:
                total_layers = candidates[0]
    except Exception as exc:
        logger.warning(
            "[GPU] GGUF metadata unavailable: %s", type(exc).__name__
        )
        total_layers = None
    finally:
        if model is not None:
            try:
                model.close()
            except Exception as exc:
                logger.warning(
                    "[GPU] GGUF metadata close failed: %s", type(exc).__name__
                )
                total_layers = None
    return total_layers


def build_partial_layer_candidates(total_layers: int) -> list[dict]:
    """総レイヤー数から75/50/25%候補をhalf-upで生成する。"""
    if (
        not isinstance(total_layers, int)
        or isinstance(total_layers, bool)
        or not 1 <= total_layers <= MAX_GGUF_BLOCK_COUNT
    ):
        return []
    candidates = []
    seen = set()
    for reason, numerator, denominator in _PARTIAL_OFFLOAD_FRACTIONS:
        # floor(x + 0.5)を整数演算で表し、banker's roundingを避ける。
        layers = (
            2 * total_layers * numerator + denominator
        ) // (2 * denominator)
        if 1 <= layers < total_layers and layers not in seen:
            seen.add(layers)
            candidates.append({"n_gpu_layers": layers, "reason": reason})
    candidates.sort(key=lambda item: item["n_gpu_layers"], reverse=True)
    return candidates


def required_vram_mb_for_layers(
    model_mb: int,
    reserve_mb: int,
    layers: int,
    total_layers: int,
) -> int | None:
    """partial offloadの保守的な事前必要量を整数MBで返す。"""
    values = (model_mb, reserve_mb, layers, total_layers)
    if any(
        not isinstance(value, int) or isinstance(value, bool)
        for value in values
    ):
        return None
    if model_mb <= 0 or reserve_mb < 0 or total_layers <= 0:
        return None
    if not 1 <= layers < total_layers:
        return None
    offload_mb = (model_mb * layers + total_layers - 1) // total_layers
    return reserve_mb + offload_mb


def select_llm_offload(
    *,
    free_mb: int,
    model_mb: int,
    reserve_mb: int,
    total_layers: int | None,
    gpu_offload_supported: bool,
    gpu_memory_available: bool,
    offload_mode: str = "auto",
    downshift_from_layers: int | None = None,
) -> dict:
    """希望モードと安全条件から、上位から下位へのロード候補を返す。"""
    offload_mode = normalize_llm_gpu_offload_mode(offload_mode)
    full_required_mb = (
        model_mb + reserve_mb
        if isinstance(model_mb, int)
        and not isinstance(model_mb, bool)
        and model_mb > 0
        and isinstance(reserve_mb, int)
        and not isinstance(reserve_mb, bool)
        and reserve_mb >= 0
        else 0
    )
    valid_free = (
        isinstance(free_mb, int)
        and not isinstance(free_mb, bool)
        and free_mb >= 0
    )
    valid_layers = (
        isinstance(total_layers, int)
        and not isinstance(total_layers, bool)
        and 1 <= total_layers <= MAX_GGUF_BLOCK_COUNT
    )
    evaluated = []
    requested_layers = None

    def cpu_result(reason: str) -> dict:
        cpu = {
            "n_gpu_layers": 0,
            "mode": "cpu",
            "reason": reason,
            "required_mb": 0,
        }
        return {
            "n_gpu_layers": 0,
            "fallback": True,
            "reason": reason,
            "selected_mode": "cpu",
            "load_candidates": [cpu],
            "evaluated_candidates": evaluated,
            "required_mb": full_required_mb,
            "offload_mode": offload_mode,
            "requested_n_gpu_layers": requested_layers,
        }

    if not gpu_offload_supported:
        return cpu_result("gpu_offload_unsupported")
    if not gpu_memory_available or not valid_free:
        return cpu_result("gpu_memory_unavailable")
    if full_required_mb <= 0:
        return cpu_result("model_size_unavailable")

    partial_candidates = (
        build_partial_layer_candidates(total_layers) if valid_layers else []
    )
    requested_layers = {
        "full": -1,
        "75": next((item["n_gpu_layers"] for item in partial_candidates
                    if item["reason"] == "partial_75"), None),
        "50": next((item["n_gpu_layers"] for item in partial_candidates
                    if item["reason"] == "partial_50"), None),
        "25": next((item["n_gpu_layers"] for item in partial_candidates
                    if item["reason"] == "partial_25"), None),
        "cpu": 0,
    }.get(offload_mode)

    allowed_layers = []
    if offload_mode == "cpu":
        return cpu_result("manual_cpu")
    if downshift_from_layers is not None:
        if downshift_from_layers == 0:
            return cpu_result("auto_downshift_cpu")
        if valid_layers:
            allowed_layers = [
                item["n_gpu_layers"]
                for item in partial_candidates
                if downshift_from_layers == -1
                or item["n_gpu_layers"] < downshift_from_layers
            ]
        # 総レイヤー数不明時は安全な下位位置を決められないためCPUだけ。
    elif offload_mode in ("auto", "full"):
        allowed_layers = [-1] + [
            item["n_gpu_layers"] for item in partial_candidates
        ]
    else:
        start = requested_layers
        if isinstance(start, int) and start > 0:
            allowed_layers = [
                item["n_gpu_layers"]
                for item in partial_candidates
                if item["n_gpu_layers"] <= start
            ]

    load_candidates = []
    if -1 in allowed_layers and free_mb >= full_required_mb:
        full = {
            "n_gpu_layers": -1,
            "mode": "full",
            "reason": "gpu_full_offload",
            "required_mb": full_required_mb,
        }
        evaluated.append({**full, "eligible": True})
        load_candidates.append(full)
    elif -1 in allowed_layers:
        evaluated.append({
            "n_gpu_layers": -1,
            "mode": "full",
            "reason": "gpu_full_offload",
            "required_mb": full_required_mb,
            "eligible": False,
        })

    if valid_layers:
        for candidate in partial_candidates:
            if candidate["n_gpu_layers"] not in allowed_layers:
                continue
            required_mb = required_vram_mb_for_layers(
                model_mb,
                reserve_mb,
                candidate["n_gpu_layers"],
                total_layers,
            )
            partial = {
                "n_gpu_layers": candidate["n_gpu_layers"],
                "mode": "partial",
                "reason": candidate["reason"],
                "required_mb": required_mb,
            }
            eligible = required_mb is not None and free_mb >= required_mb
            evaluated.append({**partial, "eligible": eligible})
            if eligible:
                load_candidates.append(partial)

    if not load_candidates:
        reason = (
            "partial_metadata_unavailable"
            if not valid_layers and offload_mode in ("75", "50", "25")
            else "insufficient_vram_for_partial"
        )
        return cpu_result(reason)

    load_candidates.append({
        "n_gpu_layers": 0,
        "mode": "cpu",
        "reason": "cpu_after_gpu_fallback",
        "required_mb": 0,
    })
    selected = load_candidates[0]
    return {
        "n_gpu_layers": selected["n_gpu_layers"],
        "fallback": selected["n_gpu_layers"] != -1,
        "reason": selected["reason"],
        "selected_mode": selected["mode"],
        "load_candidates": load_candidates,
        "evaluated_candidates": evaluated,
        "required_mb": full_required_mb,
        "offload_mode": offload_mode,
        "requested_n_gpu_layers": requested_layers,
    }


def normalize_whisper_mode(value) -> str:
    return value if isinstance(value, str) and value in WHISPER_MODES else "auto"


def select_whisper_profile(mode: str, snapshot: dict) -> tuple[str, str]:
    """設定希望と実空き容量から安全なWhisper配置と理由を返す。"""
    mode = normalize_whisper_mode(mode)
    free_mb = snapshot.get("free_mb", 0) if snapshot.get("available") else 0
    if mode == "cpu_small":
        return "cpu_small", "manual_cpu"
    if mode == "gpu_medium":
        if free_mb >= WHISPER_GPU_MEDIUM_MIN_FREE_MB:
            return "gpu_medium", "manual_gpu_medium"
        return "cpu_small", f"free<{WHISPER_GPU_MEDIUM_MIN_FREE_MB}mb"
    if mode == "gpu_small":
        if free_mb >= WHISPER_GPU_SMALL_MIN_FREE_MB:
            return "gpu_small", "manual_gpu_small"
        return "cpu_small", f"free<{WHISPER_GPU_SMALL_MIN_FREE_MB}mb"
    if free_mb >= WHISPER_GPU_MEDIUM_MIN_FREE_MB:
        return "gpu_medium", "auto_medium"
    if free_mb >= WHISPER_GPU_SMALL_MIN_FREE_MB:
        return "gpu_small", "auto_small"
    return "cpu_small", f"free<{WHISPER_GPU_SMALL_MIN_FREE_MB}mb"


# ═══════════════════════════════════════════════════════════════
# 1-A  ResourceMonitor  （観測のみ — 判断しない）
# ═══════════════════════════════════════════════════════════════
class ResourceMonitor:
    """0.5 秒ごとに VRAM / GPU / CPU を収集するバックグラウンドデーモン。"""

    POLL_SEC  = 0.5   # 収集周期
    EMA_ALPHA = 0.6   # 急変に追従できるよう強め

    def __init__(self) -> None:
        # --- 判断に使う値 ---
        self.vram_total_mb:   int   = 0
        self.vram_instant_mb: float = 0.0   # 唯一の判断軸

        # --- 非判断データ（ログ・診断専用） ---
        self.vram_used_mb: float = 0.0      # EMA、参考値
        self.gpu_pct:      float = 0.0      # non-decision metric
        self.cpu_pct:      float = 0.0      # non-decision metric

        self._pynvml_ok = False
        self._handle    = None
        self._pynvml    = None

        self._stop_event = threading.Event()

        self._init_pynvml()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """ポーリングループを止め、スレッドの終了を待つ。"""
        self._stop_event.set()
        self._thread.join(timeout=timeout)

    # ── 初期化 ──────────────────────────────────────────
    def _init_pynvml(self) -> None:
        try:
            import pynvml
            pynvml.nvmlInit()
            self._handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            info = pynvml.nvmlDeviceGetMemoryInfo(self._handle)
            self.vram_total_mb = info.total // (1024 ** 2)
            self._pynvml    = pynvml
            self._pynvml_ok = True
            logger.debug(f"[Monitor] pynvml OK: total={self.vram_total_mb}MB")
        except Exception as e:
            logger.debug(f"[Monitor] pynvml unavailable ({e}), trying nvidia-smi")
            self._try_nvidia_smi_init()

    def _try_nvidia_smi_init(self) -> None:
        try:
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.total",
                 "--format=csv,noheader,nounits"],
                timeout=3, encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.vram_total_mb = int(out.strip().split("\n")[0])
            logger.debug(f"[Monitor] nvidia-smi total={self.vram_total_mb}MB")
        except Exception:
            self.vram_total_mb = 0
            logger.debug("[Monitor] GPU not detected; CPU-only mode")

    # ── 収集ヘルパー ────────────────────────────────────
    def _collect_pynvml(self) -> tuple[int, float]:
        try:
            info = self._pynvml.nvmlDeviceGetMemoryInfo(self._handle)
            util = self._pynvml.nvmlDeviceGetUtilizationRates(self._handle)
            return info.used // (1024 ** 2), float(util.gpu)
        except Exception:
            return 0, 0.0

    def _collect_nvidia_smi(self) -> tuple[int, float]:
        try:
            out = subprocess.check_output(
                ["nvidia-smi",
                 "--query-gpu=memory.used,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                timeout=3, encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            used_str, gpu_str = out.strip().split("\n")[0].split(",")
            return int(used_str.strip()), float(gpu_str.strip())
        except Exception:
            return 0, 0.0

    def snapshot(self) -> dict:
        """判定直前のVRAM実測値を同期取得する。取得不能時は available=False。"""
        try:
            if self._pynvml_ok:
                info = self._pynvml.nvmlDeviceGetMemoryInfo(self._handle)
                total = int(info.total // (1024 ** 2))
                used = int(info.used // (1024 ** 2))
            elif self.vram_total_mb > 0:
                out = subprocess.check_output(
                    ["nvidia-smi", "--query-gpu=memory.total,memory.used",
                     "--format=csv,noheader,nounits"],
                    timeout=3, encoding="utf-8",
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                total_str, used_str = out.strip().split("\n")[0].split(",")
                total = int(total_str.strip())
                used = int(used_str.strip())
            else:
                total = used = 0
        except Exception:
            total = used = 0

        free = max(0, total - used)
        return {
            "available": total > 0,
            "total_mb": total,
            "used_mb": used,
            "free_mb": free,
            "used_ratio": (used / total) if total else 0.0,
        }

    # ── ポーリングループ ────────────────────────────────
    def _loop(self) -> None:
        alpha = self.EMA_ALPHA
        while not self._stop_event.is_set():
            try:
                if self._pynvml_ok:
                    instant, gpu = self._collect_pynvml()
                elif self.vram_total_mb > 0:
                    instant, gpu = self._collect_nvidia_smi()
                else:
                    instant, gpu = 0, 0.0

                self.vram_instant_mb = float(instant)
                self.vram_used_mb    = alpha * instant + (1 - alpha) * self.vram_used_mb
                self.gpu_pct         = alpha * gpu     + (1 - alpha) * self.gpu_pct

                try:
                    import psutil
                    self.cpu_pct = alpha * psutil.cpu_percent() + (1 - alpha) * self.cpu_pct
                except Exception:
                    pass

                logger.debug(
                    f"[Monitor] vram_instant={self.vram_instant_mb:.0f}mb "
                    f"| gpu_pct={self.gpu_pct:.1f}% (non-decision metric)"
                )
            except Exception as exc:
                logger.warning(f"[Monitor] poll error: {exc}")
            self._stop_event.wait(self.POLL_SEC)


# ═══════════════════════════════════════════════════════════════
# 1-B  VRAMGuard  （即時判定 — 予約なし）
# ═══════════════════════════════════════════════════════════════
class VRAMGuard:
    """
    Scheduler の置き換え。予約・状態・task_id は持たない。
    即時の観測値だけで判断するシンプルなガード。
    """

    SCORE_LIMIT = 0.85   # vram_score がこれ以上 → is_safe=False
    GPU_COEFF   = 0.001  # GPU% を VRAM スコアへの補助係数として微量加算（最大 +0.1）

    def __init__(self, monitor: ResourceMonitor) -> None:
        self.m = monitor

    def is_safe(self) -> bool:
        """
        True = 通常実行可。False = 軽量モードへ。
        vram_score = vram_ratio + (gpu_pct * GPU_COEFF)
          → VRAM が主軸、GPU% が補助係数として微量寄与。
          → 1スカラーで両指標を統合。VRAM 単独でも閾値超えれば判定される。
        """
        if self.m.vram_total_mb == 0:
            return True
        vram_ratio = self.m.vram_instant_mb / self.m.vram_total_mb
        vram_score = vram_ratio + (self.m.gpu_pct * self.GPU_COEFF)
        ok = vram_score < self.SCORE_LIMIT
        logger.debug(
            f"[Guard] is_safe={ok} score={vram_score:.3f} "
            f"(vram_ratio={vram_ratio:.2f} + gpu_contrib={self.m.gpu_pct * self.GPU_COEFF:.3f})"
            f" | gpu_pct={self.m.gpu_pct:.1f}% (non-decision metric)"
        )
        return ok


# ═══════════════════════════════════════════════════════════════
# 1-C  調整関数  （VRAM ベースの即時調整）
# ═══════════════════════════════════════════════════════════════
def _supports_gpu_offload() -> bool:
    try:
        from llama_cpp import llama_cpp
        return bool(llama_cpp.llama_supports_gpu_offload())
    except Exception:
        return False


def _normalize_snapshot(snapshot) -> dict:
    if not isinstance(snapshot, dict):
        snapshot = {}
    available = snapshot.get("available") is True
    values = {
        key: snapshot.get(key)
        for key in ("total_mb", "used_mb", "free_mb")
    }
    if any(
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 0
        for value in values.values()
    ):
        available = False
        values = {"total_mb": 0, "used_mb": 0, "free_mb": 0}
    if available and values["total_mb"] <= 0:
        available = False
    total = values["total_mb"]
    used = values["used_mb"]
    return {
        "available": available,
        **values,
        "used_ratio": (used / total) if total else 0.0,
    }


def adjust_llm(
    monitor: ResourceMonitor,
    model_path: str | None = None,
    gpu_offload_supported: bool | None = None,
    offload_mode: str = "auto",
    downshift_from_layers: int | None = None,
) -> dict:
    """
    LLMロード前の実空き容量・モデルサイズ・総層数から候補を決める。
    """
    supported = (
        _supports_gpu_offload()
        if gpu_offload_supported is None else bool(gpu_offload_supported)
    )
    model_mb = 0
    if model_path:
        try:
            model_mb = int(os.path.getsize(model_path) / (1024 ** 2))
        except OSError:
            pass
    total_layers = (
        read_gguf_total_layers(model_path)
        if model_path and os.path.exists(model_path) else None
    )
    try:
        snap = _normalize_snapshot(monitor.snapshot())
    except Exception:
        snap = _normalize_snapshot(None)
    reserve_mb = startup_reserve_mb(snap["total_mb"])
    result = select_llm_offload(
        free_mb=snap["free_mb"],
        model_mb=model_mb,
        reserve_mb=reserve_mb,
        total_layers=total_layers,
        gpu_offload_supported=supported,
        gpu_memory_available=snap["available"],
        offload_mode=offload_mode,
        downshift_from_layers=downshift_from_layers,
    )
    result.update({
        "snapshot": snap,
        "model_mb": model_mb,
        "startup_reserve_mb": reserve_mb,
        "total_layers": total_layers,
    })
    print(
        f"[GPU] backend_offload_supported={supported} "
        f"total_layers={total_layers if total_layers is not None else 'unknown'} "
        f"mode={result['offload_mode']} requested_layers={result['n_gpu_layers']} "
        f"reason={result['reason']}"
    )
    print(
        f"[VRAM] before_llm total={snap['total_mb']}MB used={snap['used_mb']}MB "
        f"free={snap['free_mb']}MB model={model_mb}MB "
        f"startup_reserve={reserve_mb}MB full_required={result['required_mb']}MB"
    )
    for candidate in result["evaluated_candidates"]:
        print(
            f"[VRAM] candidate={candidate['reason']} "
            f"layers={candidate['n_gpu_layers']} "
            f"required={candidate['required_mb']}MB "
            f"eligible={candidate['eligible']}"
        )
    return result


def adjust_inference(
    monitor:       ResourceMonitor,
    default_max:   int,
    delta_gpu_pct: Optional[float] = None,
) -> dict:
    """
    LLM推論直前: 実空き容量を基準に生成量を制限する。
    CPU推論時(n_gpu_layers=0)はGPU残量を理由に遮断しない。
    """
    snap = monitor.snapshot()
    gpu_inference = getattr(monitor, "llm_uses_gpu", True)
    reserve = hard_reserve_mb(snap["total_mb"])

    if not gpu_inference or not snap["available"]:
        max_t = default_max
    elif snap["free_mb"] < reserve:
        print(
            f"[VRAM] inference blocked: used={snap['used_mb']}MB "
            f"free={snap['free_mb']}MB reserve={reserve}MB"
        )
        return {
            "ok": False, "max_tokens": 0, "fallback": True,
            "reason": f"free<{reserve}mb",
            "reason_kind": "vram_hard_limit",
            "requires_relocation": True,
        }
    elif snap["free_mb"] < 1024:
        max_t = max(256, default_max // 4)
    elif snap["free_mb"] < INFERENCE_SOFT_LIMIT_MB:
        max_t = max(256, default_max // 2)
    else:
        max_t = default_max

    # ③ Whisper GPU 突入スパイク検知
    if delta_gpu_pct is not None and delta_gpu_pct > 10:
        max_t = max(256, max_t // 2)
        logger.debug(
            f"[Guard][infer] delta_gpu={delta_gpu_pct:.1f}% spike → max_tokens halved: {max_t}"
        )

    # 資源保護のための縮小が、利用者が指定した上限を超えてはならない。
    max_t = min(default_max, max_t)
    fallback = max_t < default_max
    print(
        f"[VRAM] inference allowed: used={snap['used_mb']}MB "
        f"free={snap['free_mb']}MB reserve={reserve}MB "
        f"max_tokens={max_t}"
    )
    return {
        "ok": True,
        "max_tokens": max_t,
        "fallback": fallback,
        "reason": "ok",
        "reason_kind": "ok",
        "requires_relocation": False,
    }


# ═══════════════════════════════════════════════════════════════
# 1-D  WhisperController  （唯一の状態機械）
# ═══════════════════════════════════════════════════════════════
class WhisperController:
    """
    Whisper の GPU/CPU 切替専用の状態機械（2状態: "gpu" | "cpu"）。
    gpu_recovering は廃止。「状態」ではなく「Δgpu_pct（変化量）」で干渉を検知する。
    delta_gpu_pct は adjust_inference() に渡し、急増時に LLM を追加制限する（③）。
    """

    GPU_FALLBACK_PCT = 88   # GPU% がこれを超えたら → CPU
    GPU_RECOVERY_PCT = 70   # GPU% がこれを下回ったら → GPU（ヒステリシス）

    def __init__(self) -> None:
        self.state:          str   = "cpu"
        self.gpu_available:  bool  = False
        self._prev_gpu_pct:  float = 0.0
        self.delta_gpu_pct:  float = 0.0   # 非判断 — adjust_inference に渡す
        self._delta_lock = threading.Lock()

    def update(self, monitor: ResourceMonitor) -> str:
        """
        呼び出すたびに state を更新して現在の device 文字列を返す。
        副作用: self.delta_gpu_pct を更新（呼び出し元が adjust_inference に渡す）。
        """
        with self._delta_lock:
            self.delta_gpu_pct = monitor.gpu_pct - self._prev_gpu_pct
            self._prev_gpu_pct = monitor.gpu_pct

        if not self.gpu_available:
            self.state = "cpu"
            return self.state

        if self.state == "gpu" and monitor.gpu_pct > self.GPU_FALLBACK_PCT:
            self.state = "cpu"
            logger.debug(f"[Whisper] GPU→CPU: gpu_pct={monitor.gpu_pct:.1f}%")
        elif self.state == "cpu" and monitor.gpu_pct < self.GPU_RECOVERY_PCT:
            self.state = "gpu"
            logger.debug(f"[Whisper] CPU→GPU: gpu_pct={monitor.gpu_pct:.1f}%")

        return self.state

    def uses_gpu(self) -> bool:
        return self.gpu_available and self.state == "gpu"

    def consume_delta_gpu_pct(self) -> float:
        """直近のGPU使用率変化を1回だけ返し、再利用を防ぐ。"""
        with self._delta_lock:
            delta = self.delta_gpu_pct
            self.delta_gpu_pct = 0.0
            return delta


# ═══════════════════════════════════════════════════════════════
# 1-E  WhisperPool  （Storage + WhisperController を使用）
# ═══════════════════════════════════════════════════════════════
class WhisperPool:
    """
    モデル参照を保持するだけ。GPU/CPU の判断は WhisperController に委譲。
    LLM hot reload中は新規転写を止め、進行中転写との排他を提供する。
    """

    def __init__(self) -> None:
        self._gpu_model = None
        self._cpu_model = None
        self._ctrl      = WhisperController()
        self.loaded_profile = "not_loaded"
        self.active_model_name = "unknown"
        self._transcribe_lock = threading.Lock()
        self._reload_pause = threading.Event()

    def load(self, monitor: ResourceMonitor, mode: str = "auto") -> None:
        """
        起動時に1回だけ呼ぶ。
        - CPU 版（small）は無条件でロード（VRAM 消費なし）
        - autoは空き4096MB以上でGPU medium、2048MB以上でGPU small
        - 手動GPU指定も安全閾値を満たさなければCPU smallへフォールバック
        - GPU ロード失敗時は例外を捕捉して CPU 版のみで運用
        """
        with self._transcribe_lock:
            self._load_unlocked(monitor, mode)

    def _load_unlocked(self, monitor: ResourceMonitor, mode: str) -> None:
        import whisper as _whisper

        self._cpu_model = _whisper.load_model("small", device="cpu")
        self.loaded_profile = "cpu_small"
        self.active_model_name = "small"
        print("[Whisper] CPU small loaded")

        snap = monitor.snapshot()
        requested_mode = normalize_whisper_mode(mode)
        selected, reason = select_whisper_profile(requested_mode, snap)
        print(
            f"[Whisper] requested_mode={requested_mode} selected={selected} "
            f"free={snap['free_mb']}MB reason={reason}"
        )
        if selected.startswith("gpu_"):
            model_name = selected.removeprefix("gpu_")
            try:
                self._gpu_model = _whisper.load_model(model_name, device="cuda")
                post = monitor.snapshot()
                if post["available"] and post["free_mb"] < hard_reserve_mb(post["total_mb"]):
                    self._release_gpu_model()
                    print(
                        f"[Whisper] GPU {model_name} released: free={post['free_mb']}MB "
                        f"reserve={hard_reserve_mb(post['total_mb'])}MB"
                    )
                    return
                self._ctrl.gpu_available = True
                self._ctrl.state = "gpu"
                self.loaded_profile = selected
                self.active_model_name = model_name
                print(
                    f"[Whisper] GPU {model_name} loaded: free_before={snap['free_mb']}MB "
                    f"free_after={post['free_mb']}MB"
                )
            except Exception as exc:
                self._release_gpu_model()
                print(f"[Whisper] GPU {model_name} failed; using CPU small: {exc}")
        else:
            print(
                f"[Whisper] fallback=cpu_small reason={reason}; using CPU small"
            )

    def _release_gpu_model(self) -> None:
        self._gpu_model = None
        self._ctrl.gpu_available = False
        self._ctrl.state = "cpu"
        self.loaded_profile = "cpu_small"
        self.active_model_name = "small"
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def get_model(self, monitor: ResourceMonitor) -> tuple:
        """
        推論ごとに WhisperController.update() で device を決定（ヒステリシス込み）。
        戻値: (model, is_gpu)
        delta_gpu_pct は self._ctrl.delta_gpu_pct から取得（呼び出し元が adjust_inference に渡す）。
        """
        state   = self._ctrl.update(monitor)
        use_gpu = (self._ctrl.uses_gpu() and self._gpu_model is not None)
        self.active_model_name = (
            self.loaded_profile.removeprefix("gpu_") if use_gpu else "small"
        )
        logger.debug(
            f"[WhisperPool] state={state} use_gpu={use_gpu} "
            f"delta_gpu={self._ctrl.delta_gpu_pct:.1f}%"
        )
        return (self._gpu_model if use_gpu else self._cpu_model), use_gpu

    def request_reload_pause(self) -> None:
        """新規転写を止める。進行中転写の終了待ちはworker側で行う。"""
        self._reload_pause.set()

    def begin_llm_reload(
        self,
        timeout: float = 30.0,
        closing_event: threading.Event | None = None,
    ) -> bool:
        """進行中転写の終了を有限時間待ち、reload排他を取得する。"""
        self._reload_pause.set()
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if closing_event is not None and closing_event.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if self._transcribe_lock.acquire(timeout=min(0.1, remaining)):
                return True

    def end_llm_reload(self) -> None:
        """workerが保持した排他を解放し、新規転写を再開する。"""
        try:
            self._transcribe_lock.release()
        except RuntimeError:
            pass
        self._reload_pause.clear()

    def cancel_reload_pause(self) -> None:
        self._reload_pause.clear()

    def transcribe_guarded(self, monitor: ResourceMonitor, audio, **kwargs):
        """reload中は開始せず、成功時だけ(result, is_gpu)を返す。"""
        if self._reload_pause.is_set():
            return None
        with self._transcribe_lock:
            if self._reload_pause.is_set():
                return None
            model, on_gpu = self.get_model(monitor)
            if model is None:
                return None
            kwargs["fp16"] = on_gpu
            return model.transcribe(audio, **kwargs), on_gpu

    def consume_delta_gpu_pct(self) -> float:
        return self._ctrl.consume_delta_gpu_pct()

    def status_label(self) -> str:
        if self._ctrl.uses_gpu() and self._gpu_model is not None:
            return f"GPU {self.loaded_profile.removeprefix('gpu_')}"
        if self._cpu_model is not None:
            return "CPU small"
        return "未読込"
