# Architecture Snapshot

## 1. Purpose

This document captures the current architecture of LLM Local Chat before the next refactoring/update pass.

目的は、後続作業者、特に Fable 5 が、現在の責務分離、実行時構成、データフロー、保守上の注意点を短時間で把握できるようにすることです。

このスナップショットはコード変更を目的としません。現行ファイル構成と主要クラス/関数を確認したうえで、確認できた内容だけを記録します。

## 2. Version Baseline

- Baseline: 公開版 `v1.8.2`（`CHANGELOG.md` の最新リリース `1.8.2 - 2026-09-05`）
- Snapshot target: 公開版v1.8.2の現行実装
- Main entrypoint: `LLM_Local_Chat.py`
- Runtime platform: Windows native desktop app

本書は2026-09-05にv1.8.2のコードと公開文書へ更新したスナップショットです。パーソナライズ、チャット単位の永続添付、Vision handlerに加え、保存失敗時の切替中断、音声認識世代の無効化、利用者設定を超えない`max_tokens`制限を現行baselineへ含めます。

## 3. Recent Architectural Changes

`1.3.0` では、巨大化していた `LLM_Local_Chat.py` から主要責務が段階的に分離されています。

- `controller.py`: 送信、停止、LLM完了処理のオーケストレーション。
- `prompt_builder.py`: 通常会話、家計簿、健康記録のプロンプト生成と履歴予算管理。
- `prompt_inputs.py`: 外部prompt解決、添付形式・サイズ検証、テキスト区切り、画像data URI構築。
- `attachment_store.py`: セッションID/添付ID namespace、sidecarのサイズ/SHA-256検証、保留追加・削除、path境界。
- `llm_service.py`: llama.cpp のストリーミング推論実行。
- `resource_manager.py`: 推論直前の VRAM 状態に応じた `max_tokens` 決定。
- `history_crypto.py`: WindowsユーザースコープDPAPIによる履歴暗号化、復号、エンベロープ検証。
- `session_store.py`: `chat_logs/` の暗号化保存、復号、平文移行、一覧、検索、保存期間削除、名前変更、添付メタデータとsidecarのtransaction統合。
- `audio_workers.py`: SAPI5 TTS と PyAudio/RMS-VAD/Whisper 音声認識。
- `app_composition.py`: 起動時依存生成。
- `integrations.py`: 家計簿/Biolog 連携のサニタイズ、ローカルURL制限、確認、POST処理。
- `json_extractors.py`: 家計簿/Biolog 向け LLM 応答 JSON 抽出。

また、Biolog/家計簿連携では、LLM出力をそのまま送信せず、送信前に payload をサニタイズし、ローカルAPI URLのみ許可し、確認ダイアログを経由する構造になっています。

`1.7.2`では、日付を省略したBiolog登録について送信直前にJST登録日を確定し、POST recordへ保持するようになりました。完了表示は有効なAPI応答日付を優先し、応答に日付がなければ実際にPOSTした確定日付を使用します。

`1.7.3`では、設定からsystem prompt・persona・応答言語等を合成し、UTF-8の外部TXT / MDを読み込む経路を追加しました。通常チャットではテキスト/画像添付を一時入力として扱い、履歴には添付内容を保存しません。Vision有効時だけMTMD chat handlerをLLMへ接続し、非対応モデルでは画像送信を拒否します。

`1.8.0`では、inlineとexternal promptを併用可能にし、添付を検証済み内部IDでチャットへ紐づくsidecarとして永続化しました。履歴JSONはpayloadを含まないメタデータだけを保持し、再起動復元、同名別内容、添付のみ送信、設定画面の個別/一括削除に対応します。

## 4. Current Module Responsibilities

`LLM_Local_Chat.py`

- Tkinter UI、アプリ状態、設定、チャット表示、アバター表示の中心。
- `ChatApp` が依存オブジェクトを受け取り、UIイベントを `Controller` や各サービスへ接続する。
- `init_llm()` は `resource_monitor.adjust_llm()` を使って LLM ロード時の GPU レイヤー設定を調整する。
- Vision有効時の`init_llm()`は、対応projectorとhandler名を検証し、各ロード候補へ新しいMTMD chat handlerを接続する。解放順はhandler、Llamaの順。
- `ChatApp._reload_llm()` は希望offloadモードの変更とAuto downshiftを、旧LLMのdetach/close後に新LLMをロードする世代管理付きworkerとして実行する。actual runtime stateだけを最新世代からUIへ反映する。
- `main()` が `create_app_deps(LOG_DIR)` を呼び、`ChatApp` を起動する。

`controller.py`

- `Controller` は薄いオーケストレーター。
- テキスト送信時に入力・セッション・モード・操作/モデル世代をrequest contextとして確定し、VRAM安全確認後に表示・履歴・TTS・連携処理を一度だけ開始する。
- Autoの推論直前hard limitでは、入力を保持したまま`ChatApp`へ下位offloadへのhot reloadを要求し、成功時だけ同じ入力を最大1回再試行する。
- 現在チャットの検証済み添付snapshotをrequestへ保持し、送信後も解除しない。履歴へ保存するuser値は入力欄の本文だけで、添付本文・画像本体・添付のみ送信の内部補完文は保存しない。
- プロンプト構築は `PromptBuilder`、推論は `LLMService`、VRAM判定は `ResourceManager` に委譲する。

`prompt_builder.py`

- system prompt、要約、履歴、ユーザー入力から llama.cpp 用 `messages` を組み立てる。
- `n_ctx`、`max_tokens`、履歴予算比率、token count cache を使い、古い履歴を予算内に収める。
- 家計簿モードと健康記録モードでは、LLMに JSON を出力させるための追加プロンプトを作る。
- 画像入力時はMTMD上限と同じ4,096トークンをcontext予算へ追加予約する。

`prompt_inputs.py`

- `chat_settings.json`のinline値と外部TXT / MDを順序どおり結合し、同一内容だけを重複排除する。
- TXT / MD / JSON / CSVとPNG / JPEGを検証し、実パスを保持しない`Attachment`へ読み込む。
- テキスト添付を区切り付き入力へ、画像をローカルdata URIへ変換する。どちらも自動実行しない。

`llm_service.py`

- `LLMService.generate()` が別スレッドで `llm.create_chat_completion(..., stream=True)` を実行する。
- UI操作やプロンプト判断は持たず、token/done/error コールバックだけを呼ぶ。
- `abort()` と `is_running()` で停止処理を支援する。

`resource_monitor.py` / `resource_manager.py`

- `ResourceMonitor` は VRAM/GPU/CPU をバックグラウンドで観測する。
- `VRAMGuard` は即時観測値から安全判定を行う。
- `adjust_llm()` は LLM ロード前に `n_gpu_layers` を調整する。
- `adjust_inference()` は推論直前に `max_tokens` を削減、または推論をブロックする。
- `WhisperPool` はCPU版Whisper smallと、設定・空き容量に応じたGPU版smallまたはmediumを保持する。
- `WhisperController` は GPU/CPU 切替のヒステリシスを持つ。
- `ResourceManager` は `adjust_inference()` の薄いラッパー。

`audio_workers.py`

- `TTSWorker` は Windows SAPI5 を pywin32 経由で呼び、アバターの口パクと連動する。
- `VoiceRecognizer` は PyAudio 入力、RMS-VAD、Whisper transcription を管理する。
- TTS中のVAD誤検知を避けるため、TTSアクティブ中は音声認識を抑制する設計。

`integrations.py` / `json_extractors.py`

- `json_extractors.py` は LLM応答から家計簿/健康記録 JSON を抽出する。
- `integrations.py` は抽出後の payload をサニタイズし、確認ダイアログ後にローカルAPIへ POST する。
- 許可される送信先は `http://localhost`、`http://127.0.0.1`、`http://[::1]` 相当のローカルホストのみ。
- Biolog送信では LLM出力の `user_id` は採用せず、送信時に `user_id: "self"` を付与する。

`history_crypto.py` / `session_store.py`

- セッション本文全体をDPAPIで必須暗号化し、平文フォールバックを行わない。
- v1.4.1以前の平文JSONは、元バイト列の復号照合後に原子的置換で移行する。
- `last_activity_at`と設定済み保存期間から削除候補を決定し、復号不能・現在使用中の履歴は自動削除しない。
- UI状態や LLM 処理は持たない。

`app_composition.py`

- `create_app_deps(log_dir)` で `ResourceMonitor`、`WhisperPool`、`SessionStore` を作成する。`VRAMGuard`は`resource_monitor.py`に定義されているが、この生成経路では使用しない。
- import時の重い副作用を避け、`main()` 起動時に依存生成するための境界。

## 5. Runtime Composition

起動時の構成は次の通りです。

1. `LLM_Local_Chat.main()` が Tk root を作る。
2. `create_app_deps(LOG_DIR)` が起動時依存を生成する。
3. `ChatApp(root, deps)` が UI、TTS、IntegrationBridge、Controller を接続する。
4. LLM はバックグラウンドスレッドで先にロードされる。
5. LLMロード完了後、Whisperは`_load_whisper_async()`から`deps.whisper_pool.load(deps.res_monitor)`で一度だけロードされる。
6. Whisperロード後、`VoiceRecognizer` が起動する。
7. Tk mainloop が UIイベントを処理する。

主要な実行時依存は `AppDeps` に集約されています。

- `res_monitor`
- `guard`
- `whisper_pool`
- `session_store`

ただし、現状では `ChatApp` がまだ多くの UI状態とアプリ状態を保持しています。リファクタリングは完了形ではなく、責務分離の途中段階です。

## 6. Main Data Flow

通常チャットの流れ:

1. ユーザーが本文を入力し、必要ならファイルを添付する。非ゲストでは選択時点のコピーとメタデータを現在チャットへcommitする。または `VoiceRecognizer` が音声をテキスト化する。
2. `Controller.handle_text()` が入力を受け取り、現在モードとVision可否を判定する。
3. `prompt_inputs.py`が添付を本文と区別して整形し、`PromptBuilder`がcontext上限を事前確認して`messages`を構築する。
4. `Controller` がrequest contextを保持し、`ResourceManager.decide()` がVRAM状態から`max_tokens`と実行可否を決める。
5. AutoのGPU配置でhard limitなら、旧LLMを解放して現在より低い配置へhot reloadし、同じrequest contextを最大1回だけ4へ戻す。
6. 安全確認後にユーザー表示・TTS stream・モード別pending stateを一度だけ開始する。受理済み添付は現在チャットに残す。
7. `LLMService.generate()` がストリーミング推論を別スレッドで実行する。
8. token は `ChatApp._append_stream_token()` へ渡され、チャット欄に逐次表示される。
9. 完了後、`Controller._on_llm_done()` が履歴、要約更新、TTS、保存、連携処理を進める。添付内容は履歴へ保存しない。
10. 性能ログの出力token数は生成本文をモデルtokenizerで数える。

停止処理の流れ:

1. `Controller.stop()` が `_llm_abort` を立てる。
2. `TTSWorker.stop_all()` で読み上げキューと再生を止める。
3. `LLMService.abort()` でストリーミングループに中断要求を出す。
4. 最大約3秒待機し、終了済みならUIロックを解除する。
5. 3秒以内に停止しなければ`stopping`と操作ロックを維持して再起動を案内し、低頻度監視を継続する。遅延終了時は操作世代を確認してから解除する。

## 7. Local Integration Flow

家計簿モード:

1. UI側で家計簿モードが有効になる。
2. `PromptBuilder.build_kakeibo_prompt()` が JSON 出力指示つきプロンプトを作る。
3. LLM応答後、`extract_kakeibo_json()` が JSON を抽出する。
4. `IntegrationBridge.confirm_and_send_kakeibo()` が payload をサニタイズする。
5. ローカルAPI URLであることを確認する。
6. ユーザー確認後、`KAKEIBO_API_URL` へ POST する。

健康記録/Biologモード:

1. UI側で健康記録モードが有効になる。
2. `PromptBuilder.build_health_prompt()` が JSON 出力指示つきプロンプトを作る。
3. LLM応答後、`extract_health_json()` が JSON を抽出する。
4. `prepare_biolog_record()` がユーザー原文の明示ラベル（食事ログ・行動ログ・メモ）を決定的に解析し、同じフィールドのLLM値より優先して統合する。
5. `IntegrationBridge.confirm_and_send_biolog()` が明示由来情報を保持したまま最終サニタイズする。由来情報はAPI payloadには含めない。
6. ローカルAPI URLであることを確認する。
7. ユーザー確認後、日付省略時はJST登録日を確定してrecordへ保持し、`user_id: "self"` を付与して `BIOLOG_API_URL` へ POST する。
8. 成功応答に有効な`date`があればその日付を、なければ実際にPOSTしたrecordの確定日付を完了表示に使う。

重要な保護:

- LLM出力の未知キーは payload 作成時に破棄される。
- 家計簿の `amount` は `bool` を拒否し、正の数値だけ許可する。
- Biologは、記録対象値が1つもない payload を送信しない。
- `https://localhost` や `localhost.evil.com` のような送信先は拒否される。

## 8. VRAM / Audio Safety Components

VRAM安全フィルタの設計思想は「VRAMを管理する」ではなく「VRAMから逃げる」です。

確認できる主な構成:

- `ResourceMonitor`: 0.5秒周期で VRAM/GPU/CPU を観測。
- `VRAMGuard`: `vram_score` ベースで即時安全判定。
- `adjust_llm()`: CUDA対応、GGUFサイズ、metadataの総レイヤー数、実空き容量から、Full→約75%→50%→25%→CPUの適応型GPU offload候補を選ぶ。GPUロード後も最低予約量を再確認する。
- `adjust_inference()`: 推論直前の実空き容量と動的予約領域で`max_tokens`を段階削減し、hard limitでは再配置が必要なことを返す。 段階削減後の値は利用者設定値以下へクランプする。
- `ChatApp` / `Controller`: Autoのhard limit時は現在より低いFull→約75%→50%→25%→CPUの候補だけへhot reloadし、同じ入力を最大1回継続する。推論ごとの自動upshiftは行わない。
- `WhisperPool`: LLMロード後にCPU版smallをロードし、`whisper_mode`と空き容量に応じてGPU smallまたはGPU mediumもロードする。LLM hot reload中は新規転写を止め、進行中転写とlockで排他する。
- `WhisperController`: GPU版がロード済みの場合だけ、GPU使用率のヒステリシスで使用モデルを切り替える。

音声安全側の主な構成:

- `VoiceRecognizer` は TTS中の入力を抑制し、読み上げ音声をユーザー発話として拾うリスクを下げる。
- `WHISPER_NOISE` と `WHISPER_NOISE_PARTIAL` で動画系 hallucination や不要語を除外する。
- マイクや PyAudio が使えない場合は例外で落とさず、音声認識を無効化する。
- TTS は SAPI5 を非同期再生し、停止要求時は purge/stop 相当の処理を行う。

## 9. Operational Constraints

- 通常会話から新規チャット・別履歴・ゲストモードへ切り替える前に保存を試み、保存失敗時は新規チャット等への切替を中断して現在の未保存会話を維持する。

- Windows前提。Tkinter UI、Windows SAPI5、pywin32、ローカル音声デバイスに依存する。
- Mac/Linux は通常利用の対象外。
- LLM は llama.cpp / GGUF モデルを使用する。
- GPU利用時は NVIDIA/CUDA 環境が前提。CPU-only でも一部動作可能だが性能は落ちる。
- セットアップ後の通常チャットはローカル中心。ただし Whisper 初回モデル取得や任意のローカルAPI連携は別扱い。
- 家計簿/Biolog連携はローカルAPI前提。
- 設定・履歴・アバターと相対モデルパスはアプリ配置フォルダ基準で解決する。
- VRAM安全策は OOM を完全には防がない。目的はクラッシュ頻度の確率的低減。

## 10. Known Risks / Things To Preserve

Preserve:

- `ChatApp` の既存UI挙動と、DPAPI暗号化履歴のfail-closed動作。
- 停止ボタンまわりの `_llm_abort`、UIロック、TTS停止、LLMService abort の順序。
- TTS中のVAD抑制。ここを崩すと読み上げ音声を再入力する可能性がある。
- 家計簿/Biolog送信前のサニタイズ、ローカルURL制限、確認ダイアログ。
- 終了時は新規操作を拒否し、LLM・Voice・TTS・API処理を最大6秒監視してからTkを破棄する。
- `main()` 起動時に重い依存を生成する構造。import時に Tk ウィンドウや重いモデルロードが走らないこと。

Risks:

- 添付sidecarはアプリ自身では暗号化せず、ユーザー単位の保存領域分離もない。共有保存領域の一括削除は他利用者を含む全チャット添付へ影響し得る。
- `.shiro-export`はローカル添付メタデータと実体を含まず、添付バックアップにはならない。
- `ChatApp` はまだ大きく、UI状態とアプリ制御が密結合している。
- `Controller` は薄くなっているが、`app` への直接参照が多く、完全な独立サービスではない。
- `WhisperPool` の内部属性 `_ctrl` を `Controller` 側が参照しているため、境界がやや脆い。
- VRAM観測は TOCTOU があり、他プロセスや CUDA allocator の挙動までは制御できない。
- JSON抽出は正規表現ベースであり、LLM応答の形式崩れには限界がある。健康JSONが複数ある場合は厳格JSONとして有効な最後の候補を採るため「例示→本番」は緩和できるが、逆順を意味的に完全判定するものではない。
- 公開版baselineと作業中の差分を混同しない。次作業前には、公開リポジトリのGit状態と対象差分を改めて確認する。

## 11. Next Refactoring Notes

Fable 5 に渡す場合の推奨順序:

1. まずこのスナップショットと `CHANGELOG.md` の最新`1.7.2`を読み、`1.3.0`は責務分離の履歴として参照する。
2. 次に現行ファイルを実際に読み、存在しないモジュール名や古い責務名を前提にしない。
3. 最初の改修は小さく、`ChatApp` からさらに1責務だけ切り出す。
4. 外部連携、停止処理、TTS/VAD排他、VRAMガードは高リスク領域として扱う。
5. 保存形式、設定ファイル、ローカルAPI payload の互換性を維持する。
6. リファクタ後は `python -m py_compile` と import副作用確認を行う。

最初に安全そうな候補:

- `ChatApp` 内の設定ダイアログ/設定反映処理の整理。
- チャット履歴UIと `SessionStore` 呼び出し部分の境界整理。
- `WhisperPool._ctrl` 直接参照を避けるための公開メソッド追加。
- `Controller` が参照する `app` 属性を少しずつ明示依存へ置き換える。

避けたい初手:

- 停止処理の大幅変更。
- TTS/VAD/Whisper の同時リファクタ。
- 家計簿/Biolog連携のサニタイズ削除や送信条件変更。
- VRAM安全フィルタの閾値や挙動変更。
