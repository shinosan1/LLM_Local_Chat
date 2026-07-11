from resource_monitor import adjust_inference


class ResourceManager:
    """VRAM 状態から max_tokens を決定する純計算層。LLM・session を参照しない。"""

    def __init__(self, res_monitor):
        self._monitor = res_monitor

    def decide(self, base_max_tokens: int, delta_gpu_pct: float) -> dict:
        """ok=False なら推論中止。ok=True なら max_tokens を返す。"""
        return adjust_inference(
            self._monitor,
            base_max_tokens,
            delta_gpu_pct=delta_gpu_pct,
        )
