# 第三者ソフトウェアおよびライセンス情報

## 1. 確認方法と対象

本書は、`requirements.txt`、`requirements-cpu.txt`、製品コードのimport、ならびに開発正本の既存仮想環境にある配布メタデータを確認して作成した。新規パッケージのインストール、更新またはダウンロードは行っていない。

表のバージョンは、GPU向けrequirementsと確認時の開発仮想環境の情報である。CPU向けrequirementsでは、`llama-cpp-python==0.3.34`、`torch==2.6.0+cpu`、`torchvision==0.21.0+cpu`、`torchaudio==2.6.0+cpu` を指定する。配布時は、実際に同梱・使用する版とライセンス文書を改めて確認する必要がある。

本ソフトウェア本体のライセンスはルートの `LICENSE` にあるMIT Licenseであり、以下の第三者ソフトウェア、GGUFモデル、Whisperモデル、CUDA、Windowsコンポーネントまたは利用者が追加したソフトウェアの権利を許諾するものではない。

## 2. 直接依存パッケージ

| 名称 | 用途 | 確認版 | メタデータのライセンス表記 | 同梱ライセンス等の所在 |
|---|---|---:|---|---|
| llama-cpp-python | GGUF形式のローカルLLM実行 | 0.3.34 | MIT | `llama_cpp_python-0.3.34.dist-info/licenses/LICENSE.md` |
| torch | PyTorchテンソル処理・GPU/CPU実行 | 2.6.0+cu124 | BSD-3-Clause | `torch-2.6.0+cu124.dist-info/LICENSE`、`NOTICE` |
| torchvision | 画像処理・Vision関連 | 0.21.0+cu124 | BSD | `torchvision-0.21.0+cu124.dist-info/LICENSE` |
| torchaudio | 音声処理関連 | 2.6.0+cu124 | ライセンス名の記載なし | `torchaudio-2.6.0+cu124.dist-info/LICENSE` |
| Pillow | 添付画像の検証・処理 | 12.2.0 | MIT-CMU | `pillow-12.2.0.dist-info/licenses/LICENSE` |
| PyAudio | マイク入力 | 0.2.14 | MIT | `PyAudio-0.2.14.dist-info/LICENSE.txt` |
| numpy | 数値処理 | 2.4.3 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | `numpy-2.4.3.dist-info/licenses/LICENSE.txt` および同梱の個別LICENSE群 |
| openai-whisper | 音声認識モデルの読込み・推論 | 20250625 | MIT | `openai_whisper-20250625.dist-info/licenses/LICENSE` |
| pywin32 | Windows DPAPI、SAPI5等のWindows連携 | 309 | PSF | 配布メタデータの `License: PSF` 欄。確認時のdist-infoには個別ライセンスファイルを確認できなかった。 |
| nvidia-ml-py | GPUメモリ監視 | 13.610.43 | BSD | 配布メタデータの `License: BSD` 欄。確認時のdist-infoには個別ライセンスファイルを確認できなかった。 |
| cryptography | 持ち出し用暗号化形式等 | 49.0.0 | Apache-2.0 OR BSD-3-Clause | `cryptography-49.0.0.dist-info/licenses/LICENSE`、`LICENSE.APACHE`、`LICENSE.BSD` |

`llama-cpp-python`のネイティブ部分、PyTorch関連パッケージのバイナリ、CUDA関連要素には、配布物の構成に応じて追加のNOTICEや条件が含まれる場合がある。ここでは、確認できたPython配布メタデータを超えてライセンスを推測しない。実行ファイル化または依存パッケージの再配布を行う場合は、各配布物に含まれるLICENSE・NOTICE・依存関係を確認し、必要な文書を同梱すること。

## 3. 実行環境とモデル

| 要素 | 本プロジェクトでの位置付け | 確認できる情報 |
|---|---|---|
| CPython | アプリケーション実行環境 | 開発仮想環境はCPython 3.12.10を参照する。Pythonランタイムは本リポジトリに同梱されていないため、配布時は使用するランタイムのライセンス文書を確認すること。 |
| Windows / Win32 API | DPAPI、SAPI5、ウィンドウ・音声デバイス等のOS機能 | 本リポジトリはWindowsやSDKを再配布するものではない。利用条件は各提供元の条件に従う。 |
| GGUFモデル | 利用者が別途用意するローカルLLMモデル | モデルごとに配布元・版・利用条件が異なる。本リポジトリからモデルのライセンスを断定しない。 |
| Whisperモデル | `openai-whisper`が必要時に取得・利用するモデル | モデル重みの取得先・キャッシュ・利用条件はPythonパッケージ本体とは別に確認すること。 |
| CUDA / NVIDIAドライバー | GPU向けrequirementsで利用し得る実行環境 | 本リポジトリはこれらを同梱しない。利用・再配布時は提供元の条件に従う。 |

## 4. 配布時の注意

この文書はライセンス本文の代替ではない。依存パッケージ、Pythonランタイム、モデル、実行ファイル等を配布する場合は、実際の同梱物について必要なライセンス本文、NOTICE、著作権表示および配布条件を確認してください。`LICENSE`のMIT Licenseと第三者ソフトウェアのライセンスは、別々に適用されます。
