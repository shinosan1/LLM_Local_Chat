@echo off
chcp 65001
cd /D "%~dp0"
rem 仮想環境名をご自身の環境に合わせて変更してください（例: .venv, venv, LLM）
call "%~dp0.venv\Scripts\activate.bat"
echo AI対話型アシスタントを起動しています。しばらくお待ちください。
python tk_chat_local.py
pause