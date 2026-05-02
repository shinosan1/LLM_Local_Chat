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
import datetime
import threading
import queue
import time
import struct
import math
import random

import tkinter as tk
from tkinter import (
    scrolledtext, Menu, BooleanVar,
    messagebox, filedialog, simpledialog,
)

from llama_cpp import Llama
from PIL import Image, ImageTk
import pyttsx3
import pyaudio
import whisper
import numpy as np

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

HISTORY_BUDGET_RATIO = 0.60
SYSTEM_BUF_TOKENS    = 256
SUMMARY_THRESHOLD    = 4       # 何ターンごとに要約を更新するか

# ── 音声 (VAD) ──────────────────────────────
AUDIO_RATE         = 16000
AUDIO_CHUNK        = 1024
AUDIO_FORMAT       = pyaudio.paInt16
DEFAULT_VAD_RMS    = 150
VAD_SPEECH_CHUNKS  = 6
VAD_SILENCE_CHUNKS = 30
VAD_MAX_SECONDS    = 6

WHISPER_NOISE = {
    "字幕", "翻訳",
    "。", "、", "…", " ", "　",
}
# 動画系 hallucination 専用（部分一致フィルター）
WHISPER_NOISE_PARTIAL = {
    "ご視聴ありがとうございました",
    "チャンネル登録をお願い",
    "チャンネル登録よろしく",
    "高評価よろしくお願い",
    "いいねとチャンネル登録",
    "概要欄をご確認",
    "私のビデオを見て",
    "動画を見てください",
    "チャンネルに登録",
}

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
FONT_BOLD  = ("Meiryo", 10, "bold")
FONT_TITLE = ("Meiryo", 12, "bold")
FONT_CHAT  = ("Meiryo", 11)
FONT_SMALL = ("Meiryo",  9)


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
    )
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                defaults.update(json.load(f))
        except Exception:
            pass
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
def init_llm(model_path: str, n_ctx: int) -> Llama:
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"モデルが見つかりません:\n{model_path}")
    return Llama(
        model_path   = model_path,
        n_ctx        = n_ctx,
        n_threads    = 8,
        n_gpu_layers = -1,
        n_batch      = 512,
        verbose      = False,
    )


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

        # 温度
        lbl(3, "温度 (0.0 – 2.0):")
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
                raise ValueError("温度は 0.0〜2.0 の範囲で入力してください")
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
        self.win.attributes("-topmost", True, "-transparentcolor", "black")
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
#  ■ TTS ワーカー
# ═══════════════════════════════════════════════════════
class TTSWorker:
    def __init__(self, avatar: AvatarWindow, root: tk.Tk) -> None:
        self.avatar      = avatar
        self.root        = root
        self.enabled     = True
        self.on_start    = None   # TTS開始時コールバック
        self.on_stop     = None   # TTS終了時コールバック
        self._q          = queue.Queue()
        self._stop       = threading.Event()
        self._engine_ref = None
        threading.Thread(target=self._run, daemon=True).start()

    def speak(self, text: str) -> None:
        if self.enabled and text.strip():
            # 長すぎるテキストは先頭200文字に切り詰める
            text = text[:200]
            self._q.put(text)
            if self.on_start:
                self.root.after(0, self.on_start)

    def stop_all(self) -> None:
        """キューをクリアして再生中も停止"""
        self._stop.set()
        with self._q.mutex:
            self._q.queue.clear()
        try:
            self._engine_ref.stop()
            self._engine_ref.endLoop()
        except Exception:
            pass
        if self.on_stop:
            self.root.after(0, self.on_stop)

    def _run(self) -> None:
            import subprocess
            import time

            def _speak_sapi(text: str) -> None:
                # 発話関数自体でも enabled をチェックするようにガード
                if not self.enabled:
                    return

                safe_text = text.replace("'", "''")
                speed = 2
                ps = (
                    "Add-Type -AssemblyName System.Speech;"
                    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                    f"$s.Rate = {speed};"
                    "$s.SetOutputToDefaultAudioDevice();"
                    f"$s.Speak('{safe_text}');"
                )
                try:
                    subprocess.run(
                        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                        timeout=60,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except Exception as e:
                    print(f"[TTS PowerShell エラー] {e}")

            # ─── 起動時の処理 ───
            # 10秒待機してから、enabled の場合のみ起動発話
            time.sleep(10.0) 
            if self.enabled:
                _speak_sapi("システムを起動しました。")
                print("[TTS] 起動発話完了")

            # ─── メインループ ───
            while True:
                text = self._q.get()
                
                # キューから取得したテキストが空ならスキップ
                if not text:
                    self._q.task_done()
                    continue

                # stopフラグの判定
                stopped = self._stop.is_set()
                self._stop.clear()

                if stopped:
                    self._q.task_done()
                    continue

                try:
                    # enabled でない場合はスキップ
                    if not self.enabled:
                        continue

                    self.root.after(0, self.avatar.start_speaking)
                    # Bluetoothデバイス等の復帰待ち
                    time.sleep(0.5)
                    _speak_sapi(text)
                except Exception as e:
                    print(f"[TTS エラー] {e}")
                finally:
                    # キューが空ならアバターの口パクを止める
                    if self._q.empty():
                        self.root.after(0, self.avatar.stop_speaking)
                        if self.on_stop:
                            self.root.after(0, self.on_stop)
                    self._q.task_done()

# ═══════════════════════════════════════════════════════
#  ■ VAD + Whisper 音声認識
# ═══════════════════════════════════════════════════════
class VoiceRecognizer:
    """
    PyAudio 直接制御 + RMS-VAD による音声認識。
    whisper モデルは外部から渡す（バックグラウンドでロード済み）。
    マイクが存在しない環境でも例外を出さずに動作する。
    """
    def __init__(
        self,
        whisper_model,
        on_text,
        vad_threshold: int = DEFAULT_VAD_RMS,
    ) -> None:
        # 1. まず属性を初期化する
        self.whisper_model = whisper_model
        self.on_text = on_text
        self.vad_threshold = vad_threshold
        
        # 2. スレッド制御用のフラグを定義（★ここが重要）
        self._enabled = threading.Event() 
        self._active = True
        self._tts_active = False
        self._flush_request = False
        
        # 3. コールバックの初期化
        self.on_idle = None
        self.on_listening = None
        self.on_processing = None

        # 初期状態は無効に設定
        self._enabled.clear()

        # 4. モデルがある場合のみスレッドを開始
        if self.whisper_model is not None:
            threading.Thread(target=self._loop, daemon=True).start()
        else:
            print("[VoiceRecognizer] Whisper未ロードのためスレッドを開始しません。")
    @property
    def enabled(self) -> bool:
        return self._enabled.is_set()

    @enabled.setter
    def enabled(self, v: bool) -> None:
        if v:
            self._flush_request = True  # 復帰時にバッファをフラッシュ
            self._enabled.set()
        else:
            self._enabled.clear()

    @staticmethod
    def _rms(data: bytes) -> float:
        n = len(data) // 2
        if n == 0:
            return 0.0
        shorts = struct.unpack(f"{n}h", data[: n * 2])
        return math.sqrt(sum(s * s for s in shorts) / n)

    def _fire(self, cb) -> None:
        """コールバックを安全に呼ぶ"""
        if cb:
            try:
                cb()
            except Exception:
                pass

    def _loop(self) -> None:
        # Bluetoothデバイスが既定の場合、接続確立を待つ
        time.sleep(2.0)
        pa = pyaudio.PyAudio()
        try:
            stream = pa.open(
                format=AUDIO_FORMAT,
                channels=1,
                rate=AUDIO_RATE,
                input=True,
                frames_per_buffer=AUDIO_CHUNK,
            )
        except Exception as e:
            print(f"[マイク初期化エラー] {e}  (音声認識は無効になります)")
            pa.terminate()
            return

        try:
            _last_text      = ""
            _last_text_time = 0.0
            _SAME_TEXT_EXPIRE = 10.0  # 同一テキストの有効期限（秒）
            while self._active:
                if not self._enabled.is_set():
                    self._fire(self.on_idle)
                    time.sleep(0.1)
                    continue

                self._fire(self.on_idle)

                # マイク復帰直後はバッファをフラッシュして古い音を捨てる
                if self._flush_request:
                    self._flush_request = False
                    try:
                        for _ in range(8):  # 約500ms分を読み捨て
                            stream.read(AUDIO_CHUNK, exception_on_overflow=False)
                    except Exception:
                        pass

                # ── 発話開始待ち ─────────────────────
                consecutive = 0
                pre_buf: list[bytes] = []
                while self._active and self._enabled.is_set():
                    try:
                        chunk = stream.read(
                            AUDIO_CHUNK, exception_on_overflow=False)
                    except Exception:
                        time.sleep(0.05)
                        continue
                    # TTS発話中はバッファを蓄積せず即スキップ
                    # （閾値10倍では開放型イヤホン環境でTTS音声を拾ってしまうため）
                    if self._tts_active:
                        consecutive = 0
                        pre_buf.clear()
                        continue
                    pre_buf.append(chunk)
                    if len(pre_buf) > VAD_SPEECH_CHUNKS + 2:
                        pre_buf.pop(0)
                    rms = self._rms(chunk)
                    if rms > self.vad_threshold:
                        consecutive += 1
                        if consecutive >= VAD_SPEECH_CHUNKS:
                            print(f"[VAD] 発話検知 RMS={rms:.1f} threshold={self.vad_threshold}")
                            break
                    else:
                        consecutive = 0

                if not (self._active and self._enabled.is_set()):
                    continue

                self._fire(self.on_listening)

                # ── 発話録音 ─────────────────────────
                frames        = list(pre_buf)
                silence_count = 0
                max_chunks    = VAD_MAX_SECONDS * AUDIO_RATE // AUDIO_CHUNK

                while self._active and len(frames) < max_chunks:
                    try:
                        chunk = stream.read(
                            AUDIO_CHUNK, exception_on_overflow=False)
                    except Exception:
                        break
                    # 録音中にTTSが始まったら録音データを破棄してやり直し
                    if self._tts_active:
                        frames = []
                        break
                    frames.append(chunk)
                    if self._rms(chunk) < self.vad_threshold:
                        silence_count += 1
                        if silence_count >= VAD_SILENCE_CHUNKS:
                            break
                    else:
                        silence_count = 0

                # TTS中に録音が破棄された場合はWhisper処理をスキップ
                if not frames:
                    continue

                self._fire(self.on_processing)

                # ── Whisper 認識 ──────────────────────
                if self.whisper_model is None:
                    continue

                audio_np = (
                    np.frombuffer(b"".join(frames), dtype=np.int16)
                    .astype(np.float32) / 32768.0
                )
                try:
                    _t = time.time()
                    _on_gpu = str(
                        next(self.whisper_model.parameters()).device
                    ) != "cpu"
                    res = self.whisper_model.transcribe(
                        audio_np,
                        language="ja",
                        fp16=_on_gpu,
                        no_speech_threshold=0.8,
                        logprob_threshold=-1.5,
                        condition_on_previous_text=False,
                        initial_prompt="自己紹介、こんにちは、ありがとうございます。",
                        beam_size=1,
                        best_of=1,
                        temperature=0.0,
                    )
                    text = res.get("text", "").strip()
                    print(f"[Whisper] {time.time()-_t:.1f}秒 tts_active={self._tts_active} 結果='{text}'")
                    _noise = (
                        not text
                        or len(text) < 2
                        or len(text) > 100   # 100文字超はhallucination
                        or text in WHISPER_NOISE
                        or all(c in "。、…　 " for c in text)
                        or any(p in text for p in WHISPER_NOISE_PARTIAL)
                    )
                    if not _noise:
                        now = time.time()
                        # 同一テキストでも一定時間経過後は有効とする
                        if (text != _last_text
                                or now - _last_text_time > _SAME_TEXT_EXPIRE):
                            _last_text      = text
                            _last_text_time = now
                            self.on_text(text)
                except Exception as e:
                    print(f"[Whisper エラー] {e}")
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            pa.terminate()

    def stop(self) -> None:
        self._active = False


# ═══════════════════════════════════════════════════════
#  ■ メインアプリ
# ═══════════════════════════════════════════════════════
class ChatApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
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

        # ── ランタイム変数 ────────────────────────
        self._is_thinking  = False
        self._llm_abort    = False   # 生成中断フラグ
        self._is_guest     = False
        self._llm_loading  = True
        self.llm: Llama | None        = None
        self._whisper_model           = None   # バックグラウンドでロード
        self._files: list[str]        = []
        self._current_session: dict   = {}
        self._current_path: str | None = None
        self._voice: VoiceRecognizer | None = None

        os.makedirs(LOG_DIR, exist_ok=True)

        # ── アバター ──────────────────────────────
        self.avatar = AvatarWindow(root)

        # ── TTS ───────────────────────────────────
        self.tts         = TTSWorker(self.avatar, root)
        self.tts.enabled = self._cfg.get("tts_enabled", False)
        # TTS発話中はVADを抑制してハウリング・誤認識を防ぐ
        self.tts.on_start = self._on_tts_start
        self.tts.on_stop  = self._on_tts_stop

        # ── UI 構築 ───────────────────────────────
        self._build_ui()

        # ── 初期セッション ────────────────────────
        self._new_session()
        self._refresh_chat_list()

        # ── バックグラウンドロード ─────────────────
        self._reload_llm()
        self._load_whisper_async()

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
                llm = init_llm(self._model_path, self._n_ctx)
                self.root.after(0, lambda: self._on_llm_ready(llm, None))
            except Exception as e:
                self.root.after(0, lambda err=e: self._on_llm_ready(None, err))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_llm_ready(self, llm, err) -> None:
        self._llm_loading = False
        if llm:
            self.llm = llm
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
            self.root.after(0, lambda: self._on_whisper_ready(None))
            return
        
        def _worker() -> None:
            import torch
            cuda_ok = torch.cuda.is_available()
            print(f"[Whisper] CUDA利用可能: {cuda_ok}")
            if cuda_ok:
                print(f"[Whisper] GPU: {torch.cuda.get_device_name(0)}"
                      f" / VRAM空き: {torch.cuda.mem_get_info()[0]//1024**2}MB")

            devices = [("cuda", "medium")] if cuda_ok else []
            devices.append(("cpu", "medium"))

            for device, model_name in devices:
                try:
                    print(f"[Whisper] モデルロード開始 ({model_name} / {device}) …")
                    wm = whisper.load_model(model_name, device=device)
                    print(f"[Whisper] モデルロード完了 ({model_name} / {device})")
                    self.root.after(0, lambda m=wm: self._on_whisper_ready(m))
                    return
                except Exception as e:
                    print(f"[Whisper ロードエラー ({device})] {type(e).__name__}: {e}")
            self.root.after(0, lambda: self._on_whisper_ready(None))

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
            font=("Meiryo", 10, "bold"))
        self._chat_text.tag_config(
            "user_msg",
            foreground="#FFFFFF",
            font=FONT_CHAT,
            lmargin1=24, lmargin2=24)
        self._chat_text.tag_config(
            "ai_lbl",
            foreground=C["fg_sub"],
            font=("Meiryo", 10, "bold"))
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
            bd=0, font=("Segoe UI Symbol", 18),
            cursor="hand2",
        )
        self._btn_mic.pack(side=tk.LEFT, padx=(6, 0), pady=6)

        # 停止ボタン（TTS / 音声認識）
        self._btn_stop = tk.Button(
            in_box, text="⏹",
            command=self._stop_all,
            bg=C["bg_input"], fg=C["mic_on"],
            bd=0, font=("Segoe UI Symbol", 18),
            cursor="hand2",
        )
        self._btn_stop.pack(side=tk.LEFT, padx=(4, 0), pady=6)

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
            font=("Meiryo", 8),
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
            os.remove(target)
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
            with open(target, encoding="utf-8") as f:
                data = json.load(f)
            data["title"] = new_title
            with open(target, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            messagebox.showerror("名前変更エラー", str(e))
            return

        if self._current_path == target:
            self._current_session["title"] = new_title
            self._title_var.set(new_title)

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
            messagebox.showinfo(
                "音声認識", "Whisper モデルを読み込んでいます。\nしばらくお待ちください。")
            return
        self._voice.enabled = not self._voice.enabled
        self._btn_mic.config(
            fg=C["mic_on"] if self._voice.enabled else C["mic_off"])
        self._update_status()

    def _stop_all(self) -> None:
        """LLM生成・TTS・マイク入力をすべて即時停止する"""
        # ① LLM生成を強制中断（別スレッドでreset）
        if self._is_thinking:
            self._llm_abort = True
            def _force_reset():
                try:
                    self.llm.reset()
                except Exception:
                    pass
            threading.Thread(target=_force_reset, daemon=True).start()

        # ② TTS停止
        self.tts.stop_all()
        self.root.after(0, self.avatar.stop_speaking)

        # ③ UIを待機状態に戻す（マイクはOFFにしない）
        self._is_thinking = False
        self._llm_abort   = False
        self._stream_buf  = ""
        self._btn_send.config(state=tk.NORMAL)
        self._chat_write("\n⛔ 停止しました\n" + "─" * 50 + "\n\n", "divider")
        self._update_status()

    def _voice_input(self, text: str) -> None:
        if self._is_thinking or not text.strip():
            return
        self._entry.insert(tk.END, text)
        self._send()

    # ══════════════════════════════════════════════
    #  チャット送信
    # ══════════════════════════════════════════════
    def _send(self) -> None:
        # ─────────────────────────────────────────
        #  ★ バグ修正箇所 ③:
        #    _is_thinking のチェックのみ行い、
        #    LLM がまだロード中でも入力テキストは受け付ける。
        #    (llm is None の場合はエラーを表示して抜ける)
        # ─────────────────────────────────────────
        if self._is_thinking:
            return

        if self.llm is None:
            messagebox.showwarning(
                "準備中",
                "モデルを読み込んでいます。\nしばらくお待ちください。")
            return

        text = self._entry.get("1.0", tk.END).strip()
        if not text:
            return

        self._entry.delete("1.0", tk.END)
        self._llm_abort   = False   # 新規送信時は中断フラグをクリア
        self._is_thinking = True
        self._btn_send.config(state=tk.DISABLED)
        self._update_status()

        self._chat_write("\n", "")
        self._chat_write("👤 あなた\n", "user_lbl")
        self._chat_write(f"{text}\n", "user_msg")
        self._chat_write("─" * 50 + "\n", "divider")
        self._chat_write("🤖 AI\n", "ai_lbl")
        # ストリーミングカーソル位置を記録
        self._stream_mark = self._chat_text.index(tk.END)
        self._stream_buf  = ""

        threading.Thread(
            target=self._llm_worker, args=(text,), daemon=True).start()

    # ── LLM ワーカー（別スレッド） ───────────────
    def _llm_worker(self, user_text: str) -> None:
        reply = ""
        try:
            self.llm.reset()

            messages = build_messages_safe(
                self.llm,
                self._current_session.get("history", []),
                user_text,
                self._n_ctx,
                self._max_tokens,
                summary=self._current_session.get("summary", ""),
            )

            for chunk in self.llm.create_chat_completion(
                messages    = messages,
                max_tokens  = self._max_tokens,
                temperature = self._temperature,
                stream      = True,
            ):
                if self._llm_abort:
                    break
                delta = chunk["choices"][0].get("delta", {})
                token = delta.get("content", "")
                if token:
                    reply += token
                    self.root.after(
                        0, lambda t=token: self._append_stream_token(t))

            # 中断された場合は履歴に残さず終了
            if self._llm_abort:
                print("[LLM] 中断されました")
                self._llm_abort = False
                return

            reply = reply.strip()
            print(f"[LLM] 生成完了 ({len(reply)}文字)")
            self.root.after(0, lambda r=reply: self._on_llm_done(user_text, r))

        except Exception as e:
            print(f"[LLM] 例外発生: {type(e).__name__}: {e}")
            err = f"[エラー: {e}]"
            self.root.after(
                0, lambda m=err, u=user_text: self._on_llm_error(u, m))

    # ── ストリーミングトークン追記（メインスレッド） ─
    def _append_stream_token(self, token: str) -> None:
        self._stream_buf += token
        self._chat_write(token, "ai_msg")

    # ── LLM 完了（メインスレッド） ───────────────
    def _on_llm_done(self, user_text: str, reply: str) -> None:
        # 停止ボタンで中断された場合は何もしない
        if self._llm_abort:
            print("[LLM] _on_llm_done: abort済みのためスキップ")
            return
        print(f"[LLM] _on_llm_done 呼び出し")
        if not self._stream_buf:
            # ストリーミング出力が 0 の場合（まれ）
            self._chat_write(reply, "ai_msg")
        self._chat_write("\n" + "─" * 50 + "\n\n", "divider")

        # 履歴更新
        self._current_session.setdefault("history", []).append(
            {"user": user_text, "assistant": reply})

        # タイトル自動生成
        if self._current_session.get("title") == "新しいチャット":
            t = user_text.replace("\n", " ").strip()
            self._current_session["title"] = (
                t[:20] + ("…" if len(t) > 20 else ""))
            self._title_var.set(self._current_session["title"])

        # 要約更新（閾値超過時）
        h = self._current_session["history"]
        if len(h) >= SUMMARY_THRESHOLD and len(h) % SUMMARY_THRESHOLD == 0:
            threading.Thread(
                target=self._update_summary, daemon=True).start()

        # TTS
        self.tts.speak(reply)

        # 保存
        self._save_now()

        # ─────────────────────────────────────────
        #  ★ バグ修正箇所 ⑤:
        #    _is_thinking = False と btn_send NORMAL への復元を
        #    このメソッド（メインスレッド）で確実に実行する。
        # ─────────────────────────────────────────
        self._is_thinking = False
        self._btn_send.config(state=tk.NORMAL)
        self._stream_buf  = ""
        self._update_status()
        self._entry.focus_set()     # 送信完了後すぐに入力欄へフォーカス

    # ── LLM エラー（メインスレッド） ─────────────
    def _on_llm_error(self, user_text: str, err_msg: str) -> None:
        self._chat_write(err_msg + "\n", "err")
        self._chat_write("─" * 50 + "\n\n", "divider")

        self._current_session.setdefault("history", []).append(
            {"user": user_text, "assistant": err_msg})

        self._is_thinking = False
        self._btn_send.config(state=tk.NORMAL)
        self._stream_buf  = ""
        self._update_status()
        self._entry.focus_set()

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
        self._current_session = {
            "title":   "新しいチャット",
            "history": [],
            "summary": "",
        }
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
        history = self._current_session.get("history", [])
        if not history:
            return
        if not self._current_path:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            self._current_path = os.path.join(
                LOG_DIR, f"chat_{ts}.json")
        try:
            with open(self._current_path, "w", encoding="utf-8") as f:
                json.dump(
                    self._current_session, f,
                    ensure_ascii=False, indent=2)
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
        if not os.path.exists(LOG_DIR):
            return
        kw = self._search_var.get().strip().lower()
        for fn in sorted(os.listdir(LOG_DIR), reverse=True):
            if not fn.endswith(".json"):
                continue
            fp = os.path.join(LOG_DIR, fn)
            try:
                with open(fp, encoding="utf-8") as f:
                    d = json.load(f)
                title   = d.get("title", fn)
                summary = d.get("summary", "")
                # 部分一致: タイトル + 要約を対象
                if kw and kw not in (title + summary).lower():
                    continue
                self._chat_list.insert(tk.END, f"  {title}")
                self._files.append(fp)
            except Exception:
                pass

    def _load_selected(self, _event=None) -> None:
        sel = self._chat_list.curselection()
        if not sel:
            return
        fp = self._files[sel[0]]
        try:
            with open(fp, encoding="utf-8") as f:
                data = json.load(f)
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
            mic_enabled   = self._voice.enabled if self._voice else False,
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
if __name__ == "__main__":
    root = tk.Tk()
    app  = ChatApp(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()
