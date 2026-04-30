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

> **⚠️ 事前準備①：Pythonのインストール**  
> **Python 3.12.10（64bit）** を必ずインストールしてください。  
> [https://www.python.org/downloads/release/python-31210/](https://www.python.org/downloads/release/python-31210/)  
>  
> インストーラー起動時に必ず **「Add Python to PATH」にチェック** を入れてください。  
> チェックを忘れると以降のコマンドがすべて動作しません。

> **⚠️ 事前準備②：Gitのインストール**  
> Gitが必要です。以下のURLからインストールしてください。  
> [https://git-scm.com/download/win](https://git-scm.com/download/win)  
>  
> インストール時はすべてデフォルト設定のままで問題ありません。

> **⚠️ 事前準備③：CUDA Toolkitのインストール（GPU使用の場合のみ）**  
> NVIDIA GPUでCUDAを使用する場合は、CUDA Toolkit 12.4をインストールしてください。  
> [https://developer.nvidia.com/cuda-12-4-0-download-archive](https://developer.nvidia.com/cuda-12-4-0-download-archive)  
>  
> CPUのみで使用する場合はインストール不要です。  
> インストール後は必ずPCを再起動してください。

### 1. リポジトリをクローン

```bash
git clone https://github.com/shinosan1/tk_chat_local.git
cd tk_chat_local
```

> **⚠️ Privateリポジトリの場合：**  
> git cloneの際にGitHubのユーザー名とパスワード（Personal Access Token）の入力を求められる場合があります。  
> その場合はGitHubにログインした状態でクローンするか、作者に直接お問い合わせください。

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
pip install -r requirements.txt --no-cache-dir
```

> **Permission Deniedエラーが出る場合：**  
> `--no-cache-dir`オプションを付けることで解決できます。管理者権限は不要です。

> **PyAudioのインストールでエラーが出る場合：**
> ```bash
> pip install pipwin
> pipwin install pyaudio
> ```

### 4. GGUFモデルを用意する

[Hugging Face](https://huggingface.co/) から `.gguf` 形式のモデルをダウンロードして `models/` フォルダに配置してください。  
動作確認済み：`gemma-3-4b-it-q4_k_m.gguf`

> **⚠️ 初回起動時の注意：**  
> 音声認識（Whisper）のモデルが初回起動時に自動ダウンロードされます（約1.5GB）。  
> ダウンロード完了まで時間がかかる場合があります。インターネット接続が必要です。

---

## 動作確認済み・推奨モデル一覧

本アプリで使用できる日本語対応GGUFモデルの一覧です。  
すべて [Hugging Face](https://huggingface.co/) からダウンロードできます。  
ファイルは `models/` フォルダに配置してください。

> **推奨量子化形式：** `Q4_K_M`（品質と速度のバランスが最良）

### 軽量モデル（4B以下・VRAM 4GB〜・低スペックPC向け）

| モデル名 | パラメータ | 特徴 | ダウンロード |
|---|---|---|---|
| gemma-3-4b-it | 4B | 動作確認済み。日本語品質良好 | [リンク](https://huggingface.co/lmstudio-community/gemma-3-4b-it-GGUF) |
| LFM2.5-1.2B-JP | 1.2B | 日本語特化の超軽量モデル | [リンク](https://huggingface.co/ggml-org/LFM2.5-1.2B-JP-GGUF) |
| llm-jp-3.1-1.8b-instruct4 | 1.8B | NII開発の日本語特化モデル | [リンク](https://huggingface.co/llm-jp/llm-jp-3.1-1.8b-instruct4) |

### 中量モデル（7B〜9B・VRAM 6GB〜・推奨）

| モデル名 | パラメータ | 特徴 | ダウンロード |
|---|---|---|---|
| phi-4 | 14B | 動作確認済み。高品質な応答 | [リンク](https://huggingface.co/lmstudio-community/phi-4-GGUF) |
| Qwen2.5-7B-Instruct | 7B | 多言語対応・日本語品質高い | [リンク](https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF) |
| japanese-stablelm-instruct-gamma-7B | 7B | 日本語特化のStableLM | [リンク](https://huggingface.co/TheBloke/japanese-stablelm-instruct-gamma-7B-GGUF) |
| NVIDIA-Nemotron-Nano-9B-v2-Japanese | 9B | NVIDIA製日本語特化モデル | [リンク](https://huggingface.co/mmnga/NVIDIA-Nemotron-Nano-9B-v2-Japanese-gguf) |

### 大型モデル（13B以上・VRAM 12GB〜・高品質）

| モデル名 | パラメータ | 特徴 | ダウンロード |
|---|---|---|---|
| Llama-3-ELYZA-JP-8B | 8B | ELYZA製日本語特化Llama3 | [リンク](https://huggingface.co/elyza/Llama-3-ELYZA-JP-8B-GGUF) |
| llm-jp-3.1-13b-instruct4 | 13B | NII開発・高品質日本語モデル | [リンク](https://huggingface.co/llm-jp/llm-jp-3.1-13b-instruct4) |

### モデル選択の目安

| VRAM | 推奨モデル |
|---|---|
| 4GB | LFM2.5-1.2B-JP / llm-jp-3.1-1.8b |
| 6GB | gemma-3-4b-it（動作確認済み） |
| 8GB | Qwen2.5-7B / japanese-stablelm-7B |
| 12GB以上 | phi-4 / llm-jp-3.1-13b |

> モデルの進化は速いため、最新情報は [Hugging Face](https://huggingface.co/models?library=gguf&language=ja) で「gguf japanese」で検索してください。  
> **リンク切れの場合はHugging Face（https://huggingface.co/）でモデル名を直接検索してください。**


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
  "model_path": "C:\\your\\path\\to\\models\\gemma-3-4b-it-q4_k_m.gguf",
  ...
}
```


### 7. 起動

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

起動して数秒後に音声認識が自動的に有効になります。  
一定以上の音量を検知すると自動的に録音を開始し、Whisperで文字起こしします。  
認識結果は自動的に入力欄に送信されます。  
マイクボタンをクリックすると音声認識をOFFにできます。

> 音声認識の有効化後、数秒のタイムラグがあります。  
> TTS読み上げ中はハウリング防止のため音声入力が自動的に抑制されます。

### TTS（読み上げ）

AIの返答をWindows SAPIで自動読み上げします。  
メニューからTTSのON/OFFを切り替えられます。  
読み上げ中は「停止」ボタンで即時中断できます。

### ゲストモード

「ゲストモード: OFF」ボタンをクリックするとゲストモードがONになります。  
ゲストモードがONのときの会話は**一切保存されません**。  
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

本アプリは個人が学習・研究目的で開発したものです。  
動作保証・サポートは一切行っておりません。利用により生じたいかなる損害についても作者は責任を負いません。

### 禁止事項

以下の行為を明示的に禁止します。また、以下に列挙されていない行為であっても、
本アプリの趣旨・精神に反すると作者が判断した場合は禁止事項とみなします：

- リバースエンジニアリング・逆コンパイル・解析・改ざん
- 本アプリおよびそのコードを使用した商用サービスの構築・販売・配布
- 本アプリを用いた違法行為・他者への迷惑行為
- 著作権表示・免責事項の削除または改ざん
- 本アプリの全部または一部を無断で複製・転載・再配布すること
- 本アプリを使用して生成したコンテンツを作者の許可なく商業利用すること
- その他、作者が不適切と判断する一切の行為

### 保証の否認

- 本アプリの動作・出力内容・AIの回答精度について一切保証しません
- 環境・設定・使用モデルにより動作結果が異なる場合があります
- 予告なく開発・メンテナンスを終了する場合があります
- 本アプリを利用したことによる直接的・間接的損害について作者は一切の責任を負いません

### 準拠法

本免責事項は日本法に準拠します。

