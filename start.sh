#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if ! command -v python3.12 >/dev/null; then
  echo 'Instale Python 3.12 e o módulo venv da sua distribuição.' >&2
  exit 1
fi
for tool in ffmpeg ffprobe; do
  if ! command -v "$tool" >/dev/null; then
    echo "$tool ausente. Instale o pacote ffmpeg da sua distribuição." >&2
    exit 1
  fi
done
filters=$(ffmpeg -hide_banner -filters)
encoders=$(ffmpeg -hide_banner -encoders)
if ! [[ "$filters" =~ [[:space:]]ass[[:space:]] && "$encoders" =~ [[:space:]]libx264[[:space:]] ]]; then
  echo 'Use uma distribuição do FFmpeg com libass e libx264.' >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  python3.12 -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
exec .venv/bin/python -m uvicorn cliplab.app:create_app --factory --host 127.0.0.1 --port 8765
