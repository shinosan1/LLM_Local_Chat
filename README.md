# LLM Local Chat

ローカルLLM（llama.cpp）＋ Whisper音声認識＋ Windows SAPIを組み合わせた日本語AIチャットアプリです。  
**セットアップ完了後は完全オフラインで動作します。会話内容が外部サーバーに送信されることはありません。**

機密情報を含む業務利用や、プライバシーを重視する用途に適しています。

![Python](https://img.shields.io/badge/Python-3.12.10-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)
![License](https://img.shields.io/badge/License-All%20Rights%20Reserved-red)

---

## 主な機能

- ストリーミング表示によるリアルタイム応答
- PyAudio + RMS-VAD + Whisper による常駐音声認識
- Windows SAPI（PowerShell経由）によるTTS読み上げ
- アバターウィンドウ（瞬き・口パクアニメーション）
- 会話の要約メモリ（長期会話対応）
- チャット履歴の保存・読み込み・検索
- ゲストモード（保存なし）
- ダークテーマUI

---

## 動作環境

| 項目 | 要件 |
|---|---|
| OS | Windows 10 / 11（64bit） |
| Python | 3.12.10（64bit） |
| GPU | NVIDIA GPU推奨（VRAM 6GB以上）／CPUのみでも動作可 |
| RAM | 8GB以上推奨 |

> **Mac・Linuxは非対応です。**  
> TTSにWindows SAPI（PowerShell）を使用しているため、Windows専用となっています。

---

## セットアップ

### 1. リポジトリをクローン

```bash
git clone https://github.com/shinosan1/tk_chat_local.git
cd tk_chat_local
```

### 2. 仮想環境の作成（推奨）

依存ライブラリの競合を避けるため、仮想環境の使用を強く推奨します。

```bash
python -m venv .venv
.venv\Scripts\activate
```

> 以降のインストールコマンドはすべて仮想環境を有効化した状態で実行してください。  
> プロンプトの先頭に `(.venv)` と表示されていれば有効化されています。

### 3. 依存ライブラリのインストール

**① まず torch（CUDA版）を先にインストールします：**

```bash
pip install torch==2.6.0+cu124 torchvision==0.21.0+cu124 torchaudio==2.6.0+cu124 --index-url https://download.pytorch.org/whl/cu124
```

> CPUのみの場合：
> ```bash
> pip install torch torchvision torchaudio
> ```

**② 次に llama-cpp-python（CUDA版）をインストールします：**

```bash
pip install llama-cpp-python==0.3.20 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124
```

> ※ `cu124` の部分はご自身のCUDAバージョンに合わせて変更してください（cu118、cu121等）。  
> CPUのみの場合：`pip install llama-cpp-python==0.3.20`

**③ 残りのライブラリをインストールします：**

```bash
pip install -r requirements.txt
```

> **PyAudioのインストールでエラーが出る場合：**
> ```bash
> pip install pipwin
> pipwin install pyaudio
> ```

### 4. GGUFモデルを用意する

[Hugging Face](https://huggingface.co/) から `.gguf` 形式のモデルをダウンロードして `models/` フォルダに配置してください。  
動作確認済み：`gemma-3-4b-it-q4_k_m.gguf`

### 5. アバター画像を用意する（任意）

以下の4枚のPNG画像を `avatars/` フォルダに配置してください：

| ファイル名 | 内容 |
|---|---|
| `default_avatar.png` | 通常状態 |
| `speaking_avatar.png` | 口を開けた状態 |
| `blink_avatar.png` | 目を閉じた状態 |
| `blink_speaking_avatar.png` | 目を閉じ口を開けた状態 |

画像がない場合は透明画像で代替されます（アバターなしで動作します）。

### 6. 設定ファイルを用意する

`chat_settings.json.example` を `chat_settings.json` にコピーして、`model_path` をご自身のGGUFモデルのパスに書き換えてください。

```bash
copy chat_settings.json.example chat_settings.json
```

その後、`chat_settings.json` の `model_path` を編集します：

```json
{
  "model_path": "C:\\Users\\yourname\\models\\gemma-3-4b-it-q4_k_m.gguf",
  ...
}
```

### 7. パスを設定する

`tk_chat_local.py` の冒頭にある以下の定数を**ご自身の環境に合わせて変更**してください：

```python
DEFAULT_MODEL_PATH = r"C:\path\to\your\model.gguf"
AVATAR_DEFAULT     = r"C:\path\to\avatars\default_avatar.png"
AVATAR_SPEAKING    = r"C:\path\to\avatars\speaking_avatar.png"
AVATAR_BLINK       = r"C:\path\to\avatars\blink_avatar.png"
AVATAR_BLINK_SPK   = r"C:\path\to\avatars\blink_speaking_avatar.png"
```

### 8. 起動

```bash
python tk_chat_local.py
```

> **⚠️ 起動時間について**  
> 起動後、LLMモデルの読み込みに**30秒〜数分**かかります（モデルサイズ・環境によります）。  
> ステータスバーに「⏳ モデル読込中…」と表示されている間はチャットできません。  
> また音声認識（Whisper）の初期化にも別途**数秒〜10秒程度**のタイムラグがあります。  
> マイクアイコンが有効になるまでしばらくお待ちください。

---

## 使い方

### 基本操作

起動するとメインウィンドウとアバターウィンドウが開きます。  
ステータスバーが「✅ 待機中」になったらチャット可能です。

テキスト入力欄にメッセージを入力して `Enter` キーまたは「送信」ボタンで送信します。  
`Shift + Enter` で改行できます。

### 音声入力

マイクボタンをクリックしてONにすると音声認識が有効になります。  
一定以上の音量を検知すると自動的に録音を開始し、Whisperで文字起こしします。  
認識結果は自動的に入力欄に送信されます。

> 音声認識の有効化後、数秒のタイムラグがあります。  
> TTS読み上げ中はハウリング防止のため音声入力が自動的に抑制されます。

### TTS（読み上げ）

AIの返答をWindows SAPIで自動読み上げします。  
メニューからTTSのON/OFFを切り替えられます。  
読み上げ中は「停止」ボタンで即時中断できます。

### ゲストモード

「ゲストモード: OFF」ボタンをクリックするとゲストモードに切り替わります。  
ゲストモード中の会話は**一切保存されません**。  
プライベートな会話や一時的な用途に使用してください。  
再度クリックすると通常モードに戻り、新しいセッションが開始されます。

### チャット履歴

左ペインに過去の会話一覧が表示されます。クリックで読み込めます。  
上部の検索欄でタイトル・要約の部分一致検索ができます。  
右クリックで個別削除が可能です。  
履歴は `chat_logs/` フォルダにJSON形式で保存されます。

### 要約メモリ

一定ターン数ごとに会話の要約が自動生成され、次の応答に注入されます。  
長期会話でもコンテキストが維持されやすくなっています。

### 設定

メニューから「設定」を開くと以下を変更できます：

| 項目 | 内容 |
|---|---|
| モデルファイル | GGUFモデルのパスを変更・参照 |
| コンテキスト長 | n_ctx（推奨: 8192） |
| 最大返答トークン数 | max_tokens（推奨: 256〜512） |
| 温度 | temperature（0.0〜2.0） |
| 音声検出感度 | VAD RMS閾値（小さいほど高感度） |

### アバター

アバターウィンドウはドラッグで自由に移動できます。  
右クリックで表示/非表示を切り替えられます。

---

## よくある質問

**Q. OllamaやLM Studioを使わないのはなぜですか？**  
A. llama.cppを直接Pythonバインディング（llama-cpp-python）で呼び出すことで、外部サービスへの依存をなくし、完全にオフラインで動作させています。

**Q. CPUだけでも動きますか？**  
A. 動作します。ただしモデルの応答速度が大幅に低下します。4Bクラスのモデルであれば実用範囲内です。

**Q. Whisperのモデルは自動でダウンロードされますか？**  
A. はい。初回起動時に `whisper.load_model("medium")` が自動的にダウンロードします（約1.5GB）。

**Q. TTSにpyttsx3ではなくPowerShellを使っているのはなぜですか？**  
A. pyttsx3はBluetooth出力デバイスとの相性問題があり、出力先を既定デバイスに固定できないケースがありました。PowerShell経由でWindows SAPIを直接呼び出すことでこの問題を解決しています。

---

## 免責事項

本アプリは個人利用・学習目的で開発したものです。  
動作保証・サポートは行っておりません。自己責任でご利用ください。

以下の行為を禁止します：

- リバースエンジニアリング・逆コンパイル・解析
- 本アプリを使用した商用サービスの構築・販売
- 著作権表示の削除・改ざん
