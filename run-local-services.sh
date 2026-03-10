#!/bin/bash
killall -9 autossh
killall -9 ssh


# 启动Nginx AI API的对外服务
autossh -M 0 -R 0.0.0.0:11436:127.0.0.1:11436 -f -N -o "ServerAliveInterval 60" -o "ServerAliveCountMax 3" -i ~/.ssh/studio_02 -p 27959 zt@172.96.254.246

# 启动本地YIZHAO SSH Tunnel代理
autossh -M 0 -f -N -o "ServerAliveInterval 60" -o "ServerAliveCountMax 3" -i ~/.ssh/studio_02 -p 27959 -L :4001:127.0.0.1:8888 zt@172.96.254.246

echo "API Server & SSH tunnel created successfully."
