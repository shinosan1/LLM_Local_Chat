"""
Controller: 薄いオーケストレーター。判断のみ行い、実行は各サービスに委譲する。
  - LLMService    : ストリーミング推論（純実行）
  - PromptBuilder : messages 構築（純構築）
  - ResourceManager: max_tokens 決定（純計算）
"""
from dataclasses import dataclass
import threading
import time
from typing import Any, Callable
from tkinter import messagebox

from json_extractors import extract_health_json, extract_kakeibo_json
from llm_service import LLMService
from prompt_builder import PromptBuilder
from resource_manager import ResourceManager


@dataclass(frozen=True)
class ControllerDeps:
    res_monitor: Any
    whisper_pool: Any
    strip_code_blocks: Callable[[str], str]
    summary_threshold: int


class Controller:
    def __init__(self, app, deps: ControllerDeps):
        self._app = app
        self._res_monitor       = deps.res_monitor
        self._whisper_pool      = deps.whisper_pool
        self._strip_code_blocks = deps.strip_code_blocks
        self._SUMMARY_THRESHOLD = deps.summary_threshold

        self._llm_service = LLMService(app.llm)
        self._prompt_builder = PromptBuilder(self._app.SYSTEM_PROMPT)
        self._resource_mgr = ResourceManager(self._res_monitor)
        self._token_cost_cache: dict[tuple[str, str], int] = {}

    def clear_token_cache(self) -> None:
        self._token_cost_cache.clear()

    # ── ボイス入力 ──────────────────────────────────────────
    def handle_voice(self, text: str) -> None:
        if self._app._is_thinking or not text.strip():
            return
        self._app._entry.insert("end", text)
        self.handle_text()

    # ── チャット送信 ──────────────────────────────────────
    def handle_text(self) -> None:
        if self._app._is_thinking:
            return

        if self._app.llm is None:
            messagebox.showwarning(
                "準備中",
                "モデルを読み込んでいます。\nしばらくお待ちください。")
            return

        text = self._app._entry.get("1.0", "end").strip()
        if not text:
            return

        request_started_at = time.perf_counter()
        self._app._entry.delete("1.0", "end")
        self._app._llm_abort   = False
        self._app._is_thinking = True
        self._app._btn_send.config(state="disabled")

        self._app._update_status()

        self._app._chat_write("\n", "")
        self._app._chat_write("👤 あなた\n", "user_lbl")
        self._app._chat_write(f"{text}\n", "user_msg")
        self._app._chat_write("─" * 50 + "\n", "divider")
        self._app._chat_write("🤖 AI\n", "ai_lbl")
        self._app._stream_mark = self._app._chat_text.index("end")
        self._app._stream_buf  = ""

        # mode決定（1回だけ）
        if self._app._kakeibo_mode:
            mode = "kakeibo"
        elif self._app._health_mode:
            mode = "health"
        else:
            mode = "default"

        # pending状態管理 + llm_input生成（modeベースで1回だけ）
        if mode == "kakeibo":
            self._app._kakeibo_pending_text = text
            self._app._health_pending_text  = None
            llm_input = self._prompt_builder.build_kakeibo_prompt(text)
        elif mode == "health":
            self._app._health_pending_text  = text
            self._app._kakeibo_pending_text = None
            llm_input = self._prompt_builder.build_health_prompt(text)
        else:
            self._app._kakeibo_pending_text = None
            self._app._health_pending_text  = None
            llm_input = text
        # messages構築
        messages = self._prompt_builder.build(
            llm_input,
            self._app._current_session,
            mode=mode,
            llm=self._app.llm,
            n_ctx=self._app._n_ctx,
            max_tokens=self._app._max_tokens,
            token_cost_cache=self._token_cost_cache,
            count_tokens_func=self._app._count_tokens,
            history_budget_ratio=self._app._history_budget_ratio,
            system_buf_tokens=self._app._system_buf_tokens,
        )
        prompt_build_ms = (time.perf_counter() - request_started_at) * 1000

        # ② VRAM / max_tokens 決定（ResourceManager の責務）
        decision = self._resource_mgr.decide(
            self._app._max_tokens,
            self._whisper_pool._ctrl.delta_gpu_pct,
        )
        if not decision["ok"]:
            _msg = f"VRAM使用率が高いため実行できません（{decision['reason']}）"
            print(f"[Guard] {_msg}")
            total_ms = (time.perf_counter() - request_started_at) * 1000
            print(
                f"[Perf] prompt_build_ms={prompt_build_ms:.1f} "
                f"first_token_ms=n/a tokens_per_sec=0.00 "
                f"total_ms={total_ms:.1f} status=guard_blocked"
            )
            self._on_llm_done(llm_input, _msg)
            return

        # ③ LLM 実行（LLMService に完全委譲）
        self._llm_service.generate(
            messages    = messages,
            max_tokens  = decision["max_tokens"],
            temperature = self._app._temperature,
            on_token = lambda t: self._app.root.after(
                0, lambda _t=t: self._app._append_stream_token(_t)
            ),
            on_done = lambda r: self._app.root.after(
                0, lambda _r=r: self._on_llm_done(llm_input, _r)
            ),
            on_error = lambda e: self._app.root.after(
                0, lambda _e=e: self._on_llm_error(llm_input, f"[エラー: {_e}]")
            ),
            request_started_at = request_started_at,
            prompt_build_ms = prompt_build_ms,
        )

    # ── 停止 ──────────────────────────────────────────────
    def stop(self) -> None:
        """LLM・TTS・マイクを完全に、即座に、依存関係なしで止める"""
        print("[System] 停止命令を執行します")

        self._app._llm_abort   = True
        self._app._is_thinking = True
        self._app._btn_send.config(state="disabled")

        self._app.tts.stop_all()
        self._app.avatar.win.after(0, self._app.avatar.stop_speaking)

        if self._app._voice:
            self._app._voice._tts_active = False
            self._app._voice.vad_threshold = self._app._vad_thresh

        # LLMService のストリーミングループを中断
        self._llm_service.abort()

        def _wait_and_unlock():
            # is_running() が False になるまで最大3秒待機
            for _ in range(30):
                if not self._llm_service.is_running():
                    break
                time.sleep(0.1)

            def _unlock():
                self._app._is_thinking = False
                self._app._llm_abort   = False
                self._app._stream_buf  = ""
                self._app._btn_send.config(state="normal")
                self._app._chat_write(
                    "\n⛔ 完全に停止しました\n" + "─" * 50 + "\n\n", "divider")
                self._app._update_status()
            self._app.root.after(0, _unlock)

        threading.Thread(target=_wait_and_unlock, daemon=True).start()

    # ── LLM 完了（メインスレッド） ─────────────────────
    def _on_llm_done(self, user_text: str, reply: str) -> None:
        if self._app._llm_abort:
            print("[LLM] _on_llm_done: abort済みのためスキップ")
            return
        print("[LLM] _on_llm_done 呼び出し")
        if not self._app._stream_buf:
            self._app._chat_write(reply, "ai_msg")
        print(f"[LLM] reply received ({len(reply)} chars)")
        self._app._chat_write("\n" + "─" * 50 + "\n\n", "divider")

        display_text = self._app._kakeibo_pending_text if self._app._kakeibo_pending_text else user_text

        self._app._current_session.setdefault("history", []).append(
            {"user": display_text, "assistant": reply})

        if self._app._current_session.get("title") == "新しいチャット":
            t = display_text.replace("\n", " ").strip()
            self._app._current_session["title"] = (
                t[:20] + ("…" if len(t) > 20 else ""))
            self._app._title_var.set(self._app._current_session["title"])

        h = self._app._current_session["history"]
        if len(h) >= self._SUMMARY_THRESHOLD and len(h) % self._SUMMARY_THRESHOLD == 0:
            threading.Thread(
                target=self._app._update_summary, daemon=True).start()
        print(f"[LLM] reply processed ({len(reply)} chars)")

        if self._app._kakeibo_pending_text:
            record = extract_kakeibo_json(reply)
            if record and record.get("amount"):
                self._app._confirm_and_send_kakeibo(record)
            else:
                self._app._chat_write(
                    "⚠ 家計簿データを抽出できませんでした。金額・店名を具体的に入力してください。\n",
                    "err",
                )
            self._app._kakeibo_pending_text = None

        biolog_record = None
        print("health_pending =", self._app._health_pending_text)
        if self._app._health_pending_text:
            biolog_record = extract_health_json(reply)
            if not biolog_record:
                self._app._chat_write(
                    "⚠ 健康データを抽出できませんでした。体重・食事内容を具体的に入力してください。\n",
                    "err",
                )
            self._app._health_pending_text = None

        self._app.tts.speak(self._strip_code_blocks(reply))

        if biolog_record:
            self._app._confirm_and_send_biolog(biolog_record)

        self._app._save_now()

        self._app._is_thinking = False
        self._app._btn_send.config(state="normal")
        self._app._stream_buf  = ""
        self._app._update_status()
        self._app._entry.focus_set()

    # ── LLM エラー（メインスレッド） ───────────────────
    def _on_llm_error(self, user_text: str, err_msg: str) -> None:
        self._app._chat_write(err_msg + "\n", "err")
        self._app._chat_write("─" * 50 + "\n\n", "divider")

        self._app._current_session.setdefault("history", []).append(
            {"user": user_text, "assistant": err_msg})

        self._app._is_thinking = False
        self._app._btn_send.config(state="normal")
        self._app._stream_buf  = ""
        self._app._update_status()
        self._app._entry.focus_set()
