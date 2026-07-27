# Architecture Snapshot

## 1. Purpose

This document captures the current architecture of LLM Local Chat before the next refactoring/update pass.

目的は、後続作業者、特に Fable 5 が、現在の責務分離、実行時構成、データフロー、保守上の注意点を短時間で把握できるようにすることです。

このスナップショットはコード変更を目的としません。現行ファイル構成と主要クラス/関数を確認したうえで、確認できた内容だけを記録します。

## 2. Version Baseline

- Baseline: `CHANGELOG.md` の最新エントリ `1.3.0 - 2026-06-25`
- Snapshot target: 現在のワークツリー
- Main entrypoint: `LLM_Local_Chat.py`
- Runtime platform: Windows native desktop app

`git status` 上では未コミット変更と未追跡ファイルが多いため、この文書は「現在の作業ツリー」を基準にしています。過去リリースとの差分ではなく、次作業に渡すための現状整理です。

## 3. Recent Architectural Changes

`1.3.0` では、巨大化していた `LLM_Local_Chat.py` から主要責務が段階的に分離されています。

- `controller.py`: 送信、停止、LLM完了処理のオーケストレーション。
- `prompt_builder.py`: 通常会話、家計簿、健康記録のプロンプト生成と履歴予算管理。
- `llm_service.py`: llama.cpp のストリーミング推論実行。
- `resource_manager.py`: 推論直前の VRAM 状態に応じた `max_tokens` 決定。
- `session_store.py`: `chat_logs/` の保存、読み込み、一覧、検索、削除、名前変更。
- `audio_workers.py`: SAPI5 TTS と PyAudio/RMS-VAD/Whisper 音声認識。
- `app_composition.py`: 起動時依存生成。
- `integrations.py`: 家計簿/Biolog 連携のサニタイズ、ローカルURL制限、確認、POST処理。
- `json_extractors.py`: 家計簿/Biolog 向け LLM 応答 JSON 抽出。

また、Biolog/家計簿連携では、LLM出力をそのまま送信せず、送信前に payload をサニタイズし、ローカルAPI URLのみ許可し、確認ダイアログを経由する構造になっています。

## 4. Current Module Responsibilities

`LLM_Local_Chat.py`

- Tkinter UI、アプリ状態、設定、チャット表示、アバター表示の中心。
- `ChatApp` が依存オブジェクトを受け取り、UIイベントを `Controller` や各サービスへ接続する。
- `init_llm()` は `resource_monitor.adjust_llm()` を使って LLM ロード時の GPU レイヤー設定を調整する。
- `main()` が `create_app_deps(LOG_DIR)` を呼び、`ChatApp` を起動する。

`controller.py`

- `Controller` は薄いオーケストレーター。
- テキスト送信、音声入力、停止、LLM完了後の履歴更新、TTS、連携処理への分岐を管理する。
- プロンプト構築は `PromptBuilder`、推論は `LLMService`、VRAM判定は `ResourceManager` に委譲する。

`prompt_builder.py`

- system prompt、要約、履歴、ユーザー入力から llama.cpp 用 `messages` を組み立てる。
- `n_ctx`、`max_tokens`、履歴予算比率、token count cache を使い、古い履歴を予算内に収める。
- 家計簿モードと健康記録モードでは、LLMに JSON を出力させるための追加プロンプトを作る。

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

`session_store.py`

- セッションJSONの保存、読み込み、検索、削除、名前変更を担当する。
- UI状態や LLM 処理は持たない。

`app_composition.py`

- `create_app_deps(log_dir)` で `ResourceMonitor`、`VRAMGuard`、`WhisperPool`、`SessionStore` を作成する。
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

1. ユーザーがテキスト入力、または `VoiceRecognizer` が音声をテキスト化する。
2. `Controller.handle_text()` が入力を受け取り、現在モードを判定する。
3. `PromptBuilder` が `messages` を構築する。
4. `ResourceManager.decide()` が VRAM 状態から `max_tokens` と実行可否を決める。
5. `LLMService.generate()` がストリーミング推論を別スレッドで実行する。
6. token は `ChatApp._append_stream_token()` へ渡され、チャット欄に逐次表示される。
7. 完了後、`Controller._on_llm_done()` が履歴、要約更新、TTS、保存、連携処理を進める。
8. 性能ログの出力token数は生成本文をモデルtokenizerで数える。

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
7. ユーザー確認後、`user_id: "self"` を付与して `BIOLOG_API_URL` へ POST する。

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
- `adjust_llm()`: CUDA対応とGGUFサイズを含む実空き容量判定で、全層GPU要求またはCPUフォールバックを選ぶ。
- `adjust_inference()`: 推論直前の実空き容量と動的予約領域で`max_tokens`を段階削減し、危険時だけ推論をブロックする。
- `WhisperPool`: LLMロード後にCPU版smallをロードし、`whisper_mode`と空き容量に応じてGPU smallまたはGPU mediumもロードする。
- `WhisperController`: GPU版がロード済みの場合だけ、GPU使用率のヒステリシスで使用モデルを切り替える。

音声安全側の主な構成:

- `VoiceRecognizer` は TTS中の入力を抑制し、読み上げ音声をユーザー発話として拾うリスクを下げる。
- `WHISPER_NOISE` と `WHISPER_NOISE_PARTIAL` で動画系 hallucination や不要語を除外する。
- マイクや PyAudio が使えない場合は例外で落とさず、音声認識を無効化する。
- TTS は SAPI5 を非同期再生し、停止要求時は purge/stop 相当の処理を行う。

## 9. Operational Constraints

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

- `ChatApp` の既存UI挙動、保存形式、チャット履歴JSON形式。
- 停止ボタンまわりの `_llm_abort`、UIロック、TTS停止、LLMService abort の順序。
- TTS中のVAD抑制。ここを崩すと読み上げ音声を再入力する可能性がある。
- 家計簿/Biolog送信前のサニタイズ、ローカルURL制限、確認ダイアログ。
- 終了時は新規操作を拒否し、LLM・Voice・TTS・API処理を最大6秒監視してからTkを破棄する。
- `main()` 起動時に重い依存を生成する構造。import時に Tk ウィンドウや重いモデルロードが走らないこと。

Risks:

- `ChatApp` はまだ大きく、UI状態とアプリ制御が密結合している。
- `Controller` は薄くなっているが、`app` への直接参照が多く、完全な独立サービスではない。
- `WhisperPool` の内部属性 `_ctrl` を `Controller` 側が参照しているため、境界がやや脆い。
- VRAM観測は TOCTOU があり、他プロセスや CUDA allocator の挙動までは制御できない。
- JSON抽出は正規表現ベースであり、LLM応答の形式崩れには限界がある。健康JSONが複数ある場合は厳格JSONとして有効な最後の候補を採るため「例示→本番」は緩和できるが、逆順を意味的に完全判定するものではない。
- 現在のワークツリーは未コミット/未追跡ファイルが多く、次作業前に差分確認が必要。

## 11. Next Refactoring Notes

Fable 5 に渡す場合の推奨順序:

1. まずこのスナップショットと `CHANGELOG.md` の `1.3.0` を読む。
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
