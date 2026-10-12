#!/bin/sh
# Daily backup of the database (consistent copy while the server runs), keeps 30 days inside the data volume.
# cron:  15 3 * * *  cd /opt/acbench/cloud_server && ./backup.sh
# Also copy the backups off this host, e.g.:  docker cp acbench:/data/backups ./backups-offsite
set -eu
docker compose exec -T acbench python server.py backup --to "/data/backups/acbench-$(date +%F).db"
docker compose exec -T acbench sh -c 'find /data/backups -name "acbench-*.db" -mtime +30 -delete'
