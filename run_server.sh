#!/usr/bin/env bash
# macOS / Linux 실행: ./run_server.sh  (옵션은 serve.py 와 동일)
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt qrcode
fi
exec .venv/bin/python serve.py "$@"
