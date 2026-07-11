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

import logging
import subprocess
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


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

        self._init_pynvml()
        threading.Thread(target=self._loop, daemon=True).start()

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

    # ── ポーリングループ ────────────────────────────────
    def _loop(self) -> None:
        alpha = self.EMA_ALPHA
        while True:
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
            time.sleep(self.POLL_SEC)


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
def adjust_llm(monitor: ResourceMonitor) -> dict:
    """
    LLM ロード前: VRAM 使用率だけを見て n_gpu_layers を決定。
    GPU% は参考ログのみ。VRAM = 唯一の判断軸。
    fallback=True / False 両ケースがログに出ることを保証。
    """
    if monitor.vram_total_mb == 0:
        logger.debug("[Guard][llm_init] no GPU → n_gpu_layers=-1 (CPU fallback will be handled by llama.cpp)")
        return {"n_gpu_layers": -1, "fallback": False, "reason": "no_gpu"}

    ratio = monitor.vram_instant_mb / monitor.vram_total_mb

    if ratio > 0.85:
        result = {"n_gpu_layers": 0, "fallback": True,  "reason": f"ratio={ratio:.2f}>0.85"}
    else:
        result = {"n_gpu_layers": -1, "fallback": False, "reason": f"ratio={ratio:.2f}"}

    logger.debug(
        f"[Guard][llm_init] vram={monitor.vram_instant_mb:.0f}mb ratio={ratio:.2f} "
        f"→ {result} | gpu_pct={monitor.gpu_pct:.1f}% (non-decision metric)"
    )
    return result


def adjust_inference(
    monitor:       ResourceMonitor,
    default_max:   int,
    delta_gpu_pct: Optional[float] = None,
) -> dict:
    """
    LLM 推論直前: max_tokens を VRAM 使用量に応じて削減。
    「現在値を見て対応」するだけ。予測・予約なし。
    fallback=True / False 両ケースがログに出ることを保証。

    ② 先読み係数: VRAM 帯に応じた +500/+800/+1200MB バッファを加算して判断。
    ③ delta_gpu_pct が +10 以上 → Whisper GPU 突入スパイク → max_tokens 50% 追加削減。
    """
    raw_used = monitor.vram_instant_mb

    # ② バッファをレンジ化（KV cache 先読み係数）
    if   raw_used < 4000: buffer = 500
    elif raw_used < 6000: buffer = 800
    else:                 buffer = 1200
    virtual_used = raw_used + buffer

    # 実行中止判定
    if virtual_used > 7000:
        logger.debug(f"[Guard][infer] virtual_used={virtual_used:.0f}mb → BLOCK")
        return {"ok": False, "max_tokens": 0, "fallback": True, "reason": "vram>7000"}

    # max_tokens 段階削減
    if   virtual_used > 6000: max_t = max(256, default_max // 4)
    elif virtual_used > 4500: max_t = max(256, default_max // 2)
    else:                     max_t = default_max

    # ③ Whisper GPU 突入スパイク検知
    if delta_gpu_pct is not None and delta_gpu_pct > 10:
        max_t = max(256, max_t // 2)
        logger.debug(
            f"[Guard][infer] delta_gpu={delta_gpu_pct:.1f}% spike → max_tokens halved: {max_t}"
        )

    fallback = max_t < default_max
    logger.debug(
        f"[Guard][infer] raw={raw_used:.0f}mb virtual={virtual_used:.0f}mb buffer={buffer} "
        f"max_tokens={default_max}→{max_t} fallback={fallback} "
        f"| gpu_pct={monitor.gpu_pct:.1f}% (non-decision metric)"
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
        self._prev_gpu_pct:  float = 0.0
        self.delta_gpu_pct:  float = 0.0   # 非判断 — adjust_inference に渡す

    def update(self, monitor: ResourceMonitor) -> str:
        """
        呼び出すたびに state を更新して現在の device 文字列を返す。
        副作用: self.delta_gpu_pct を更新（呼び出し元が adjust_inference に渡す）。
        """
        self.delta_gpu_pct = monitor.gpu_pct - self._prev_gpu_pct
        self._prev_gpu_pct = monitor.gpu_pct

        if self.state == "gpu" and monitor.gpu_pct > self.GPU_FALLBACK_PCT:
            self.state = "cpu"
            logger.debug(f"[Whisper] GPU→CPU: gpu_pct={monitor.gpu_pct:.1f}%")
        elif self.state == "cpu" and monitor.gpu_pct < self.GPU_RECOVERY_PCT:
            self.state = "gpu"
            logger.debug(f"[Whisper] CPU→GPU: gpu_pct={monitor.gpu_pct:.1f}%")

        return self.state

    def uses_gpu(self) -> bool:
        return self.state == "gpu"


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

    def load(self, monitor: ResourceMonitor) -> None:
        """
        起動時に1回だけ呼ぶ。
        - CPU 版（small）は無条件でロード（VRAM 消費なし）
        - GPU 版（medium）は VRAM 使用率 < 70% の場合のみロード
        - GPU ロード失敗時は例外を捕捉して CPU 版のみで運用
        """
        import whisper as _whisper

        self._cpu_model = _whisper.load_model("small", device="cpu")
        logger.debug("[WhisperPool] CPU版(small)ロード完了")

        # VRAM を唯一の判断軸として使用（GPU% は参考ログのみ）
        vram_ok = (
            monitor.vram_total_mb == 0
            or monitor.vram_instant_mb / monitor.vram_total_mb < 0.70
        )
        if vram_ok:
            try:
                self._gpu_model  = _whisper.load_model("medium", device="cuda")
                self._ctrl.state = "gpu"
                logger.debug("[WhisperPool] GPU版(medium)ロード完了")
            except Exception as exc:
                logger.warning(f"[WhisperPool] GPU版ロード失敗 → CPU版のみ: {exc}")
        else:
            vram_ratio = monitor.vram_instant_mb / monitor.vram_total_mb
            logger.debug(
                f"[WhisperPool] VRAM使用率高({vram_ratio:.2f})のためCPU版のみ "
                f"| gpu_pct={monitor.gpu_pct:.1f}% (non-decision metric)"
            )

    def get_model(self, monitor: ResourceMonitor) -> tuple:
        """
        推論ごとに WhisperController.update() で device を決定（ヒステリシス込み）。
        戻値: (model, is_gpu)
        delta_gpu_pct は self._ctrl.delta_gpu_pct から取得（呼び出し元が adjust_inference に渡す）。
        """
        state   = self._ctrl.update(monitor)
        use_gpu = (self._ctrl.uses_gpu() and self._gpu_model is not None)
        logger.debug(
            f"[WhisperPool] state={state} use_gpu={use_gpu} "
            f"delta_gpu={self._ctrl.delta_gpu_pct:.1f}%"
        )
        return (self._gpu_model if use_gpu else self._cpu_model), use_gpu
