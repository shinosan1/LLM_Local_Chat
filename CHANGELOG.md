# Changelog

本ファイルはLLM Local Chatの変更履歴を記録します。  
バージョン管理は [Semantic Versioning](https://semver.org/lang/ja/) に準拠します。

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
