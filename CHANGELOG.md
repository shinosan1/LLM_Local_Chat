# Changelog

本ファイルはLLM Local Chatの変更履歴を記録します。  
バージョン管理は [Semantic Versioning](https://semver.org/lang/ja/) に準拠します。

---

## [未リリース] - 2026-07-19

### 追加
- 設定画面にWhisper実行モード（自動・GPU small・GPU medium・CPU small）を追加し、選択モデルとデバイスを診断ログ・ステータスへ表示
- 通常生成と要約を同一`LLMService`で直列化し、操作世代IDで古いコールバックを無効化する排他制御を追加
- チャット履歴検索用にタイトル・要約のメモリインデックスを追加し、変更されたJSONだけを再読込する回帰テストを追加
- ガード・エラー・要約・停止・VAD設定を対象とする`unittest`回帰テストを追加
- LLM応答のストリーミング表示中に、文末(`。`・`！`・`？`・改行)まで生成された文から順次TTSで読み上げる機能を追加
- 生成設定にSAPI5読み上げ速度(`tts_rate`, `-10`〜`10`, 既定値`0`)を追加。保存後、次の読み上げから再起動なしで反映
- ストリーミングTTSの文分割、コード/JSONブロック除外、TTS/Whisper競合、速度検証、SAPI完了待機を確認する回帰テストを追加
- Whisper認識ごとに入力デバイス、録音時間、RMS、使用モデル、セグメント品質指標(`no_speech_prob`・`avg_logprob`・`compression_ratio`)、送信/破棄理由を出力する診断ログを追加(音声データは保存しない)

### 変更
- 設定・履歴・アバターと相対モデルパスを、起動時のカレントディレクトリではなくアプリ配置フォルダ基準で解決するよう変更
- 生成速度ログの`tokens_per_sec`をストリームチャンク数ではなく、モデルtokenizerで数えた実出力トークン数から算出するよう変更
- モデル再読込時に旧モデルを明示解放し、世代IDによる古い結果の破棄と失敗時の旧設定復旧を追加。終了時はLLM・音声・TTS・API処理を最大6秒監視
- 自動モードではLLMロード後の空きVRAMが4096MB以上ならWhisper medium、2048MB以上ならsmallをGPUへ配置し、それ未満ではCPU smallへフォールバックするよう変更
- VRAM安全判定を固定7,000MBからGPU総容量・実空き容量・動的予約領域ベースへ変更し、LLMをWhisperより先にGPUロードするよう起動順を直列化
- 8GB級GPUではLLMを優先し、空き容量が4,096MB未満の場合はWhisper mediumをロードせずCPU smallへフォールバックするよう変更
- 起動時にCUDAオフロード対応、要求GPUレイヤー、LLMロード前後のVRAM内訳、Whisper配置方針を診断ログへ表示
- TTSのみ有効な構成ではWhisperをロードせず、起動発話をWhisper準備処理から独立して一度だけ実行するよう変更
- トークンコストキャッシュを最大2048件のLRUへ変更
- TTS実装に合わせて未使用の`pyttsx3`依存を削除し、CPU/CUDA別のPyTorch導入手順を整理
- 複数文の連続読み上げを1つのTTSバッチとして管理し、文間でアバターとマイク抑制状態が解除されないよう変更
- SAPI5の`SpVoice`を文ごとの生成からTTSワーカースレッド内での再利用へ変更
- 通常再生から`PurgeBeforeSpeak`を除外し、`RunningState`監視を`WaitUntilDone(50)`による完了待機へ変更。Purgeは停止要求時のみ実行

### 修正
- 実機で観測された高圧縮・中程度no-speechのWhisper無音誤認識（`compression_ratio >= 2.40`かつ`no_speech_prob >= 0.50`）を、正常発話の保護帯を維持しながら送信前に破棄
- 停止要求から3秒以内にLLMワーカーが終了しない場合、UIロックを偽装解除せず再起動を案内し、操作世代を確認しながら遅延終了を監視するよう修正
- TTS終了後800ms以内に次の発話が始まるとマイク抑制が誤解除される競合を、既存のTTS世代カウンタで防止
- マイク切断・ドライバ障害時の読取エラーを、秘密情報を含めず30秒間隔で記録し、復旧も確認できるよう修正
- Biolog送信前に日付・型・有限値・API準拠の数値範囲を検証し、NaN/Infinityやネストした非文字列値を含むレコードを全体拒否するよう修正
- 複数の健康JSONがある応答では厳格JSONとして有効な最後の候補を採用し、先行する例示JSONを誤採用する問題を緩和
- LLMの`reset()`失敗時に性能計測が未初期化の`reply`を参照する問題を修正
- 未参照だったストリーミング位置属性`_stream_mark`の代入を削除
- 健康測定値だけの入力をLLMが`memo`へ複製し、既存メモを上書きする問題をプロンプト制約と送信前サニタイズで防止
- 健康JSONが構文的に正しくても全項目が空の場合は、送信を諦めずJSON専用再抽出を1回行うよう修正
- 健康記録で`memo: null`を送信せず、行動・食事・メモだけの記録をBiologへ連携できるよう修正
- 健康モードで明示されたメモ・備考をBiologの`memo`へ登録できるよう修正
- 健康記録JSONを応答先頭で確定して表示・TTS・履歴から除外し、途中切れ時は排他制御下でJSON専用再抽出を1回行うよう修正
- 健康測定値が行動ログへ重複登録される問題を、プロンプト制約とサニタイズで防止
- 健康記録・家計簿モードの履歴とタイトルに、LLM向け指示文ではなくユーザー原文だけを保存するよう修正
- 要約中・停止待機中・履歴切替後に同一`Llama`へ並行アクセスできる経路を修正
- VRAMガード遮断と生成エラーを会話履歴・要約・TTSへ混入させず、入力原文を再送可能にするよう修正
- VAD閾値を1〜32767に検証し、boolや既存の不正設定値を既定値へ戻すよう修正
- Whisperセグメントの品質指標を使い、低信頼無音(`no_speech_prob >= 0.60`かつ`avg_logprob <= -0.90`)と高圧縮反復(`compression_ratio >= 2.40`)を送信前に破棄する保守的なハルシネーションフィルターを追加
- Whisper解析中にTTSが開始された場合、世代カウンタで一時的な重複も検出し、認識結果を送信前に破棄するよう修正(自己音声による再送信ループを防止)
- 高い読み上げ速度や短い冒頭文で、非同期SAPI再生の開始競合により文頭または文全体が欠ける可能性を修正
- 停止処理(`Controller.stop`)の `_voice.tts_active` を実フラグ名 `_tts_active` に修正(旧コードは未使用属性を作るだけで、停止時の即時マイク排他解除が効かなかった)
- 起動時にWhisperロードをスキップした場合、マイクボタン押下時に「設定で起動時マイクを有効にして再起動」の正しい案内を表示(従来は「読み込んでいます」と表示され続けた)

### リポジトリ整備
- `gitignore` をドット付き `.gitignore` にリネームして除外を有効化(`backup_before_*/`・`OLD_DATA/`・`*.bak`・`*.bak_*`・`advanced_settings.json` を追加)
- v1.2.0〜v1.3.0のモジュール分離11ファイルと速度最適化を含む現行実装をコミット固定
- 現行仕様書 `docs/CURRENT_SPEC.md` を追加(コード読解に基づく現行仕様の基準資料)

---

## [1.3.0] - 2026-06-25

### 追加
- 段階的リファクタリングにより、`LLM_Local_Chat.py` から主要責務を分離
  - `controller.py`: 送信・停止・LLM完了処理のオーケストレーション
  - `prompt_builder.py`: 通常・家計簿・健康記録プロンプト生成
  - `json_extractors.py`: 家計簿/Biolog向けLLM応答JSON抽出
  - `session_store.py`: `chat_logs/` の保存・読み込み・一覧・検索・削除・名前変更
  - `audio_workers.py`: `TTSWorker` / `VoiceRecognizer`
  - `app_composition.py`: 起動時依存生成 (`AppDeps`, `create_app_deps`)
  - `integrations.py`: 家計簿/Biolog送信前ガード・サニタイズ・確認ダイアログ・POST処理
- Biolog/家計簿送信前の安全ガードを追加
  - サニタイズ済みpayloadのみ確認ダイアログに表示
  - `localhost` / `127.0.0.1` / `::1` 以外のAPI URLを拒否
  - `https://localhost`、`localhost.evil.com`、malformed URL、port不正URLを拒否
  - LLM出力の未知キー、`user_id`, `token`, `url`, `headers` を破棄

### 変更
- `Controller` の `sys.modules["__main__"]` 依存を廃止し、`ControllerDeps` 経由の明示依存注入へ変更
- `PromptBuilder` に家計簿・健康記録プロンプト生成を移動し、`ChatApp` のUI責務を軽量化
- import時の `ResourceMonitor` / `VRAMGuard` / `WhisperPool` 生成を廃止し、`main()` 起動時の `create_app_deps()` で生成する構成へ変更
- `ChatApp` は `AppDeps` を受け取り、`SessionStore`、`WhisperPool`、`ResourceMonitor` を明示的に利用する構成へ変更
- `TTSWorker` / `VoiceRecognizer` は `audio_workers.py` へ移動し、音声スレッド・VAD・TTSの既存挙動を維持
- 家計簿/Biolog連携は `IntegrationBridge` に分離し、`ChatApp` にはController互換の薄い委譲メソッドのみ残す構成へ変更

### 修正
- 家計簿の `amount` 判定で `bool` を明示的に拒否
- Biologの `user_id` はLLM出力を採用せず、送信時に固定値 `self` を付与する挙動を明確化
- `LLM_Local_Chat.py` を import しただけでTkウィンドウ生成や重い依存生成が走らない構造に修正

### 検証
- `py_compile` による構文確認
- `import LLM_Local_Chat` / `import app_composition` / `import integrations` の副作用確認
- JSON抽出、プロンプト生成、SessionStore、IntegrationBridge、audio_workers の軽量直接確認
- Dドライブ実行用フォルダへの明示ファイルコピーと、D側 `.venv` での import / py_compile 確認

---

## [1.2.0] - 2026-05-06

### 追加
- **VRAM安全フィルタ** (`resource_monitor.py` 新規作成)
  - `ResourceMonitor`: 0.5秒周期で VRAM/GPU/CPU を収集するデーモン（pynvml → nvidia-smi → CPU-only でフォールバック）
  - `VRAMGuard`: `vram_score = vram_ratio + gpu_pct * 0.001` による即時安全判定（予約・状態なし）
  - `adjust_llm()`: ロード前の `n_gpu_layers` 自動調整（VRAM使用率 > 85% → CPU専用）
  - `adjust_inference()`: 推論直前の `max_tokens` 段階削減（レンジ化バッファ +500/800/1200MB）+ Whisper ΔWhisperGPU% スパイク検知
  - `WhisperController`: GPU/CPU ヒステリシス切替（88%→CPU、70%→GPU）+ `delta_gpu_pct` 変化量追跡
  - `WhisperPool`: GPU版(medium) + CPU版(small) を起動時に両ロード、ゼロコスト切替

### 変更
- `init_llm()`: `adjust_llm()` による `n_gpu_layers` 自動調整（VRAM高負荷時 CPU 実行へフォールバック）
- `_load_whisper_async()`: 従来のデバイスループ → `WhisperPool.load()` に置き換え（GPU+CPU 同時ロード）
- `VoiceRecognizer._loop()`: `self.whisper_model.transcribe()` → `self.whisper_model.get_model()` + `model.transcribe()` に変更（ヒステリシス付き切替）
- `_llm_worker()`: `adjust_inference()` による `max_tokens` 動的調整を追加。VRAM 危機時は推論中止しメッセージを表示
- `chat_settings.json`: `vram_danger_gpu_pct`, `vram_danger_vram_pct` フィールドを追加

### 設計思想
- 「VRAMを管理する」ではなく「VRAMから逃げる」設計
- OOM完全防止は不可能。「クラッシュではなく劣化」で済ませることが目標
- VRAM が唯一の判断軸（GPU% は参考ログのみ。WhisperController のみ GPU% を使用）
- 既知の限界: TOCTOU あり・単一プロセス内のみ有効・確率的削減が目的

---

## [1.1.2] - 2026-05-06

### 修正
- `_strip_code_blocks`: `re.DOTALL` 依存を廃止し `[\s\S]*?` パターンに変更  
  → 言語指定あり・なし・複数行すべてのコードブロックを確実に TTS から除去
- `_health_build_prompt`: `meal_detail` のハルシネーション抑制  
  → フィールド説明に「ユーザーの発言通りの表記を使用、言い換え・造語・変換禁止」を明示

### 変更
- `activity_log` フィールド説明を「運動のみ」→「運動・作業・出来事など今日の活動内容」に拡張  
  → 「アプリのアップデートをした」などの日常活動報告も記録対象に

---

## [1.1.1] - 2026-05-06

### 修正
- TTS読み上げからコードブロック（` ``` ... ``` `）を除外 — チャット画面への表示は変更なし
- `import re` をトップレベルに一本化（`_extract_kakeibo_json` / `_extract_health_json` 内の重複を削除）

### 追加
- `_strip_code_blocks(text: str) -> str` ユーティリティ関数（モジュールレベル）

---

## [1.1.0] - 2026-05-06

### 追加
- **Biolog連携**: 健康記録モード（💪ボタン）でLLMレスポンスから体重・体脂肪等をJSON抽出し、Biolog API (`localhost:8766/api/health/record`) へ自動POST
- `BIOLOG_URL` 環境変数によるエンドポイント切り替え対応（デフォルト: `http://localhost:8766`）
- `request_id` (UUID v4) による冪等性キー付与（skills.md §3-2 準拠）
- `meal_detail` + `activity_log` → `memo` フィールドへの自動変換
- HTTPError (422等) の詳細エラーメッセージをチャットに表示
- `import uuid` 追加

### 修正
- `HEALTH_API_URL` が未接続ポート(`8001`)を指していた問題を修正 → `BIOLOG_API_URL` (`8766`) に変更
- `_health_build_prompt` の `muscle_mass` 単位を骨格筋率(%) → 筋肉量(kg) に修正（Biolog スキーマ準拠）
- `protein_intake` フィールドをLLMプロンプトから削除（Biolog スキーマ外フィールド）

### 変更
- `_send_to_health_api` → `_send_to_biolog_api` に刷新（Biolog スキーマ対応）
- `_on_llm_done` 内の健康記録処理を `tts.speak()` 後に移動（API送信とTTSを並行実行）
- README.md に v1.1.0 バッジ・Biolog連携の使い方・環境変数説明を追記
- CHANGELOG.md を新規作成 → 既存エントリへの追記に変更

### アーキテクチャ（維持）
- `LLM_Local_Chat.py`: Windowsホスト実行を維持（SAPI5 TTS は Windows COM 専用のため Docker 非対応）
- `biolog-api`: Dockerコンテナ (docker-compose.yml, port 8766) — 変更なし
- `_tts_active` フラグ（VoiceRecognizer の VAD 排他制御）: 変更なし

---

## [1.0.6] - 2026-05-04

### 修正
- 停止ボタンを押した後にチャットを入力するとアプリが強制終了する問題を修正
    - `_stop_all`の冒頭で即座に`_is_thinking=True`・`_btn_send=DISABLED`を設定し、LLMリセット完了まで次の送信をブロックするよう変更
    - LLMリセット完了後に`_unlock`でUIを解除する方式に変更し、リセット中の二重送信によるクラッシュを防止
    - TTS起動発話ロジックの適正化
        -TTSWorker 内での直接発話を廃止し、メインアプリ（ChatApp）側の初期化完了タイミングで発話するように変更。これにより、バックグラウンドでの予期せぬブロッキングを防止。
    - LLM停止処理の安全性向上
        - ストリーミング生成中に llm.reset() を呼び出すと発生していた C++ レベルのクラッシュ問題を修正。
        - 停止ボタン押下時、即座にリセットせず、ワーカーのスレッド終了を安全に待機してからUIを解放するフロー（_wait_and_unlock）を導入。
    - フラグ管理の厳格化
        - 中断（Abort）時のフラグ処理を見直し、中断後に不要な履歴保存やUI更新が重複して発生しないようガード処理を強化。
    - コードのクリーンアップ
        - 使用されなくなった古いリセット用メソッドや、不適切なコメントアウトの削除。

---

## [1.0.5] - 2026-05-03

### 修正
- 停止ボタンが効かない不具合を修正
- TTSがOFFでも発話する不具合を修正
- 生成設定でマイク・TTSが両方ONになって起動する不具合を修正
- コードの大幅修正によりアプリが起動しなかった不具合を修正
- マイクにチェックが入らない不具合を修正
- 設定ダイアログ変更時に起動発話が誤って発火する不具合を修正
    - 起動発話は`_initial_greeting_done`フラグで管理し、アプリ起動時の1回のみ実行するよう変更

### 変更
- `requirements.txt`に`pywin32==311`を追加
- TTSエンジンをPowerShell（SAPI5）から`win32com`（pythoncom経由）に変更
    - `pywin32`パッケージを使用してWindows SAPIを直接呼び出すよう変更

### 既知の不具合（次バージョンで対応予定）
- `VoiceRecognizer`：Bluetoothデバイス等の遅延でクラッシュする場合がある
- `ChatApp`：`_on_tts_stop`の0.8秒待機がスレッドを乱発する

---

## [1.0.4] - 2026-05-03

### 修正
- TTSWorker（読み上げ機能）の安定性向上
    - `_run`メソッド内の二重ループ構造を解消し、処理を一本化することで動作の不安定さを解消
    - PowerShell（SAPI5）呼び出し時のテキストエスケープ処理とプロセス終了処理を最適化
- 音声認識（VoiceRecognizer）のデバイス判定を修正
    - Whisperモデルの`device`属性を直接参照するように変更し、CPU/GPUの判定精度を改善

### 変更
- `import torch`を追加
- AvatarWindowクラスのリファクタリング
    - 画像読み込み・リサイズ処理（`_load`メソッド）を統合し、コードの重複を排除
    - アバターの状態管理を整理し、初期化時の堅牢性を向上
- 全体的なコードのクリーンアップ
    - 各クラス間の重複ロジックを削除し、将来的な修正時のデッドロックや競合を抑制するための構造改善

---

## [1.0.3] - 2026-05-03

### 修正
- マイク・TTSを無効に設定した場合Whisperモデルを起動しないように修正
- VoiceRecognizerクラスの初期化エラー（AttributeError）を修正
    - 原因：`__init__`内で`self._enabled`が定義される前に`.clear()`が呼び出されていたため
    - 対応：属性の宣言順序を見直し、`threading.Event()`の初期化を先に行うよう変更

### 変更
- 動作環境のGPU推奨VRAMを6GB以上→8GB以上に変更
- 生成設定の「温度」を「会話の自由度」へ変更
- 生成設定に各項目の説明文を追加

---

## [1.0.2] - 2026-05-02

### 修正
- 初期起動時にマイク・TTSがOFFになるように修正
- 起動時の待機時間を10秒に設定（TTS着火安定化）
- `_stop`フラグの判定とクリアの順序を修正（2回目以降のTTSが再生されない問題）
- 終了時にmic_enabled・tts_enabledを設定ファイルに保存するよう修正
- メニューのTTS切り替え時に即座に設定ファイルへ保存するよう修正（`_toggle_tts`メソッド追加）
- `_open_settings`にmic_enabled・tts_enabledの初期値渡しと反映処理を追加
- TTS有効時に起動発話「システムを起動しました。」を追加（PowerShell初期化兼用）

### 変更
- 実行ファイル名を`tk_chat_local.py` → `LLM_Local_Chat.py`に変更
- 起動バッチファイル名を`AIローカル対話型AI.bat` → `LLMローカル対話型AI.bat`に変更
- README：GitインストールのTrueTypeフォント文字化け注意事項を追加
- README：「必ずお読みください」セクションを追加（音声ハルシネーション対処法）
- README：動作確認済みモデル一覧にgemma-4-E4B-Q4_K_M・LFM2.5-1.2B-Instruct-Q4_K_Mを追加

---

## [1.0.1] - 2026-05-01

### 修正
- アバターパスのスラッシュ抜けを修正
- ローカルパスを相対パスに変更（ポータビリティ向上）
- マイクボタンの動作説明を修正（起動後自動ON・ボタンでOFF）
- 停止ボタンの説明を修正（タイミングによっては中断できない場合がある旨を追記）
- phi-4の動作確認済み表記をphi-3.5に修正
- マイクON設定で起動した際にマイクが機能しないバグを修正
- VoiceRecognizer初期状態をOFFに変更・設定ファイルの値を優先して反映するよう修正
- マイクのデフォルトをONからOFFに変更
- TTSのデフォルトをONからOFFに変更

### 追加
- 設定ダイアログに「起動時にマイクを有効にする」「起動時にTTSを有効にする」チェックボックスを追加
- マイク・TTSの起動時状態を設定ファイル（chat_settings.json）で永続化するよう修正
- README：仮想環境の作成手順を追加
- README：Python・Git・CUDA Toolkitのインストール案内を追加
- README：Whisper初回ダウンロード（約1.5GB）の注意を追加
- README：Privateリポジトリでのclone方法の説明を追加
- README：動作確認済みモデル一覧を追加
- README：モデルリンク切れ時の案内を追加
- README：モデル利用規約に関する免責事項を追加
- README：「導入責任者の方へ」セクションを追加（セキュリティ特性・推奨事項）
- requirements.txtにsetuptools==78.1.1を追加
- chat_settings.json.exampleを追加
- .gitignoreを追加
- CHANGELOG.mdを追加
- 操作マニュアル（PDF）を追加

### 変更
- 動作保証をWindows 11のみに変更（Windows 10を除外）
- README：pip installコマンドに--no-cache-dirを追加
- README：ゲストモードの説明を正確な動作に修正
- README：「作者に直接お問い合わせ」をPAT発行案内に変更
- README：音声入力・TTSの説明をデフォルトOFFに合わせて修正
- README：セキュリティ項目にモデルによる外部通信の可能性がある旨の注意を追加
- README：diskcache（CVE-2025-69872）の脆弱性情報とpip-auditでの定期確認を推奨する旨を追加
- 免責事項を強化（禁止事項・保証の否認・準拠法を追加）
- ライセンス表記をMITからAll Rights Reservedに変更

---

## [1.0.0] - 2026-04-30

### 初回リリース

#### 主な機能
- ローカルLLM（llama-cpp-python）によるストリーミングチャット
- PyAudio + RMS-VAD + Whisper（medium）による常駐音声認識
- Windows SAPI（PowerShell経由）によるTTS読み上げ
- アバターウィンドウ（瞬き・口パクアニメーション）
- 会話の要約メモリ（長期会話対応）
- チャット履歴の保存・読み込み・検索
- ゲストモード（会話を保存しないプライベートモード）
- ダークテーマUI
- 生成設定（モデルパス・コンテキスト長・温度・VAD感度）

#### 動作確認済みモデル
- gemma-3-4b-it-q4_k_m.gguf
- phi-3.5-Q8_0.gguf

---

*以降のバージョンはこのファイルに追記していきます。*
