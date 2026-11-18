#!/bin/bash
# Backup do Drone Manager: logs de voo, banco de dados, configurações (sem segredos) e código
DEST="/mnt/e/Backup_DroneManager"
SRC="$HOME/drone-manager"
mkdir -p "$DEST/logs" "$DEST/banco" "$DEST/app" || { echo "ERRO: o HD não está ligado ou o caminho está errado: $DEST"; read -p "Pressione Enter para fechar"; exit 1; }
cp -ru "$SRC/data/logs/." "$DEST/logs/"
python3 - "$SRC/data/drone.db" "$DEST/banco/drone-$(date +%F).db" <<'PY'
import sqlite3, sys
a = sqlite3.connect(sys.argv[1]); b = sqlite3.connect(sys.argv[2])
a.backup(b); b.close(); a.close()
PY
ls -1t "$DEST"/banco/drone-*.db | tail -n +11 | xargs -r rm
cp -ru "$SRC/app.py" "$SRC/static" "$SRC/requirements.txt" "$SRC/README.md" "$SRC/GEMINI.md" "$SRC/start.sh" "$SRC/backup.sh" "$SRC/.env.example" "$SRC/.agent" "$DEST/app/"
echo "Logs: $(ls "$SRC/data/logs" | wc -l) na origem, $(ls "$DEST/logs" | wc -l) no backup"
echo "Banco: $(ls "$DEST/banco" | wc -l) cópia(s) guardada(s)"
echo "Backup concluído em: $DEST"
read -p "Pressione Enter para fechar"
