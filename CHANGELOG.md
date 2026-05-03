# Changelog

本ファイルはLLM Local Chatの変更履歴を記録します。  
バージョン管理は [Semantic Versioning](https://semver.org/lang/ja/) に準拠します。

---
## [1.0.5] - 2026-05-03
### 修正
- 停止ボタンが効かないのを修正
- requirements.txtの更新
    - pywin32==309　の追加
- TTSがOFFでも発話する不具合を修正
- 既知の不具合あり
    - TSWorker	起動時の挨拶がループ構造上、出ない場合がある_initial_greeting_done の判定を get の外側で行うか、初期値としてキューに入れる。
    - VoiceRecognizer	Bluetoothデバイス等の遅延でクラッシュする	stream.read を try-except で囲んでいるのは良いが、None が返った際の処理を追加。
    - ChatApp	_stop_all が重複定義されている	メソッドを1つに統合。
    - ChatApp	_on_tts_stop の 0.8秒待機がスレッドを乱発する	threading.Timer を使うか、TTSWorker 側で完了イベントを発火させる。
    - TTSWorker "システムを起動しました"が着火しない
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
- マイクON設定で起動した際にマイクが機能しないバグを修正（`_on_whisper_ready`内でmic_enabledを反映するよう変更）
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
