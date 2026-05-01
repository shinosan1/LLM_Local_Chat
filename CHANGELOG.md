# Changelog

本ファイルはLLM Local Chatの変更履歴を記録します。  
バージョン管理は [Semantic Versioning](https://semver.org/lang/ja/) に準拠します。

---
## [1.0.2] - 2026-05-02
### 修正
- 初期起動時にマイク・TTSがOFFになるように修正

## [1.0.1] - 2026-05-01

### 修正
- アバターパスのスラッシュ抜けを修正
- ローカルパスを相対パスに変更（ポータビリティ向上）
- マイクボタンの動作説明を修正（起動後自動ON・ボタンでOFF）
- 停止ボタンの説明を修正（タイミングによっては中断できない場合がある旨を追記）
- phi-4の動作確認済み表記をphi-3.5に修正
- README：Hugging Faceアカウント登録・利用規約同意が必要な場合がある旨を追記
- README：「導入責任者の方へ」セクションを追加（セキュリティ特性・推奨事項）
- 動作保証をWindows 11のみに変更（Windows 10を除外）
- 設定ダイアログに「起動時にマイクを有効にする」「起動時にTTSを有効にする」チェックボックスを追加
- マイク・TTSの起動時状態を設定ファイル（chat_settings.json）で永続化するよう修正
- VoiceRecognizer初期状態をOFFに変更・設定ファイルの値を優先して反映するよう修正
- README：セキュリティ項目にモデルによる外部通信の可能性がある旨の注意を追加
- README：diskcache（CVE-2025-69872）の脆弱性情報とpip-auditでの定期確認を推奨する旨を追加
- マイクON設定で起動した際にマイクが機能しないバグを修正（_on_whisper_ready内でmic_enabledを反映するよう変更）
- README：動作確認済みモデル一覧にgemma-4-E4B-Q4_K_M（自前量子化）・LFM2.5-1.2B-Instruct-Q4_K_Mを追加
- README：推奨モデル一覧を削除し動作確認済みモデルのみに整理
- マイクのデフォルトをONからOFFに変更（明示的にONにする形に変更）
- TTSのデフォルトをONからOFFに変更
- README：音声入力・TTSの説明をデフォルトOFFに合わせて修正

### 追加
- README：仮想環境の作成手順を追加
- README：Python・Git・CUDA Toolkitのインストール案内を追加
- README：Whisper初回ダウンロード（約1.5GB）の注意を追加
- README：Privateリポジトリでのclone方法の説明を追加
- README：日本語対応GGUFモデル一覧を追加
- README：モデルリンク切れ時の案内を追加
- README：モデル利用規約に関する免責事項を追加
- requirements.txtにsetuptools==78.1.1を追加
- chat_settings.json.exampleを追加
- .gitignoreを追加
- CHANGELOG.mdを追加
- 操作マニュアル（PDF）を追加

### 変更
- README：pip installコマンドに--no-cache-dirを追加
- README：ゲストモードの説明を正確な動作に修正
- README：「作者に直接お問い合わせ」をPAT発行案内に変更
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
