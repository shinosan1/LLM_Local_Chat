# LLM Local Chat Skills

このファイルは、LLM Local Chat を修正・リファクタリングするときに守るべき設計知識を記録する。

目的はコーディング規約ではなく、過去の不具合から得た「壊してはいけない挙動」を明文化すること。

---

## 1. Architecture Rules

- `LLM_Local_Chat.py` は起動、UI、依存接続の中心として扱う。
- import 時に重い処理を起動しない。
  - `Tk()` 生成
  - `ResourceMonitor()` 生成
  - `WhisperPool()` 生成
  - LLMロード
  - Whisperロード
- 依存生成は `app_composition.create_app_deps()` に集約する。
- `app_composition.py` / `integrations.py` は `LLM_Local_Chat.py` を import しない。
- 循環 import を作らない。

---

## 2. Threading Rules

- Tkinter widget は worker thread から直接更新しない。
- UI更新は必ず `root.after(...)` 経由にする。
- 以下のスレッドから直接UIを触らない。
  - LLM streaming thread
  - TTS thread
  - Whisper / VAD thread
  - API POST worker
  - summary worker
- worker thread から呼ぶ callback は、UI側で `root.after()` されているか確認する。

---

## 3. Stop Sequence Rules

停止処理は安全上重要。単純化しない。

維持する順序:

1. abort / thinking flags を設定する
2. 送信ボタンを一時的に無効化する
3. LLMService の streaming を abort する
4. TTS queue を停止・排出する
5. avatar speaking 状態を止める
6. 音声入力の TTS/VAD 状態を戻す
7. LLM thread の停止を待つ
8. UI状態を復帰する

禁止:

- streaming中に安易に `llm.reset()` する
- stop handler を統合・短縮する
- `_llm_abort`, `_is_thinking`, `_stream_buf` の更新順序を理由なく変える

---

## 4. TTS / VAD Interference Rules

TTS音声をマイクが再認識しないための処理は必須。

維持する挙動:

- TTS開始時に `_tts_active=True`
- TTS中はVAD録音バッファを蓄積しない
- 録音中にTTSが始まったら録音データを破棄する
- TTS終了後は `root.after(800, _restore_vad)` で戻す
- TTS queue が空のときだけ `_tts_active=False`
- マイク復帰時にバッファをフラッシュする
- 同一テキストの短時間連続認識を抑制する

これらを削ると、自己認識ループ、幻聴入力、無限発話再帰が起きる。

---

## 5. Streaming Rules

- `stream=True` の逐次表示を維持する。
- 応答全文を生成完了まで待ってから表示する形に変えない。
- token受信ごとのUI表示は `root.after()` 経由にする。
- abort時は履歴保存、TTS、後続処理が重複しないようにする。

---

## 6. Queue Safety Rules

- `queue.task_done()` を二重に呼ばない。
- queueを複数箇所から無秩序に空にしない。
- TTS queue の所有権は `TTSWorker` に寄せる。
- `stop_all()` の queue drain と `_stop_flag` 更新順序を安易に変えない。

---

## 7. VRAM / Resource Rules

- VRAM制御は `resource_monitor.py` 側に閉じ込める。
- UIからVRAM状態を直接判断しない。
- LLMロード時は `adjust_llm(res_monitor)` を使う。
- 推論直前の token 調整は `ResourceManager` / `adjust_inference()` 経由にする。
- WhisperのGPU/CPU切替は `WhisperPool.get_model(res_monitor)` に任せる。
- `ResourceMonitor`, `VRAMGuard`, `WhisperPool`, `SessionStore` は `create_app_deps()` で生成する。

---

## 8. Integration Safety Rules

家計簿/Biolog連携は外部送信を伴うため、安全ガードを弱めない。

維持する挙動:

- POST前に確認ダイアログを表示する
- ダイアログに表示するのはサニタイズ済みpayloadのみ
- 未サニタイズのLLM出力は表示・送信しない
- API URLは `http` の `localhost` / `127.0.0.1` / `::1` のみ許可
- `localhost.evil.com` や `https://localhost` は拒否
- `urlparse(url).port` の `ValueError` は送信拒否扱い
- 家計簿 `amount` は正の `int` / `float` のみ許可し、`bool` は拒否
- Biolog の LLM由来 `user_id` は破棄し、送信時に `self` を付与する
- HTTP POST は worker thread で実行し、UI更新は `root.after()` 経由

---

## 9. Session Rules

- `chat_logs/` のJSON形式を変えない。
  - `title`
  - `history`
  - `summary`
- guest mode では保存しない。
- 既存ログへの破壊的テストを行わない。
- `SessionStore` の `save`, `rename`, `delete` テストは一時ディレクトリで行う。
- `chat_settings.json` はユーザー環境設定なので不用意に上書きしない。

---

## 10. Refactoring Rules

- 挙動維持をアーキテクチャ上の美しさより優先する。
- 一度に複数の責務を移動しない。
- UI文言、ダイアログ文言、API payload、タイムアウト、例外処理は理由なく変えない。
- 「冗長に見える処理」は、過去の競合回避である可能性を疑う。
- 変更後は最低限以下を確認する。
  - `py_compile`
  - import副作用
  - 直接テスト
  - 可能なら手動起動

---

## 11. Forbidden Simplifications

以下は禁止。

- worker thread からTkinter widgetを直接更新する
- stop処理を短くまとめる
- TTS/VADの `_tts_active` 制御を削る
- queue drain処理を単純化する
- `messagebox.askyesno` をPOST worker内で呼ぶ
- `localhost` の文字列包含判定でURLを許可する
- `amount=True` を数値として扱う
- `LLM_Local_Chat.py` を `app_composition.py` や `integrations.py` から import する
- 実データ `chat_logs/` をテストで削除・上書きする
- D側 `.venv`, `models`, `chat_logs`, `chat_settings.json` を同期処理で上書き・削除する

---

## 12. Regression Checklist

リファクタ後に確認する項目。

- 通常チャット送信
- 停止後の再送信
- ストリーミング表示
- 履歴保存・読み込み・検索・名前変更・削除
- guest modeで保存されないこと
- 家計簿モードの確認ダイアログ
- Biologモードの確認ダイアログ
- API URLガード
- TTS ON/OFF
- マイクON/OFF
- TTS中にマイクが自己音声を拾わないこと
- importだけで重い初期化が走らないこと
