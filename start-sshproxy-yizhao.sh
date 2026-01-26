#!/bin/bash
current_pid=$$

existing_pids=$(ps -ef | grep "/usr/bin/ssh" | grep "4001:127.0.0.1:8888" | grep -v grep | grep -v "$current_pid" | awk '{print $2}')

if [ -n "$existing_pids" ]; then
    echo "SSH tunnel with 4001:127.0.0.1:8888 already exists (PIDs: $existing_pids), skipping..."
    exit 0
fi

autossh -M 0 -f -N -o "ServerAliveInterval 60" -o "ServerAliveCountMax 3" -i ~/.ssh/studio_02 -p 27959 -L :4001:127.0.0.1:8888 zt@172.96.254.246

echo "SSH tunnel created successfully."
