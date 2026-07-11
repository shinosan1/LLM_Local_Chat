import threading
import time


class LLMService:
    """LLM ストリーミング推論の純実行層。判断・構築・UI操作は一切持たない。"""

    def __init__(self, llm):
        self.llm     = llm
        self._abort  = False
        self._thread = None

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
    ) -> None:
        """別スレッドでストリーミング推論を実行する。"""
        self._abort = False

        def worker():
            started_at = request_started_at if request_started_at is not None else time.perf_counter()
            first_token_at = None
            emitted_tokens = 0
            status = "completed"
            try:
                self.llm.reset()
                reply = ""

                for chunk in self.llm.create_chat_completion(
                    messages    = messages,
                    max_tokens  = max_tokens,
                    temperature = temperature,
                    stream      = True,
                ):
                    if self._abort:
                        status = "aborted"
                        return

                    token = chunk["choices"][0]["delta"].get("content", "")
                    if token:
                        emitted_tokens += 1
                        if first_token_at is None:
                            first_token_at = time.perf_counter()
                        reply += token
                        on_token(token)

                if not self._abort:
                    on_done(reply.strip())
                else:
                    status = "aborted"

            except Exception as e:
                status = "error"
                on_error(e)
            finally:
                total_ms = (time.perf_counter() - started_at) * 1000
                if first_token_at is None:
                    first_token_ms = "n/a"
                    tokens_per_sec = 0.0
                else:
                    first_token_ms = f"{(first_token_at - started_at) * 1000:.1f}"
                    elapsed = max(time.perf_counter() - first_token_at, 0.001)
                    tokens_per_sec = emitted_tokens / elapsed
                pb_ms = prompt_build_ms if prompt_build_ms is not None else 0.0
                print(
                    f"[Perf] prompt_build_ms={pb_ms:.1f} "
                    f"first_token_ms={first_token_ms} "
                    f"tokens_per_sec={tokens_per_sec:.2f} "
                    f"total_ms={total_ms:.1f} status={status}"
                )

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def abort(self) -> None:
        self._abort = True

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
