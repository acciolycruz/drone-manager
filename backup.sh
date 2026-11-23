#!/bin/bash
# Backup do Drone Manager: logs de voo, banco de dados, configurações (sem segredos) e código
SRC="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SRC/.env" ]; then
  set -a
  source "$SRC/.env"
  set +a
fi
DEST="${BACKUP_DIR:-/mnt/e/Backup_DroneManager}"
NO_PAUSE=0
if [ "${1:-}" = "--no-pause" ]; then
  NO_PAUSE=1
fi
if ! mkdir -p "$DEST/logs" "$DEST/banco" "$DEST/app" 2>/dev/null; then
  echo "ERRO: o HD externo não está conectado ou o caminho está inacessível: $DEST"
  [ "$NO_PAUSE" -eq 0 ] && read -p "Pressione Enter para fechar"
  exit 1
fi
cp -ru "$SRC/data/logs/." "$DEST/logs/"
python3 - "$SRC/data/drone.db" "$DEST/banco/drone-$(date +%F).db" <<'PY'
import sqlite3, sys
a = sqlite3.connect(sys.argv[1]); b = sqlite3.connect(sys.argv[2])
a.backup(b); b.close(); a.close()
PY
ls -1t "$DEST"/banco/drone-*.db | tail -n +11 | xargs -d '\n' -r rm
cp -ru "$SRC/app.py" "$SRC/static" "$SRC/requirements.txt" "$SRC/README.md" "$SRC/GEMINI.md" "$SRC/start.sh" "$SRC/backup.sh" "$SRC/.env.example" "$SRC/.agent" "$DEST/app/"
echo "Logs: $(ls "$SRC/data/logs" | wc -l) na origem, $(ls "$DEST/logs" | wc -l) no backup"
echo "Banco: $(ls "$DEST/banco" | wc -l) cópia(s) guardada(s)"
echo "Backup concluído em: $DEST"
[ "$NO_PAUSE" -eq 0 ] && read -p "Pressione Enter para fechar"
exit 0
