"""
Controller: 薄いオーケストレーター。判断のみ行い、実行は各サービスに委譲する。
  - LLMService    : ストリーミング推論（純実行）
  - PromptBuilder : messages 構築（純構築）
  - ResourceManager: max_tokens 決定（純計算）
"""
from collections import OrderedDict
from dataclasses import dataclass
import json
import threading
import time
from typing import Any
from tkinter import messagebox

from integrations import BiologValidationError, prepare_biolog_record
from json_extractors import (
    extract_health_json,
    extract_kakeibo_json,
    extract_kakeibo_transactions,
    strip_health_json,
)
from kakeibo_split import (
    MAX_KAKEIBO_TRANSACTIONS_PER_INPUT,
    build_kakeibo_candidates,
)
from llm_service import LLMService
from prompt_builder import PromptBuilder, PromptInputTooLargeError
from prompt_inputs import (
    IMAGE_CONTEXT_TOKEN_RESERVE,
    PromptInputError,
    attachment_display_names,
    build_multimodal_user_content,
    format_text_attachment_input,
    validate_attachment_set,
)
from resource_manager import ResourceManager


STOP_WAIT_TIMEOUT_SECONDS = 3.0
STOP_WAIT_POLL_SECONDS = 0.1
STOP_WAIT_TIMEOUT_POLL_SECONDS = 0.5


class StreamingTTSBuffer:
    """ストリーミング応答を文単位に分割し、コードフェンス内を除外する。"""

    _BOUNDARIES = frozenset("。！？!?\n")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._text = ""
        self._in_code_fence = False
        self._backticks = 0

    def feed(self, chunk: str) -> list[str]:
        sentences: list[str] = []
        for char in chunk:
            if char == "`":
                self._backticks += 1
                if self._backticks == 3:
                    self._in_code_fence = not self._in_code_fence
                    self._backticks = 0
                continue

            if self._backticks:
                if not self._in_code_fence:
                    self._text += "`" * self._backticks
                self._backticks = 0

            if self._in_code_fence:
                continue

            self._text += char
            if char in self._BOUNDARIES:
                sentence = self._text.strip()
                self._text = ""
                if sentence:
                    sentences.append(sentence)
        return sentences

    def finalize(self) -> str:
        if self._backticks and not self._in_code_fence:
            self._text += "`" * self._backticks
        sentence = self._text.strip()
        self.reset()
        return sentence


class LeadingJsonFilter:
    """健康応答の先頭JSONだけを隠し、後続の自然文を透過する。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._buffer = ""
        self._state = "detecting"

    def feed(self, chunk: str) -> str:
        if self._state == "passthrough":
            return chunk
        if self._state == "after_json":
            visible = chunk.lstrip("\r\n")
            if not visible:
                return ""
            self._state = "passthrough"
            return visible

        self._buffer += chunk
        stripped = self._buffer.lstrip()
        if not stripped:
            return ""

        if stripped.startswith("{"):
            try:
                _, end = json.JSONDecoder().raw_decode(stripped)
            except json.JSONDecodeError:
                return ""
            remainder = stripped[end:].lstrip("\r\n")
            self._buffer = ""
            self._state = "passthrough" if remainder else "after_json"
            return remainder

        if stripped.startswith("`"):
            if len(stripped) < 3:
                return ""
            if not stripped.startswith("```"):
                return self._release()
            header_end = stripped.find("\n")
            if header_end < 0:
                return ""
            header = stripped[:header_end].strip().lower()
            if header not in ("```", "```json"):
                return self._release()
            closing = stripped.find("```", header_end + 1)
            if closing < 0:
                return ""
            remainder = stripped[closing + 3:].lstrip("\r\n")
            self._buffer = ""
            self._state = "passthrough" if remainder else "after_json"
            return remainder

        return self._release()

    def _release(self) -> str:
        text = self._buffer
        self._buffer = ""
        self._state = "passthrough"
        return text

    def finalize(self) -> str:
        if self._state in ("passthrough", "after_json"):
            return ""
        self._buffer = ""
        return ""


class TokenCostCache(OrderedDict):
    """モデル再読込まで使う、件数制限付きのLRUキャッシュ。"""

    def __init__(self, max_entries: int = 2048) -> None:
        super().__init__()
        self.max_entries = max_entries

    def get(self, key, default=None):
        if key not in self:
            return default
        self.move_to_end(key)
        return super().get(key)

    def __setitem__(self, key, value) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self.max_entries:
            self.popitem(last=False)


@dataclass(frozen=True)
class ControllerDeps:
    res_monitor: Any
    whisper_pool: Any
    summary_threshold: int


class Controller:
    def __init__(self, app, deps: ControllerDeps):
        self._app = app
        self._res_monitor       = deps.res_monitor
        self._whisper_pool      = deps.whisper_pool
        self._SUMMARY_THRESHOLD = deps.summary_threshold

        self._llm_service = LLMService(app.llm)
        self._prompt_builder = PromptBuilder(self._app.SYSTEM_PROMPT)
        self._resource_mgr = ResourceManager(self._res_monitor)
        self._token_cost_cache = TokenCostCache(max_entries=2048)
        self._tts_stream = StreamingTTSBuffer()
        self._health_json_filter = LeadingJsonFilter()
        self._active_mode = "default"
        self._active_attachments = ()
        self._operation_generation = 0
        self._operation_state = "idle"
        self._request_id = 0

    def clear_token_cache(self) -> None:
        self._token_cost_cache.clear()

    def _consume_whisper_gpu_delta(self) -> float:
        consume = getattr(self._whisper_pool, "consume_delta_gpu_pct", None)
        if callable(consume):
            return consume()
        delta = self._whisper_pool._ctrl.delta_gpu_pct
        self._whisper_pool._ctrl.delta_gpu_pct = 0.0
        return delta

    def _post_ui(self, callback) -> None:
        post = getattr(self._app, "_post_ui", None)
        if callable(post):
            post(callback)
        else:
            self._app.root.after(0, callback)

    def is_busy(self) -> bool:
        # 家計簿の確認・POSTシーケンス中も busy とする。前のPOST完了を待つ間は
        # 操作状態が idle に戻っているため、これが無いと次の送信を受け付けてしまい、
        # LLM処理を最後まで走らせてから「登録処理中」と断るUXになる。
        return (
            self._operation_state != "idle"
            or self._llm_service.is_running()
            or bool(getattr(self._app, "_llm_loading", False))
            or bool(getattr(self._app, "_kakeibo_sequence_active", False))
        )

    def operation_label(self) -> str:
        label = {
            "generating": "⏳ 生成中…",
            "extracting_health": "🩺 健康データ再抽出中…",
            "summarizing": "📝 要約中…",
            "stopping": "⏳ 停止処理中…",
            "reloading_llm": "⏳ LLM再読み込み中…",
        }.get(self._operation_state)
        if label is not None:
            return label
        # 生成自体は終わっていても家計簿の確認・POSTシーケンスが続いている間は
        # is_busy() が真なので、待機中とは表示しない。
        if getattr(self._app, "_kakeibo_sequence_active", False):
            return "🏠 家計簿確認・登録中…"
        return "✅ 待機中"

    def _begin_operation(self, state: str) -> int:
        self._operation_generation += 1
        self._operation_state = state
        self._app._is_thinking = True
        self._app._btn_send.config(state="disabled")
        set_attachment_controls = getattr(
            self._app, "_set_attachment_controls", None)
        if callable(set_attachment_controls):
            set_attachment_controls(False)
        self._app._update_status()
        return self._operation_generation

    def _finish_operation(self, generation: int) -> None:
        if generation != self._operation_generation:
            return
        self._operation_state = "idle"
        self._app._is_thinking = False
        self._app._llm_abort = False
        # 家計簿の確認・POSTシーケンスが進行中なら送信ボタンを戻さない。
        # 戻すと is_busy() は真のままなのにUIだけ操作可能に見えてしまう。
        # 再有効化はシーケンス側の終了処理(_finish)が担当する。
        if (
            not getattr(self._app, "_kakeibo_sequence_active", False)
            and not getattr(self._app, "_llm_loading", False)
            and getattr(self._app, "llm", None) is not None
        ):
            self._app._btn_send.config(state="normal")
        set_attachment_controls = getattr(
            self._app, "_set_attachment_controls", None)
        if callable(set_attachment_controls):
            set_attachment_controls(True)
        self._app._stream_buf = ""
        self._active_attachments = ()
        self._active_mode = "default"
        self._health_json_filter.reset()
        self._app._update_status()
        self._app._entry.focus_set()

    # ── ボイス入力 ──────────────────────────────────────────
    def handle_voice(self, text: str) -> None:
        if self.is_busy() or not text.strip():
            return
        self._app._entry.insert("end", text)
        self.handle_text()

    # ── チャット送信 ──────────────────────────────────────
    def handle_text(self) -> None:
        if self.is_busy():
            return

        if self._app.llm is None:
            messagebox.showwarning(
                "準備中",
                "モデルを読み込んでいます。\nしばらくお待ちください。")
            return

        text = self._app._entry.get("1.0", "end").strip()

        snapshot_attachments = getattr(
            self._app, "_snapshot_attachments", lambda: ())
        attachments = tuple(snapshot_attachments())
        if not text and not attachments:
            return

        request_started_at = time.perf_counter()
        if self._app._kakeibo_mode:
            mode = "kakeibo"
        elif self._app._health_mode:
            mode = "health"
        else:
            mode = "default"

        if attachments and mode != "default":
            messagebox.showwarning(
                "添付ファイル",
                "添付ファイルは通常チャットでのみ使用できます。",
            )
            return
        try:
            validate_attachment_set(attachments)
        except PromptInputError as exc:
            messagebox.showwarning("添付ファイル", str(exc))
            return
        has_image = any(attachment.kind == "image" for attachment in attachments)
        can_send_images = getattr(self._app, "_can_send_images", lambda: False)
        if has_image and not can_send_images():
            messagebox.showwarning(
                "画像を送信できません",
                "現在のモデルは画像入力に対応していません。",
            )
            return

        if mode == "kakeibo":
            llm_input = self._prompt_builder.build_kakeibo_prompt(text)
        elif mode == "health":
            llm_input = self._prompt_builder.build_health_prompt(text)
        else:
            prompt_text = text or "添付ファイルの内容を確認してください。"
            llm_input = format_text_attachment_input(prompt_text, attachments)
        try:
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
                extra_reserved_tokens=(
                    IMAGE_CONTEXT_TOKEN_RESERVE if has_image else 0
                ),
                enforce_context_limit=True,
                require_full_history=bool(attachments),
            )
        except PromptInputTooLargeError as exc:
            guidance = (
                "保持中の添付を減らすか解除する、本文を短くする、または"
                "新しいチャットで再度送信してください。"
                if attachments
                else "入力内容を減らして再度送信してください。"
            )
            messagebox.showwarning(
                "入力が長すぎます",
                f"{exc}\n\n{guidance}",
            )
            return
        if has_image:
            messages[-1]["content"] = build_multimodal_user_content(
                llm_input, attachments)

        self._app._entry.delete("1.0", "end")
        self._app._llm_abort = False
        generation = self._begin_operation("generating")
        self._request_id += 1
        request = {
            "request_id": self._request_id,
            "generation": generation,
            "text": text,
            "session": self._app._current_session,
            "session_path": getattr(self._app, "_current_path", None),
            "model_generation": getattr(self._app, "_llm_load_generation", 0),
            "mode": mode,
            "messages": messages,
            "attachments": attachments,
            "has_image": has_image,
            "retry_used": False,
            "ui_committed": False,
            "request_started_at": request_started_at,
        }
        self._attempt_request(request)

    def _request_is_current(
        self, request: dict, *, check_model_generation: bool = True
    ) -> bool:
        return (
            request["generation"] == self._operation_generation
            and request["session"] is self._app._current_session
            and request["session_path"] == getattr(self._app, "_current_path", None)
            and (
                not check_model_generation
                or request["model_generation"]
                == getattr(self._app, "_llm_load_generation", 0)
            )
            and not getattr(self._app, "_closing", False)
        )

    def _commit_request_ui(self, request: dict) -> None:
        if request["ui_committed"]:
            return
        text = request["text"]
        mode = request["mode"]
        self._app._chat_write("\n", "")
        self._app._chat_write("👤 あなた\n", "user_lbl")
        visible_text = text or "（添付ファイルのみ）"
        self._app._chat_write(f"{visible_text}\n", "user_msg")
        attachments = request.get("attachments", ())
        if attachments:
            names = "、".join(attachment_display_names(attachments))
            self._app._chat_write(f"📎 添付: {names}\n", "attachment")
        self._app._chat_write("─" * 50 + "\n", "divider")
        self._app._chat_write("🤖 AI\n", "ai_lbl")
        self._app._stream_buf = ""
        self._tts_stream.reset()
        self._app.tts.begin_stream()
        self._active_mode = mode
        self._active_attachments = attachments
        self._health_json_filter.reset()
        if mode == "kakeibo":
            self._app._kakeibo_pending_text = text
            self._app._health_pending_text = None
        elif mode == "health":
            self._app._health_pending_text = text
            self._app._kakeibo_pending_text = None
        else:
            self._app._kakeibo_pending_text = None
            self._app._health_pending_text = None
        request["ui_committed"] = True

    def _attempt_request(self, request: dict) -> None:
        if not self._request_is_current(request):
            return
        decision = self._resource_mgr.decide(
            self._app._max_tokens,
            self._consume_whisper_gpu_delta(),
        )
        if not decision["ok"]:
            state = getattr(self._app, "_llm_offload_state", None)
            actual_layers = (
                state.get("n_gpu_layers") if isinstance(state, dict) else None
            )
            desired_mode = getattr(
                self._app, "_llm_gpu_offload_mode", "auto")
            can_downshift = (
                desired_mode == "auto"
                and isinstance(actual_layers, int)
                and actual_layers != 0
                and not request["retry_used"]
                and decision.get("requires_relocation", True)
            )
            if can_downshift:
                request["retry_used"] = True
                self._operation_state = "reloading_llm"
                self._app._update_status()

                def _resume(success: bool, error) -> None:
                    if not self._request_is_current(
                        request, check_model_generation=False
                    ):
                        return
                    if success:
                        request["model_generation"] = getattr(
                            self._app, "_llm_load_generation", 0)
                        self._operation_state = "generating"
                        self._attempt_request(request)
                        return
                    message = (
                        f"VRAM不足に対する安全なLLM再配置に失敗しました（{error}）"
                        if error else
                        "VRAM不足に対する安全なLLM再配置に失敗しました"
                    )
                    self._on_request_rejected(
                        request["text"], message, request["generation"])

                started = self._app._request_auto_downshift(_resume)
                if started:
                    return

            message = (
                f"VRAM不足のため安全に実行できません（{decision['reason']}）"
                if decision.get("reason_kind") == "vram_hard_limit"
                else f"LLMを実行できません（{decision['reason']}）"
            )
            total_ms = (
                time.perf_counter() - request["request_started_at"]
            ) * 1000
            print(
                f"[Perf] prompt_build_ms=0.0 first_token_ms=n/a "
                f"tokens_per_sec=0.00 total_ms={total_ms:.1f} "
                "status=guard_blocked"
            )
            self._on_request_rejected(
                request["text"], message, request["generation"])
            return

        if getattr(self._app, "llm", None) is None:
            self._on_request_rejected(
                request["text"],
                "LLMを利用可能な状態へ読み込めませんでした",
                request["generation"],
            )
            return

        self._commit_request_ui(request)
        text = request["text"]
        messages = request["messages"]
        prompt_build_ms = (
            time.perf_counter() - request["request_started_at"]
        ) * 1000

        started = self._llm_service.generate(
            messages    = messages,
            max_tokens  = decision["max_tokens"],
            temperature = self._app._temperature,
            on_token = lambda t: self._post_ui(
                lambda _t=t, _g=request["generation"]:
                    self._on_stream_token(_t, _g)
            ),
            on_done = lambda r: self._post_ui(
                lambda _r=r, _g=request["generation"]:
                    self._on_llm_done(text, _r, _g)
            ),
            on_error = lambda e: self._post_ui(
                lambda _e=e, _g=request["generation"]: self._on_llm_error(
                    text,
                    self._format_generation_error(_e, request["has_image"]),
                    _g,
                )
            ),
            request_started_at = request["request_started_at"],
            prompt_build_ms = prompt_build_ms,
        )
        if not started:
            self._on_llm_error(
                text,
                "[エラー: LLMは既に実行中です]",
                request["generation"],
            )

    @staticmethod
    def _format_generation_error(error: Exception, has_image: bool) -> str:
        message = str(error)
        lowered = message.lower()
        context_markers = (
            "n_ctx",
            "context window",
            "context size",
            "context length",
            "too many tokens",
            "maximum context",
            "position exceeds",
        )
        if has_image and any(marker in lowered for marker in context_markers):
            return (
                "画像を含む入力がcontext上限を超えました。"
                "添付を解除してより小さい画像へ変更する、本文を短くする、"
                "または新しいチャットで再度送信してください。"
            )
        return f"[エラー: {message}]"

    # ── 停止 ──────────────────────────────────────────────
    def _on_stream_token(self, token: str, generation: int) -> None:
        """表示を更新し、完成した文を生成完了前にTTSへ渡す。"""
        if generation != self._operation_generation:
            return
        if self._active_mode == "kakeibo":
            # 家計簿モードのLLM応答は構造化候補抽出だけに使い、
            # 通常のAI文章としてチャット欄へ表示・読み上げしない。
            return
        visible = (
            self._health_json_filter.feed(token)
            if self._active_mode == "health"
            else token
        )
        self._emit_visible_text(visible)

    def _emit_visible_text(self, text: str) -> None:
        if not text:
            return
        self._app._append_stream_token(text)
        for sentence in self._tts_stream.feed(text):
            self._app.tts.speak(sentence)

    def stop(self) -> None:
        """LLM・TTS・マイクを完全に、即座に、依存関係なしで止める"""
        if getattr(self._app, "_llm_loading", False):
            return
        print("[System] 停止命令を執行します")

        self._app._llm_abort = True
        generation = self._begin_operation("stopping")

        self._app.tts.stop_all()
        self._tts_stream.reset()
        self._app.avatar.win.after(0, self._app.avatar.stop_speaking)

        if self._app._voice:
            self._app._voice._tts_active = False
            self._app._voice.vad_threshold = self._app._vad_thresh

        # LLMService のストリーミングループを中断
        self._llm_service.abort()

        def _wait_and_unlock():
            deadline = time.monotonic() + STOP_WAIT_TIMEOUT_SECONDS
            timeout_notified = False
            while self._llm_service.is_running():
                if not timeout_notified and time.monotonic() >= deadline:
                    timeout_notified = True

                    def _notify_timeout():
                        if generation != self._operation_generation:
                            return
                        self._app._chat_write(
                            "\n⚠ LLMを3秒以内に停止できませんでした。"
                            "安全のため操作をロックしています。"
                            "アプリを終了して再起動してください。\n",
                            "err",
                        )
                        messagebox.showwarning(
                            "停止できませんでした",
                            "LLMを3秒以内に停止できませんでした。\n"
                            "安全のため新しい推論は開始しません。\n"
                            "アプリを終了して再起動してください。",
                        )

                    self._post_ui(_notify_timeout)
                time.sleep(
                    STOP_WAIT_TIMEOUT_POLL_SECONDS
                    if timeout_notified
                    else STOP_WAIT_POLL_SECONDS
                )

            def _unlock():
                if generation != self._operation_generation:
                    return
                self._app._chat_write(
                    "\n⛔ 完全に停止しました\n" + "─" * 50 + "\n\n", "divider")
                self._finish_operation(generation)
            self._post_ui(_unlock)

        threading.Thread(target=_wait_and_unlock, daemon=True).start()

    # ── LLM 完了（メインスレッド） ─────────────────────
    def _on_llm_done(self, user_text: str, reply: str, generation: int) -> None:
        if generation != self._operation_generation or self._app._llm_abort:
            print("[LLM] _on_llm_done: 古い操作またはabort済みのためスキップ")
            return
        print("[LLM] _on_llm_done 呼び出し")
        if self._active_mode == "health":
            self._emit_visible_text(self._health_json_filter.finalize())
            try:
                biolog_record, explicit_fields = prepare_biolog_record(
                    extract_health_json(reply), user_text
                )
            except BiologValidationError as exc:
                self._on_health_validation_failed(
                    user_text, reply, generation, exc
                )
                return
            if not biolog_record:
                print("[Health] JSONに送信可能な項目がないため再抽出します")
                self._start_health_retry(user_text, generation)
                return
            self._complete_response(
                user_text,
                reply,
                generation,
                biolog_record=biolog_record,
                biolog_explicit_fields=explicit_fields,
            )
            return

        self._complete_response(user_text, reply, generation)

    def _start_health_retry(self, user_text: str, generation: int) -> None:
        if generation != self._operation_generation:
            return
        decision = self._resource_mgr.decide(
            self._app._max_tokens,
            self._consume_whisper_gpu_delta(),
        )
        if not decision["ok"]:
            self._on_health_retry_failed(user_text, generation)
            return

        self._operation_state = "extracting_health"
        self._app._update_status()
        prompt = self._prompt_builder.build_health_extraction_prompt(user_text)
        started = self._llm_service.extract_health(
            prompt,
            max_tokens=min(decision["max_tokens"], 384),
            on_done=lambda result: self._post_ui(
                lambda r=result, u=user_text, g=generation:
                    self._on_health_retry_done(u, r, g),
            ),
            on_error=lambda error: self._post_ui(
                lambda e=error, u=user_text, g=generation:
                    self._on_health_retry_error(u, e, g),
            ),
        )
        if not started:
            self._on_health_retry_error(
                user_text, RuntimeError("LLMは既に実行中です"), generation
            )

    def _on_health_retry_done(
        self, user_text: str, reply: str, generation: int
    ) -> None:
        if generation != self._operation_generation or self._app._llm_abort:
            return
        try:
            biolog_record, explicit_fields = prepare_biolog_record(
                extract_health_json(reply), user_text
            )
        except BiologValidationError as exc:
            self._on_health_validation_failed(
                user_text, reply, generation, exc
            )
            return
        if not biolog_record:
            self._on_health_retry_failed(user_text, generation)
            return
        self._complete_response(
            user_text,
            reply,
            generation,
            biolog_record=biolog_record,
            biolog_explicit_fields=explicit_fields,
        )

    def _on_health_retry_error(
        self, user_text: str, error: Exception, generation: int
    ) -> None:
        if generation != self._operation_generation:
            return
        print(f"[健康再抽出エラー] {error}")
        self._on_health_retry_failed(user_text, generation)

    def _on_health_validation_failed(
        self,
        user_text: str,
        reply: str,
        generation: int,
        error: BiologValidationError,
    ) -> None:
        if generation != self._operation_generation:
            return
        print(f"[Health] validation failed: {error}")
        self._app._chat_write(
            "⚠ 健康記録に不正な形式または範囲外の値があるため、"
            "Biologへ送信しませんでした。入力内容を確認してください。\n",
            "err",
        )
        self._complete_response(user_text, reply, generation)

    def _on_health_retry_failed(self, user_text: str, generation: int) -> None:
        if generation != self._operation_generation:
            return
        self._app._chat_write(
            "⚠ 健康データを抽出できませんでした。体重・食事内容を具体的に入力してください。\n",
            "err",
        )
        self._complete_response(user_text, "", generation)

    def _complete_response(
        self,
        user_text: str,
        raw_reply: str,
        generation: int,
        *,
        biolog_record: dict | None = None,
        biolog_explicit_fields=None,
    ) -> None:
        if generation != self._operation_generation:
            return

        is_kakeibo = self._active_mode == "kakeibo"

        if not is_kakeibo:
            if self._active_mode == "health":
                assistant_text = strip_health_json(self._app._stream_buf)
                if not assistant_text:
                    assistant_text = (
                        "健康記録を抽出しました。"
                        if biolog_record
                        else "健康記録を抽出できませんでした。"
                    )
                    self._emit_visible_text(assistant_text)
            else:
                assistant_text = raw_reply
                if not self._app._stream_buf:
                    self._emit_visible_text(raw_reply)

        trailing_sentence = self._tts_stream.finalize()
        if trailing_sentence:
            self._app.tts.speak(trailing_sentence)
        self._app.tts.end_stream()
        print(f"[LLM] reply received ({len(raw_reply)} chars)")
        self._app._chat_write("\n" + "─" * 50 + "\n\n", "divider")

        # 家計簿モードのやり取りは通常会話履歴へ一切追加しない
        # (次回プロンプト・セッション再表示・エクスポート・要約への混入を防ぐ)。
        should_summarize = False
        if not is_kakeibo:
            self._app._current_session.setdefault("history", []).append(
                {"user": user_text, "assistant": assistant_text}
            )

            if self._app._current_session.get("title") == "新しいチャット":
                t = user_text.replace("\n", " ").strip()
                if not t and self._active_attachments:
                    t = f"添付: {self._active_attachments[0].name}"
                self._app._current_session["title"] = (
                    t[:20] + ("…" if len(t) > 20 else "")
                )
                self._app._title_var.set(self._app._current_session["title"])

            h = self._app._current_session["history"]
            should_summarize = (
                len(h) >= self._SUMMARY_THRESHOLD
                and len(h) % self._SUMMARY_THRESHOLD == 0
            )
        print(f"[LLM] reply processed ({len(raw_reply)} chars)")

        if self._app._kakeibo_pending_text:
            try:
                transactions = extract_kakeibo_transactions(raw_reply)
                if transactions is None:
                    # 旧形式(単一レコード)しか返らなかった場合のフォールバック。
                    # 入力全体を1取引として扱う従来経路と同じ結果になる。
                    llm_record = extract_kakeibo_json(raw_reply)
                    transactions = [{
                        "source_text": user_text,
                        **{
                            k: v for k, v in (llm_record or {}).items()
                            if k in ("store", "category", "type", "memo")
                        },
                    }]
                result = build_kakeibo_candidates(transactions, user_text)
                status = result["status"]
                if status == "ok":
                    self._app._confirm_and_send_kakeibo(result["candidates"])
                elif status == "too_many":
                    self._app._chat_write(
                        f"⚠ 一度に登録できる取引は最大"
                        f"{MAX_KAKEIBO_TRANSACTIONS_PER_INPUT}件です。"
                        "入力を分けてください。\n",
                        "err",
                    )
                elif status == "invalid_split":
                    self._app._chat_write(
                        "⚠ 取引を正しく分割できませんでした。"
                        "内容を分けて入力してください。\n",
                        "err",
                    )
                elif status == "uncovered_amount":
                    self._app._chat_write(
                        "⚠ 入力に含まれる金額をすべて取引として扱えませんでした。"
                        "登録漏れを避けるため、この入力は登録していません。"
                        "1件ずつ入力を分けてください。\n",
                        "err",
                    )
                elif status == "ambiguous_split":
                    self._app._chat_write(
                        "⚠ 取引の区切りを確定できませんでした。"
                        "金額だけの取引が含まれている可能性があります。"
                        "店名などを添えて1件ずつ入力してください。\n",
                        "err",
                    )
                elif status == "ambiguous_date":
                    self._app._chat_write(
                        "⚠ 入力に複数の日付があり、日付の書かれていない取引に"
                        "どれを使うか確定できませんでした。"
                        "取引ごとに日付を書くか、入力を分けてください。\n",
                        "err",
                    )
                elif status == "no_amount":
                    self._app._chat_write(
                        "⚠ 金額を確認できないため登録できません。"
                        "金額を入力してもう一度送信してください。\n",
                        "err",
                    )
                elif status == "invalid_amount_format":
                    self._app._chat_write(
                        "⚠ 金額の書き方を確認できません。"
                        "「1000円」「1万円」のように入力してください。\n",
                        "err",
                    )
                elif status == "multiple_amounts":
                    self._app._chat_write(
                        "⚠ 取引を正しく分割できませんでした。"
                        "1件ずつ金額が分かるように入力を分けてください。\n",
                        "err",
                    )
            except Exception as exc:
                print(f"[Kakeibo Error] {exc}")
                self._app._chat_write(
                    "⚠ 家計簿の確認処理中にエラーが発生したため登録しませんでした。\n",
                    "err",
                )
            finally:
                self._app._kakeibo_pending_text = None

        if self._app._health_pending_text:
            self._app._health_pending_text = None
        if biolog_record:
            self._app._confirm_and_send_biolog(
                biolog_record, biolog_explicit_fields
            )

        self._app._save_now()
        if should_summarize:
            self._start_summary()
        else:
            self._finish_operation(generation)

    # ── LLM エラー（メインスレッド） ───────────────────
    def _on_llm_error(self, user_text: str, err_msg: str, generation: int) -> None:
        self._on_request_rejected(user_text, err_msg, generation)

    def _on_request_rejected(
        self, user_text: str, message: str, generation: int
    ) -> None:
        if generation != self._operation_generation:
            return
        self._tts_stream.reset()
        self._app.tts.end_stream()
        self._app._chat_write(message + "\n", "err")
        self._app._chat_write("─" * 50 + "\n\n", "divider")
        self._app._kakeibo_pending_text = None
        self._app._health_pending_text = None
        if not self._app._entry.get("1.0", "end").strip():
            self._app._entry.insert("1.0", user_text)
        self._finish_operation(generation)

    def _start_summary(self) -> None:
        generation = self._begin_operation("summarizing")
        prompt = self._app._build_summary_prompt()
        started = self._llm_service.summarize(
            prompt,
            on_done=lambda summary: self._post_ui(
                lambda s=summary, g=generation: self._on_summary_done(s, g)),
            on_error=lambda error: self._post_ui(
                lambda e=error, g=generation: self._on_summary_error(e, g)),
        )
        if not started:
            self._on_summary_error(RuntimeError("LLMは既に実行中です"), generation)

    def _on_summary_done(self, summary: str, generation: int) -> None:
        if generation != self._operation_generation:
            return
        if summary:
            self._app._apply_summary(summary)
        self._finish_operation(generation)

    def _on_summary_error(self, error: Exception, generation: int) -> None:
        if generation != self._operation_generation:
            return
        print(f"[要約エラー] {error}")
        self._finish_operation(generation)

    def begin_shutdown(self) -> None:
        """終了処理用に古いコールバックを無効化し、LLMへ停止を要求する。"""
        self._operation_generation += 1
        self._operation_state = "stopping"
        self._app._is_thinking = True
        self._app._llm_abort = True
        self._llm_service.abort()
