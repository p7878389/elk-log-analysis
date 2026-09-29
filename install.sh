#!/bin/sh
# macOS / Linux 安装入口：找到 Python 3.8+ 后运行 install.py，参数原样透传（./install.sh --help 查看选项）
DIR=$(cd "$(dirname "$0")" && pwd)
for py in python3 python; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>/dev/null; then
    exec "$py" "$DIR/install.py" "$@"
  fi
done
echo "未找到 Python 3.8+。macOS：xcode-select --install 或 brew install python；Linux：用包管理器安装 python3" >&2
exit 1
