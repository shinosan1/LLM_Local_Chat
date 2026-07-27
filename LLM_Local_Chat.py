# -*- coding: utf-8 -*-
# =============================================================
#  LLM Local Chat
# =============================================================
#
#  【修正履歴】
#  - WHISPER_NOISE から「ありがとうございました」「おやすみなさい」を除去
#    → 実発話が誤ってノイズ判定されていた
#  - WHISPER_NOISE_PARTIAL（部分一致）を追加
#    → 「ご視聴ありがとうございました」等の hallucination のみ除外
#  - VoiceRecognizer: 直前と同一テキストの連続認識をスキップ
#  - VoiceRecognizer: _loop内で_last_textを管理し誤爆を防止
#  - _on_whisper_ready(): 完了後に _update_status() / _mic_idle() を呼ぶ
#    → Whisperロード完了後もマイク状態がUIに反映されなかった
#  - _on_whisper_ready(): Whisperロード失敗時にステータスバーへエラー表示
#  - 要約処理も LLMService 経由で直列実行
#  - _save_now(): _refresh_chat_list() を root.after() 経由に変更（スレッド安全）
#  - _stop_voice() → _stop_all() に刷新
#    → LLM生成・TTS・マイクをすべて即時停止
#    → llm.reset() を別スレッドで強制実行してストリーミングを中断
#    → tts.stop_all() で engine.stop() + endLoop() を呼び TTS を即時停止
#  - TTSWorker: _engine_ref を保持して stop_all() から直接 engine を停止
#  - TTSWorker: stop_all() 直後にキューから取り出したテキストをスキップ
#  - _llm_worker(): _llm_abort フラグをチェックしてストリーミングを中断
#  - _on_llm_done(): _llm_abort=True の場合は TTS・履歴保存をスキップ
#  - _on_close(): root.destroy() を after(200) 経由にして TclError を防止
#  - _update_status(): LLMロード中でもマイク状態をステータスバーに表示
#  - Whisper: small/cuda → medium/cuda に変更（精度向上）
#  - Whisper: CUDA失敗時に CPU へ自動フォールバック
#  - Whisper: fp16 をデバイスに応じて自動判定（GPU=True / CPU=False）
#  - Whisper: initial_prompt を追加して日本語認識精度を改善
#  - Whisper: beam_size=1 / temperature=0.0 で速度優先設定
#  - Whisper: no_speech_threshold=0.8 / logprob_threshold=-1.5 に調整
#  - アバター初期位置を +1550+500 に変更（メインウィンドウと重ならない）
#  - 停止ボタンの fg を mic_on カラーに変更（視認性向上）
#  - 入力エリア下に免責文言を追加
#  - VAD診断ログを削除（デバッグ用途のため）
#  - TTSWorker: 2回目以降のTTSが再生されない問題を修正
#    → _run() の try ブロック内で continue するとfinallyが先に実行され
#       task_done() が二重呼び出しになりキューが詰まっていた
#    → 停止フラグのチェックを try の外に移動して修正（最小変更）
#  - VoiceRecognizer: TTS中のVAD誤検知対策を「閾値10倍」→「完全スキップ」に変更
#    → 開放型イヤホン（Float Run）環境でTTS音声RMS=2530が閾値1500を超えてしまい
#       TTS読み上げ音声をユーザー発話として誤認識していた
#    → _tts_active=True の間は発話開始待ちループでチャンクを読み捨て
#       録音中にTTSが始まった場合も録音データを破棄してWhisper処理をスキップ
#
#  【維持した仕様】
#  ・左ペインにチャット一覧（Listbox）
#  ・チャット削除（右クリックコンテキストメニュー）
#  ・検索（タイトル部分一致フィルター）
#  ・要約メモリ（会話の要約を JSON に保持しプロンプトへ注入）
#  ・アバター瞬きアニメーション（BLINK_DURATION / INTERVAL）
#  ・アバター口パクアニメーション
#  ・PyAudio + RMS-VAD + Whisper 常駐音声認識
#  ・Windows SAPI5 TTS（アバター連動）
#  ・ストリーミング表示（create_chat_completion stream=True）
#  ・タイトル自動生成（最初のメッセージ先頭20文字）
#  ・ゲストモード（保存しない）
#  ・設定ダイアログ（モデルパス / n_ctx / max_tokens /
#                   temperature / VAD 感度）
#  ・テキストとして保存
#  ・ダークテーマ
# =============================================================

import os
import sys
import json
import gc
import threading
import time
import random

import tkinter as tk
from tkinter import (
    scrolledtext, Menu, BooleanVar,
    messagebox, filedialog, simpledialog,
)

from llama_cpp import Llama
from PIL import Image, ImageTk
# whisper は resource_monitor.WhisperPool 内で import する（MODIFIED）

# ADDED: VRAM安全フィルタ（resource_monitor.py）
from app_composition import AppDeps, create_app_deps
from atomic_io import atomic_write_json
from audio_workers import (
    DEFAULT_TTS_RATE,
    DEFAULT_VAD_RMS,
    MAX_VAD_RMS,
    MAX_TTS_RATE,
    MIN_VAD_RMS,
    MIN_TTS_RATE,
    TTSWorker,
    VoiceRecognizer,
    normalize_tts_rate,
    normalize_vad_threshold,
    should_load_whisper,
    should_queue_initial_greeting,
)
from integrations import IntegrationBridge
from kakeibo_amount import normalize_manual_amount_input
from kakeibo_confirmation import can_submit_kakeibo_candidate
from prompt_builder import KAKEIBO_EXPENSE_CATS, KAKEIBO_INCOME_CATS
from resource_monitor import adjust_llm, normalize_whisper_mode

# ─────────────────────────────────────────────
#  Windows UTF-8 出力設定（起動直後に実行）
# ─────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════
#  ■ 基本設定
# ═══════════════════════════════════════════════════════
APP_DIR = os.path.dirname(os.path.abspath(__file__))


def app_path(*parts: str) -> str:
    """カレントディレクトリに依存しないアプリ内パスを返す。"""
    return os.path.join(APP_DIR, *parts)


def resolve_model_path(path: str) -> str:
    """絶対パスは維持し、相対モデルパスだけアプリ基準で解決する。"""
    return path if os.path.isabs(path) else app_path(path)


DEFAULT_MODEL_PATH = r"models\gemma-3-4b-it-q4_k_m.gguf"
AVATAR_DEFAULT     = app_path("avatars", "default_avatar.png")
AVATAR_SPEAKING    = app_path("avatars", "speaking_avatar.png")
AVATAR_BLINK       = app_path("avatars", "blink_avatar.png")
AVATAR_BLINK_SPK   = app_path("avatars", "blink_speaking_avatar.png")
LOG_DIR            = app_path("chat_logs")
SETTINGS_FILE      = app_path("chat_settings.json")

SYSTEM_PROMPT = (
    "あなたは「シロ」という名前の、親しみやすく丁寧な日本語を話すアシスタントです。"
    "ユーザーの質問や雑談に対して、具体的な内容や役立つ情報を添えて2〜3文で返答してください。"
    "「承知いたしました」などの一言だけで終わらせず、会話を広げるように心がけてください。"
)

DEFAULT_N_CTX      = 8192
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMP       = 0.7
DEFAULT_N_THREADS_BATCH = 12
DEFAULT_N_BATCH = 1024
DEFAULT_N_UBATCH = 512
DEFAULT_FLASH_ATTN = True
DEFAULT_OFFLOAD_KQV = True
DEFAULT_USE_MMAP = True

HISTORY_BUDGET_RATIO = 0.60
SYSTEM_BUF_TOKENS    = 256
SUMMARY_THRESHOLD    = 4       # 何ターンごとに要約を更新するか

# ── アバター ─────────────────────────────────
MOUTH_INTERVAL_MS  = 140
AVATAR_SCALE       = 0.5
BLINK_DURATION_MS  = 150
BLINK_INTERVAL_MIN = 2000
BLINK_INTERVAL_MAX = 6000

# ── カラーテーマ（ChatGPT 風ダーク）──────────
C = {
    "bg_main":     "#212121",
    "bg_side":     "#171717",
    "bg_input":    "#2F2F2F",
    "bg_selected": "#3A3A3A",
    "fg_main":     "#ECECEC",
    "fg_sub":      "#9B9B9B",
    "accent":      "#10A37F",
    "mic_on":      "#EF4444",
    "mic_off":     "#666666",
    "mic_active":  "#FBBF24",
    "status_bg":   "#0D0D0D",
    "guest_tag":   "#F59E0B",
    "error_fg":    "#FF6B6B",
    "delete_fg":   "#EF4444",
    "divider":     "#3A3A3A",
}
_FONT_JP   = "Meiryo"          if sys.platform == "win32" else "Noto Sans CJK JP"
_FONT_ICON = "Segoe UI Emoji"  if sys.platform == "win32" else "Noto Sans CJK JP"
FONT_BOLD  = (_FONT_JP, 10, "bold")
FONT_TITLE = (_FONT_JP, 12, "bold")
FONT_CHAT  = (_FONT_JP, 11)
FONT_SMALL = (_FONT_JP,  9)
WHISPER_MODE_LABELS = {
    "auto": "自動（推奨）",
    "gpu_small": "GPU small（高速）",
    "gpu_medium": "GPU medium（高精度・高VRAM）",
    "cpu_small": "CPU small（省VRAM）",
}


def _valid_positive_int(value, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return fallback
    return value


def _valid_bool(value, fallback: bool) -> bool:
    if not isinstance(value, bool):
        return fallback
    return value


# ═══════════════════════════════════════════════════════
#  ■ 設定 I/O
# ═══════════════════════════════════════════════════════
def load_settings() -> dict:
    defaults = dict(
        model_path    = DEFAULT_MODEL_PATH,
        n_ctx         = DEFAULT_N_CTX,
        max_tokens    = DEFAULT_MAX_TOKENS,
        temperature   = DEFAULT_TEMP,
        tts_enabled   = False,
        tts_rate      = DEFAULT_TTS_RATE,
        mic_enabled   = False,
        whisper_mode  = "auto",
        vad_threshold = DEFAULT_VAD_RMS,
        n_threads_batch = DEFAULT_N_THREADS_BATCH,
        n_batch       = DEFAULT_N_BATCH,
        n_ubatch      = DEFAULT_N_UBATCH,
        flash_attn    = DEFAULT_FLASH_ATTN,
        offload_kqv   = DEFAULT_OFFLOAD_KQV,
        use_mmap      = DEFAULT_USE_MMAP,
    )
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                defaults.update(json.load(f))
        except Exception:
            pass
    defaults["n_threads_batch"] = _valid_positive_int(
        defaults.get("n_threads_batch"), DEFAULT_N_THREADS_BATCH)
    defaults["n_batch"] = _valid_positive_int(
        defaults.get("n_batch"), DEFAULT_N_BATCH)
    defaults["n_ubatch"] = _valid_positive_int(
        defaults.get("n_ubatch"), DEFAULT_N_UBATCH)
    defaults["flash_attn"] = _valid_bool(
        defaults.get("flash_attn"), DEFAULT_FLASH_ATTN)
    defaults["offload_kqv"] = _valid_bool(
        defaults.get("offload_kqv"), DEFAULT_OFFLOAD_KQV)
    defaults["use_mmap"] = _valid_bool(
        defaults.get("use_mmap"), DEFAULT_USE_MMAP)
    defaults["tts_rate"] = normalize_tts_rate(
        defaults.get("tts_rate"), DEFAULT_TTS_RATE)
    defaults["vad_threshold"] = normalize_vad_threshold(
        defaults.get("vad_threshold"), DEFAULT_VAD_RMS)
    defaults["whisper_mode"] = normalize_whisper_mode(
        defaults.get("whisper_mode"))
    return defaults


def save_settings(d: dict) -> None:
    try:
        atomic_write_json(SETTINGS_FILE, d)
    except Exception as e:
        print(f"[設定保存エラー] {e}")


# ═══════════════════════════════════════════════════════
#  ■ LLM ユーティリティ
# ═══════════════════════════════════════════════════════
def init_llm(model_path: str, n_ctx: int, res_monitor, perf_settings: dict | None = None) -> Llama:
    model_path = resolve_model_path(model_path)
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"モデルが見つかりません:\n{model_path}")
    params = adjust_llm(res_monitor, model_path=model_path)
    perf = perf_settings or {}
    base_kwargs = dict(
        model_path   = model_path,
        n_ctx        = n_ctx,
        n_threads    = 8,
        n_gpu_layers = params["n_gpu_layers"],
        n_batch      = 512,
        verbose      = False,
    )
    perf_kwargs = dict(
        n_threads_batch = _valid_positive_int(
            perf.get("n_threads_batch"), DEFAULT_N_THREADS_BATCH),
        n_batch = _valid_positive_int(perf.get("n_batch"), DEFAULT_N_BATCH),
        n_ubatch = _valid_positive_int(
            perf.get("n_ubatch"), DEFAULT_N_UBATCH),
        flash_attn = _valid_bool(perf.get("flash_attn"), DEFAULT_FLASH_ATTN),
        offload_kqv = _valid_bool(
            perf.get("offload_kqv"), DEFAULT_OFFLOAD_KQV),
        use_mmap = _valid_bool(perf.get("use_mmap"), DEFAULT_USE_MMAP),
    )

    last_err = None
    layer_modes = [params["n_gpu_layers"]]
    if params["n_gpu_layers"] == -1:
        layer_modes.append(0)

    before = params["snapshot"]
    for layer_index, layers in enumerate(layer_modes):
        layer_base = {**base_kwargs, "n_gpu_layers": layers}
        attempts = [("perf", {**layer_base, **perf_kwargs})]
        if perf_kwargs["flash_attn"]:
            no_flash = dict(perf_kwargs)
            no_flash["flash_attn"] = False
            attempts.append(("perf_no_flash_attn", {**layer_base, **no_flash}))
        attempts.append(("base", layer_base))

        if layer_index:
            print("[LLM] GPU load failed; retrying once on CPU")
            try:
                import gc
                import torch
                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:
                pass

        for idx, (label, kwargs) in enumerate(attempts[:3], 1):
            try:
                if label != "perf":
                    print(f"[LLM] retry load with {label}")
                llm = Llama(**kwargs)
                res_monitor.llm_uses_gpu = layers == -1
                after = res_monitor.snapshot()
                print(
                    f"[VRAM] after_llm total={after['total_mb']}MB "
                    f"used={after['used_mb']}MB free={after['free_mb']}MB "
                    f"delta={after['used_mb'] - before['used_mb']}MB"
                )
                print(
                    f"[LLM] load complete device={'GPU' if layers == -1 else 'CPU'} "
                    f"requested_layers={layers}"
                )
                return llm
            except Exception as e:
                last_err = e
                shown = {k: kwargs.get(k) for k in (
                    "n_threads", "n_threads_batch", "n_batch", "n_ubatch",
                    "n_gpu_layers", "flash_attn", "offload_kqv", "use_mmap"
                ) if k in kwargs}
                print(
                    f"[LLM] load attempt {idx} failed ({label}): {shown} "
                    f"-> {type(e).__name__}: {e}"
                )
    raise last_err


def count_tokens(llm: Llama, text: str) -> int:
    try:
        return len(llm.tokenize(text.encode("utf-8"), add_bos=False))
    except Exception:
        return max(1, len(text) // 2)


# ═══════════════════════════════════════════════════════
#  ■ 設定ダイアログ
# ═══════════════════════════════════════════════════════
class SettingsDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, cfg: dict):
        super().__init__(parent)
        self.title("生成設定")
        self.configure(bg=C["bg_main"])
        self.resizable(False, False)
        self.result = None
        P = dict(padx=16, pady=8)

        def lbl(row: int, text: str) -> None:
            tk.Label(self, text=text, bg=C["bg_main"], fg=C["fg_main"]
                     ).grid(row=row, column=0, sticky="w", **P)

        def ent(row: int, val, width: int = 14) -> tk.Entry:
            e = tk.Entry(self, width=width,
                         bg=C["bg_input"], fg=C["fg_main"],
                         insertbackground="white", bd=1)
            e.insert(0, str(val))
            e.grid(row=row, column=1, sticky="w", **P)
            return e

        # モデルパス
        lbl(0, "モデルファイル (.gguf):")
        mf = tk.Frame(self, bg=C["bg_main"])
        mf.grid(row=0, column=1, columnspan=2, sticky="ew", **P)
        self.e_model = tk.Entry(
            mf, width=46,
            bg=C["bg_input"], fg=C["fg_main"],
            insertbackground="white", bd=1)
        self.e_model.insert(0, cfg.get("model_path", DEFAULT_MODEL_PATH))
        self.e_model.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(
            mf, text="参照…",
            bg=C["accent"], fg="white", bd=0, padx=6,
            command=self._browse,
        ).pack(side=tk.LEFT, padx=(6, 0))

        # コンテキスト長
        lbl(1, "コンテキスト長 (n_ctx):")
        self.e_ctx = ent(1, cfg.get("n_ctx", DEFAULT_N_CTX))
        tk.Label(self, text="推奨: 8192",
                 bg=C["bg_main"], fg=C["fg_sub"],
                 font=FONT_SMALL).grid(row=1, column=2, sticky="w", **P)

        # 最大返答トークン
        lbl(2, "最大返答トークン数:")
        self.e_tok = ent(2, cfg.get("max_tokens", DEFAULT_MAX_TOKENS))
        tk.Label(self, text="推奨: 256〜512",
                 bg=C["bg_main"], fg=C["fg_sub"],
                 font=FONT_SMALL).grid(row=2, column=2, sticky="w", **P)

        # 会話の自由度
        lbl(3, "会話の自由度 (0.0 – 2.0):")
        self.e_temp = ent(3, cfg.get("temperature", DEFAULT_TEMP))

        # VAD 感度
        lbl(4, "音声検出感度 (RMS 閾値):")
        self.e_vad = ent(4, cfg.get("vad_threshold", DEFAULT_VAD_RMS))
        tk.Label(self, text="小さいほど高感度",
                 bg=C["bg_main"], fg=C["fg_sub"],
                 font=FONT_SMALL).grid(row=4, column=2, sticky="w", **P)

        # TTS 読み上げ速度
        lbl(5, "読み上げ速度 (-10 ～ 10):")
        self.e_tts_rate = ent(5, cfg.get("tts_rate", DEFAULT_TTS_RATE))
        tk.Label(
            self,
            text="0: 標準 / +2: 速め（次の読み上げから反映）",
            bg=C["bg_main"], fg=C["fg_sub"], font=FONT_SMALL,
        ).grid(row=5, column=2, sticky="w", **P)

        # Whisper 実行モード（起動時のみ反映）
        lbl(6, "Whisper実行モード:")
        current_mode = normalize_whisper_mode(cfg.get("whisper_mode"))
        self.v_whisper_mode = tk.StringVar(
            value=WHISPER_MODE_LABELS[current_mode])
        whisper_menu = tk.OptionMenu(
            self, self.v_whisper_mode, *WHISPER_MODE_LABELS.values())
        whisper_menu.config(
            bg=C["bg_input"], fg=C["fg_main"],
            activebackground=C["accent"], bd=0, width=25,
        )
        whisper_menu["menu"].config(bg=C["bg_input"], fg=C["fg_main"])
        whisper_menu.grid(row=6, column=1, sticky="w", **P)
        tk.Label(
            self, text="変更は次回起動時に反映",
            bg=C["bg_main"], fg=C["fg_sub"], font=FONT_SMALL,
        ).grid(row=6, column=2, sticky="w", **P)

        # 起動時マイクON/OFF
        self.v_mic = BooleanVar(value=cfg.get("mic_enabled", False))
        tk.Checkbutton(
            self, text="起動時にマイクを有効にする",
            variable=self.v_mic,
            bg=C["bg_main"], fg=C["fg_main"],
            selectcolor=C["bg_input"], activebackground=C["bg_main"],
        ).grid(row=7, column=0, columnspan=3, sticky="w", padx=16, pady=4)

        # 起動時TTS ON/OFF
        self.v_tts = BooleanVar(value=cfg.get("tts_enabled", False))
        tk.Checkbutton(
            self, text="起動時にTTS読み上げを有効にする",
            variable=self.v_tts,
            bg=C["bg_main"], fg=C["fg_main"],
            selectcolor=C["bg_input"], activebackground=C["bg_main"],
        ).grid(row=8, column=0, columnspan=3, sticky="w", padx=16, pady=4)

        tk.Label(
            self,
            text="※ 体感速度はWindowsの音声エンジンによって異なります",
            bg=C["bg_main"], fg=C["fg_sub"], font=FONT_SMALL,
        ).grid(row=9, column=0, columnspan=3, sticky="w", padx=16, pady=(2, 4))

        # ボタン
        bf = tk.Frame(self, bg=C["bg_main"])
        bf.grid(row=10, column=0, columnspan=3, pady=16)
        tk.Button(
            bf, text="保存して適用",
            bg=C["accent"], fg="white", width=14, bd=0,
            command=self._save,
        ).pack(side=tk.LEFT, padx=8)
        tk.Button(
            bf, text="キャンセル",
            bg=C["bg_input"], fg=C["fg_main"], width=10, bd=0,
            command=self.destroy,
        ).pack(side=tk.LEFT, padx=8)

        self.grab_set()

    def _browse(self) -> None:
        p = filedialog.askopenfilename(
            title="GGUF モデルを選択",
            filetypes=[("GGUF model", "*.gguf"), ("All files", "*.*")])
        if p:
            self.e_model.delete(0, tk.END)
            self.e_model.insert(0, p)

    def _save(self) -> None:
        try:
            mp  = self.e_model.get().strip()
            ctx = int(self.e_ctx.get())
            tok = int(self.e_tok.get())
            tmp = float(self.e_temp.get())
            vad = int(self.e_vad.get())
            tts_rate = int(self.e_tts_rate.get())
            if not (0.0 <= tmp <= 2.0):
                raise ValueError("会話の自由度は 0.0〜2.0 の範囲で入力してください")
            if tok < 1 or ctx < 512:
                raise ValueError("トークン数が小さすぎます (n_ctx は 512 以上)")
            if not MIN_TTS_RATE <= tts_rate <= MAX_TTS_RATE:
                raise ValueError("読み上げ速度は -10〜10 の整数で入力してください")
            if not MIN_VAD_RMS <= vad <= MAX_VAD_RMS:
                raise ValueError(
                    f"音声検出の感度は {MIN_VAD_RMS}〜{MAX_VAD_RMS} "
                    "の整数で入力してください")
            whisper_mode = next(
                key for key, label in WHISPER_MODE_LABELS.items()
                if label == self.v_whisper_mode.get()
            )
            self.result = dict(
                model_path=mp, n_ctx=ctx, max_tokens=tok,
                temperature=tmp, vad_threshold=vad,
                tts_rate=tts_rate,
                whisper_mode=whisper_mode,
                mic_enabled=self.v_mic.get(),
                tts_enabled=self.v_tts.get())
            self.destroy()
        except ValueError as e:
            messagebox.showerror("入力エラー", str(e), parent=self)
        except Exception:
            messagebox.showerror("入力エラー", "数値が無効です", parent=self)


# ═══════════════════════════════════════════════════════
#  ■ 家計簿 確認ダイアログ
# ═══════════════════════════════════════════════════════
class KakeiboConfirmDialog(tk.Toplevel):
    """家計簿候補の編集可能な確認画面。

    LLM候補・原文由来amountはあくまで初期候補であり、ユーザーが確認チェックを
    有効にするまで登録ボタンは無効のまま。type/category/amount/date/store/memo
    のいずれかを編集すると確認チェックは自動的に解除される(初期化中のプログラム
    的な値設定はこの対象に含めない)。
    """

    def __init__(self, parent: tk.Tk, candidate: dict, user_text: str):
        super().__init__(parent)
        self.title("家計簿へ登録")
        self.configure(bg=C["bg_main"])
        self.resizable(False, False)
        self.result: dict | None = None
        self._initializing = True
        P = dict(padx=16, pady=6)

        def lbl(row: int, text: str) -> None:
            tk.Label(self, text=text, bg=C["bg_main"], fg=C["fg_main"]
                     ).grid(row=row, column=0, sticky="w", **P)

        lbl(0, "認識・入力内容:")
        self.original_text = tk.Text(
            self, width=46, height=4, wrap="word",
            bg=C["bg_input"], fg=C["fg_sub"], bd=1)
        self.original_text.insert("1.0", user_text)
        self.original_text.config(state="disabled")
        self.original_text.grid(row=0, column=1, columnspan=2, sticky="ew", **P)

        lbl(1, "日付 (YYYY-MM-DD):")
        self.v_date = tk.StringVar(value=candidate.get("date") or "")
        tk.Entry(
            self, width=16, textvariable=self.v_date,
            bg=C["bg_input"], fg=C["fg_main"], bd=1,
        ).grid(row=1, column=1, sticky="w", **P)

        lbl(2, "金額 (円):")
        amount = candidate.get("amount")
        self.v_amount = tk.StringVar(value=str(amount) if amount is not None else "")
        tk.Entry(
            self, width=16, textvariable=self.v_amount,
            bg=C["bg_input"], fg=C["fg_main"], bd=1,
        ).grid(row=2, column=1, sticky="w", **P)

        lbl(3, "支出・収入:")
        self.v_type = tk.StringVar(value=candidate.get("type") or "")
        type_frame = tk.Frame(self, bg=C["bg_main"])
        type_frame.grid(row=3, column=1, columnspan=2, sticky="w", **P)
        tk.Radiobutton(
            type_frame, text="支出", value="支出", variable=self.v_type,
            bg=C["bg_main"], fg=C["fg_main"], selectcolor=C["bg_input"],
            activebackground=C["bg_main"],
        ).pack(side=tk.LEFT)
        tk.Radiobutton(
            type_frame, text="収入", value="収入", variable=self.v_type,
            bg=C["bg_main"], fg=C["fg_main"], selectcolor=C["bg_input"],
            activebackground=C["bg_main"],
        ).pack(side=tk.LEFT, padx=(12, 0))

        lbl(4, "カテゴリ:")
        self.v_category = tk.StringVar(value=candidate.get("category") or "")
        self.category_menu = tk.OptionMenu(self, self.v_category, "")
        self.category_menu.config(
            bg=C["bg_input"], fg=C["fg_main"], bd=0, width=20)
        self.category_menu.grid(row=4, column=1, sticky="w", **P)

        lbl(5, "取引先・店舗:")
        self.v_store = tk.StringVar(value=candidate.get("store") or "")
        tk.Entry(
            self, width=30, textvariable=self.v_store,
            bg=C["bg_input"], fg=C["fg_main"], bd=1,
        ).grid(row=5, column=1, columnspan=2, sticky="ew", **P)

        lbl(6, "メモ:")
        self.v_memo = tk.StringVar(value=candidate.get("memo") or "")
        tk.Entry(
            self, width=30, textvariable=self.v_memo,
            bg=C["bg_input"], fg=C["fg_main"], bd=1,
        ).grid(row=6, column=1, columnspan=2, sticky="ew", **P)

        self.v_confirmed = BooleanVar(value=False)
        tk.Checkbutton(
            self, text="原文と登録内容を確認しました", variable=self.v_confirmed,
            bg=C["bg_main"], fg=C["fg_main"], selectcolor=C["bg_input"],
            activebackground=C["bg_main"], command=self._update_submit_state,
        ).grid(row=7, column=0, columnspan=3, sticky="w", padx=16, pady=(8, 4))

        bf = tk.Frame(self, bg=C["bg_main"])
        bf.grid(row=8, column=0, columnspan=3, pady=16)
        self.btn_submit = tk.Button(
            bf, text="登録", bg=C["accent"], fg="white", width=12, bd=0,
            command=self._submit,
        )
        self.btn_submit.pack(side=tk.LEFT, padx=8)
        tk.Button(
            bf, text="キャンセル", bg=C["bg_input"], fg=C["fg_main"], width=10, bd=0,
            command=self.destroy,
        ).pack(side=tk.LEFT, padx=8)

        # 初期化中はcategory一覧の構築・初期値セットが確認チェックを解除しないようにする
        self._refresh_category_options(initial_category=candidate.get("category"))
        self._initializing = False

        self.v_date.trace_add("write", self._on_field_edited)
        self.v_amount.trace_add("write", self._on_field_edited)
        self.v_store.trace_add("write", self._on_field_edited)
        self.v_memo.trace_add("write", self._on_field_edited)
        self.v_category.trace_add("write", self._on_field_edited)
        self.v_type.trace_add("write", self._on_type_changed)

        self._update_submit_state()
        self.grab_set()

    def _refresh_category_options(self, initial_category=None) -> None:
        type_value = self.v_type.get()
        options = (
            KAKEIBO_EXPENSE_CATS if type_value == "支出"
            else KAKEIBO_INCOME_CATS if type_value == "収入"
            else []
        )

        menu = self.category_menu["menu"]
        menu.delete(0, "end")
        for option in options:
            menu.add_command(
                label=option,
                command=lambda value=option: self.v_category.set(value),
            )

        if type_value not in ("支出", "収入"):
            self.v_category.set("")
            self.category_menu.config(state="disabled")
            return

        self.category_menu.config(state="normal")
        if initial_category in options:
            self.v_category.set(initial_category)
        else:
            self.v_category.set("その他支出" if type_value == "支出" else "その他収入")

    def _on_type_changed(self, *_args) -> None:
        was_initializing = self._initializing
        self._refresh_category_options()
        if not was_initializing:
            self.v_confirmed.set(False)
        self._update_submit_state()

    def _on_field_edited(self, *_args) -> None:
        if self._initializing:
            return
        self.v_confirmed.set(False)
        self._update_submit_state()

    def _current_record(self) -> dict:
        return {
            "date": self.v_date.get().strip(),
            "amount": normalize_manual_amount_input(self.v_amount.get()),
            "type": self.v_type.get() or None,
            "category": self.v_category.get() or None,
            "store": self.v_store.get(),
            "memo": self.v_memo.get(),
        }

    def _update_submit_state(self) -> None:
        can_submit = can_submit_kakeibo_candidate(
            self._current_record(), self.v_confirmed.get())
        self.btn_submit.config(state="normal" if can_submit else "disabled")

    def _submit(self) -> None:
        record = self._current_record()
        if not can_submit_kakeibo_candidate(record, self.v_confirmed.get()):
            return
        self.result = record
        self.destroy()


# ═══════════════════════════════════════════════════════
#  ■ アバターウィンドウ（瞬き + 口パク）
# ═══════════════════════════════════════════════════════
class AvatarWindow:
    """
    瞬きアニメーション: ランダム間隔で _do_blink → _end_blink
    口パクアニメーション: start_speaking → _mouth_loop → stop_speaking
    両アニメーションは blinking フラグで干渉を防ぐ
    """
    def __init__(self, root: tk.Tk) -> None:
        self.root    = root
        self.visible = True

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-transparentcolor", "black")
        except tk.TclError:
            pass  # Linux では -transparentcolor 非対応
        self.win.configure(bg="black")

        self.speaking   = False
        self.mouth_open = False
        self.blinking   = False
        self._ox = self._oy = 0

        # 画像ロード
        self.img_def       = self._load(AVATAR_DEFAULT)
        self.img_spk       = self._load(AVATAR_SPEAKING)
        self.img_blink     = (self._load(AVATAR_BLINK)
                              if os.path.exists(AVATAR_BLINK)
                              else self.img_def)
        self.img_blink_spk = (self._load(AVATAR_BLINK_SPK)
                              if os.path.exists(AVATAR_BLINK_SPK)
                              else self.img_blink)

        self.lbl = tk.Label(self.win, image=self.img_def, bg="black")
        self.lbl.pack()
        self.win.geometry("+1550+500")

        # ドラッグ移動
        self.win.bind("<ButtonPress-1>",
                      lambda e: (setattr(self, "_ox",
                                         e.x_root - self.win.winfo_x()),
                                 setattr(self, "_oy",
                                         e.y_root - self.win.winfo_y())))
        self.win.bind("<B1-Motion>",
                      lambda e: self.win.geometry(
                          f"+{e.x_root - self._ox}+{e.y_root - self._oy}"))

        # 右クリックメニュー
        m = Menu(self.win, tearoff=0)
        m.add_command(label="表示/非表示", command=self.toggle_visible)
        self.win.bind("<Button-3>",
                      lambda e: m.tk_popup(e.x_root, e.y_root))

        # 瞬きスケジュール開始
        self._schedule_blink()

    # ── 画像ロード ────────────────────────────
    def _load(self, path: str) -> ImageTk.PhotoImage:
        try:
            img = Image.open(path)
            w, h = img.size
            img = img.resize(
                (int(w * AVATAR_SCALE), int(h * AVATAR_SCALE)),
                Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(img)
        except Exception:
            return ImageTk.PhotoImage(
                Image.new("RGBA", (100, 100), (0, 0, 0, 0)))

    # ── 表示制御 ──────────────────────────────
    def toggle_visible(self) -> None:
        self.visible = not self.visible
        (self.win.deiconify if self.visible else self.win.withdraw)()

    # ── 瞬き ──────────────────────────────────
    def _schedule_blink(self) -> None:
        interval = random.randint(BLINK_INTERVAL_MIN, BLINK_INTERVAL_MAX)
        try:
            self.win.after(interval, self._do_blink)
        except tk.TclError:
            pass

    def _do_blink(self) -> None:
        if not self.visible:
            self._schedule_blink()
            return
        try:
            self.blinking = True
            img = (self.img_blink_spk
                   if (self.speaking and self.mouth_open)
                   else self.img_blink)
            self.lbl.config(image=img)
            self.win.after(BLINK_DURATION_MS, self._end_blink)
        except tk.TclError:
            pass

    def _end_blink(self) -> None:
        try:
            self.blinking = False
            img = (self.img_spk
                   if (self.speaking and self.mouth_open)
                   else self.img_def)
            self.lbl.config(image=img)
            self._schedule_blink()
        except tk.TclError:
            pass

    # ── 口パク ────────────────────────────────
    def start_speaking(self) -> None:
        if not self.speaking:
            self.speaking = True
            self._mouth_loop()

    def stop_speaking(self) -> None:
        self.speaking = False
        if not self.blinking:
            try:
                self.lbl.config(image=self.img_def)
            except tk.TclError:
                pass

    def _mouth_loop(self) -> None:
        if not self.speaking:
            return
        self.mouth_open = not self.mouth_open
        if not self.blinking:
            try:
                self.lbl.config(
                    image=self.img_spk if self.mouth_open else self.img_def)
            except tk.TclError:
                return
        try:
            self.win.after(MOUTH_INTERVAL_MS, self._mouth_loop)
        except tk.TclError:
            pass


# ═══════════════════════════════════════════════════════
#  ■ ChatApp クラス (ver.1.0.3 修正・完全版)
# ═══════════════════════════════════════════════════════
class ChatApp:
    def __init__(self, root: tk.Tk, deps: AppDeps) -> None:
        self.root = root
        self._deps = deps
        self.SYSTEM_PROMPT = SYSTEM_PROMPT
        self.root.title("LLM Local Chat")
        self.root.geometry("1100x850")
        self.root.configure(bg=C["bg_main"])

        # ── 設定読み込み ──────────────────────────
        self._cfg         = load_settings()
        self._model_path  = self._cfg["model_path"]
        self._n_ctx       = self._cfg["n_ctx"]
        self._max_tokens  = self._cfg["max_tokens"]
        self._temperature = self._cfg["temperature"]
        self._vad_thresh  = self._cfg.get("vad_threshold", DEFAULT_VAD_RMS)
        self._llm_perf_settings = {
            "n_threads_batch": self._cfg["n_threads_batch"],
            "n_batch": self._cfg["n_batch"],
            "n_ubatch": self._cfg["n_ubatch"],
            "flash_attn": self._cfg["flash_attn"],
            "offload_kqv": self._cfg["offload_kqv"],
            "use_mmap": self._cfg["use_mmap"],
        }

        # ── ランタイム変数 ────────────────────────
        self._is_thinking  = False
        self._closing      = False
        self._llm_abort    = False   # 生成中断フラグ
        self._is_guest     = False
        self._llm_loading  = True
        self.llm: Llama | None        = None
        self._whisper_model           = None   # バックグラウンドでロード
        self._whisper_load_skipped    = False  # 起動時にWhisperロードをスキップしたか
        self._whisper_load_started    = False  # LLM準備後に初回だけロードする
        self._llm_load_generation     = 0
        self._llm_load_active         = threading.Event()
        self._files: list[str]        = []
        self._current_session: dict   = {}
        self._current_path: str | None = None
        self._voice: VoiceRecognizer | None = None
        self._stream_buf = ""
        self._kakeibo_mode         = False
        self._kakeibo_pending_text: str | None = None
        self._health_mode          = False
        self._health_pending_text:  str | None = None
        self._count_tokens = count_tokens
        self._history_budget_ratio = HISTORY_BUDGET_RATIO
        self._system_buf_tokens = SYSTEM_BUF_TOKENS

        self._session_store = deps.session_store
        self._integrations = IntegrationBridge(self.root, self._chat_write)

        # ── アバター ──────────────────────────────
        self.avatar = AvatarWindow(root)

        # ── TTS ───────────────────────────────────
        # TTSWorker側のエラーを回避するため self.avatar と root を渡す
        self.tts         = TTSWorker(self.avatar, root)
        self.tts.enabled = self._cfg.get("tts_enabled", False)
        self.tts.rate    = self._cfg.get("tts_rate", DEFAULT_TTS_RATE)
        
        # TTSのイベントとマイク制御を紐付け
        self.tts.on_start = self._on_tts_start
        self.tts.on_stop  = self._on_tts_stop

        # ── UI 構築 ───────────────────────────────
        self._build_ui()

        # ── 初期セッション ────────────────────────
        self._new_session()
        self._refresh_chat_list()

        # ── Controller（UIと実行制御の責務分離） ──
        from controller import Controller, ControllerDeps
        deps = ControllerDeps(
            res_monitor       = self._deps.res_monitor,
            whisper_pool      = self._deps.whisper_pool,
            summary_threshold = SUMMARY_THRESHOLD,
        )
        self._ctrl = Controller(self, deps)

        # ── バックグラウンドロード ─────────────────
        self._reload_llm()

    # ── 停止・制御 ──────────────────────────────

    def _stop_all(self) -> None:
        self._ctrl.stop()

    def _post_ui(self, callback) -> bool:
        """終了後のTkへワーカースレッドが通知しないための共通入口。"""
        if self._closing:
            return False

        def _run_if_open():
            if not self._closing:
                callback()

        try:
            self.root.after(0, _run_if_open)
            return True
        except (tk.TclError, RuntimeError):
            return False

    # ══════════════════════════════════════════════
    #  LLM ロード
    # ══════════════════════════════════════════════
    @staticmethod
    def _close_llm_instance(llm) -> None:
        if llm is None:
            return
        close = getattr(llm, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:
                print(f"[LLM] モデル解放エラー: {exc}")
        del llm
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def _detach_current_llm(self) -> None:
        current = self.llm
        self.llm = None
        try:
            service_llm = self._ctrl._llm_service.detach_llm()
        except RuntimeError:
            self.llm = current
            raise
        self._close_llm_instance(service_llm)
        if current is not service_llm:
            self._close_llm_instance(current)

    def _reload_llm(
        self,
        rollback_config: tuple[str, int] | None = None,
        recovery: bool = False,
    ) -> None:
        if self._ctrl._llm_service.is_running():
            raise RuntimeError("LLM実行中はモデルを再読込できません")
        self._llm_load_generation += 1
        generation = self._llm_load_generation
        self._llm_loading = True
        self._detach_current_llm()
        model_path = self._model_path
        n_ctx = self._n_ctx
        perf_settings = dict(self._llm_perf_settings)
        self._status_set(
            f"⏳ モデル読込中: {os.path.basename(model_path)} …")

        def _worker() -> None:
            self._llm_load_active.set()
            try:
                llm = init_llm(
                    model_path, n_ctx, self._deps.res_monitor, perf_settings)
                if generation != self._llm_load_generation or self._closing:
                    self._close_llm_instance(llm)
                    return
                self._post_ui(lambda: self._on_llm_ready(
                    generation, llm, None, rollback_config, recovery))
            except Exception as e:
                self._post_ui(lambda err=e: self._on_llm_ready(
                    generation, None, err, rollback_config, recovery))
            finally:
                self._llm_load_active.clear()

        threading.Thread(target=_worker, daemon=True).start()

    def _on_llm_ready(
        self, generation, llm, err, rollback_config=None, recovery=False
    ) -> None:
        if generation != self._llm_load_generation or self._closing:
            self._close_llm_instance(llm)
            return
        self._llm_loading = False
        if llm:
            self.llm = llm
            self._ctrl._llm_service.attach_llm(llm)
            self._ctrl.clear_token_cache()
            self._update_status()
            if recovery:
                print("[LLM] 以前のモデル設定へ復旧しました")
        else:
            if rollback_config is not None and not recovery:
                self._model_path, self._n_ctx = rollback_config
                self._cfg["model_path"] = self._model_path
                self._cfg["n_ctx"] = self._n_ctx
                save_settings(self._cfg)
                print(f"[LLM] 新モデル読込失敗。以前の設定へ復旧します: {err}")
                self._reload_llm(recovery=True)
                return
            self._status_set("❌ モデル読込失敗")
            messagebox.showerror(
                "モデル読込エラー",
                f"モデルの読み込みに失敗しました。\n"
                f"設定からパスを確認してください。\n\n{err}")
        self._start_whisper_after_llm_once()

    def _start_whisper_after_llm_once(self) -> None:
        """LLMのVRAM確保後に、Whisperを初回だけロードする。"""
        if self._whisper_load_started:
            return
        self._whisper_load_started = True
        self._load_whisper_async()

    # ══════════════════════════════════════════════
    #  Whisper ロード（バックグラウンド）
    # ══════════════════════════════════════════════
    def _load_whisper_async(self) -> None:
        if not should_load_whisper(self._cfg.get("mic_enabled")):
            print("[System] マイクが無効なため、Whisperのロードをスキップします。")
            self._whisper_load_skipped = True
            self._post_ui(self._on_whisper_skipped)
            return
        
        def _worker() -> None:
            # MODIFIED: WhisperPool が GPU+CPU 両ロードを管理（VRAMベースで判断）
            self._deps.whisper_pool.load(
                self._deps.res_monitor,
                mode=self._cfg.get("whisper_mode", "auto"),
            )
            wm = (
                self._deps.whisper_pool
                if self._deps.whisper_pool._cpu_model is not None
                else None
            )
            self._post_ui(lambda m=wm: self._on_whisper_ready(m))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_whisper_skipped(self) -> None:
        self._whisper_model = None
        self._update_status()
        self._queue_initial_greeting()

    def _on_whisper_ready(self, wm) -> None:
        self._whisper_model = wm
        if wm is None:
            print("[Whisper] ロード無効 → 音声認識は無効")
            self._status_set("⚠ Whisper ロード無効（音声認識無効）")
            self._queue_initial_greeting()
            return
        print("[Whisper] VoiceRecognizer を起動します")
        self._voice = VoiceRecognizer(
            wm,
            on_text=lambda t: self._post_ui(
                lambda tx=t: self._voice_input(tx)),
            vad_threshold=self._vad_thresh,
            res_monitor=self._deps.res_monitor,
        )
        self._voice.on_idle       = lambda: self._post_ui(self._mic_idle)
        self._voice.on_listening  = lambda: self._post_ui(self._mic_listening)
        self._voice.on_processing = lambda: self._post_ui(self._mic_processing)
        # スレッド起動後に設定ファイルのmic_enabledを反映する
        self._voice.enabled = self._cfg.get("mic_enabled", False)
        print(f"[Whisper] 音声認識開始 / VAD閾値={self._vad_thresh}")
        self._update_status()
        self._mic_idle()

        self._queue_initial_greeting()

    def _queue_initial_greeting(self) -> None:
        if should_queue_initial_greeting(
            self.tts.enabled, self.tts._initial_greeting_done
        ):
            self.tts._initial_greeting_done = True
            self.tts.speak("システムを起動しました。")
            print("[TTS] 起動発話をキューに追加")

    # ══════════════════════════════════════════════
    #  UI 構築
    # ══════════════════════════════════════════════
    def _build_ui(self) -> None:
        # ── メニューバー ──────────────────────────
        bar = Menu(self.root)

        mf = Menu(bar, tearoff=0)
        bar.add_cascade(label="ファイル", menu=mf)
        mf.add_command(label="新規チャット",       command=self._new_session)
        mf.add_command(label="保存",              command=self._save_now)
        mf.add_command(label="テキストとして保存", command=self._save_as_text)
        mf.add_command(label="設定",              command=self._open_settings)
        mf.add_separator()
        mf.add_command(label="終了",              command=self._on_close)

        mv = Menu(bar, tearoff=0)
        bar.add_cascade(label="表示", menu=mv)
        self._tts_var = BooleanVar(value=self.tts.enabled)
        mv.add_checkbutton(
            label="TTS 音声出力",
            variable=self._tts_var,
            command=self._toggle_tts)
        mv.add_command(
            label="アバター表示/非表示",
            command=self.avatar.toggle_visible)

        self.root.config(menu=bar)
        self.root.bind("<Control-n>", lambda e: self._new_session())
        self.root.bind("<Control-s>", lambda e: self._save_now())

        # ── 左サイドバー ──────────────────────────
        sb = tk.Frame(self.root, bg=C["bg_side"], width=260)
        sb.pack(side=tk.LEFT, fill=tk.Y)
        sb.pack_propagate(False)

        tk.Label(
            sb, text="LLM Local Chat",
            bg=C["bg_side"], fg=C["fg_main"],
            font=FONT_TITLE,
        ).pack(pady=15)

        tk.Button(
            sb, text="＋ 新しいチャット",
            command=self._new_session,
            bg=C["accent"], fg="white",
            font=FONT_BOLD, relief=tk.FLAT,
            cursor="hand2",
        ).pack(fill=tk.X, padx=10, pady=5)

        self._btn_guest = tk.Button(
            sb, text="ゲストモード: OFF",
            command=self._toggle_guest,
            bg=C["bg_input"], fg=C["fg_sub"],
            relief=tk.FLAT, cursor="hand2",
        )
        self._btn_guest.pack(fill=tk.X, padx=10, pady=5)

        # 検索ボックス
        sf = tk.Frame(sb, bg=C["bg_side"])
        sf.pack(fill=tk.X, padx=10, pady=5)
        tk.Label(
            sf, text="🔍",
            bg=C["bg_side"], fg=C["fg_sub"],
        ).pack(side=tk.LEFT)
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._refresh_chat_list())
        tk.Entry(
            sf, textvariable=self._search_var,
            bg=C["bg_input"], fg=C["fg_main"],
            insertbackground="white", bd=0,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=3)

        # チャット Listbox
        tk.Label(
            sb, text="チャット履歴",
            bg=C["bg_side"], fg=C["fg_sub"],
            font=FONT_SMALL,
        ).pack(anchor="w", padx=12, pady=(4, 0))

        list_frame = tk.Frame(sb, bg=C["bg_side"])
        list_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        sb_scroll = tk.Scrollbar(list_frame, orient=tk.VERTICAL,
                                 width=6, bg=C["bg_side"])
        self._chat_list = tk.Listbox(
            list_frame,
            yscrollcommand=sb_scroll.set,
            bg=C["bg_side"], fg=C["fg_main"],
            selectbackground=C["bg_selected"],
            selectforeground=C["fg_main"],
            activestyle="none",
            relief=tk.FLAT, bd=0,
            font=FONT_SMALL, cursor="hand2",
        )
        sb_scroll.config(command=self._chat_list.yview)
        sb_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._chat_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._chat_list.bind("<<ListboxSelect>>", self._load_selected)
        self._chat_list.bind("<Button-3>",         self._on_list_right_click)

        # ── 右メインペイン ─────────────────────────
        rp = tk.Frame(self.root, bg=C["bg_main"])
        rp.pack(side=tk.RIGHT, expand=True, fill=tk.BOTH)

        # タイトル
        self._title_var = tk.StringVar(value="新しいチャット")
        tk.Label(
            rp, textvariable=self._title_var,
            bg=C["bg_main"], fg=C["fg_main"],
            font=FONT_TITLE,
        ).pack(pady=(10, 0))

        # 要約メモリ表示
        self._summary_var = tk.StringVar(value="")
        self._summary_label = tk.Label(
            rp, textvariable=self._summary_var,
            bg=C["bg_side"], fg=C["fg_sub"],
            font=FONT_SMALL, anchor="w",
            padx=16, pady=3,
        )
        self._summary_label.pack(fill=tk.X)

        # チャット表示エリア
        self._chat_text = scrolledtext.ScrolledText(
            rp,
            state=tk.DISABLED,
            bg=C["bg_main"], fg=C["fg_main"],
            font=FONT_CHAT, wrap=tk.WORD,
            bd=0, padx=20, pady=10,
            selectbackground=C["bg_selected"],
        )
        self._chat_text.tag_config(
            "user_lbl",
            foreground=C["accent"],
            font=("Noto Sans CJK JP", 10, "bold"))
        self._chat_text.tag_config(
            "user_msg",
            foreground="#FFFFFF",
            font=FONT_CHAT,
            lmargin1=24, lmargin2=24)
        self._chat_text.tag_config(
            "ai_lbl",
            foreground=C["fg_sub"],
            font=("Noto Sans CJK JP", 10, "bold"))
        self._chat_text.tag_config(
            "ai_msg",
            foreground="#D1D5DB",
            font=FONT_CHAT,
            lmargin1=24, lmargin2=24)
        self._chat_text.tag_config(
            "err",
            foreground=C["error_fg"],
            lmargin1=24, lmargin2=24)
        self._chat_text.tag_config(
            "divider",
            foreground=C["divider"])
        self._chat_text.tag_config(
            "kakeibo_ok",
            foreground="#10A37F",
            font=FONT_SMALL,
            lmargin1=24, lmargin2=24)
        self._chat_text.tag_config(
            "health_ok",
            foreground="#34D399",
            font=FONT_SMALL,
            lmargin1=24, lmargin2=24)
        self._chat_text.pack(expand=True, fill=tk.BOTH)

        # 右クリックコピー
        copy_menu = Menu(self._chat_text, tearoff=0)
        copy_menu.add_command(
            label="選択テキストをコピー", command=self._copy_selected)
        copy_menu.add_command(
            label="全文コピー", command=self._copy_all)
        self._chat_text.bind(
            "<Button-3>",
            lambda e: copy_menu.tk_popup(e.x_root, e.y_root))

        # ── ステータスバー ─────────────────────────
        self._status_var = tk.StringVar(value="起動中…")
        tk.Label(
            self.root,
            textvariable=self._status_var,
            bg=C["status_bg"], fg=C["fg_sub"],
            font=FONT_SMALL, anchor=tk.W, padx=10,
        ).pack(side=tk.BOTTOM, fill=tk.X)

        # ── 入力エリア ─────────────────────────────
        in_outer = tk.Frame(rp, bg=C["bg_main"])
        in_outer.pack(fill=tk.X, padx=20, pady=(4, 12))

        # 免責文言（入力ボックスの上に表示）
        tk.Label(
            in_outer,
            text="⚠ AIは間違えることがあります。重要な情報は必ずご自身で確認してください。",
            bg=C["bg_main"], fg=C["fg_sub"],
            font=FONT_SMALL, anchor=tk.CENTER,
        ).pack(fill=tk.X, pady=(0, 4))

        in_box = tk.Frame(
            in_outer,
            bg=C["bg_input"],
            highlightbackground=C["divider"],
            highlightthickness=1,
        )
        in_box.pack(fill=tk.X)

        # マイクボタン
        self._btn_mic = tk.Button(
            in_box, text="🎤",
            command=self._toggle_mic,
            bg=C["bg_input"], fg=C["mic_on"],
            bd=0, font=(_FONT_ICON, 18),
            cursor="hand2",
        )
        self._btn_mic.pack(side=tk.LEFT, padx=(6, 0), pady=6, anchor="s")

        # 停止ボタン（TTS / 音声認識）
        self._btn_stop = tk.Button(
            in_box, text="⏹",
            command=self._stop_all,
            bg=C["bg_input"], fg=C["mic_on"],
            bd=0, font=(_FONT_ICON, 18),
            cursor="hand2",
        )
        self._btn_stop.pack(side=tk.LEFT, padx=(4, 0), pady=6, anchor="s")

        # 家計簿モードボタン
        self._btn_kakeibo = tk.Button(
            in_box, text="🏠",
            command=self._toggle_kakeibo,
            bg=C["bg_input"], fg=C["fg_sub"],
            bd=0, font=(_FONT_ICON, 18),
            cursor="hand2",
        )
        self._btn_kakeibo.pack(side=tk.LEFT, padx=(4, 0), pady=6, anchor="s")

        # 健康記録モードボタン
        self._btn_health = tk.Button(
            in_box, text="💪",
            command=self._toggle_health,
            bg=C["bg_input"], fg=C["fg_sub"],
            bd=0, font=(_FONT_ICON, 18),
            cursor="hand2",
        )
        self._btn_health.pack(side=tk.LEFT, padx=(4, 0), pady=6, anchor="s")

        # ─────────────────────────────────────────
        #  ★ バグ修正箇所 ①:
        #    tk.Text を直接 pack し、state は常に NORMAL のまま維持。
        #    _entry を disable にする処理を一切設けない。
        # ─────────────────────────────────────────
        # width=1: 未指定だとTkの既定値(80文字)が要求幅になり、ウィンドウ幅次第で
        # in_box全体の要求幅が実幅を超え、送信ボタンが1pxに潰れることがあるため、
        # 実際の幅はfill=X/expand=Trueに委ねる。
        self._entry = tk.Text(
            in_box,
            height=5, width=1,
            bg=C["bg_input"], fg=C["fg_main"],
            insertbackground="white",
            bd=0, font=FONT_CHAT,
            wrap=tk.WORD,
        )
        self._entry.pack(
            side=tk.LEFT, fill=tk.X, expand=True,
            padx=8, pady=6)

        # ─────────────────────────────────────────
        #  ★ バグ修正箇所 ②:
        #    lambda でタプルを返すと "break" が認識されない。
        #    専用メソッド _on_entry_return で return "break" を確実に返す。
        # ─────────────────────────────────────────
        self._entry.bind("<Return>",       self._on_entry_return)
        self._entry.bind("<Shift-Return>", self._on_entry_shift_return)

        # 送信ボタン
        self._btn_send = tk.Button(
            in_box, text="送信",
            command=self._send,
            bg=C["accent"], fg="white",
            width=8, bd=0, cursor="hand2",
        )
        self._btn_send.pack(side=tk.RIGHT, padx=(0, 8), pady=6, anchor="s")

        tk.Label(
            in_outer,
            text="Enter: 送信  /  Shift+Enter: 改行",
            bg=C["bg_main"], fg=C["fg_sub"],
            font=("Noto Sans CJK JP", 8),
        ).pack(anchor="e")

    # ── Entry キーバインド ─────────────────────
    def _on_entry_return(self, event) -> str:
        """Enter キー: 送信して改行を抑制"""
        self._send()
        return "break"          # ← 文字列 "break" を確実に返す

    def _on_entry_shift_return(self, event) -> None:
        """Shift+Enter: 改行（デフォルト動作をそのまま許可）"""
        # return None → Tkinter はデフォルト動作（改行挿入）を実行する
        return None

    # ══════════════════════════════════════════════
    #  右クリック削除
    # ══════════════════════════════════════════════
    def _on_list_right_click(self, event) -> None:
        idx = self._chat_list.nearest(event.y)
        if idx < 0 or idx >= len(self._files):
            return
        self._chat_list.selection_clear(0, tk.END)
        self._chat_list.selection_set(idx)
        self._chat_list.activate(idx)

        ctx = Menu(self.root, tearoff=0)
        ctx.add_command(
            label="このチャットを削除",
            foreground=C["delete_fg"],
            command=lambda i=idx: self._delete_chat(i),
        )
        ctx.add_command(
            label="名前を変更",
            command=lambda i=idx: self._rename_chat(i),
        )
        try:
            ctx.tk_popup(event.x_root, event.y_root)
        finally:
            ctx.grab_release()

    def _delete_chat(self, idx: int) -> None:
        if self._ctrl.is_busy():
            messagebox.showwarning(
                "警告", "AIの処理中はチャットを削除できません。")
            return
        if idx < 0 or idx >= len(self._files):
            return
        target = self._files[idx]
        title  = self._chat_list.get(idx).strip()
        if not messagebox.askyesno(
            "チャットの削除",
            f"「{title}」を削除しますか？\nこの操作は元に戻せません。",
            icon="warning",
        ):
            return
        try:
            self._session_store.delete(target)
        except Exception as e:
            messagebox.showerror("削除エラー", f"削除できませんでした。\n{e}")
            return

        if self._current_path == target:
            self._new_session()

        self._refresh_chat_list()

    def _rename_chat(self, idx: int) -> None:
        if self._ctrl.is_busy():
            messagebox.showwarning(
                "警告", "AIの処理中は名前を変更できません。")
            return
        if idx < 0 or idx >= len(self._files):
            return
        target  = self._files[idx]
        old_title = self._chat_list.get(idx).strip()

        new_title = simpledialog.askstring(
            "名前を変更",
            "新しいタイトルを入力してください:",
            initialvalue=old_title,
            parent=self.root,
        )
        if not new_title or new_title == old_title:
            return
        try:
            data = self._session_store.rename(target, new_title)
        except Exception as e:
            messagebox.showerror("名前変更エラー", str(e))
            return

        if self._current_path == target:
            self._current_session["title"] = data.get("title", new_title)
            self._title_var.set(self._current_session["title"])

        self._refresh_chat_list()

    # ══════════════════════════════════════════════
    #  マイク UI
    # ══════════════════════════════════════════════
    def _mic_idle(self) -> None:
        if self._voice and self._voice.enabled:
            self._btn_mic.config(fg=C["mic_on"], text="🎤")

    def _mic_listening(self) -> None:
        if self._voice and self._voice.enabled:
            self._btn_mic.config(fg=C["mic_active"], text="🔴")

    def _mic_processing(self) -> None:
        if self._voice and self._voice.enabled:
            self._btn_mic.config(fg=C["mic_active"], text="⏳")

    def _on_tts_start(self) -> None:
        """TTS発話中はVAD閾値を大幅に上げてハウリングを防ぐ"""
        if self._voice:
            self._voice.mark_tts_started()
            print("[TTS] 発話開始 → VAD感度を下げる")

    def _on_tts_stop(self) -> None:
        """TTS発話終了後にVAD閾値を元に戻す"""
        print(f"[TTS] 発話終了 → muted={getattr(self, '_tts_muted_mic', False)}")
        if self._voice:
            # 少し待ってから感度を戻す（残響が消えるのを待つ）
            voice = self._voice
            generation = voice._tts_generation
            self.root.after(
                800,
                lambda v=voice, g=generation: self._restore_vad(v, g),
            )

    def _restore_vad(self, voice, generation: int) -> None:
        """VAD感度を通常に戻す"""
        if (
            self._voice is voice
            and voice.finish_tts_if_generation(generation)
        ):
            print("[TTS] VAD感度を通常に戻す")

    def _toggle_mic(self) -> None:
        if self._voice is None:
            if self._whisper_load_skipped:
                messagebox.showinfo(
                    "音声認識",
                    "音声認識は起動時に読み込まれていません。\n"
                    "設定で「起動時にマイクを有効にする」をオンにして、\n"
                    "アプリを再起動してください。")
            else:
                messagebox.showinfo(
                    "音声認識", "Whisper モデルを読み込んでいます。\nしばらくお待ちください。")
            return
        self._voice.enabled = not self._voice.enabled
        self._btn_mic.config(
            fg=C["mic_on"] if self._voice.enabled else C["mic_off"])
        self._update_status()

    # ══════════════════════════════════════════════
    #  家計簿連携
    # ══════════════════════════════════════════════
    def _toggle_kakeibo(self) -> None:
        self._kakeibo_mode = not self._kakeibo_mode
        if self._kakeibo_mode:
            self._btn_kakeibo.config(fg=C["accent"])
            self._chat_write(
                "\n🏠 家計簿モード ON\n"
                "   支出・収入の内容を話しかけてください。\n"
                "   例:「セブンで水を150円買った」「今日マックで980円使った」\n"
                + "─" * 50 + "\n\n",
                "kakeibo_ok",
            )
        else:
            self._btn_kakeibo.config(fg=C["fg_sub"])
            self._chat_write(
                "\n🏠 家計簿モード OFF\n" + "─" * 50 + "\n\n",
                "divider",
            )

    def _confirm_and_send_kakeibo(self, candidate: dict, user_text: str) -> None:
        dialog = KakeiboConfirmDialog(self.root, candidate, user_text)
        self.root.wait_window(dialog)
        if dialog.result is None:
            self._chat_write("家計簿への登録をキャンセルしました。\n", "divider")
            return
        self._integrations.send_kakeibo(dialog.result)

    # ══════════════════════════════════════════════
    #  健康記録連携
    # ══════════════════════════════════════════════
    def _toggle_health(self) -> None:
        self._health_mode = not self._health_mode
        if self._health_mode:
            self._btn_health.config(fg=C["accent"])
            self._chat_write(
                "\n💪 健康記録モード ON\n"
                "   体重・食事・活動内容を話しかけてください。\n"
                "   例:「体重70kg、体脂肪18%、昼は回鍋肉を食べた」\n"
                + "─" * 50 + "\n\n",
                "health_ok",
            )
        else:
            self._btn_health.config(fg=C["fg_sub"])
            self._chat_write(
                "\n💪 健康記録モード OFF\n" + "─" * 50 + "\n\n",
                "divider",
            )

    def _confirm_and_send_biolog(
        self, record: dict, explicit_fields=None
    ) -> None:
        self._integrations.confirm_and_send_biolog(record, explicit_fields)

    def _voice_input(self, text: str) -> None:
        self._ctrl.handle_voice(text)

    # ══════════════════════════════════════════════
    #  チャット送信
    # ══════════════════════════════════════════════
    def _send(self) -> None:
        self._ctrl.handle_text()

    # ── ストリーミングトークン追記（メインスレッド） ─
    def _append_stream_token(self, token: str) -> None:
        self._stream_buf += token
        self._chat_write(token, "ai_msg")

    # ── チャットテキスト追記ヘルパー ─────────────
    def _chat_write(self, text: str, tag: str) -> None:
        self._chat_text.config(state=tk.NORMAL)
        self._chat_text.insert(tk.END, text, tag)
        self._chat_text.config(state=tk.DISABLED)
        self._chat_text.yview(tk.END)

    # ══════════════════════════════════════════════
    #  要約メモリ
    # ══════════════════════════════════════════════
    def _build_summary_prompt(self) -> str:
        history_text = "\n".join(
            f"User: {h['user']}\nAssistant: {h.get('assistant', '')}"
            for h in self._current_session.get("history", [])
            if h.get("assistant")
        )
        return (
            "以下の会話を50文字以内の日本語で1行に要約してください。\n\n"
            f"{history_text}\n\n要約:"
        )

    def _apply_summary(self, summary: str) -> None:
        self._current_session["summary"] = summary
        self._summary_var.set(f"📝 メモリ: {summary}")
        self._save_now()

    # ══════════════════════════════════════════════
    #  セッション管理
    # ══════════════════════════════════════════════
    def _new_session(self) -> None:
        if hasattr(self, "_ctrl") and self._ctrl.is_busy():
            messagebox.showwarning(
                "警告", "AIの処理中です。しばらくお待ちください。")
            return
        self._current_session = self._session_store.new_session()
        self._current_path = None
        self._title_var.set("新しいチャット")
        self._summary_var.set("")
        # チャット表示クリア
        self._chat_text.config(state=tk.NORMAL)
        self._chat_text.delete("1.0", tk.END)
        self._chat_text.config(state=tk.DISABLED)
        # ─────────────────────────────────────────
        #  ★ バグ修正箇所 ⑥:
        #    新規セッション後に _entry へ確実にフォーカスを当てる。
        # ─────────────────────────────────────────
        self._entry.focus_set()
        self._update_status()

    def _save_now(self) -> None:
        if self._is_guest:
            return
        try:
            saved_path = self._session_store.save(
                self._current_session, self._current_path)
            if not saved_path:
                return
            self._current_path = saved_path
            # リスト更新は必ずメインスレッドで行う
            self._post_ui(self._refresh_chat_list)
        except Exception as e:
            print(f"[保存エラー] {e}")

    def _save_as_text(self) -> None:
        if not self._current_session.get("history"):
            messagebox.showinfo("保存", "保存する会話がありません")
            return
        path = filedialog.asksaveasfilename(
            title="テキストとして保存",
            defaultextension=".txt",
            filetypes=[("テキストファイル", "*.txt")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"タイトル: {self._current_session['title']}\n")
                f.write("=" * 60 + "\n\n")
                for h in self._current_session["history"]:
                    f.write(f"👤 ユーザー\n{h['user']}\n\n")
                    f.write(f"🤖 アシスタント\n{h.get('assistant','')}\n")
                    f.write("-" * 60 + "\n")
            messagebox.showinfo("保存完了", "テキストファイルとして保存しました")
        except Exception as e:
            messagebox.showerror("保存エラー", str(e))

    def _refresh_chat_list(self) -> None:
        self._chat_list.delete(0, tk.END)
        self._files = []
        kw = self._search_var.get()
        for session in self._session_store.list_sessions(kw):
            self._chat_list.insert(tk.END, f"  {session['title']}")
            self._files.append(session["path"])

    def _load_selected(self, _event=None) -> None:
        if self._ctrl.is_busy():
            messagebox.showwarning(
                "警告", "AIの処理中はチャットを切り替えられません。")
            return
        sel = self._chat_list.curselection()
        if not sel:
            return
        fp = self._files[sel[0]]
        try:
            data = self._session_store.load(fp)
        except Exception as e:
            messagebox.showerror("読込エラー", str(e))
            return

        self._current_session = data
        self._current_path    = fp
        self._title_var.set(data.get("title", "不明"))
        summary = data.get("summary", "")
        self._summary_var.set(f"📝 メモリ: {summary}" if summary else "")

        # チャット再描画
        self._chat_text.config(state=tk.NORMAL)
        self._chat_text.delete("1.0", tk.END)
        for h in data.get("history", []):
            self._chat_text.insert(tk.END, "\n", "")
            self._chat_text.insert(tk.END, "👤 あなた\n", "user_lbl")
            self._chat_text.insert(tk.END, f"{h.get('user','')}\n", "user_msg")
            self._chat_text.insert(tk.END, "─" * 50 + "\n", "divider")
            self._chat_text.insert(tk.END, "🤖 AI\n", "ai_lbl")
            self._chat_text.insert(
                tk.END, f"{h.get('assistant','')}\n", "ai_msg")
            self._chat_text.insert(tk.END, "─" * 50 + "\n\n", "divider")
        self._chat_text.config(state=tk.DISABLED)
        self._chat_text.yview(tk.END)

        # ─────────────────────────────────────────
        #  ★ バグ修正箇所 ⑦:
        #    既存チャット読み込み後も _entry へフォーカスを当てる。
        # ─────────────────────────────────────────
        self._entry.focus_set()
        self._update_status()

    # ══════════════════════════════════════════════
    #  ゲストモード
    # ══════════════════════════════════════════════
    def _toggle_guest(self) -> None:
        if self._ctrl.is_busy():
            messagebox.showwarning(
                "警告", "AIの処理中はゲストモードを切り替えられません。")
            return
        self._is_guest = not self._is_guest
        self._btn_guest.config(
            text="ゲストモード: ON"  if self._is_guest else "ゲストモード: OFF",
            fg  =C["guest_tag"]      if self._is_guest else C["fg_sub"],
        )
        self._new_session()

    # ══════════════════════════════════════════════
    #  設定ダイアログ
    # ══════════════════════════════════════════════
    def _toggle_tts(self) -> None:
        self.tts.enabled = self._tts_var.get()
        self._cfg["tts_enabled"] = self.tts.enabled
        save_settings(self._cfg)

    def _open_settings(self) -> None:
        dlg = SettingsDialog(self.root, dict(
            model_path    = self._model_path,
            n_ctx         = self._n_ctx,
            max_tokens    = self._max_tokens,
            temperature   = self._temperature,
            vad_threshold = self._vad_thresh,
            tts_rate      = self.tts.rate,
            mic_enabled = self._cfg.get("mic_enabled", False),
            whisper_mode = self._cfg.get("whisper_mode", "auto"),
            tts_enabled   = self.tts.enabled,
        ))
        self.root.wait_window(dlg)
        if dlg.result is None:
            return

        new = dlg.result
        whisper_mode_changed = (
            new["whisper_mode"] != self._cfg.get("whisper_mode", "auto")
        )
        model_changed = (
            new["model_path"] != self._model_path
            or new["n_ctx"] != self._n_ctx
        )
        if model_changed and (self._ctrl.is_busy() or self._llm_loading):
            messagebox.showwarning(
                "設定",
                "AIの処理中またはモデル読込中はモデル設定を変更できません。\n"
                "その他の設定だけを適用します。",
            )
            new["model_path"] = self._model_path
            new["n_ctx"] = self._n_ctx
            model_changed = False
        self._max_tokens  = new["max_tokens"]
        self._temperature = new["temperature"]
        self._vad_thresh  = new["vad_threshold"]
        if self._voice:
            self._voice.vad_threshold = self._vad_thresh
            self._voice.enabled = new["mic_enabled"]
        self.tts.enabled = new["tts_enabled"]
        self.tts.rate = new["tts_rate"]
        self._tts_var.set(new["tts_enabled"])  # メニューのチェック状態を同期

        self._cfg.update(new)
        save_settings(self._cfg)

        if whisper_mode_changed:
            messagebox.showinfo(
                "Whisper設定",
                "Whisper実行モードは次回起動時に反映されます。",
            )

        if model_changed:
            rollback_config = (self._model_path, self._n_ctx)
            self._model_path = new["model_path"]
            self._n_ctx      = new["n_ctx"]
            self._reload_llm(rollback_config=rollback_config)
        else:
            self._update_status()

    # ══════════════════════════════════════════════
    #  コピー
    # ══════════════════════════════════════════════
    def _copy_selected(self) -> None:
        try:
            text = self._chat_text.get(tk.SEL_FIRST, tk.SEL_LAST)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
        except tk.TclError:
            pass

    def _copy_all(self) -> None:
        text = self._chat_text.get("1.0", tk.END)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    # ══════════════════════════════════════════════
    #  ステータスバー
    # ══════════════════════════════════════════════
    def _status_set(self, msg: str) -> None:
        self._status_var.set(msg)

    def _update_status(self) -> None:
        if self._llm_loading or self.llm is None:
            # LLMロード中でもマイク状態だけ反映する
            if self._voice:
                mic = "マイクON" if self._voice.enabled else "マイクOFF"
                self._status_var.set(
                    f"⏳ モデル読込中… | {mic}")
            return
        mic_stat = "マイク無効"
        if self._voice:
            mic_stat = "マイクON" if self._voice.enabled else "マイクOFF"
        status_label = getattr(self._deps.whisper_pool, "status_label", None)
        whisper_stat = status_label() if status_label else "不明"
        think = (
            self._ctrl.operation_label()
            if hasattr(self, "_ctrl")
            else ("⏳ 生成中…" if self._is_thinking else "✅ 待機中")
        )
        mn    = os.path.basename(self._model_path)
        turns = len(self._current_session.get("history", []))
        guest = " [ゲスト]" if self._is_guest else ""
        self._status_var.set(
            f"{think}{guest} | {mn} | "
            f"{self._max_tokens}tok / temp:{self._temperature} | "
            f"{turns}ターン | {mic_stat} / Whisper:{whisper_stat}")

    # ══════════════════════════════════════════════
    #  終了処理
    # ══════════════════════════════════════════════
    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._llm_load_generation += 1
        self._ctrl.begin_shutdown()
        self._integrations.begin_closing()
        if self._voice:
            self._voice.stop()
            self._cfg["mic_enabled"] = self._voice.enabled
        self._cfg["tts_enabled"] = self.tts.enabled
        save_settings(self._cfg)
        self.tts.terminate()
        self._deps.res_monitor.stop()
        self._save_now()
        deadline = time.monotonic() + 6.0

        def _wait_for_workers() -> None:
            llm_running = self._ctrl._llm_service.is_running()
            loading = self._llm_load_active.is_set()
            pending_api = self._integrations.pending_operations()
            if not llm_running and not loading and not pending_api:
                self.root.destroy()
                return
            if time.monotonic() >= deadline:
                pending = []
                if llm_running:
                    pending.append("llm")
                if loading:
                    pending.append("llm_load")
                pending.extend(pending_api)
                print(f"[Shutdown] timeout pending={pending}")
                self.root.destroy()
                return
            self.root.after(100, _wait_for_workers)

        self.root.after(0, _wait_for_workers)


# ═══════════════════════════════════════════════════════
#  ■ 起動
# ═══════════════════════════════════════════════════════
def main() -> None:
    root = tk.Tk()
    deps = create_app_deps(LOG_DIR)
    app  = ChatApp(root, deps)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
