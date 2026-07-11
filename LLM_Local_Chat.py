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
#  - _update_summary(): _is_thinking 中は LLM 競合をスキップ
#  - _update_summary(): _save_now() を root.after() 経由でメインスレッドから呼ぶ
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
#  ・pyttsx3 TTS（アバター連動）
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
import threading
import time
import random
import re  # v1.1.1

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
from audio_workers import DEFAULT_VAD_RMS, TTSWorker, VoiceRecognizer
from integrations import IntegrationBridge
from resource_monitor import adjust_llm

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
DEFAULT_MODEL_PATH = r"models\gemma-3-4b-it-q4_k_m.gguf"
AVATAR_DEFAULT     = r"avatars\default_avatar.png"
AVATAR_SPEAKING    = r"avatars\speaking_avatar.png"
AVATAR_BLINK       = r"avatars\blink_avatar.png"
AVATAR_BLINK_SPK   = r"avatars\blink_speaking_avatar.png"
LOG_DIR            = "chat_logs"
SETTINGS_FILE      = "chat_settings.json"

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
        mic_enabled   = False,
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
    return defaults


def save_settings(d: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[設定保存エラー] {e}")


# ═══════════════════════════════════════════════════════
#  ■ LLM ユーティリティ
# ═══════════════════════════════════════════════════════
def init_llm(model_path: str, n_ctx: int, res_monitor, perf_settings: dict | None = None) -> Llama:
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"モデルが見つかりません:\n{model_path}")
    params = adjust_llm(res_monitor)   # ADDED: VRAMベースで n_gpu_layers を決定
    perf = perf_settings or {}
    base_kwargs = dict(
        model_path   = model_path,
        n_ctx        = n_ctx,
        n_threads    = 8,
        n_gpu_layers = params["n_gpu_layers"],   # MODIFIED (-1 or 0)
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

    attempts = [("perf", {**base_kwargs, **perf_kwargs})]
    if perf_kwargs["flash_attn"]:
        no_flash = dict(perf_kwargs)
        no_flash["flash_attn"] = False
        attempts.append(("perf_no_flash_attn", {**base_kwargs, **no_flash}))
    attempts.append(("base", base_kwargs))

    last_err = None
    for idx, (label, kwargs) in enumerate(attempts[:3], 1):
        try:
            if label != "perf":
                print(f"[LLM] retry load with {label}")
            return Llama(**kwargs)
        except Exception as e:
            last_err = e
            shown = {k: kwargs.get(k) for k in (
                "n_threads", "n_threads_batch", "n_batch", "n_ubatch",
                "n_gpu_layers", "flash_attn", "offload_kqv", "use_mmap"
            ) if k in kwargs}
            print(f"[LLM] load attempt {idx} failed ({label}): {shown} -> {type(e).__name__}: {e}")
            if label == "perf" and not (
                isinstance(e, TypeError) or perf_kwargs["flash_attn"]
            ):
                raise
            if label == "perf_no_flash_attn" and not isinstance(e, TypeError):
                raise
    raise last_err


def count_tokens(llm: Llama, text: str) -> int:
    try:
        return len(llm.tokenize(text.encode("utf-8"), add_bos=False))
    except Exception:
        return max(1, len(text) // 2)


def build_messages_safe(
    llm: Llama,
    history: list,
    user_text: str,
    n_ctx: int,
    max_tokens: int,
    summary: str = "",
) -> list:
    """トークン予算内に収まる履歴メッセージリストを構築する"""
    sys_content = SYSTEM_PROMPT
    if summary:
        sys_content += f"\n\n[これまでの会話の要約]: {summary}"

    sys_tokens  = count_tokens(llm, sys_content) + SYSTEM_BUF_TOKENS
    user_tokens = count_tokens(llm, user_text)
    budget = int(
        (n_ctx - max_tokens - sys_tokens - user_tokens) * HISTORY_BUDGET_RATIO
    )
    budget = max(0, budget)

    selected: list = []
    for h in reversed(history):
        cost = (count_tokens(llm, h.get("user", ""))
                + count_tokens(llm, h.get("assistant", "")) + 12)
        if budget - cost < 0:
            break
        selected.insert(0, h)
        budget -= cost

    msgs = [{"role": "system", "content": sys_content}]
    for h in selected:
        msgs.append({"role": "user",      "content": h.get("user",      "")})
        msgs.append({"role": "assistant", "content": h.get("assistant", "")})
    msgs.append({"role": "user", "content": user_text})
    return msgs


def _strip_code_blocks(text: str) -> str:
    """TTSに渡す前にコードブロック（```...```）を除去する。 v1.1.2"""
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


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

        # 起動時マイクON/OFF
        self.v_mic = BooleanVar(value=cfg.get("mic_enabled", False))
        tk.Checkbutton(
            self, text="起動時にマイクを有効にする",
            variable=self.v_mic,
            bg=C["bg_main"], fg=C["fg_main"],
            selectcolor=C["bg_input"], activebackground=C["bg_main"],
        ).grid(row=5, column=0, columnspan=3, sticky="w", padx=16, pady=4)

        # 起動時TTS ON/OFF
        self.v_tts = BooleanVar(value=cfg.get("tts_enabled", False))
        tk.Checkbutton(
            self, text="起動時にTTS読み上げを有効にする",
            variable=self.v_tts,
            bg=C["bg_main"], fg=C["fg_main"],
            selectcolor=C["bg_input"], activebackground=C["bg_main"],
        ).grid(row=6, column=0, columnspan=3, sticky="w", padx=16, pady=4)

        # ボタン
        bf = tk.Frame(self, bg=C["bg_main"])
        bf.grid(row=7, column=0, columnspan=3, pady=16)
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
            if not (0.0 <= tmp <= 2.0):
                raise ValueError("会話の自由度は 0.0〜2.0 の範囲で入力してください")
            if tok < 1 or ctx < 512:
                raise ValueError("トークン数が小さすぎます (n_ctx は 512 以上)")
            self.result = dict(
                model_path=mp, n_ctx=ctx, max_tokens=tok,
                temperature=tmp, vad_threshold=vad,
                mic_enabled=self.v_mic.get(),
                tts_enabled=self.v_tts.get())
            self.destroy()
        except ValueError as e:
            messagebox.showerror("入力エラー", str(e), parent=self)
        except Exception:
            messagebox.showerror("入力エラー", "数値が無効です", parent=self)


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
        self._llm_abort    = False   # 生成中断フラグ
        self._is_guest     = False
        self._llm_loading  = True
        self.llm: Llama | None        = None
        self._whisper_model           = None   # バックグラウンドでロード
        self._whisper_load_skipped    = False  # 起動時にWhisperロードをスキップしたか
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
            strip_code_blocks = _strip_code_blocks,
            summary_threshold = SUMMARY_THRESHOLD,
        )
        self._ctrl = Controller(self, deps)

        # ── バックグラウンドロード ─────────────────
        self._reload_llm()
        self._load_whisper_async()

    # ── 停止・制御 ──────────────────────────────

    def _stop_all(self) -> None:
        self._ctrl.stop()

    # ══════════════════════════════════════════════
    #  LLM ロード
    # ══════════════════════════════════════════════
    def _reload_llm(self) -> None:
        self._llm_loading = True
        self.llm = None
        self._status_set(
            f"⏳ モデル読込中: {os.path.basename(self._model_path)} …")

        def _worker() -> None:
            try:
                llm = init_llm(
                    self._model_path, self._n_ctx, self._deps.res_monitor,
                    self._llm_perf_settings)
                self.root.after(0, lambda: self._on_llm_ready(llm, None))
            except Exception as e:
                self.root.after(0, lambda err=e: self._on_llm_ready(None, err))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_llm_ready(self, llm, err) -> None:
        self._llm_loading = False
        if llm:
            self.llm = llm
            self._ctrl._llm_service.llm = llm  # モデル差し替え時に LLMService へ反映
            self._ctrl.clear_token_cache()
            self._update_status()
        else:
            self._status_set("❌ モデル読込失敗")
            messagebox.showerror(
                "モデル読込エラー",
                f"モデルの読み込みに失敗しました。\n"
                f"設定からパスを確認してください。\n\n{err}")

    # ══════════════════════════════════════════════
    #  Whisper ロード（バックグラウンド）
    # ══════════════════════════════════════════════
    def _load_whisper_async(self) -> None:
        # ── 追加: マイクもTTSも無効なら、モデルロード自体をスキップ ──
        if not self._cfg.get("mic_enabled") and not self._cfg.get("tts_enabled"):
            print("[System] マイク/TTSが無効なため、Whisperのロードをスキップします。")
            self._whisper_load_skipped = True
            self.root.after(0, lambda: self._on_whisper_ready(None))
            return
        
        def _worker() -> None:
            # MODIFIED: WhisperPool が GPU+CPU 両ロードを管理（VRAMベースで判断）
            self._deps.whisper_pool.load(self._deps.res_monitor)
            wm = (
                self._deps.whisper_pool
                if self._deps.whisper_pool._cpu_model is not None
                else None
            )
            self.root.after(0, lambda m=wm: self._on_whisper_ready(m))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_whisper_ready(self, wm) -> None:
        self._whisper_model = wm
        if wm is None:
            print("[Whisper] ロード無効 → 音声認識は無効")
            self._status_set("⚠ Whisper ロード無効（音声認識無効）")
            return
        print("[Whisper] VoiceRecognizer を起動します")
        self._voice = VoiceRecognizer(
            wm,
            on_text=lambda t: self.root.after(
                0, lambda tx=t: self._voice_input(tx)),
            vad_threshold=self._vad_thresh,
            res_monitor=self._deps.res_monitor,
        )
        self._voice.on_idle       = lambda: self.root.after(0, self._mic_idle)
        self._voice.on_listening  = lambda: self.root.after(
            0, self._mic_listening)
        self._voice.on_processing = lambda: self.root.after(
            0, self._mic_processing)
        # スレッド起動後に設定ファイルのmic_enabledを反映する
        self._voice.enabled = self._cfg.get("mic_enabled", False)
        print(f"[Whisper] 音声認識開始 / VAD閾値={self._vad_thresh}")
        self._update_status()
        self._mic_idle()

        # Whisper準備完了後にTTS起動発話（TTS ONかつ未発話の場合のみ）
        if self.tts.enabled and not self.tts._initial_greeting_done:
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
        self._btn_mic.pack(side=tk.LEFT, padx=(6, 0), pady=6)

        # 停止ボタン（TTS / 音声認識）
        self._btn_stop = tk.Button(
            in_box, text="⏹",
            command=self._stop_all,
            bg=C["bg_input"], fg=C["mic_on"],
            bd=0, font=(_FONT_ICON, 18),
            cursor="hand2",
        )
        self._btn_stop.pack(side=tk.LEFT, padx=(4, 0), pady=6)

        # 家計簿モードボタン
        self._btn_kakeibo = tk.Button(
            in_box, text="🏠",
            command=self._toggle_kakeibo,
            bg=C["bg_input"], fg=C["fg_sub"],
            bd=0, font=(_FONT_ICON, 18),
            cursor="hand2",
        )
        self._btn_kakeibo.pack(side=tk.LEFT, padx=(4, 0), pady=6)

        # 健康記録モードボタン
        self._btn_health = tk.Button(
            in_box, text="💪",
            command=self._toggle_health,
            bg=C["bg_input"], fg=C["fg_sub"],
            bd=0, font=(_FONT_ICON, 18),
            cursor="hand2",
        )
        self._btn_health.pack(side=tk.LEFT, padx=(4, 0), pady=6)

        # ─────────────────────────────────────────
        #  ★ バグ修正箇所 ①:
        #    tk.Text を直接 pack し、state は常に NORMAL のまま維持。
        #    _entry を disable にする処理を一切設けない。
        # ─────────────────────────────────────────
        self._entry = tk.Text(
            in_box,
            height=3,
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
        self._btn_send.pack(side=tk.RIGHT, padx=(0, 8), pady=6)

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
            self._voice._tts_active = True
            print("[TTS] 発話開始 → VAD感度を下げる")

    def _on_tts_stop(self) -> None:
        """TTS発話終了後にVAD閾値を元に戻す"""
        print(f"[TTS] 発話終了 → muted={getattr(self, '_tts_muted_mic', False)}")
        if self._voice:
            # 少し待ってから感度を戻す（残響が消えるのを待つ）
            self.root.after(800, self._restore_vad)

    def _restore_vad(self) -> None:
        """VAD感度を通常に戻す"""
        if self._voice and self.tts._q.empty():
            self._voice._tts_active = False
            print("[TTS] VAD感度を通常に戻す")

    def _restore_mic(self) -> None:
        pass  # 旧方式の残骸（互換性のため残す）

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

    def _confirm_and_send_kakeibo(self, record: dict) -> None:
        self._integrations.confirm_and_send_kakeibo(record)

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

    def _confirm_and_send_biolog(self, record: dict) -> None:
        self._integrations.confirm_and_send_biolog(record)

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
    def _update_summary(self) -> None:
        if self.llm is None or self._is_thinking:
            return
        try:
            history_text = "\n".join(
                f"User: {h['user']}\nAssistant: {h.get('assistant', '')}"
                for h in self._current_session.get("history", [])
                if h.get("assistant")
            )
            prompt = (
                "以下の会話を50文字以内の日本語で1行に要約してください。\n\n"
                f"{history_text}\n\n要約:"
            )
            self.llm.reset()
            res = self.llm(
                prompt, max_tokens=80, temperature=0.3, stop=["\n"])
            summary = res["choices"][0]["text"].strip()
            if summary:
                self._current_session["summary"] = summary
                self.root.after(
                    0,
                    lambda s=summary: self._summary_var.set(
                        f"📝 メモリ: {s}"))
                self.root.after(0, self._save_now)
        except Exception as e:
            print(f"[要約エラー] {e}")

    # ══════════════════════════════════════════════
    #  セッション管理
    # ══════════════════════════════════════════════
    def _new_session(self) -> None:
        if self._is_thinking:
            messagebox.showwarning(
                "警告", "AI が応答中です。しばらくお待ちください。")
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
            self.root.after(0, self._refresh_chat_list)
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
        self._is_thinking     = False   # 安全のためリセット
        self._btn_send.config(state=tk.NORMAL)

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
            mic_enabled = self._cfg.get("mic_enabled", False),
            tts_enabled   = self.tts.enabled,
        ))
        self.root.wait_window(dlg)
        if dlg.result is None:
            return

        new = dlg.result
        model_changed = (
            new["model_path"] != self._model_path
            or new["n_ctx"] != self._n_ctx
        )
        self._max_tokens  = new["max_tokens"]
        self._temperature = new["temperature"]
        self._vad_thresh  = new["vad_threshold"]
        if self._voice:
            self._voice.vad_threshold = self._vad_thresh
            self._voice.enabled = new["mic_enabled"]
        self.tts.enabled = new["tts_enabled"]
        self._tts_var.set(new["tts_enabled"])  # メニューのチェック状態を同期

        self._cfg.update(new)
        save_settings(self._cfg)

        if model_changed:
            self._model_path = new["model_path"]
            self._n_ctx      = new["n_ctx"]
            self._reload_llm()
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
        think = "⏳ 生成中…" if self._is_thinking else "✅ 待機中"
        mn    = os.path.basename(self._model_path)
        turns = len(self._current_session.get("history", []))
        guest = " [ゲスト]" if self._is_guest else ""
        self._status_var.set(
            f"{think}{guest} | {mn} | "
            f"{self._max_tokens}tok / temp:{self._temperature} | "
            f"{turns}ターン | {mic_stat}")

    # ══════════════════════════════════════════════
    #  終了処理
    # ══════════════════════════════════════════════
    def _on_close(self) -> None:
        if self._voice:
            self._voice.stop()
            self._cfg["mic_enabled"] = self._voice.enabled
        self._cfg["tts_enabled"] = self.tts.enabled
        save_settings(self._cfg)
        self.tts.stop_all()
        self._save_now()
        self.root.after(200, self.root.destroy)


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
