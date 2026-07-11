# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

あなたは丁寧で親しみやすい同僚です。日本語でお願いします。
各タスクの実行後、エラーや無限ループが発生していないか必ずログを確認し、もし異常があれば即座に停止して報告せよ。

---

## 起動・実行コマンド

```powershell
# 仮想環境を有効化してから起動
.venv\Scripts\activate
python LLM_Local_Chat.py

# Biolog API コンテナを起動（docker-compose.yml は親ディレクトリ）
cd ..\
docker-compose up -d biolog-api
curl http://localhost:8766/api/health/health   # ヘルスチェック

# 依存ライブラリのインストール（torch/llama-cpp-python は別途、requirements.txt 参照）
pip install -r requirements.txt --no-cache-dir
```

テストフレームワークは存在しない。動作確認はアプリを直接起動して手動で行う。

---

## アーキテクチャ概要

エントリーポイントは `LLM_Local_Chat.py` 1ファイルのみ。クラス構成:

| クラス | 役割 |
|---|---|
| `ChatApp` | メインコントローラー。Tkinter ウィンドウ、LLM 呼び出し、各モードの制御 |
| `TTSWorker` | バックグラウンドスレッドで SAPI5 (win32com) を叩く TTS キューワーカー |
| `VoiceRecognizer` | PyAudio + RMS-VAD + Whisper による常駐音声認識。`_tts_active` フラグでマイク排他制御 |
| `AvatarWindow` | 別 Toplevel ウィンドウで瞬き・口パクアニメーション |
| `SettingsDialog` | 設定ダイアログ（モデルパス・n_ctx・temperature・VAD 閾値） |

## スレッド設計（最重要）

```
メインスレッド (Tkinter mainloop)
  │  root.after() でのみ UI 更新
  ├─ LLM ワーカースレッド (_llm_worker)
  │    完了時: root.after(0, _on_llm_done)
  ├─ TTSWorker._play_loop (daemon)
  │    発話開始: root.after(0, on_start) → _on_tts_start → voice._tts_active = True
  │    発話終了: root.after(0, on_stop)  → _on_tts_stop  → root.after(800, _restore_vad)
  │                                                          → voice._tts_active = False
  ├─ VoiceRecognizer._loop (daemon)
  │    _tts_active=True の間はマイクを完全スキップ（ハウリング防止）
  └─ _send_to_biolog_api / _send_to_kakeibo_api (daemon, 都度生成)
```

**UI を更新するコードは必ず `root.after(0, ...)` 経由で実行すること。スレッドから直接 Tkinter を操作するとクラッシュする。**

## 主要フラグと排他制御

- `_is_thinking`: LLM 生成中。True の間は音声入力からの再送信をブロック
- `_llm_abort`: 停止ボタン押下フラグ。`_on_llm_done` で True なら TTS・保存をスキップ
- `voice._tts_active`: TTS 再生中にマイクチャンクを読み捨て。**このフラグは TTSWorker の on_start/on_stop コールバック以外から変更しないこと**（`_stop_all` のみ例外）

## 会話モード

`_on_llm_done` 内で以下の順に処理される:

1. 家計簿モード (`_kakeibo_mode`): `_extract_kakeibo_json(reply)` → `_send_to_kakeibo_api()`
2. 健康記録モード (`_health_mode`): `_extract_health_json(reply)` → TTS 後に `_send_to_biolog_api()`
3. `tts.speak(reply)` 呼び出し

LLM には「通常テキスト＋JSONブロック」を返させるプロンプト (`_health_build_prompt`, `_kakeibo_build_prompt`) を注入。抽出は正規表現で ` ```json ` ブロックをパース。

## Biolog API 連携（v1.1.0）

- エンドポイント: `POST http://localhost:8766/api/health/record`（`BIOLOG_URL` env var で変更可）
- スキーマ詳細は `skills.md` を必ず参照（フィールド範囲・必須条件・冪等性キー）
- `meal_detail` / `activity_log` はスキーマ外のため `memo` フィールドに文字列結合して送信
- 計測値（weight/body_fat/muscle_mass/bmr）が1つも取れない場合は API を呼ばない（422回避）

## 外部サービス構成

| サービス | ポート | docker-compose.yml サービス名 |
|---|---|---|
| Biolog API (FastAPI) | 8766 | `biolog-api` |
| 家計簿ブリッジ | 8765 | `kakeibo-bridge` |
| Biolog ダッシュボード (Streamlit) | 8501 | `biolog-streamlit` |
| SQLite DB | — | `D:\AI\kakeibo\kakeibo_app\kakeibo_project\kakeibo.db` |

## 設定ファイル

- `chat_settings.json` — 実行時設定（model_path, n_ctx, max_tokens, temperature, tts_enabled, mic_enabled, vad_threshold）
- `chat_settings.json.example` — テンプレート
- `chat_logs/` — セッション JSON の保存先

## 重要な制約

- **Windows 専用**: SAPI5 TTS は `win32com.client.Dispatch("SAPI.SpVoice")` を使用しており、Linux/Docker コンテナ内では動作不可
- **モデルファイル**: `models/` に GGUF 形式で配置。git 管理外（容量大）
- **Biolog API の WAL 禁止**: `PRAGMA journal_mode=WAL` は NTFS バインドマウント非互換。`DELETE` モードのみ使用（skills.md §絶対ルール 参照）
