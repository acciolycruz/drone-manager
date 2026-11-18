#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ -f .venv/bin/activate ]; then
  source .venv/bin/activate
fi
if [ -f "$HOME/.cargo/env" ]; then
  source "$HOME/.cargo/env"
fi

# Carrega variáveis de ambiente do arquivo .env (se existir)
if [ -f .env ]; then
  set -a
  source .env
  set +a
else
  echo "[AVISO] Arquivo .env não encontrado. Use 'cp .env.example .env' para configurar suas chaves."
fi

export TZ_OFFSET="${TZ_OFFSET:--3}"
export MEDIA_DIR="${MEDIA_DIR:-/mnt/e/DJI}"

# Verificação rápida de ferramentas CLI externas no PATH
for cmd in dji-log exiftool ffmpeg; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "[AVISO] Ferramenta CLI '$cmd' não encontrada no PATH. Algumas funções podem não operar."
  fi
done

exec uvicorn app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
