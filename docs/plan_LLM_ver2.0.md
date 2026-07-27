Ready for review
Select text to add comments on the plan
VRAM多層安全フィルタ 実装プラン
Context
LLM（Gemma 4B Q4_K_M、2.25 GB VRAM）とWhisper medium（1.5 GB VRAM）が同一GPUで同時動作する場合、 VRAM枯渇により推論品質低下・OOMクラッシュが発生しうる。

この実装は「VRAMを制御するシステム」ではなく「VRAM事故の確率を下げる多層フィルタ」である。

VRAM は CPU メモリのようにスケジュール可能ではない（断片化・外部ライブラリ・TOCTOU）
OOM の完全防止は不可能。目標は「クラッシュ頻度の大幅削減」と「再ロードの回避」
多層フィルタ = 予約帳簿（第1層）+ リアルタイム監視（第2層）+ ヒステリシス切替（第3層）
設計全体は「確率的削減」を意図したヒューリスティックの集積であり、数学的保証はない
既存UIロジック・スレッドモデルは変更しない。

設計の前提と既知の限界
確定している前提
#	前提	影響
A	GPU は NVIDIA（pynvml で監視可能）	失敗時は nvidia-smi パース or CPU-only にフォールバック
B	LLM_Local_Chat は Windows ネイティブ（SAPI5 TTS が Linux 非対応）	Docker 実行時は TTS 無効・ヘッドレスモード必須
C	Whisper はロード時にデバイス決定、推論中の切替不可	デバイス決定はロード前の1回のみ
D	LLM の n_ctx は init_llm() 呼び出し時のみ変更可	推論中の fallback = max_tokens 削減のみ
E	chat_settings.json に VRAM 制御フィールドを追加	既存フィールドとの後方互換を保つ
既知の設計上の限界（OOM を完全には防げない理由）
GPU VRAM はスケジュール可能なリソースではない。 CPU メモリと異なり、CUDA アロケータは断片化・非線形増加・ドライバ保有領域を持つ。 pynvml の空き値と「次の cudaMalloc が成功するか」は一致しない。

限界	内容	対策
VRAM 予測の誤差	KV キャッシュは非線形。断片化で同じ数値でも OOM する場合がある	予測値に ×1.4 の安全係数を乗せる
外部ライブラリの自律確保	llama.cpp / whisper / torch が内部バッファを独自に確保する	try_reserve を「参考値」と位置づけ、安全保証とは言わない
CUDA context 初期化ピーク	初期化時に一時的に大きな確保が走る	WhisperPool の永続ロードで初期化ピークを起動時に吸収する
モニタリング遅延	pynvml は allocator 後スナップショット（実行前ピークを見ていない）	EMA ではなく **瞬時値優先 + execution-time guard（推論直前の再チェック）**で補う
この制御レイヤの目標: 「OOM を完全に防ぐ」ではなく 「クラッシュ頻度を大幅に削減し、再ロードを回避する」

対象ファイル
ファイル	操作
LLM_Local_Chat/resource_monitor.py	新規作成（Step 1 成果物）
LLM_Local_Chat/LLM_Local_Chat.py	追記のみ（Step 2、既存ロジック変更禁止）
LLM_Local_Chat/chat_settings.json	キー追記（Step 2）
docker-compose.yml	GPU フラグ追記（Step 3）
LLM_Local_Chat/Dockerfile.headless	新規作成（Step 3）
LLM_Local_Chat/README.md	更新（Step 4）
LLM_Local_Chat/CHANGELOG.md	追記（Step 4）
依存関係
Step 1 (resource_monitor.py 新規)
    │
    └─▶ Step 2 (LLM_Local_Chat.py 統合)
            │
            ├─▶ Step 3 (Docker 対応)   ← Step 2 と並行作業可能
            └─▶ Step 4 (ドキュメント)  ← Step 2 完了後
Step 1: コアロジック（resource_monitor.py 新規作成）
入力: pynvml / nvidia-smi 出力、psutil 処理: ResourceMonitor（観測）+ VRAMGuard（即時判定）+ SoftAllocator（調整）+ WhisperPool（Storage） 出力: LLM_Local_Chat.py に依存しない独立モジュール 削除: 予約・task_id・ヒステリシス状態機械（すべて廃止）

1-A: ResourceMonitor クラス（観測のみ）
class ResourceMonitor:
    POLL_SEC  = 0.5   # 収集周期
    EMA_ALPHA = 0.6   # 急変に追従できるよう強め

    vram_total_mb:   int    # GPU 総 VRAM
    vram_instant_mb: float  # 使用中 VRAM（瞬時値）— 判断の主役
    vram_used_mb:    float  # 使用中 VRAM（EMA、参考値）
    gpu_pct:         float  # GPU 使用率（EMA）
    cpu_pct:         float  # CPU 使用率（EMA）
デーモンスレッドで 0.5 秒ごとに収集
pynvml 初期化失敗 → nvidia-smi パース → 失敗 → VRAM=0（CPU のみモード）
収集ごとに logging.debug("[Monitor] vram_instant=Xmb gpu_pct=Y%") を出力
1-B: VRAMGuard（即時安全判定 — 関数3つだけ）
DANGER_GPU_PCT  = 88    # GPU使用率がこれ以上 → 危険
DANGER_VRAM_PCT = 0.90  # VRAM使用率がこれ以上 → 危険

def is_safe(monitor: ResourceMonitor) -> bool:
    """
    現在の状態が安全かを即時判定。True = 通常実行可。False = 軽量モードで実行。
    予約なし・状態なし・1行の判断。
    限界: 単一プロセス内のみ有効。TOCTOU あり。
    """
    if monitor.vram_total_mb == 0: return True
    return (monitor.gpu_pct < DANGER_GPU_PCT and
            monitor.vram_instant_mb / monitor.vram_total_mb < DANGER_VRAM_PCT)

def adjust_llm_params(monitor: ResourceMonitor) -> dict:
    """
    LLM ロード前: VRAM 使用量に応じてパラメータを調整する。
    「予測」ではなく「現在値を見て段階的に対応するだけ」。
    fallback=False/True の両方がログに出ることを保証。
    """
    used = monitor.vram_instant_mb
    if used > 6000:
        result = {"n_gpu_layers": 0, "fallback": True,  "reason": "vram_high"}
    elif used > 4500:
        result = {"n_gpu_layers": -1, "fallback": False, "reason": "vram_mid"}
    else:
        result = {"n_gpu_layers": -1, "fallback": False, "reason": "ok"}
    logging.debug(f"[Guard][llm_init] vram={used:.0f}MB → {result}")
    return result

def adjust_inference(monitor: ResourceMonitor, default_max: int) -> dict:
    """
    LLM 推論直前: VRAM 使用量に応じて max_tokens を調整する。
    「予測」ではなく「現在値を見て段階的に対応するだけ」。
    fallback=False/True の両方がログに出ることを保証。
    """
    used = monitor.vram_instant_mb
    if used > 7000:
        logging.debug(f"[Guard][infer] vram={used:.0f}MB → BLOCK")
        return {"ok": False, "max_tokens": 0, "fallback": True, "reason": "vram_critical"}
    elif used > 6000: max_t = max(256, default_max // 4)
    elif used > 4500: max_t = max(256, default_max // 2)
    else:             max_t = default_max
    fallback = max_t < default_max
    logging.debug(f"[Guard][infer] vram={used:.0f}MB max_tokens={default_max}→{max_t} fallback={fallback}")
    return {"ok": True, "max_tokens": max_t, "fallback": fallback, "reason": "ok"}
1-C: WhisperPool（Storage + 即時切替）
class WhisperPool:
    """
    モデル参照を保持するだけ。判断は is_safe() の即時チェックのみ（状態機械なし）。
    """
    _gpu_model: Optional[whisper.Whisper] = None  # 起動時ロード（条件付き）
    _cpu_model: Optional[whisper.Whisper] = None  # 常にロード（VRAM不使用）

    def load(self, monitor: ResourceMonitor):
        """起動時に1回。is_safe() で即時判定してロード先を決める。"""
        self._cpu_model = whisper.load_model("small", device="cpu")   # 無条件
        if is_safe(monitor):
            try:
                self._gpu_model = whisper.load_model("medium", device="cuda")
                logging.debug("[WhisperPool] GPU版ロード完了")
            except Exception as e:
                logging.warning(f"[WhisperPool] GPU版ロード失敗 → CPU版のみ: {e}")

    def get_model(self, monitor: ResourceMonitor) -> tuple[whisper.Whisper, bool]:
        """
        推論ごとに即時判定（ヒステリシスなし）。
        is_safe() が False なら CPU に落とす。再ロードなし。
        """
        use_gpu = (self._gpu_model is not None and monitor.gpu_pct < DANGER_GPU_PCT)
        if use_gpu:
            logging.debug(f"[WhisperPool] GPU使用: gpu_pct={monitor.gpu_pct:.1f}%")
        else:
            logging.debug(f"[WhisperPool] CPU fallback: gpu_pct={monitor.gpu_pct:.1f}% "
                          f"gpu_model={'あり' if self._gpu_model else 'なし'}")
        return (self._gpu_model if use_gpu else self._cpu_model), use_gpu
責務境界（最終構造）
ResourceMonitor（観測のみ）
  → vram_instant_mb, gpu_pct を公開

VRAMGuard 関数群（即時判断 — 状態なし）
  → is_safe(monitor)             # 安全チェック
  → adjust_llm_params(monitor)   # LLMロード前の調整
  → adjust_inference(monitor, n) # 推論直前の調整

WhisperPool（Storage + 即時切替）
  → load(monitor)     # is_safe() で GPU/CPU を判断
  → get_model(monitor) # gpu_pct で即時切替

廃止したもの:
  × VRAMScheduler（予約帳簿）
  × _Reservation（task_id管理）
  × try_reserve / release
  × permanent 予約
  × ヒステリシス状態機械
  × WORST_CASE_COEFF 予測係数
ログフォーマット例:

DEBUG [Monitor]      vram_instant=2400mb gpu_pct=12.3%
DEBUG [Guard][llm_init] vram=2400mb → {"n_gpu_layers": -1, "fallback": false, "reason": "ok"}
DEBUG [WhisperPool]  GPU使用: gpu_pct=12.3%
DEBUG [Guard][infer] vram=3900mb max_tokens=1024→1024 fallback=False
DEBUG [Guard][infer] vram=6200mb max_tokens=1024→256  fallback=True
DEBUG [WhisperPool]  CPU fallback: gpu_pct=89.1% gpu_model=あり
Step 2: LLM_Local_Chat.py への統合
入力: Step 1 の resource_monitor.py 処理: 既存コードに # ADDED / # MODIFIED タグ付きで追記（4 か所 + import） 出力: 変更済み LLM_Local_Chat.py、更新済み chat_settings.json

統合箇所（全 7 か所）
2-A: import & シングルトン初期化（ファイル先頭付近）
# ADDED: VRAM安全フィルタ（予約なし・シンプル構成）
from resource_monitor import ResourceMonitor, WhisperPool, adjust_llm_params, adjust_inference
_res_monitor  = ResourceMonitor()   # バックグラウンドデーモン起動
_whisper_pool = WhisperPool()       # 起動時に GPU+CPU 両方ロード
2-B: init_llm() ラッパー（line 233 付近）
def init_llm(model_path: str, n_ctx: int) -> Llama:
    params = adjust_llm_params(_res_monitor)   # ADDED（現在のVRAMを見て調整）
    return Llama(
        model_path   = model_path,
        n_ctx        = n_ctx,                  # 変更しない（ロード後は変更不可）
        n_threads    = 8,
        n_gpu_layers = params["n_gpu_layers"], # MODIFIED（-1 or 0）
        n_batch      = 512,
        verbose      = False,
    )
2-C: Whisper ロードを WhisperPool.load() に置き換え（_load_whisper_async, line 1097 付近）
_whisper_pool.load(_res_monitor)   # MODIFIED（is_safe() でGPU/CPUを判断）
wm = _whisper_pool
2-D: Whisper 推論を WhisperPool.get_model() 経由に変更（_loop, line 869 付近）
model, is_gpu = _whisper_pool.get_model(_res_monitor)   # ADDED（即時判定）
res = model.transcribe(audio_np, ..., fp16=is_gpu)      # MODIFIED
# release 不要（予約なし）
2-E: LLM 推論直前チェック（_llm_worker, line 1885 付近）
dec = adjust_inference(_res_monitor, self._max_tokens)   # ADDED（現在のVRAMを見て調整）
if not dec["ok"]:
    self.root.after(0, lambda: self._on_llm_done(
        f"VRAM使用率が高いため実行できません（{dec['reason']}）", user_text))
    return
result = llm.create_chat_completion(
    messages   = messages,
    max_tokens = dec["max_tokens"],   # MODIFIED（段階削減済み）
    temperature= self._temperature,
    stream     = True,
)
# release 不要（予約なし）。_llm_abort は既存のまま動作する。
2-F: chat_settings.json への追記
{
  "vram_danger_gpu_pct":   88,
  "vram_danger_vram_pct":  90
}
スレッド安全保証
ResourceMonitor は読み取り専用プロパティのみ公開（ロック不要）
adjust_* 関数は ResourceMonitor の読み取りのみ（副作用なし、スレッドセーフ）
WhisperPool.get_model() はロックフリー（参照読み取りのみ）
UI スレッドへのコールバックなし（root.after() 不要）
Step 3: Docker 環境対応
入力: 現行 docker-compose.yml（kakeibo-bridge, biolog-api, biolog-streamlit） 処理: GPU フラグ追記 + LLM_Local_Chat 向けヘッドレス Dockerfile 作成 出力: 更新済み docker-compose.yml、Dockerfile.headless

⚠ LLM_Local_Chat は Windows ネイティブ（SAPI5 TTS = win32com が Linux 非対応） Dockerコンテナ内実行 = TTS 無効・ヘッドレスモード必須

3-A: docker-compose.yml への GPU フラグ追記
# バックエンドサービス（biolog-api など）に GPU が必要な場合
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
3-B: Dockerfile.headless（LLM_Local_Chat 用）
ベース: python:3.12-slim
除外: pywin32（SAPI5）
追加: libsndfile1, ffmpeg（Whisper 前処理）、libportaudio2（マイク）
起動コマンド: python LLM_Local_Chat.py --headless
3-C: 不足パッケージ一覧
パッケージ	Windows	Linux Docker
pywin32 (SAPI5)	必須	不可・除外
pyaudio	必須	libportaudio2 追加必要
pynvml	必須	nvidia-driver バインド必要
whisper	必須	ffmpeg 必要
tkinter	必須	X11 転送 or ヘッドレス
今回はユーザー回答より Step 3 = docker-compose.yml の GPU フラグ確認のみ。 Dockerfile.headless は作成不要。

3-D: Docker 起動例（X11 + Audio + GPU）
docker run --gpus all \
  -e DISPLAY=$DISPLAY \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  --device /dev/snd --group-add audio \
  llm-local-chat
Step 4: ドキュメント更新
入力: Step 2 完了済み実装 処理: README.md への VRAM 制御説明追記、CHANGELOG.md バージョン追記 出力: 更新済み README.md、CHANGELOG.md

README.md 追記内容
VRAM 制御レイヤの仕組み（自動フォールバック、安全マージン）
GPU VRAM 容量別 動作可否表（4GB/6GB/8GB）
DEBUG ログの見方（[Monitor]/[Scheduler] プレフィックス）
Docker ヘッドレスモードの制限（TTS なし）
CHANGELOG.md 追記内容
## [1.x.0] - 2026-05-06
### Added
- VRAMリソース制御レイヤ（resource_monitor.py）
- LLM / Whisper ロード前 VRAM 予測・自動フォールバック
- DEBUGログ: [Monitor] / [Scheduler] プレフィックスで全判断を追跡可能
- 実装では`whisper_model_size`案を、実行先とサイズを一元指定する`whisper_mode`へ置換。`vram_safety_margin`は安全装置を設定で緩和できないよう外部設定化せず、動的予約領域を内部基準として維持
### Changed
- Whisper デバイス選択を VRAM 残量に基づく動的決定に変更
- init_llm() の n_ctx / n_gpu_layers を VRAM 判断で自動調整
- LLM 推論の max_tokens を VRAM 予約残量で動的削減
成功条件の検証方法
条件	確認手順
LLM生成中Whisper起動でクラッシュしない	音声入力しながら長文生成を実行、エラーなし確認
VRAM高負荷時Whisper→CPU フォールバック	`whisper_mode`でGPUを指定しても必要空き容量未満ならCPU smallになることを確認
モデル再ロードなし	ログに [WhisperPool] ロード が2回出ないことを確認
UIフリーズなし	長文生成中に他ボタンが応答することを確認
DEBUG ログに判断理由出力	logging.DEBUG で実行し [Scheduler] reserve_ok/reserve_ng 行を確認
フォールバック両ケース	reserve_ok→VRAM余裕あり、reserve_ng→フォールバック の両ログを確認
実運用シミュレーション（壊れないか確認）
シナリオ	期待動作
ケース1: LLM+Whisper同時開始	両方 try_reserve → 合計予測 > vram_safe の場合、後続が False → Whisper は CPU で実行
ケース2: VRAM予測ズレ（OOM）	OOM は OS レベル。try/except で LLM 呼び出しを囲み、クラッシュではなくエラーメッセージ表示
ケース3: フォールバック連鎖	Whisper→CPU、LLM は max_tokens 削減で継続。CPU 版 Whisper は常に使えるため無限連鎖なし
ケース4: LLM 途中キャンセル	_llm_abort フラグ → ストリーム終了 → finally: scheduler.release(task_id) で必ず解放
確定事項（ユーザー回答より）
項目	回答	影響
Docker スコープ	Windows ネイティブのまま	LLM_Local_Chat は Windows で直接実行。Step 3 = docker-compose.yml GPU フラグ確認のみ（Dockerfile.headless 不要）
GPU VRAM	8 GB 以上（RTX 4060 Ti 等）	LLM ~2.5 GB + Whisper medium 1.5 GB = 4.0 GB。安全空き 6.8 GB（15% マージン）内で余裕で共存可能。通常使用でフォールバックは発動しない
VRAM 閾値設計（8 GB GPU 向け）
モデル	VRAM 予測	8 GB での可否
Gemma 4B Q4_K_M + n_ctx=8192	~2,580 MB	✅
Whisper medium（GPU）	~1,500 MB	✅
合計	~4,080 MB	✅ 安全空き 6,800 MB 以内
フォールバック強制テスト: vram_safety_margin = 0.99 に設定して発動を確認する
