# LLM Local Chat 現行仕様

作成日: 2026-07-11 / 最終更新: 2026-08-27
基準: 公開版v1.8.0の現行コードの静的読解、回帰テスト、および§13に明記した範囲の実機確認結果。
このファイルは「現在実装されている事実」だけを記載する。将来予定・改善案は含まない。
確認済み事実と推測は区別して記載する(推測には「推測」と明記)。

---

## 1. アプリ概要

- **目的**: ローカルPC上でLLM(llama.cpp / GGUF)と日本語チャットするWindowsデスクトップアプリ。音声入力(Whisper)、音声読み上げ(SAPI5 TTS)、アバター表示、家計簿・健康記録のローカルAPI連携クライアントを持つ。
- **主な利用方法**: GUI(Tkinter)を起動し、テキストまたは音声で対話する。会話はセッションJSONとして保存・再読込できる。
- **実行に必要な構成**: リポジトリのコード一式に加え、`.venv`(Python実行環境)、`models/` へ配置するGGUFモデル、`chat_settings.json`(実行時設定)を各自で用意する。これらはリポジトリに含まれない。
- **対応OS**: Windows専用(SAPI5 TTS が `win32com.client.Dispatch("SAPI.SpVoice")` に依存。`audio_workers.py` の `TTSWorker._execute_sapi_speak`)。README/CHANGELOGでは動作保証を Windows 11 のみと記載。
- **想定Pythonバージョン**: 3.12(動作確認は Python 3.12.10 の `.venv` で実施)。

## 2. 起動方法

- **Pythonエントリーポイント**: `LLM_Local_Chat.py` の `main()`。`if __name__ == "__main__"` ガードあり。
- **Windowsで通常使用する起動ファイル**:
  - `LLMローカル対話型AI.bat` — スクリプト位置の `.venv` を activate して `python LLM_Local_Chat.py` を実行(リポジトリ同梱)。
  - 連携機能まで使う場合は、別途用意した家計簿/BiologのローカルAPIを起動し、連携APIのURLを環境変数(`KAKEIBO_API_URL`・`KAKEIBO_BRIDGE_PORT`・`BIOLOG_URL`)で指定してから起動する。家計簿アプリ、DB、`kakeibo-bridge`サーバー、そのDocker/Compose定義および一括起動スクリプトはリポジトリに含まれない。
  - `start.sh` は Dockerコンテナ+X11転送前提のLinux用であり、Windows通常経路ではない(推測)。
- **起動から画面表示までの処理順**(`main` → `ChatApp.__init__`):
  1. `tk.Tk()` でルートウィンドウ生成
  2. `app_composition.create_app_deps(LOG_DIR)` — `ResourceMonitor`(監視デーモン開始)・`WhisperPool`(空)・`SessionStore` を生成
  3. `ChatApp.__init__` — `load_settings()` → `AvatarWindow` → `TTSWorker`(再生スレッド開始) → `_build_ui()` → `_new_session()` → `Controller` 生成
  4. `_reload_llm()` — **バックグラウンドスレッドで** `init_llm()`(モデルロード)。UIはブロックされない
  5. LLMロード完了後、`_load_whisper_async()`で条件付きWhisperロード(下記)。LLMとWhisperは同時ロードしない
  6. `root.mainloop()`
- **モデルを読み込むタイミング**: 起動直後(手順4)、設定画面で `model_path` / `n_ctx` / `llm_gpu_offload_mode` を変更したとき、および自動モードで「GPU配置を再評価」または推論直前のVRAM hard limitによるdownshiftが必要なとき(`ChatApp._open_settings` / `Controller` → `_reload_llm`)。
- **Whisperを読み込む条件**: 設定の `mic_enabled` が true の場合のみ。TTSだけが有効な場合はWhisperをロードせず、起動発話は独立して一度だけ実行する。
- **import時の副作用**: モジュールimportだけではTkウィンドウ・モデルロード・監視スレッドは発生しない。ただし `from llama_cpp import Llama` 等の重量DLLロード、`sys.stdout.reconfigure`、`audio_workers.py` の pyaudio/pywin32 の try-importは実行される。

## 3. 主要機能

### ローカルLLMチャット
- 概要: llama-cpp-python によるローカル推論チャット。
- 操作: 入力欄にテキスト → Enter または「送信」。
- 実装: `controller.py` `Controller.handle_text` → `llm_service.py` `LLMService.generate`。
- 使用条件: GGUFモデルファイルが `model_path` に存在すること。
- 保存/外部影響: 応答完了ごとにセッションJSONを自動保存(ゲストモード時を除く)。

### パーソナライズとファイル添付
- `chat_settings.json`の任意キーからsystem prompt、ユーザー向けpersona、応答言語、内部思考を表示しない指示を組み立てる。外部promptはUTF-8のTXT / MD、相対パスはアプリ配置フォルダ基準。inline system → external system → inline persona → external persona → response language → reasoning visibility → external profile/instructionsの順に併用し、同一ファイル・同一文章は重複挿入しない。読込失敗はUI警告後、利用できるinline値または既定promptで継続する。
- 通常チャットでTXT / MD / JSON / CSVとPNG / JPEGを選択できる。最大8件、画像1枚、テキスト1MiB/件、画像10MiB/件・16メガピクセル以下。上限超過は切り捨てず送信しない。
- 非ゲスト添付は検証済み`session_id`と添付`id`でチャットへ関連付け、`chat_logs/attachments/<session-id>/<attachment-id>.<ext>`へ添付時点の生bytesをコピーする。元ファイル名は保存パス・削除キーに使わない。`ChatApp._attachments`は現在表示中チャットの検証済みsidecarだけを推論用に復元したメモリ表現。
- 新規チャット、別履歴読込、モデル切替、終了時は現在表示のメモリ参照だけを外し、元チャットのsidecarとメタデータを削除しない。元チャットへ戻るか再起動後に保存済みチャットを開くと復元する。Vision非対応モデルへの切替でも画像sidecarを削除しない。
- 通常チャットの各後続ターンで、テキストは区切り付きブロック、画像はVision利用時だけローカルdata URIとして現在入力へ1回だけ組み込む。成功・生成エラー・停止・資源拒否・auto downshift再試行では解除しない。
- 履歴JSONへ保存する添付情報は`id/name/kind/mime_type/extension/size/sha256`だけ。元ファイル絶対パス、画像bytes/base64/data URI、テキスト本文は保存しない。履歴へ保存するuser値は元の入力本文だけで、添付のみ送信時の内部補完文も保存しない。ただしLLMが添付内容を引用したassistant回答は通常どおり履歴・要約へ残り得る。
- 添付がある場合、system prompt・要約・全履歴・添付込み現在入力・応答予約・画像予約を同じtoken計算へ含め、`n_ctx`超過時は全履歴を黙って省略せず送信前に警告する。画像は4,096 tokensを予約し、実行時のcontext例外もUIへ表示する。
- 本文なし＋添付ありは中立な内部指示で送信可能、本文・添付ともになしは送信しない。添付は家計簿・健康記録モードでは使用しない。同名・別内容は別IDとして保持し、UIでは`(2)`等を付けて区別する。同一payloadの重複は除外する。
- UIの「添付解除」は確認後、現在チャットの全添付メタデータとsidecarを削除する。チャット削除は当該チャット専用namespaceを削除する。設定画面は全保存済み添付の一覧・件数・合計容量、内部ID単位の個別削除、全チャット対象の一括削除を提供し、会話本文は削除しない。
- 追加・削除は`.pending-add-*` / `.pending-delete-*` / pending session namespaceを用いる。チャット削除では暗号化履歴も`.pending-session-delete-<session-id>-<transaction-id>.bin`へ原子的に移してから削除し、暗号化メタデータのcommit状態から再開する。初回履歴保存前に停止して所有履歴がないstrict UUID namespaceへ残ったpending addも回収する。欠損、読込不能、サイズ不一致、SHA-256不一致、reparse/unsafe pathの添付はLLMへ渡さず警告する。部分削除は成功・失敗・スキップ・再試行待ちを表示する。
- `.shiro-export`は`session_id`と`attachments`を除外するため、添付実体・メタデータは移行・復元されず、添付バックアップにはならない。
- ユーザーアカウント単位の認証・権限・添付保存領域分離はない。同じ保存領域を複数人で共有する場合、一括削除は他利用者を含む全チャット添付へ影響し得る。sidecarはアプリ自身では暗号化しない。

### ストリーミング表示
- 概要: 生成トークンを逐次チャット欄に表示。
- 実装: `LLMService.generate`(`create_chat_completion(stream=True)`) → `root.after` 経由で `ChatApp._append_stream_token`。
- 外部影響: なし。

### 会話履歴・セッション保存/読込
- 概要: `{session_id, title, history, summary, last_activity_at, attachments}` をWindowsの現在ユーザースコープDPAPIで暗号化し、JSONエンベロープとして保存。`attachments`はpayloadを含まない復元メタデータで、旧履歴では両追加キーを省略可能。左サイドバーで一覧・検索・読込・削除・名前変更。
- 操作: Ctrl+S / メニュー「保存」、一覧クリックで読込、右クリックで削除・名前変更、検索ボックスでタイトル+要約の部分一致絞り込み。
- 実装: `session_store.py` `SessionStore`(save/load/list_sessions/delete/rename)、`ChatApp._save_now` `_load_selected` `_delete_chat` `_rename_chat` `_refresh_chat_list`。
- 保存: アプリ配置フォルダ内の `chat_logs/chat_YYYYMMDD_HHMMSS_ffffff.json`（衝突時は連番）。平文フォールバックなし。
- 移行: v1.4.1以前の平文JSONは初回起動時の確認後、元バイト列の復号照合と原子的置換を経て暗号化。1件でも失敗した場合は通常起動を中止。
- 保存期間: `history_retention_days`（0/30/90/180/365、既定0）。有限値は候補件数の確認後に適用し、`last_activity_at`基準で現在開いている履歴と復号不能ファイルを除外。

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
- 使用条件: 起動時に`mic_enabled`がtrue + pyaudio利用可 + マイクデバイス存在。初回はWhisperモデルのダウンロードが発生(`~/.cache/whisper`)。
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

### モデル切り替え / LLM GPUオフロード hot reload
- 概要: 設定画面でGGUFパス・n_ctx・LLM GPUオフロード希望モードを変更すると、アプリを再起動せずバックグラウンド再ロードする。希望モードは自動 / Full GPU / 約75% / 約50% / 約25% / CPU。
- 実装: `ChatApp._open_settings` → 変更検知 → `_reload_llm`。UI操作を一時停止し、`LLMService`から旧参照をdetachしてworkerでclose/CUDA cleanupを完了してから新LLMをロードする。旧・新モデルを同時保持しない。
- 排他: reload開始時に新規Whisper転写を止め、進行中転写の終了を有限時間待つ。受理された最新reload世代だけを`LLMService`へattachし、stale/closing後のロード結果はworkerで解放する。
- 失敗時: 手動変更のpreflight失敗は旧LLMを維持する。detach後の非VRAMロード失敗は旧設定を最大1回だけ復旧する。自動downshiftは危険な上位配置へ戻さず、CPUまで安全側へだけ進む。

### VRAM監視・自動フォールバック
- 概要: VRAM/GPU使用率を0.5秒周期で観測し、(a)モデルロード前にFull→約75%→50%→25%→CPUの順で `n_gpu_layers` を決定、(b)推論直前の `max_tokens` 段階削減、(c)自動モードのhard limit時に現在より低いoffloadへhot reloadして同一入力を最大1回継続、(d)WhisperのGPU/CPUヒステリシス切替を行う。Full条件と既存予約量は緩和しない。
- 実装: `resource_monitor.py` `ResourceMonitor` / `adjust_llm` / `adjust_inference` / `WhisperController` / `WhisperPool`、呼び出しは `init_llm`・`resource_manager.py` `ResourceManager.decide`・`VoiceRecognizer._loop`。
- 外部影響: なし(観測のみ。pynvml → nvidia-smi → CPU-only の順にフォールバック)。

### 家計簿API連携クライアント
- 概要: 家計簿モード中の発話から支出/収入の取引候補を抽出し、確認ダイアログ後に別プロセスの`kakeibo-bridge` APIへPOST。家計簿アプリ、DB、Web UI、API/bridgeサーバーは本リポジトリに含まれない。
- 操作: 🏠ボタンでモードON/OFF。
- 実装: `PromptBuilder.build_kakeibo_prompt` → `json_extractors.py` `extract_kakeibo_transactions` → `kakeibo_split.py` `build_kakeibo_candidates` → `LLM_Local_Chat.py` `ChatApp._confirm_and_send_kakeibo` → `integrations.py` `IntegrationBridge.send_kakeibo`。
- **複数取引**: 1回の入力から最大10件(`MAX_KAKEIBO_TRANSACTIONS_PER_INPUT`)の取引候補を生成できる。**一括登録ではなく、1取引 = 1確認 = 1 POST を維持する。**
  - 11件以上は入力全体を拒否する(確認画面0件・POST 0件)。先頭N件だけ処理する部分成功は行わない。
  - LLMが返す `source_text` は信用せず、原文に実在すること・空でないこと・範囲が重複しないこと・原文順に並べられること・許可キーのみであることを検証する。完全一致を優先し、半角スペース(U+0020)・全角スペース(U+3000)だけが異なる場合は、一意で非重複な原文spanへ対応できるときだけ `user_text[start:end]` の実スライスへ復元する。句読点・数字・改行等の差や曖昧一致は許可しない。1つでも満たさなければ入力全体を拒否する。
  - **原文の金額をすべての断片が覆っていることを確認する。** どの断片にも含まれない金額表現が原文に残っている場合(LLMが取引を取りこぼした場合)は入力全体を拒否する。件数上限の判定だけでは検出できないため、`kakeibo_amount.find_amount_spans()` で原文側の金額位置と突き合わせる。
  - **金額だけの断片を独立した取引として受理しない。** 「スーパーで2000円と500円」をLLMが「スーパーで2000円」「500円」へ切り分けた場合のように、同一文脈内の複数金額を人工的に分割したものは入力全体を拒否する。
  - 最終 `amount` と `date` はLLM値を採用せず、各取引の原文断片から `kakeibo_amount` / `kakeibo_date` が機械抽出する。断片自身に日付表現が無い場合、**原文全体の明示日付がちょうど1種類のときだけ**その日付へフォールバックする。複数種類ある場合はどれを適用すべきか一意に決められないため入力全体を拒否する。日付が1つも無ければ従来どおり実行日を使う。
  - 日付の対応形式は `YYYY/MM/DD`、`YYYY-MM-DD`、`M/D`、`YYYY年M月D日`、`M月D日`。数字は全角も正規化する。無効な年付き日本語日付の内部を、年なし形式として実行年で再解釈しない。相対日付は解析しない。
  - `8/20 1603円` のような日付直後の空白と正常金額は許可する。`1 603円` のように金額の数字列自体が空白で分断された表記は不正形式として拒否する。
  - `2170円8月18日` のように金額直後へ `M月D日` / `YYYY年M月D日` が続く場合は、暦日として妥当な日本語日付表現に限って金額の右境界として許可する。通常の数字連結や実在しない月日は許可しない。
  - 1つの断片に有効な金額候補が複数ある場合は1取引と断定せず、入力全体を拒否する。
  - POSTは直列化する(前の完了通知を受けてから次の確認画面を開く)。途中のPOSTが失敗した場合は残りの候補を処理せず停止し、このクライアントからPOST済みの取引は取り消さない。
  - 1件だけの入力は従来どおり1候補として扱う。
  - **確認・POSTシーケンスが進行中の間は `Controller.is_busy()` が真を返し、キーボード入力・音声入力とも新しい送信を開始しない。** 完了・スキップ・中止・エラーのいずれでもフラグは解除され、予期しない例外でも解除される。
- 旧形式(単一レコードJSON)しか返らなかった場合は、入力全体を1取引として扱うフォールバック経路がある。
- 外部影響: **あり**(別途用意したローカルの`kakeibo-bridge` APIへのPOST)。台帳側での保存結果と内部構成は本リポジトリの実装範囲外。詳細は§10。

### Biolog連携(健康記録)
- 概要: 健康記録モード中の発話から体重・食事等を抽出し、確認ダイアログ後にローカルAPIへPOST。
- 操作: 💪ボタンでモードON/OFF。
- 実装: `PromptBuilder.build_health_prompt` → `extract_health_json` → `prepare_biolog_record` → `IntegrationBridge.confirm_and_send_biolog`。ユーザー原文の `食事ログ`／`行動ログ`／`メモ`（各「追加」形式を含む）は決定的に解析され、同じフィールドのLLM抽出値より優先される。
- 日付確定: 日付省略時は送信直前にJSTの登録日を確定してPOST recordの`date`へ保持する。明示済みの有効日付は変更しない。成功表示はAPI応答の有効な`date`を優先し、応答に日付がなければ実際にPOSTした確定recordの`date`を使用する。
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
| 添付操作(ファイルを添付 / ファイル名表示 / 添付解除) | `_select_attachments` / `_attachment_var` / `_clear_attachments` |
| 🎤 マイクボタン | `_toggle_mic`(状態色: 赤=ON待機、黄=認識中/処理中、灰=OFF。`_mic_idle` / `_mic_listening` / `_mic_processing`) |
| ⏹ 停止ボタン | `_stop_all` → `Controller.stop`(LLM生成・TTS・マイクを停止) |
| 🏠 家計簿モードボタン | `_toggle_kakeibo` |
| 💪 健康記録モードボタン | `_toggle_health` |
| 「送信」ボタン | `_send` → `Controller.handle_text`(生成中は無効化) |
| ステータスバー | `_update_status`(生成中/待機中、ゲスト表示、モデル名、max_tokens/temp、ターン数、マイク状態) |
| 免責文言(入力欄上) | `_build_ui` 内の固定Label |
| 設定画面「生成設定」 | `SettingsDialog`(モデルパス+参照、LLM GPUオフロード希望モード6種、actual GPUオフロード率・配置層数の読み取り専用表示、GPU配置の自動再評価、n_ctx、最大返答トークン、会話の自由度0.0〜2.0、VAD感度、起動時マイク/TTSチェック、保存済み添付管理) |
| アバターウィンドウ | `AvatarWindow`(枠なし・最前面・ドラッグ移動・右クリックで表示切替) |

## 5. モジュール構成

| ファイル | 主な責務 | 主なクラス・関数 | 呼び出し元 | 呼び出し先 |
|---|---|---|---|---|
| LLM_Local_Chat.py | エントリーポイント、Tkinter UI、設定I/O、LLMロード | `main` `ChatApp` `SettingsDialog` `AvatarWindow` `load_settings` `save_settings` `init_llm` `count_tokens` | 起動スクリプト | app_composition, controller, audio_workers, integrations, resource_monitor(adjust_llm) |
| app_composition.py | 起動時依存の生成(composition root) | `AppDeps` `create_app_deps` | `main` | resource_monitor, session_store |
| controller.py | 送信・停止・要約、操作世代ID、トークンコストLRUの管理 | `Controller` `ControllerDeps` `TokenCostCache` | `ChatApp.__init__` | llm_service, prompt_builder, resource_manager, json_extractors |
| llm_service.py | 通常生成と要約の排他的な別スレッド実行+[Perf]計測ログ | `LLMService`(`generate` `summarize` `abort` `is_running`) | Controller | llama_cpp |
| prompt_builder.py | messages構築、履歴トークン予算管理、モード別プロンプト | `PromptBuilder`(`build` `build_kakeibo_prompt` `build_health_prompt`) | Controller | — |
| prompt_inputs.py | 外部prompt解決、添付の検証・上限、テキスト区切り、画像data URI化 | `Attachment` `load_attachment` `resolve_system_prompt` `build_multimodal_user_content` | ChatApp, Controller | Pillow |
| attachment_store.py | UUID namespace内の添付sidecar、SHA-256検証、保留追加/削除、reparse・path境界 | `AttachmentStore` `CleanupResult` | SessionStore | atomic_io |
| json_extractors.py | LLM応答からのJSON抽出(正規表現) | `extract_kakeibo_transactions` `extract_kakeibo_json` `extract_health_json` | Controller._on_llm_done | — |
| kakeibo_split.py | 1入力を複数取引候補へ分割し、source_textの実在・重複・順序・件数上限・原文金額の網羅・断片の文脈・日付の一意性を検証 | `MAX_KAKEIBO_TRANSACTIONS_PER_INPUT` `normalize_transactions` `build_kakeibo_candidates` | Controller._on_llm_done | kakeibo_amount, kakeibo_confirmation, kakeibo_date, prompt_builder |
| session_store.py | セッションJSONの保存/読込/削除/改名、検索キャッシュ、添付メタデータとsidecarのtransaction統合 | `SessionStore` | create_app_deps → ChatApp | attachment_store, portable_history |
| integrations.py | 連携payloadサニタイズ、localhost制限、確認ダイアログ、POST | `IntegrationBridge` `sanitize_kakeibo_record` `sanitize_biolog_record` `is_allowed_local_api_url` | ChatApp(委譲メソッド経由) | urllib |
| audio_workers.py | SAPI5 TTSワーカー、VAD+Whisper音声認識 | `TTSWorker` `VoiceRecognizer` | ChatApp | win32com, pyaudio, WhisperPool.get_model |
| resource_manager.py | 推論直前のmax_tokens決定の薄いラッパー | `ResourceManager`(`decide`) | Controller | resource_monitor.adjust_inference |
| resource_monitor.py | VRAM/GPU/CPU観測、ロード前/推論前調整、Whisper GPU/CPU切替 | `ResourceMonitor` `VRAMGuard` `adjust_llm` `adjust_inference` `WhisperController` `WhisperPool` | app_composition, init_llm, resource_manager, VoiceRecognizer | pynvml / nvidia-smi / psutil / whisper |

スレッド構成: メイン(Tk mainloop)/通常生成・要約(`LLMService`で排他実行)/TTS再生/音声認識/リソース監視/連携POST。UI更新は `root.after` 経由。

## 6. 設定仕様(chat_settings.json)

設定はアプリ配置フォルダ基準の `chat_settings.json`。履歴・アバターも同じ基準で解決する。`model_path` が絶対パスなら維持し、相対パスだけアプリ配置フォルダ基準で解決する。ファイル欠損・JSON破損時は全キー既定値で継続(通知なし)。

| キー | 型 | 既定値 | 用途 | 設定画面から変更 | 再起動が必要か | 使用コード位置 |
|---|---|---|---|---|---|---|
| model_path | str | models\gemma-3-4b-it-q4_k_m.gguf | GGUFモデルパス | ○ | 不要(即再ロード) | `init_llm`, `ChatApp._reload_llm` |
| n_ctx | int | 8192 | コンテキスト長 | ○(512以上) | 不要(即再ロード) | `init_llm`, `PromptBuilder.build` |
| llm_gpu_offload_mode | str | auto | auto / full / 75 / 50 / 25 / cpu（希望モードのみ。actual stateは非永続） | ○ | 不要(hot reload) | `ChatApp._open_settings`, `_reload_llm`, `init_llm` |
| max_tokens | int | 1024 | 最大生成トークン | ○(1以上) | 不要(即時) | `ResourceManager.decide` 経由 |
| temperature | float | 0.7 | 生成温度 | ○(0.0〜2.0) | 不要(即時) | `LLMService.generate` |
| tts_enabled | bool | false | 起動時TTS状態 | ○ | 不要(切替は即時。ただしWhisperロード条件には起動時値が使われる) | `TTSWorker.enabled`, `_load_whisper_async` |
| mic_enabled | bool | false | 起動時マイク状態 | ○ | **条件付きで必要**(起動時にWhisper未ロードの場合、有効化には再起動が必要) | `VoiceRecognizer.enabled`, `_load_whisper_async` |
| whisper_mode | str | auto | auto / gpu_small / gpu_medium / cpu_small | ○ | **必要** | `WhisperPool.load` |
| vad_threshold | int | 150 | VAD感度(RMS閾値) | ○ | 不要(即時) | `VoiceRecognizer` |
| n_threads_batch | int | 12 | llama.cppバッチスレッド数 | ✕(ファイルのみ) | モデル再ロードが必要 | `init_llm`(`_valid_positive_int` 検証あり) |
| n_batch | int | 1024 | バッチサイズ | ✕ | モデル再ロードが必要 | `init_llm`(検証あり) |
| n_ubatch | int | 512 | マイクロバッチサイズ | ✕ | モデル再ロードが必要 | `init_llm`(検証あり) |
| flash_attn | bool | true | Flash Attention使用 | ✕ | モデル再ロードが必要 | `init_llm`(検証あり。失敗時は自動でOFFリトライ) |
| offload_kqv | bool | true | KQVのGPUオフロード | ✕ | モデル再ロードが必要 | `init_llm`(検証あり) |
| use_mmap | bool | true | mmap使用 | ✕ | モデル再ロードが必要 | `init_llm`(検証あり) |
| vram_danger_gpu_pct | int | (ファイル上88) | — | ✕ | — | **未使用**(コードから参照ゼロ。閾値は `resource_monitor.py` の定数にハードコード) |
| vram_danger_vram_pct | int | (ファイル上90) | — | ✕ | — | **未使用**(同上) |
| system_prompt | str | 未指定 | inline system prompt。空/未指定時はコード既定 | ✕(ファイルのみ) | 必要 | `resolve_system_prompt` |
| user_personalization | str | 未指定 | ユーザー向けpersona文章 | ✕ | 必要 | `resolve_system_prompt` |
| response_language | str | 未指定 | 応答言語の指示 | ✕ | 必要 | `resolve_system_prompt` |
| reasoning_visibility_instruction | str | 未指定 | 内部思考・分析を表示しないための指示 | ✕ | 必要 | `resolve_system_prompt` |
| external_prompt_files | object | 未指定 | system/persona/profile/instructions用TXT / MD。相対パスはアプリ配置フォルダ基準 | ✕ | 必要 | `resolve_system_prompt` |
| vision_enabled | bool | false | Vision handlerを有効化 | ✕ | モデル再ロードが必要 | `vision_settings_from_config`, `init_llm` |
| vision_handler | str | gemma4 | `gemma4` / `llava15` | ✕ | モデル再ロードが必要 | `create_vision_chat_handler` |
| vision_projector_path | str | 空 | 対応projector / mmproj GGUF。相対パスはアプリ配置フォルダ基準 | ✕ | モデル再ロードが必要 | `_validate_vision_settings` |

- 値検証: perf系6キー(`n_threads_batch`〜`use_mmap`)のみ `_valid_positive_int` / `_valid_bool` でフォールバックあり。その他のキーはファイル値をそのまま採用(型不正時は後段処理でエラーになり得る)。
- 自動保存: 終了時(`_on_close`)・設定ダイアログ保存時・TTSメニュー切替時に `save_settings` で全体を上書き保存。
- `chat_settings.json.example` がテンプレート。`tts_enabled` はコード既定値と同じ `false`。

## 7. モデルと生成処理

- **対応モデル形式**: GGUF(llama-cpp-python 0.3.34 CUDA 12.4版)。
- **Vision**: `vision_enabled=true`かつ有効なprojectorがある場合だけ、0.3.34の`Gemma4ChatHandler`または`Llava15ChatHandler`を`chat_handler`として各ロード候補へ新規生成する。画像はローカルdata URIで渡し、MTMDの`image_max_tokens`と事前context予約を4,096にそろえる。handlerはLlamaより先に解放する。Vision無効時は既存のテキスト専用Llama引数を維持する。
- **読み込み処理**: `init_llm(model_path, n_ctx, res_monitor, perf_settings)`。
  1. パス存在チェック(なければ `FileNotFoundError`)
  2. `vocab_only`のGGUF metadataからモデル名に依存せず総レイヤー数を取得。取得不能でもFull条件成立時は従来どおり`n_gpu_layers=-1`、Full条件不成立時は安全側でCPUを選ぶ
  3. `adjust_llm(res_monitor, model_path)`でCUDA対応と実空き容量を確認。Fullは従来の`GGUFサイズ+予約領域`を満たす場合だけ選び、不足時は線形概算で条件を満たす約75%→50%→25%→CPUの順に候補化する
  4. 各候補は性能設定込み → flash_attnのみOFF → 基本設定のみ(`n_threads=8, n_batch=512`)の互換リトライを行う。明示的なGPUメモリ不足だけ一段低い層数へ移り、非VRAMエラーは層数変更で隠さない
  5. GPUロード成功後も実空き容量が最低予約量を満たすか確認し、不足・取得不能なら安全に解放して下位候補へ移る。解放に失敗した場合は追加ロードせず停止する
- **GPUオフロード設定/表示**: `llm_gpu_offload_mode`には希望モードだけを保存する。ロード後安全確認を通ったactual runtime stateは別に`ChatApp`が保持し、設定画面に割合・配置層数・Full/Partial/CPU・選択理由を読み取り専用表示する。Fullの`-1`は総層数へ変換し、受理された最新reload世代だけを反映する。actual stateは`chat_settings.json`へ保存しない。
- **コンテキスト長/最大トークン/temperature**: 設定値(§6)。temperatureはUIで0.0〜2.0に制限。
- **推論直前の動的制限**: `ResourceManager.decide` → `adjust_inference`。実空き容量が予約領域(512MBまたは総容量の6%)未満で希望モードが`auto`かつGPU配置中なら、UI/履歴/TTS/連携副作用を開始する前に現在より低いoffloadへhot reloadし、同じrequest contextを最大1回だけ生成へ戻す。再試行後も不足、手動モード、CPU配置、再配置失敗では理由を表示して入力欄へ原文を戻す。512〜1024MBでmax_tokensを1/4、1024〜1536MBで1/2(下限256)。CPU推論はGPU残量で遮断しない。
- **読み込み失敗時**: ステータスバー「❌ モデル読込失敗」+エラーダイアログ(`_on_llm_ready`)。アプリは落ちない(送信時は「準備中」警告)。
- **生成停止**: ⏹ → `Controller.stop`。`_llm_abort` フラグ+`LLMService.abort()`(ストリーミングループ内で中断)+TTS停止+アバター停止。最大3秒待機し、終了済みならUIロックを解除する。3秒以内に停止できない場合は安全のため`stopping`状態と操作ロックを維持して再起動を案内し、低頻度の監視を継続する。ワーカーが遅れて終了した場合は、同じ操作世代であることを確認してロックを解除する。中断時は履歴保存・TTSをスキップ。
- **プロンプト構築**: `PromptBuilder.build`。system prompt(「シロ」ペルソナ固定文)+モードヒント+要約(あれば)+トークン予算内の直近履歴+ユーザー入力。履歴予算は `(n_ctx - max_tokens - system - user) × 0.60`、ペアごとのトークンコストをキャッシュ。
- **性能ログ**: `tokens_per_sec` はストリームチャンク数ではなく、完了・中断時点の生成本文をモデルtokenizerで数えた出力トークン数から算出する。
- **要約メモリの発生条件**: §3参照(4ターンごと、別スレッド、max_tokens=80/temperature=0.3/stop=["\n"])。

## 8. 音声仕様

- **マイク入力**: PyAudio 16kHz/mono/1024チャンク常駐読み取り(`VoiceRecognizer._loop`)。マイク初期化失敗時は例外を出さず音声認識を無効化。実行中の読取障害は例外クラスだけを初回と30秒間隔で記録し、復旧時にも一度記録する。
- **VAD**: RMS閾値方式。閾値超えが6チャンク連続で発話開始、閾値未満が30チャンク連続または6秒で録音終了。閾値は設定 `vad_threshold`(既定150、設定画面から即時変更可)。
- **Whisper**: openai-whisper。`language="ja"`, `beam_size=1`, `temperature=0.0`, `no_speech_threshold=0.8`, `logprob_threshold=-1.5`, `condition_on_previous_text=False`。認識後の品質ゲートでは、実発話保護・無音確率・平均logprob・短句反復に加え、`compression_ratio >= 2.40`かつ`no_speech_prob >= 0.50`の実測型を無音ハルシネーションとして破棄する。高compression単独、または`no_speech_prob < 0.50`ではこの追加条件だけを理由に破棄しない。
- **GPU/CPU切り替え**: `whisper_mode=auto`ではLLMロード後の実空き容量が4096MB以上ならGPU medium、2048MB以上ならGPU small、それ未満ならCPU smallを選ぶ。手動GPU指定も同じ安全閾値を下回ればCPU smallへフォールバックする。GPU版が存在する場合のみ、`WhisperController`がGPU使用率>88%でCPUへ、<70%でGPUへ切り替える。
- **TTS**: SAPI5非同期再生(キュー方式)。停止は purge 相当(`Speak("", 3)`)。読み上げ前に ```コードブロック``` を除去。
- **音声入力とTTSの排他**: TTS発話開始で `VoiceRecognizer._tts_active=True` になり、マイクチャンクを完全読み捨て(ハウリング・自己認識防止)。発話終了時のTTS世代を保存し、800ms後もVoiceRecognizerインスタンスと世代が変わっていない場合だけ解除する(`ChatApp._on_tts_start` / `_on_tts_stop` / `_restore_vad`)。待機中に次の発話が始まった場合、古い復帰処理は何もしない。
- **起動時の読み込み条件**: `mic_enabled` が true のときだけ、LLMロード完了後にWhisperをロード(§2)。
- **設定変更後に再起動が必要な項目**: 起動時にWhisperがロードされなかった場合の`mic_enabled`と`whisper_mode`(§6)。perf系6キーもモデル再ロードが必要(設定画面からは変更不可)。

## 9. 保存データ

| データ | 保存場所 | 形式 | 読み込み処理 | 書き込み処理 | 削除される条件 |
|---|---|---|---|---|---|
| 設定 | chat_settings.json(アプリフォルダ) | JSON | `load_settings` | `save_settings`(終了時・設定保存時・TTS切替時) | 自動削除なし |
| 会話履歴・セッション | chat_logs/chat_YYYYMMDD_HHMMSS_ffffff.json | DPAPI暗号化JSONエンベロープ | `SessionStore.load` / `list_sessions` | `SessionStore.save`(応答完了ごと・Ctrl+S・終了時) | 個別削除、または確認済み保存期間を超過した場合 |
| 要約メモリ | セッションJSON内 `summary` | 文字列 | `_load_selected` | `_update_summary` → `_save_now` | セッション削除に従属 |
| テキストエクスポート | ユーザー指定パス | 平文.txt（毎回警告） | — | `_save_as_text` | 自動削除なし |
| ログ | 専用ファイルなし(標準出力へのprint。loggingはハンドラ未設定で既定非表示) | — | — | — | — |
| アバター画像 | avatars/(4ファイル) | PNG | `AvatarWindow._load`(読み取りのみ) | なし | なし(アプリは書き込まない) |
| Whisperキャッシュ | ~/.cache/whisper | モデルファイル | whisper内部(初回ダウンロード) | whisper内部 | アプリは削除しない |
| チャット添付実体 | chat_logs/attachments/&lt;session-id&gt;/&lt;attachment-id&gt;.&lt;ext&gt; | 生bytesのsidecar（アプリによる暗号化なし） | `AttachmentStore.read`（サイズ/SHA-256照合） | 添付選択時にpending add→履歴メタデータcommit→finalize | 現在チャットの添付解除、設定の個別/一括削除、所属チャット削除 |
| チャット添付メタデータ | セッションJSON内 `attachments` | `id/name/kind/mime_type/extension/size/sha256` | `SessionStore.load_attachments` | `SessionStore.add_attachments` | sidecar削除transactionと同時。本文・画像・元絶対パスは保存しない |

- 破損時挙動: 一覧生成では壊れたJSONを黙ってスキップ、個別読込ではエラーダイアログ。添付sidecarの欠損・読込不能・サイズ/SHA不一致は当該添付だけをLLM入力から除外し警告する。
- ゲストモード中はセッション書き込みが行われない。

## 10. 外部連携

- **対象**: 別プロセスで稼働する`kakeibo-bridge` APIとBiolog(健康記録)APIへのHTTP POST。いずれも任意の連携先で、本リポジトリはクライアント処理だけを持つ。
- **非同梱**: 家計簿アプリ、家計簿DB、家計簿Web UI、家計簿API/`kakeibo-bridge`サーバー、および家計簿側のDocker/Compose定義。
- **URL**(`integrations.py` 冒頭で決定):
  - 家計簿: 環境変数 `KAKEIBO_API_URL`(既定 `http://127.0.0.1:8767`。`KAKEIBO_BRIDGE_PORT` で上書き可)+ `/api/kakeibo/record`
  - Biolog: 環境変数 `BIOLOG_URL`(既定 `http://localhost:8766`)+ `/api/health/record`
- **localhost制限**: `is_allowed_local_api_url` が scheme=http かつ host が `localhost` / `127.0.0.1` / `::1` の場合のみ許可。それ以外(https含む)は送信を中止しチャット欄へ警告表示。
- **ポート制限**: 同じく `is_allowed_local_api_url` が `LOCAL_API_PORTS`(= `KAKEIBO_BRIDGE_PORT` と `8766`)以外のポートを拒否する。localhost上の無関係なサービスへJSONを送らないための多層防御。`KAKEIBO_API_URL` で別ポートを指定した場合も、`KAKEIBO_BRIDGE_PORT` を合わせないと送信は中止される。
- **送信前確認**: サニタイズ済みpayloadを整形表示した確認ダイアログ(はい/いいえ)を必ず経由。
- **サニタイズ**(`sanitize_kakeibo_record` / `sanitize_biolog_record`):
  - 許可キーのホワイトリスト方式(未知キー・`user_id`・`token`・`url`・`headers` 等はここで脱落)
  - 家計簿: `amount` は bool を拒否し、正の数値のみ許可
  - Biolog: APIスキーマと同じ日付・型・有限値・数値範囲を確認する。整数項目は整数値floatだけ整数へ正規化し、bool・文字列数値・NaN/Infinity・範囲外・dict/listを含むレコードは全体を拒否する。記録値が1つもないpayloadは送信しない。明示ラベル由来のフィールド情報は最終サニタイズまで保持するがAPI payloadには含めない。`user_id` はLLM出力を使わず送信時に固定値 `"self"` を付与
- **健康JSON候補**: fenced/bare候補を出現位置と内容で重複排除し、厳格JSONとして有効な最後の健康候補を採用する。NaN/Infinityを含む非標準JSONは抽出・表示除去の対象にしない。この規則は「例示の後に本番JSON」が続く応答への緩和策であり、JSONの意味を判定するものではないため、逆順の応答を完全には識別できない。
- **実際に連携先へ送信する操作**: 確認ダイアログで「はい」を選んだ場合のPOSTのみ。それ以外に外部へデータを送る処理はない。POSTは都度生成のdaemonスレッドで実行、タイムアウト5秒、結果はチャット欄に表示。連携先がPOST後に行う保存処理は本リポジトリの責務外。

## 11. 終了処理

`ChatApp._on_close`(ウィンドウ×・メニュー「終了」共通)の処理順:

1. 保存可否を確認し、終了が確定した場合だけclosing状態へ移行して現在チャットの添付メモリ参照を外す。保存済みsidecar・メタデータは残し、Controllerを停止世代へ進めてLLMをabortする。
2. `IntegrationBridge.begin_closing()`で新規API送信とUI通知を拒否する。
3. `VoiceRecognizer.stop()`、設定・セッション保存、`TTSWorker.terminate()`を実行する。
4. LLM・モデルロード・連携APIスレッドを`root.after()`で最大6秒監視する。
5. 全処理終了時、またはタイムアウト時にTkを破棄する。closing後のワーカー通知は共通`_post_ui`で破棄される。

## 12. 現在確認されている不整合・既知の問題

コード読解で確認できた事実のみ。

### 修正済み(2026-07-11、コミット `3a35b2d`)

- `Controller.stop`(controller.py)の `_voice.tts_active` 参照を実フラグ名 `_tts_active` に修正。旧コードはアンダースコア無しの別属性を作るだけのno-opで、停止ボタンによる即時のマイク排他解除が効いていなかった
- 起動時にWhisperロードをスキップした場合(`_whisper_load_skipped`)、🎤ボタン押下時の案内を「音声認識は起動時に読み込まれていません。設定で『起動時にマイクを有効にする』をオンにして、アプリを再起動してください。」に変更。旧コードは「読み込んでいます。しばらくお待ちください」と表示され続けた
- ※いずれも静的確認のみ。実機での動作確認は未実施(§13)

### 未修正の既知問題

| # | 内容 | 分類 |
|---|---|---|
| 1 | `vram_danger_gpu_pct` / `vram_danger_vram_pct` — 設定ファイルに存在するが、現行コードに読む箇所がない | **未使用設定** |

## 13. 未確認事項

コードだけでは確定できない事項(実装済みの事実と混同しないこと):

- 2026-08-25までに、実機での起動・モデルロード、GPUオフロード設定のGUI smoke、再起動不要のhot reload、Auto runtime downshift、実モデル推論、家計簿候補生成を確認済み。これらの確認範囲を、未確認の音声・TTS・外部連携全般へ拡張して解釈しないこと
- v1.7.2のBiolog日付表示修正は自動テストで確認済み。重複データを避けるため、修正後の実Biolog APIへの再登録によるend-to-end確認は未実施
- v1.8.0の永続添付は再起動復元、元ファイル独立、同名別ID、添付のみ送信、個別/一括/チャット削除、欠損・改変、portable除外、inline＋external結合を含む全回帰640件を実行し、639件成功・1件skip（symlink作成権限なし）。実Windows GUIでの一連操作と実Visionモデル推論は手動確認事項
- Vision用projector / mmprojは配布物に含まれないため、実Visionモデルでの画像推論完走は未確認。設定検証、MTMD handler生成引数・解放順、非Vision拒否、data URI、context事前拒否は自動テストで確認
- 実際のマイク・オーディオドライバを使う音声認識、利用環境のSAPI5音声、別途用意した各連携APIのあらゆる構成での動作
- 実機での要約待機、停止、TTSフラグ、マイク案内の操作確認
- `llm_service.py`・`resource_manager.py` の正確な作成経緯(CHANGELOG未記載。ファイル日時と内容から、それぞれ速度最適化作業・v1.2.0後の作業で作成と推測)
- README・操作マニュアルPDFの記述と実装の逐条一致(見出しレベルの確認のみ実施)
- CHANGELOG未記載の実装: 速度最適化一式([Perf]計測、`init_llm` の3段階リトライ、perf系6設定キー、Whisper条件付きロードスキップ)が、どのバージョン番号に属するか

## 14. 仕様の根拠

本書の各記載は、以下のファイルの直接読解、2026-08-27までの回帰テスト、および§13に明記した範囲の実機確認による。主な根拠はクラス名・関数名で本文中に併記した。

- LLM_Local_Chat.py(`main`, `ChatApp`, `SettingsDialog`, `AvatarWindow`, `load_settings`, `save_settings`, `init_llm`)
- app_composition.py(`create_app_deps`)/ controller.py(`Controller`)/ llm_service.py(`LLMService`)
- prompt_builder.py(`PromptBuilder`)/ prompt_inputs.py / attachment_store.py(`AttachmentStore`) / json_extractors.py / session_store.py(`SessionStore`)
- integrations.py(`IntegrationBridge`, `sanitize_*`, `is_allowed_local_api_url`)
- audio_workers.py(`TTSWorker`, `VoiceRecognizer`)/ resource_manager.py(`ResourceManager`)
- resource_monitor.py(`ResourceMonitor`, `VRAMGuard`, `adjust_llm`, `adjust_inference`, `WhisperController`, `WhisperPool`)
- 設定・起動: chat_settings.json.example、LLMローカル対話型AI.bat、start.sh
- 参考ドキュメント(記載と実装の差異は§12参照): CHANGELOG.md、README.md、architecture_snapshot.md
