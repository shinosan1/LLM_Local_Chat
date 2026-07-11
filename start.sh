#!/bin/bash
export DISPLAY=host.docker.internal:0.0
export GTK_IM_MODULE=fcitx
export QT_IM_MODULE=fcitx
export XMODIFIERS="@im=fcitx"
export DefaultIMModule=fcitx

# 既存の fcitx を停止してから再起動
pkill -x fcitx 2>/dev/null
sleep 0.3
fcitx -d 2>/dev/null
sleep 1

cd /workspace/app/LLM_Local_Chat
python LLM_Local_Chat.py
