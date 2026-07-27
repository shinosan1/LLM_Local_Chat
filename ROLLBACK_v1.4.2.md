# v1.4.2 ロールバック手順

この文書は、v1.4.2適用前に作成した部分ソースバックアップから戻す場合の手順です。会話履歴を削除・上書きする手順ではありません。

## 重要事項

- バックアップ名に`v1.4.2`が含まれていても、内容はv1.4.2適用前の変更対象ファイルだけです。プロジェクト全体のバックアップではありません。
- `chat_logs/`、`models/`、`.venv/`、`chat_settings.json`は復元対象に含めません。
- 1件でもDPAPI暗号化した後は、v1.4.1以前のソースへ戻すだけでは履歴を読み込めません。
- 「テキストとして保存」は完全なセッションJSONではないため、旧形式へのデータ復元には使えません。暗号化後のデータ形式ロールバックは非対応です。

## 初回暗号化前のソース復元

1. Shiroと関連するPythonプロセスをすべて終了します。
2. 現在の変更対象ファイルについて、パス・サイズ・SHA-256を記録します。
3. まず`v1.4.2_recovery_hotfix_backup_20260727_01`の7ファイルを戻してコミット`aeac07d`相当へ戻し、その後、既存の`v1.4.2_source_backup_20260727_01`を使ってv1.4.2適用前へ戻します。各部分バックアップの一覧にあるファイルだけを元の相対パスへ戻し、プロジェクト全体の上書きや`git reset --hard`は使用しません。
4. v1.4.2と復旧ホットフィックスで新規追加された次の4ファイルは、導入前に存在しなかったことと、現在のSHA-256が成果物と一致することを確認してから削除します。
   - `history_crypto.py`
   - `tests/test_history_crypto.py`
   - `ROLLBACK_v1.4.2.md`
   - `tests/test_app_composition.py`
5. `chat_logs/`、`chat_settings.json`、`models/`、`.venv/`が変更されていないことを確認します。
6. 旧版のテスト手順で検証してから起動します。

## 初回暗号化後

旧ソースへ戻さないでください。v1.4.2以降のコードを維持し、元のWindowsユーザーと環境で履歴を利用してください。復号不能な履歴は削除せずバックアップを確保し、`README.md`の「起動できない場合の復旧手順」に従って手動退避してください。

この手順は平文の旧形式JSONを書き出す機能を提供しません。完全な旧形式への復号エクスポートが必要な場合は、別機能として設計・承認・実装する必要があります。

## 復旧ホットフィックス部分バックアップ

次の2フォルダはコミット`aeac07d`相当の同一7ファイルを保持しています。

- `D:\AI\LLM\LLM_Local_Chat_v1.4.2_recovery_hotfix_backup_20260727_01`
- `D:\AI\github_upload\LLM_Local_Chat_v1.4.2_recovery_hotfix_backup_20260727_01`

| 相対パス | SHA-256 |
|---|---|
| `CHANGELOG.md` | `6881cf61acaf75c933c62f983b69c34ecb2625569c4838cc3c23318b0ab68114` |
| `LLM_Local_Chat.py` | `832181593706e7778aeba2d5c63f5317828cf0a39658381a5d7a8a51e9da46bb` |
| `README.md` | `204764bb77ead438c4f7f202403da74093e634ebfd042d6ca435e86e9d8eb57e` |
| `app_composition.py` | `a637811b06ae7a904b234a81ebce7cbdf5a1751712874535b26afbfb65a75cd3` |
| `session_store.py` | `4a96c8cdc60d8157003f395b9518d01bc6b0b3ccaa54dfaee6fe5e48e5e8d4ea` |
| `tests/test_lifecycle.py` | `cbd459c8636865d3bb2922a0df27dc2f6f17e6123857beb9ea0531bbe6a1b50e` |
| `tests/test_session_store.py` | `c523a37581de745f2c6a3b47b6f3d341cbb94845afb07cfe1fdbe336eecc4c30` |
