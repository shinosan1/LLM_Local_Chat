# -*- coding: utf-8 -*-
"""
resource_monitor.py  —  VRAM安全フィルタ

設計思想: 「VRAMを管理する」ではなく「VRAMから逃げる」
  - 予約・帳簿・状態管理は持たない。即時の観測値だけで判断する。
  - OOM完全防止は不可能。「クラッシュではなく劣化」で済ませることが目標。
  - 唯一残す状態: Whisper の GPU/CPU ヒステリシス（WhisperController のみ）

既知の限界:
  - pynvml は allocator 後スナップショット（TOCTOU あり）
  - 単一プロセス内のみ有効（他プロセスの VRAM 確保は観測不可）
  - この設計は確率的削減が目的であり、OOM の完全防止は保証しない
"""

import gc
import logging
import os
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
LLM_STARTUP_RESERVE_MB = 1024
LLM_STARTUP_RESERVE_RATIO = 0.12


def hard_reserve_mb(total_mb: int) -> int:
    """GPU全体容量に応じた、推論中に残す最低空き容量。"""
    return max(MIN_HARD_RESERVE_MB, int(total_mb * HARD_RESERVE_RATIO))


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


def adjust_llm(
    monitor: ResourceMonitor,
    model_path: str | None = None,
    gpu_offload_supported: bool | None = None,
) -> dict:
    """
    LLMロード前の実空き容量とモデルサイズからGPUオフロード可否を決める。
    """
    snap = monitor.snapshot()
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
    startup_reserve = max(
        LLM_STARTUP_RESERVE_MB,
        int(snap["total_mb"] * LLM_STARTUP_RESERVE_RATIO),
    )
    required_mb = model_mb + startup_reserve

    if not supported:
        result = {"n_gpu_layers": 0, "fallback": True, "reason": "gpu_offload_unsupported"}
    elif not snap["available"]:
        result = {"n_gpu_layers": 0, "fallback": True, "reason": "gpu_memory_unavailable"}
    elif snap["free_mb"] < required_mb:
        result = {
            "n_gpu_layers": 0,
            "fallback": True,
            "reason": f"free<{required_mb}mb",
        }
    else:
        result = {"n_gpu_layers": -1, "fallback": False, "reason": "gpu_full_offload"}

    result.update({"snapshot": snap, "required_mb": required_mb, "model_mb": model_mb})
    print(
        f"[GPU] backend_offload_supported={supported} "
        f"requested_layers={result['n_gpu_layers']} reason={result['reason']}"
    )
    print(
        f"[VRAM] before_llm total={snap['total_mb']}MB used={snap['used_mb']}MB "
        f"free={snap['free_mb']}MB required={required_mb}MB"
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

    fallback = max_t < default_max
    print(
        f"[VRAM] inference allowed: used={snap['used_mb']}MB "
        f"free={snap['free_mb']}MB reserve={reserve}MB "
        f"max_tokens={max_t}"
    )
    return {"ok": True, "max_tokens": max_t, "fallback": fallback, "reason": "ok"}


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
    transcribe() インターフェースは持たない — get_model() でモデルを取得して直接呼ぶ。
    """

    def __init__(self) -> None:
        self._gpu_model = None
        self._cpu_model = None
        self._ctrl      = WhisperController()
        self.loaded_profile = "not_loaded"
        self.active_model_name = "unknown"

    def load(self, monitor: ResourceMonitor, mode: str = "auto") -> None:
        """
        起動時に1回だけ呼ぶ。
        - CPU 版（small）は無条件でロード（VRAM 消費なし）
        - autoは空き4096MB以上でGPU medium、2048MB以上でGPU small
        - 手動GPU指定も安全閾値を満たさなければCPU smallへフォールバック
        - GPU ロード失敗時は例外を捕捉して CPU 版のみで運用
        """
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

    def consume_delta_gpu_pct(self) -> float:
        return self._ctrl.consume_delta_gpu_pct()

    def status_label(self) -> str:
        if self._ctrl.uses_gpu() and self._gpu_model is not None:
            return f"GPU {self.loaded_profile.removeprefix('gpu_')}"
        if self._cpu_model is not None:
            return "CPU small"
        return "未読込"
