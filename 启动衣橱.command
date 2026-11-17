#!/bin/bash
# macOS：双击打开。第一次会自动安装（需要几分钟，只做一次）。
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "第一次运行，正在安装……"
  python3 -m venv .venv && .venv/bin/pip install -r requirements-desktop.txt || { echo "安装失败，需要先装 Python 3.10 或更高版本"; read -p "按回车退出"; exit 1; }
fi
exec .venv/bin/python desktop.py
