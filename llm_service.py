import threading
import time


def count_generated_tokens(llm, text: str) -> int:
    """生成本文をモデル自身のtokenizerで数える（チャンク数ではない）。"""
    if not text:
        return 0
    return len(llm.tokenize(text.encode("utf-8"), add_bos=False))


class LLMService:
    """LLM ストリーミング推論の純実行層。判断・構築・UI操作は一切持たない。"""

    def __init__(self, llm):
        self.llm     = llm
        self._abort  = False
        self._thread = None
        self._state_lock = threading.Lock()
        self._running = False

    def _start(self, worker, *, reset_abort: bool = False) -> bool:
        with self._state_lock:
            if self._running:
                return False
            if reset_abort:
                self._abort = False
            self._running = True
            self._thread = threading.Thread(target=worker, daemon=True)
            self._thread.start()
        return True

    def _set_idle(self) -> None:
        with self._state_lock:
            self._running = False

    def generate(
        self,
        messages: list,
        max_tokens: int,
        temperature: float,
        on_token,
        on_done,
        on_error,
        request_started_at=None,
        prompt_build_ms=None,
    ) -> bool:
        """別スレッドでストリーミング推論を実行する。"""
        def worker():
            started_at = request_started_at if request_started_at is not None else time.perf_counter()
            first_token_at = None
            status = "completed"
            result = None
            error = None
            finish_reason = "unknown"
            reply = ""
            try:
                self.llm.reset()

                for chunk in self.llm.create_chat_completion(
                    messages    = messages,
                    max_tokens  = max_tokens,
                    temperature = temperature,
                    stream      = True,
                ):
                    if self._abort:
                        status = "aborted"
                        break

                    choice = chunk["choices"][0]
                    if choice.get("finish_reason") is not None:
                        finish_reason = str(choice["finish_reason"])
                    token = choice.get("delta", {}).get("content", "")
                    if token:
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                        reply += token
                        on_token(token)

                if self._abort:
                    status = "aborted"
                else:
                    result = reply.strip()

            except Exception as e:
                status = "error"
                error = e
            finally:
                total_ms = (time.perf_counter() - started_at) * 1000
                try:
                    output_tokens = count_generated_tokens(self.llm, reply)
                except Exception as exc:
                    output_tokens = 0
                    print(f"[Perf] output token count failed: {exc}")
                if first_token_at is None:
                    first_token_ms = "n/a"
                    tokens_per_sec = 0.0
                else:
                    first_token_ms = f"{(first_token_at - started_at) * 1000:.1f}"
                    elapsed = max(time.perf_counter() - first_token_at, 0.001)
                    tokens_per_sec = output_tokens / elapsed
                pb_ms = prompt_build_ms if prompt_build_ms is not None else 0.0
                print(
                    f"[Perf] prompt_build_ms={pb_ms:.1f} "
                    f"first_token_ms={first_token_ms} "
                    f"output_tokens={output_tokens} "
                    f"tokens_per_sec={tokens_per_sec:.2f} "
                    f"total_ms={total_ms:.1f} status={status}"
                )
                print(f"[LLM] finish_reason={finish_reason}")
                self._set_idle()

            if error is not None:
                on_error(error)
            elif result is not None:
                on_done(result)

        return self._start(worker, reset_abort=True)

    def extract_health(
        self, prompt: str, max_tokens: int, on_done, on_error
    ) -> bool:
        """通常生成と同じ排他制御で健康JSONだけを再抽出する。"""
        def worker():
            result = None
            error = None
            finish_reason = "unknown"
            try:
                self.llm.reset()
                response = self.llm(
                    prompt,
                    max_tokens=max_tokens,
                    temperature=0.0,
                )
                choice = response["choices"][0]
                result = choice["text"].strip()
                if choice.get("finish_reason") is not None:
                    finish_reason = str(choice["finish_reason"])
            except Exception as exc:
                error = exc
            finally:
                print(f"[HealthRetry] finish_reason={finish_reason}")
                self._set_idle()

            if error is not None:
                on_error(error)
            else:
                on_done(result)

        return self._start(worker)

    def summarize(self, prompt: str, on_done, on_error) -> bool:
        """通常生成と同じ排他制御で非ストリーミング要約を実行する。"""
        def worker():
            result = None
            error = None
            try:
                self.llm.reset()
                response = self.llm(
                    prompt, max_tokens=80, temperature=0.3, stop=["\n"])
                result = response["choices"][0]["text"].strip()
            except Exception as exc:
                error = exc
            finally:
                self._set_idle()

            if error is not None:
                on_error(error)
            else:
                on_done(result)

        return self._start(worker)

    def abort(self) -> None:
        self._abort = True

    def detach_llm(self):
        """停止中のモデル参照を外し、呼び出し側へ返す。"""
        with self._state_lock:
            if self._running:
                raise RuntimeError("LLM実行中はモデルを解放できません")
            llm = self.llm
            self.llm = None
            return llm

    def attach_llm(self, llm) -> None:
        """停止中に新しいモデル参照を設定する。"""
        with self._state_lock:
            if self._running:
                raise RuntimeError("LLM実行中はモデルを差し替えられません")
            self.llm = llm

    def is_running(self) -> bool:
        with self._state_lock:
            return self._running
