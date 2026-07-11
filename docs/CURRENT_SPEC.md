# LLM Local Chat 現行仕様

作成日: 2026-07-11 / 最終更新: 2026-07-11(不具合2件の修正を反映)
基準: gitコミット `3a35b2d` 時点のコードを静的に読解した結果。
このファイルは「現在実装されている事実」だけを記載する。将来予定・改善案は含まない。
確認済み事実と推測は区別して記載する(推測には「推測」と明記)。

---

## 1. アプリ概要

- **目的**: ローカルPC上でLLM(llama.cpp / GGUF)と日本語チャットするWindowsデスクトップアプリ。音声入力(Whisper)、音声読み上げ(SAPI5 TTS)、アバター表示、家計簿・健康記録のローカルAPI連携を持つ。
- **主な利用方法**: GUI(Tkinter)を起動し、テキストまたは音声で対話する。会話はセッションJSONとして保存・再読込できる。
- **フォルダの役割**:
  - `C:\Users\shino\python-mysql-dev\app\LLM_Local_Chat` — 開発用(git作業の正本)。`.venv` なし。
  - `D:\AI\LLM\LLM_Local_Chat` — 実行用。`.venv`、モデルファイル、実行時設定を保持。
  - 両者は同一gitリポジトリのクローンで、コード内容は一致(2026-07-11時点で確認)。
- **対応OS**: Windows専用(SAPI5 TTS が `win32com.client.Dispatch("SAPI.SpVoice")` に依存。`audio_workers.py` の `TTSWorker._execute_sapi_speak`)。README/CHANGELOGでは動作保証を Windows 11 のみと記載。
- **想定Pythonバージョン**: 3.12(実行用 `.venv` の `pyvenv.cfg` が Python 3.12.10)。

## 2. 起動方法

- **Pythonエントリーポイント**: `LLM_Local_Chat.py` の `main()`。`if __name__ == "__main__"` ガードあり。
- **Windowsで通常使用する起動ファイル**:
  - `LLMローカル対話型AI.bat` — スクリプト位置の `.venv` を activate して `python LLM_Local_Chat.py` を実行(リポジトリ同梱)。
  - `Start_Shiro.bat`(実行用フォルダのみに存在) — 家計簿/Biolog の Dockerサービスを起動後、`.venv` を activate し、連携APIのURLを環境変数で設定して `python LLM_Local_Chat.py` を実行。※これが実運用経路かはコードだけでは確定できない(§13)。
  - `start.sh` は Dockerコンテナ+X11転送前提のLinux用であり、Windows通常経路ではない(推測)。
- **起動から画面表示までの処理順**(`main` → `ChatApp.__init__`):
  1. `tk.Tk()` でルートウィンドウ生成
  2. `app_composition.create_app_deps(LOG_DIR)` — `ResourceMonitor`(監視デーモン開始)・`VRAMGuard`・`WhisperPool`(空)・`SessionStore` を生成
  3. `ChatApp.__init__` — `load_settings()` → `AvatarWindow` → `TTSWorker`(再生スレッド開始) → `_build_ui()` → `_new_session()` → `Controller` 生成
  4. `_reload_llm()` — **バックグラウンドスレッドで** `init_llm()`(モデルロード)。UIはブロックされない
  5. `_load_whisper_async()` — 条件付きでWhisperロード(下記)
  6. `root.mainloop()`
- **モデルを読み込むタイミング**: 起動直後(手順4)と、設定画面で `model_path` または `n_ctx` を変更したとき(`ChatApp._open_settings` → `_reload_llm`)。
- **Whisperを読み込む条件**: 設定の `mic_enabled` または `tts_enabled` のどちらかが true の場合のみ(`ChatApp._load_whisper_async`)。両方 false なら**ロード自体をスキップ**し(`_whisper_load_skipped` フラグ)、音声認識は起動中ずっと利用不可。この状態で🎤を押すと「設定で起動時マイクを有効にして再起動」の案内が表示される(`_toggle_mic`)。
- **import時の副作用**: モジュールimportだけではTkウィンドウ・モデルロード・監視スレッドは発生しない(すべて `main()` 起動時に生成)。ただし `from llama_cpp import Llama` 等の重量DLLロード、`sys.stdout.reconfigure`、`audio_workers.py` の pyaudio/pyttsx3/pywin32 の try-import は import 時に走る。

## 3. 主要機能

### ローカルLLMチャット
- 概要: llama-cpp-python によるローカル推論チャット。
- 操作: 入力欄にテキスト → Enter または「送信」。
- 実装: `controller.py` `Controller.handle_text` → `llm_service.py` `LLMService.generate`。
- 使用条件: GGUFモデルファイルが `model_path` に存在すること。
- 保存/外部影響: 応答完了ごとにセッションJSONを自動保存(ゲストモード時を除く)。

### ストリーミング表示
- 概要: 生成トークンを逐次チャット欄に表示。
- 実装: `LLMService.generate`(`create_chat_completion(stream=True)`) → `root.after` 経由で `ChatApp._append_stream_token`。
- 外部影響: なし。

### 会話履歴・セッション保存/読込
- 概要: 会話を `{title, history, summary}` 形式のJSONで保存。左サイドバーで一覧・検索・読込・削除・名前変更。
- 操作: Ctrl+S / メニュー「保存」、一覧クリックで読込、右クリックで削除・名前変更、検索ボックスでタイトル+要約の部分一致絞り込み。
- 実装: `session_store.py` `SessionStore`(save/load/list_sessions/delete/rename)、`ChatApp._save_now` `_load_selected` `_delete_chat` `_rename_chat` `_refresh_chat_list`。
- 保存: `chat_logs/chat_YYYYMMDD_HHMMSS.json`。

### 要約メモリ
- 概要: 会話履歴を50文字以内で要約し、以後のシステムプロンプトに注入。画面上部に「📝 メモリ:」として表示。
- 発生条件: 履歴が `SUMMARY_THRESHOLD`(=4)ターン以上かつ4の倍数ターンに達した応答完了時(`Controller._on_llm_done`)。別スレッドで `ChatApp._update_summary` を実行。
- 実装: `ChatApp._update_summary`(`llm(prompt, max_tokens=80, temperature=0.3)` を直接呼ぶ)、注入は `prompt_builder.py` `PromptBuilder.build`。
- 保存: セッションJSONの `summary` フィールド。

### ゲストモード
- 概要: 会話を一切保存しないモード。
- 操作: サイドバー「ゲストモード: OFF/ON」ボタン。切替時に新規セッションへ移行。
- 実装: `ChatApp._toggle_guest`、保存スキップは `_save_now` 冒頭の `if self._is_guest: return`。

### 音声入力(マイク+Whisper)
- 概要: 常駐マイク監視 → RMS-VADで発話検知 → Whisperで日本語認識 → 自動送信。
- 操作: 🎤ボタンでON/OFF(Whisperロード済みの場合のみ)。
- 実装: `audio_workers.py` `VoiceRecognizer`(`_loop`、`_rms`)、認識結果は `Controller.handle_voice` へ。
- 使用条件: 起動時に `mic_enabled` または `tts_enabled` が true(Whisperロード条件)+ pyaudio利用可+マイクデバイス存在。初回はWhisperモデルのダウンロードが発生(`~/.cache/whisper`)。
- 誤認識対策: ノイズ語句フィルタ(`WHISPER_NOISE` / `WHISPER_NOISE_PARTIAL`)、100文字超の破棄、同一テキスト10秒以内の重複破棄、TTS再生中のチャンク読み捨て。

### TTS(読み上げ)
- 概要: AI応答をWindows SAPI5で読み上げ。アバターの口パクと連動。コードブロックは読み上げから除去(`_strip_code_blocks`)。
- 操作: メニュー「表示 > TTS 音声出力」チェック、または設定画面「起動時にTTS読み上げを有効にする」。
- 実装: `audio_workers.py` `TTSWorker`(`speak`/`_play_loop`/`_execute_sapi_speak`/`stop_all`)。キュー方式・非同期再生・停止対応。
- 起動時挨拶: TTS有効かつWhisperロード完了後に一度だけ「システムを起動しました。」を発話(`ChatApp._on_whisper_ready`)。

### アバター
- 概要: 別ウィンドウ(枠なし・最前面)に画像を表示し、瞬き(ランダム2〜6秒間隔)と口パク(140ms間隔)をアニメーション。
- 操作: ドラッグで移動、右クリックまたはメニュー「表示 > アバター表示/非表示」。
- 実装: `LLM_Local_Chat.py` `AvatarWindow`(`_schedule_blink`/`_do_blink`/`start_speaking`/`_mouth_loop`)。
- 使用条件: `avatars/` に画像4種(default/speaking/blink/blink_speaking)。欠損時は透明画像で継続(落ちない)。

### モデル切り替え
- 概要: 設定画面でGGUFパス・n_ctxを変更すると再ロード。
- 実装: `ChatApp._open_settings` → 変更検知 → `_reload_llm`。ロード成功時は `LLMService.llm` を差し替え、トークンコストキャッシュをクリア(`_on_llm_ready`)。

### VRAM監視・自動フォールバック
- 概要: VRAM/GPU使用率を0.5秒周期で観測し、(a)モデルロード前の `n_gpu_layers` 決定、(b)推論直前の `max_tokens` 段階削減・実行ブロック、(c)WhisperのGPU/CPUヒステリシス切替を行う。
- 実装: `resource_monitor.py` `ResourceMonitor` / `adjust_llm` / `adjust_inference` / `WhisperController` / `WhisperPool`、呼び出しは `init_llm`・`resource_manager.py` `ResourceManager.decide`・`VoiceRecognizer._loop`。
- 外部影響: なし(観測のみ。pynvml → nvidia-smi → CPU-only の順にフォールバック)。

### 家計簿連携
- 概要: 家計簿モード中の発話から支出/収入JSONを抽出し、確認ダイアログ後にローカルAPIへPOST。
- 操作: 🏠ボタンでモードON/OFF。
- 実装: `PromptBuilder.build_kakeibo_prompt` → `json_extractors.py` `extract_kakeibo_json` → `integrations.py` `IntegrationBridge.confirm_and_send_kakeibo`。
- 外部影響: **あり**(ローカル家計簿APIへの登録)。詳細は§10。

### Biolog連携(健康記録)
- 概要: 健康記録モード中の発話から体重・食事等のJSONを抽出し、確認ダイアログ後にローカルAPIへPOST。
- 操作: 💪ボタンでモードON/OFF。
- 実装: `PromptBuilder.build_health_prompt` → `extract_health_json` → `IntegrationBridge.confirm_and_send_biolog`。
- 外部影響: **あり**(ローカルBiolog APIへの登録)。詳細は§10。

### 付属ユーティリティ
- `check_audio_devices.py`: オーディオデバイス一覧表示の独立スクリプト(アプリ本体からは呼ばれない)。

## 4. 画面構成

| 画面上の名称 | コード上の対応 |
|---|---|
| メインウィンドウ(1100x850、ダークテーマ) | `ChatApp.__init__` / `_build_ui` |
| メニューバー「ファイル」(新規チャット/保存/テキストとして保存/設定/終了) | `_new_session` / `_save_now` / `_save_as_text` / `_open_settings` / `_on_close` |
| メニューバー「表示」(TTS音声出力チェック/アバター表示切替) | `_toggle_tts` / `AvatarWindow.toggle_visible` |
| 左サイドバー「＋新しいチャット」 | `_new_session`(Ctrl+Nも同じ) |
| 左サイドバー「ゲストモード」ボタン | `_toggle_guest` |
| 検索ボックス(🔍) | `_search_var` の trace → `_refresh_chat_list` |
| チャット履歴一覧(Listbox) | `_chat_list`、クリック=`_load_selected`、右クリック=`_on_list_right_click`(削除/名前変更) |
| チャット表示エリア | `_chat_text`(ScrolledText、読取専用)、右クリックでコピーメニュー(`_copy_selected` / `_copy_all`) |
| 要約メモリ表示(📝 メモリ:) | `_summary_var` / `_summary_label` |
| 入力欄(3行、Enter送信 / Shift+Enter改行) | `_entry`、`_on_entry_return` / `_on_entry_shift_return` |
| 🎤 マイクボタン | `_toggle_mic`(状態色: 赤=ON待機、黄=認識中/処理中、灰=OFF。`_mic_idle` / `_mic_listening` / `_mic_processing`) |
| ⏹ 停止ボタン | `_stop_all` → `Controller.stop`(LLM生成・TTS・マイクを停止) |
| 🏠 家計簿モードボタン | `_toggle_kakeibo` |
| 💪 健康記録モードボタン | `_toggle_health` |
| 「送信」ボタン | `_send` → `Controller.handle_text`(生成中は無効化) |
| ステータスバー | `_update_status`(生成中/待機中、ゲスト表示、モデル名、max_tokens/temp、ターン数、マイク状態) |
| 免責文言(入力欄上) | `_build_ui` 内の固定Label |
| 設定画面「生成設定」 | `SettingsDialog`(モデルパス+参照、n_ctx、最大返答トークン、会話の自由度0.0〜2.0、VAD感度、起動時マイク/TTSチェック) |
| アバターウィンドウ | `AvatarWindow`(枠なし・最前面・ドラッグ移動・右クリックで表示切替) |

## 5. モジュール構成

| ファイル | 主な責務 | 主なクラス・関数 | 呼び出し元 | 呼び出し先 |
|---|---|---|---|---|
| LLM_Local_Chat.py | エントリーポイント、Tkinter UI、設定I/O、LLMロード | `main` `ChatApp` `SettingsDialog` `AvatarWindow` `load_settings` `save_settings` `init_llm` `count_tokens` `_strip_code_blocks` | 起動スクリプト | app_composition, controller, audio_workers, integrations, resource_monitor(adjust_llm) |
| app_composition.py | 起動時依存の生成(composition root) | `AppDeps` `create_app_deps` | `main` | resource_monitor, session_store |
| controller.py | 送信・停止・LLM完了処理のオーケストレーション | `Controller` `ControllerDeps`(`handle_text` `handle_voice` `stop` `_on_llm_done` `_on_llm_error`) | `ChatApp.__init__` | llm_service, prompt_builder, resource_manager, json_extractors |
| llm_service.py | ストリーミング推論の純実行(別スレッド)+[Perf]計測ログ | `LLMService`(`generate` `abort` `is_running`) | Controller | llama_cpp |
| prompt_builder.py | messages構築、履歴トークン予算管理、モード別プロンプト | `PromptBuilder`(`build` `build_kakeibo_prompt` `build_health_prompt`) | Controller | — |
| json_extractors.py | LLM応答からのJSON抽出(正規表現) | `extract_kakeibo_json` `extract_health_json` | Controller._on_llm_done | — |
| session_store.py | セッションJSONの保存/読込/一覧/検索/削除/改名 | `SessionStore` | create_app_deps → ChatApp | — |
| integrations.py | 連携payloadサニタイズ、localhost制限、確認ダイアログ、POST | `IntegrationBridge` `sanitize_kakeibo_record` `sanitize_biolog_record` `is_allowed_local_api_url` | ChatApp(委譲メソッド経由) | urllib |
| audio_workers.py | SAPI5 TTSワーカー、VAD+Whisper音声認識 | `TTSWorker` `VoiceRecognizer` | ChatApp | win32com, pyaudio, WhisperPool.get_model |
| resource_manager.py | 推論直前のmax_tokens決定の薄いラッパー | `ResourceManager`(`decide`) | Controller | resource_monitor.adjust_inference |
| resource_monitor.py | VRAM/GPU/CPU観測、ロード前/推論前調整、Whisper GPU/CPU切替 | `ResourceMonitor` `VRAMGuard` `adjust_llm` `adjust_inference` `WhisperController` `WhisperPool` | app_composition, init_llm, resource_manager, VoiceRecognizer | pynvml / nvidia-smi / psutil / whisper |

スレッド構成: メイン(Tk mainloop、UI更新は必ず `root.after` 経由)/LLM推論(`LLMService.generate` 内 worker)/TTS再生(`TTSWorker._play_loop` daemon)/音声認識(`VoiceRecognizer._loop` daemon)/リソース監視(`ResourceMonitor._loop` daemon)/要約(`_update_summary` 都度生成)/連携POST(`IntegrationBridge._send_to_*` 都度生成)。

## 6. 設定仕様(chat_settings.json)

パスはカレントディレクトリ相対の `chat_settings.json`(`SETTINGS_FILE` 定数)。起動batがアプリフォルダへcdするため、実質アプリフォルダ直下。ファイル欠損・JSON破損時は全キー既定値で継続(通知なし)。

| キー | 型 | 既定値 | 用途 | 設定画面から変更 | 再起動が必要か | 使用コード位置 |
|---|---|---|---|---|---|---|
| model_path | str | models\gemma-3-4b-it-q4_k_m.gguf | GGUFモデルパス | ○ | 不要(即再ロード) | `init_llm`, `ChatApp._reload_llm` |
| n_ctx | int | 8192 | コンテキスト長 | ○(512以上) | 不要(即再ロード) | `init_llm`, `PromptBuilder.build` |
| max_tokens | int | 1024 | 最大生成トークン | ○(1以上) | 不要(即時) | `ResourceManager.decide` 経由 |
| temperature | float | 0.7 | 生成温度 | ○(0.0〜2.0) | 不要(即時) | `LLMService.generate` |
| tts_enabled | bool | false | 起動時TTS状態 | ○ | 不要(切替は即時。ただしWhisperロード条件には起動時値が使われる) | `TTSWorker.enabled`, `_load_whisper_async` |
| mic_enabled | bool | false | 起動時マイク状態 | ○ | **条件付きで必要**(起動時にWhisper未ロードの場合、有効化には再起動が必要) | `VoiceRecognizer.enabled`, `_load_whisper_async` |
| vad_threshold | int | 150 | VAD感度(RMS閾値) | ○ | 不要(即時) | `VoiceRecognizer` |
| n_threads_batch | int | 12 | llama.cppバッチスレッド数 | ✕(ファイルのみ) | モデル再ロードが必要 | `init_llm`(`_valid_positive_int` 検証あり) |
| n_batch | int | 1024 | バッチサイズ | ✕ | モデル再ロードが必要 | `init_llm`(検証あり) |
| n_ubatch | int | 512 | マイクロバッチサイズ | ✕ | モデル再ロードが必要 | `init_llm`(検証あり) |
| flash_attn | bool | true | Flash Attention使用 | ✕ | モデル再ロードが必要 | `init_llm`(検証あり。失敗時は自動でOFFリトライ) |
| offload_kqv | bool | true | KQVのGPUオフロード | ✕ | モデル再ロードが必要 | `init_llm`(検証あり) |
| use_mmap | bool | true | mmap使用 | ✕ | モデル再ロードが必要 | `init_llm`(検証あり) |
| vram_danger_gpu_pct | int | (ファイル上88) | — | ✕ | — | **未使用**(コードから参照ゼロ。閾値は `resource_monitor.py` の定数にハードコード) |
| vram_danger_vram_pct | int | (ファイル上90) | — | ✕ | — | **未使用**(同上) |

- 値検証: perf系6キー(`n_threads_batch`〜`use_mmap`)のみ `_valid_positive_int` / `_valid_bool` でフォールバックあり。その他のキーはファイル値をそのまま採用(型不正時は後段処理でエラーになり得る)。
- 自動保存: 終了時(`_on_close`)・設定ダイアログ保存時・TTSメニュー切替時に `save_settings` で全体を上書き保存。
- `chat_settings.json.example` がテンプレート。※exampleの `tts_enabled` は true になっており、コード既定値(false)と異なる。

## 7. モデルと生成処理

- **対応モデル形式**: GGUF(llama-cpp-python 0.3.20)。
- **読み込み処理**: `init_llm(model_path, n_ctx, res_monitor, perf_settings)`。
  1. パス存在チェック(なければ `FileNotFoundError`)
  2. `adjust_llm(res_monitor)` でVRAM使用率から `n_gpu_layers` を決定(使用率>0.85 → 0=CPU実行、それ以外 → -1=全層GPU。GPU未検出時は -1)
  3. 3段階リトライ: perf設定込み → flash_attnのみOFF → 基本設定のみ(`n_threads=8, n_batch=512`)
- **コンテキスト長/最大トークン/temperature**: 設定値(§6)。temperatureはUIで0.0〜2.0に制限。
- **推論直前の動的制限**: `ResourceManager.decide` → `adjust_inference`。VRAM使用量+先読みバッファ(500/800/1200MB)で仮想使用量を計算し、>7000MBで実行ブロック(エラーメッセージ表示)、>6000MBで max_tokens 1/4、>4500MBで 1/2(下限256)。WhisperのGPU使用率スパイク(Δ>10%)でさらに半減。
- **読み込み失敗時**: ステータスバー「❌ モデル読込失敗」+エラーダイアログ(`_on_llm_ready`)。アプリは落ちない(送信時は「準備中」警告)。
- **生成停止**: ⏹ → `Controller.stop`。`_llm_abort` フラグ+`LLMService.abort()`(ストリーミングループ内で中断)+TTS停止+アバター停止。最大3秒待機後にUIロック解除。中断時は履歴保存・TTSをスキップ。
- **プロンプト構築**: `PromptBuilder.build`。system prompt(「シロ」ペルソナ固定文)+モードヒント+要約(あれば)+トークン予算内の直近履歴+ユーザー入力。履歴予算は `(n_ctx - max_tokens - system - user) × 0.60`、ペアごとのトークンコストをキャッシュ。
- **要約メモリの発生条件**: §3参照(4ターンごと、別スレッド、max_tokens=80/temperature=0.3/stop=["\n"])。

## 8. 音声仕様

- **マイク入力**: PyAudio 16kHz/mono/1024チャンク常駐読み取り(`VoiceRecognizer._loop`)。マイク初期化失敗時は例外を出さず音声認識を無効化。
- **VAD**: RMS閾値方式。閾値超えが6チャンク連続で発話開始、閾値未満が30チャンク連続または6秒で録音終了。閾値は設定 `vad_threshold`(既定150、設定画面から即時変更可)。
- **Whisper**: openai-whisper。`language="ja"`, `beam_size=1`, `temperature=0.0`, `no_speech_threshold=0.8`, `logprob_threshold=-1.5`, `condition_on_previous_text=False`, `initial_prompt` あり。
- **GPU/CPU切り替え**: `WhisperPool` がCPU版(small)を常時ロード、起動時VRAM使用率<70%ならGPU版(medium)もロード。推論ごとに `WhisperController` がヒステリシス切替(GPU使用率>88%でCPUへ、<70%でGPUへ)。再ロードなしの即時切替。
- **TTS**: SAPI5非同期再生(キュー方式)。停止は purge 相当(`Speak("", 3)`)。読み上げ前に ```コードブロック``` を除去。
- **音声入力とTTSの排他**: TTS発話開始で `VoiceRecognizer._tts_active=True` になり、マイクチャンクを完全読み捨て(ハウリング・自己認識防止)。発話終了の800ms後、TTSキューが空なら解除(`ChatApp._on_tts_start` / `_on_tts_stop` / `_restore_vad`)。
- **起動時の読み込み条件**: `mic_enabled` または `tts_enabled` が true のときだけWhisperをロード(§2)。
- **設定変更後に再起動が必要な項目**: 起動時にWhisperがロードされなかった場合の `mic_enabled`(§6)。perf系6キーもモデル再ロードが必要(設定画面からは変更不可)。

## 9. 保存データ

| データ | 保存場所 | 形式 | 読み込み処理 | 書き込み処理 | 削除される条件 |
|---|---|---|---|---|---|
| 設定 | chat_settings.json(アプリフォルダ) | JSON | `load_settings` | `save_settings`(終了時・設定保存時・TTS切替時) | 自動削除なし |
| 会話履歴・セッション | chat_logs/chat_YYYYMMDD_HHMMSS.json | JSON `{title, history:[{user,assistant}], summary}` | `SessionStore.load` / `list_sessions` | `SessionStore.save`(応答完了ごと・Ctrl+S・終了時) | ユーザーが右クリック「削除」を確認した場合のみ(`SessionStore.delete`) |
| 要約メモリ | セッションJSON内 `summary` | 文字列 | `_load_selected` | `_update_summary` → `_save_now` | セッション削除に従属 |
| テキストエクスポート | ユーザー指定パス | .txt | — | `_save_as_text` | 自動削除なし |
| ログ | 専用ファイルなし(標準出力へのprint。loggingはハンドラ未設定で既定非表示) | — | — | — | — |
| アバター画像 | avatars/(4ファイル) | PNG | `AvatarWindow._load`(読み取りのみ) | なし | なし(アプリは書き込まない) |
| Whisperキャッシュ | ~/.cache/whisper | モデルファイル | whisper内部(初回ダウンロード) | whisper内部 | アプリは削除しない |

- 破損時挙動: 一覧生成では壊れたJSONを黙ってスキップ、個別読込ではエラーダイアログ。
- ゲストモード中はセッション書き込みが行われない。

## 10. 外部連携

- **対象**: 家計簿API と Biolog(健康記録)API。いずれもローカルで稼働している前提のHTTP POST。
- **URL**(`integrations.py` 冒頭で決定):
  - 家計簿: 環境変数 `KAKEIBO_API_URL`(既定 `http://localhost:8765`)+ `/api/kakeibo/record`
  - Biolog: 環境変数 `BIOLOG_URL`(既定 `http://localhost:8766`)+ `/api/health/record`
- **localhost制限**: `is_allowed_local_api_url` が scheme=http かつ host が `localhost` / `127.0.0.1` / `::1` の場合のみ許可。それ以外(https含む)は送信を中止しチャット欄へ警告表示。
- **送信前確認**: サニタイズ済みpayloadを整形表示した確認ダイアログ(はい/いいえ)を必ず経由。
- **サニタイズ**(`sanitize_kakeibo_record` / `sanitize_biolog_record`):
  - 許可キーのホワイトリスト方式(未知キー・`user_id`・`token`・`url`・`headers` 等はここで脱落)
  - 家計簿: `amount` は bool を拒否し、正の数値のみ許可
  - Biolog: 記録値が1つもないpayloadは送信しない。`user_id` はLLM出力を使わず送信時に固定値 `"self"` を付与
- **実際に外部データを変更する操作**: 確認ダイアログで「はい」を選んだ場合のPOSTのみ(家計簿登録・Biolog記録登録)。それ以外に外部へデータを送る処理はない。POSTは都度生成のdaemonスレッドで実行、タイムアウト5秒、結果はチャット欄に表示。

## 11. 終了処理

`ChatApp._on_close`(ウィンドウ×・メニュー「終了」共通)の処理順:

1. `VoiceRecognizer.stop()` — 認識ループの `_active` を False(スレッドは自然終了)
2. 現在のマイク/TTS状態を設定dictへ反映
3. `save_settings` — 設定ファイル上書き保存
4. `TTSWorker.stop_all()` — 読み上げキュー破棄+再生中断フラグ
5. `_save_now()` — 現在セッションを保存(ゲスト時はスキップ)
6. `root.after(200, root.destroy)` — 200ms後にウィンドウ破棄(TclError防止)

- スレッド終了: 各ワーカー(TTS/音声認識/監視)はdaemonスレッドのため、プロセス終了とともに消滅。`TTSWorker.terminate()` は定義されているが終了処理からは呼ばれていない。
- モデル/リソースの明示解放処理はない(プロセス終了に委ねる)。

## 12. 現在確認されている不整合・既知の問題

コード読解で確認できた事実のみ。

### 修正済み(2026-07-11、コミット `3a35b2d`)

- `Controller.stop`(controller.py)の `_voice.tts_active` 参照を実フラグ名 `_tts_active` に修正。旧コードはアンダースコア無しの別属性を作るだけのno-opで、停止ボタンによる即時のマイク排他解除が効いていなかった
- 起動時にWhisperロードをスキップした場合(`_whisper_load_skipped`)、🎤ボタン押下時の案内を「音声認識は起動時に読み込まれていません。設定で『起動時にマイクを有効にする』をオンにして、アプリを再起動してください。」に変更。旧コードは「読み込んでいます。しばらくお待ちください」と表示され続けた
- ※いずれも静的確認のみ。実機での動作確認は未実施(§13)

### 未修正の既知問題

| # | 内容 | 分類 |
|---|---|---|
| 1 | `build_messages_safe`(LLM_Local_Chat.py) — 全コードから呼び出しゼロ。`PromptBuilder.build` と同機能の旧実装 | **未使用コード** |
| 2 | `VRAMGuard`(resource_monitor.py) — `create_app_deps` で生成され `AppDeps.guard` に格納されるが、`guard`・`is_safe()` の参照が全コードにゼロ | **未使用コード** |
| 3 | `pyttsx3` — audio_workers.py で import 可否判定のみ行い、実使用箇所ゼロ(TTSは win32com SAPI5 直接)。requirements.txt には記載あり | **未使用コード**(依存整理は仕様判断が必要) |
| 4 | `vram_danger_gpu_pct` / `vram_danger_vram_pct` — CHANGELOG v1.2.0 に追加と記載され設定ファイルにも存在するが、現行コードに読む箇所がない。閾値は resource_monitor.py の定数(`VRAMGuard.SCORE_LIMIT`、`WhisperController.GPU_FALLBACK_PCT` 等)にハードコード | **未使用設定**(設定化するか削除するかは仕様判断が必要) |
| 5 | `_update_summary` は別スレッドで `self.llm` を直接呼ぶが、要約実行中にユーザー送信をブロックする仕組みがない(`_is_thinking` は要約開始時のチェックのみ)。同一 `Llama` オブジェクトへの並行呼び出しが理論上起こり得る | **実機確認が必要**(高リスク領域のため現状変更なし) |
| 6 | `chat_settings.json.example` の `tts_enabled` が true(コード既定値・CHANGELOG記載の「デフォルトOFF」と不一致) | **表示上の不整合**(ドキュメント/テンプレートの整合は仕様判断が必要) |
| 7 | `_restore_mic`(LLM_Local_Chat.py)はコメントで「旧方式の残骸」と明示された空メソッド | **未使用コード** |

## 13. 未確認事項

コードだけでは確定できない事項(実装済みの事実と混同しないこと):

- 実機での起動・モデルロード・基本チャット・音声・TTS・連携の動作可否(静的読解のみで、実行確認は未実施)
- `Start_Shiro.bat` が現在の実運用の起動経路かどうか(実行用フォルダにのみ存在し、内容からは実運用向けと推測されるが未確認)
- 開発用フォルダ(C側)での起動可否(`.venv` が存在しないため、現状のままでは依存が満たされない可能性が高い)
- §12-5(要約の並行実行)の実使用時の影響度、および今回修正した2件(TTSフラグ属性名・マイク案内)の実機での動作確認
- `llm_service.py`・`resource_manager.py` の正確な作成経緯(CHANGELOG未記載。ファイル日時と内容から、それぞれ速度最適化作業・v1.2.0後の作業で作成と推測)
- README・操作マニュアルPDFの記述と実装の逐条一致(見出しレベルの確認のみ実施)
- CHANGELOG未記載の実装: 速度最適化一式([Perf]計測、`init_llm` の3段階リトライ、perf系6設定キー、Whisper条件付きロードスキップ)が、どのバージョン番号に属するか

## 14. 仕様の根拠

本書の各記載は、以下のファイルの直接読解による(2026-07-11、gitコミット `3a35b2d` の状態)。主な根拠はクラス名・関数名で本文中に併記した。行番号は変動しやすいため記載していない。

- LLM_Local_Chat.py(`main`, `ChatApp`, `SettingsDialog`, `AvatarWindow`, `load_settings`, `save_settings`, `init_llm`)
- app_composition.py(`create_app_deps`)/ controller.py(`Controller`)/ llm_service.py(`LLMService`)
- prompt_builder.py(`PromptBuilder`)/ json_extractors.py / session_store.py(`SessionStore`)
- integrations.py(`IntegrationBridge`, `sanitize_*`, `is_allowed_local_api_url`)
- audio_workers.py(`TTSWorker`, `VoiceRecognizer`)/ resource_manager.py(`ResourceManager`)
- resource_monitor.py(`ResourceMonitor`, `VRAMGuard`, `adjust_llm`, `adjust_inference`, `WhisperController`, `WhisperPool`)
- 設定・起動: chat_settings.json.example、LLMローカル対話型AI.bat、Start_Shiro.bat(実行用フォルダ)、start.sh
- 参考ドキュメント(記載と実装の差異は§12参照): CHANGELOG.md、README.md、CLAUDE.md、docs/architecture_snapshot.md、skills.md
