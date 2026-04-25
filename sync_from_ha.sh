#!/bin/bash
# Sync latest files from HA config to project repo
# Run this after making changes in HA before committing

HA_CONFIG="/media/pi/NextCloud/homeassistant"
PROJECT="$(dirname "$0")"

sshpass -p "[YOUR_SSH_PASSWORD]" scp pi@[YOUR_HA_HOST]:"$HA_CONFIG/pyscript/ev_optimizer.py" \
    "$PROJECT/pyscript/ev_optimizer.py"
sshpass -p "[YOUR_SSH_PASSWORD]" scp pi@[YOUR_HA_HOST]:"$HA_CONFIG/pyscript/ev_control_loop.py" \
    "$PROJECT/pyscript/ev_control_loop.py"
sshpass -p "[YOUR_SSH_PASSWORD]" scp pi@[YOUR_HA_HOST]:"$HA_CONFIG/packages/ev_optimizer.yaml" \
    "$PROJECT/packages/ev_optimizer.yaml"
sshpass -p "[YOUR_SSH_PASSWORD]" scp pi@[YOUR_HA_HOST]:"$HA_CONFIG/pyscript/ev_schedule_data.json" \
    "$PROJECT/pyscript/ev_schedule_data.json"
sshpass -p "[YOUR_SSH_PASSWORD]" scp pi@[YOUR_HA_HOST]:"$HA_CONFIG/www/ev_schedule_grid.html" \
    "$PROJECT/www/ev_schedule_grid.html"

# Scrub token from HTML copy
sed -i 's/const HA_TOKEN = "[^"]*"/const HA_TOKEN = "PASTE_TOKEN_HERE"/' \
    "$PROJECT/www/ev_schedule_grid.html"

echo "Synced. Review changes with: git diff"
echo "Then commit with: git add . && git commit -m 'Update from HA'"
