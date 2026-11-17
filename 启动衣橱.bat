@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
  echo 第一次运行，正在安装（需要几分钟，只做一次）...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\pip install -r requirements-desktop.txt
)
.venv\Scripts\python desktop.py
