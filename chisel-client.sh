#!/bin/bash
# Chisel client with pf-based RustDesk traffic redirect
# No lo0 alias needed - pf rules redirect traffic on VPN interface

SERVER_IP="172.22.164.60"
PROBE_PORT=11436
CHISEL_BIN="$HOME/proxy-tools/chisel"
BRIDGE_SCRIPT="$HOME/proxy-tools/udp-tcp-bridge.py"
CHISEL_SERVER="https://${SERVER_IP}:8080"
AUTH="zt:Chisel@2026!"
TLS_SKIP_VERIFY="--tls-skip-verify"
LOG="$HOME/proxy-tools/chisel-client.log"
PID_FILE="$HOME/proxy-tools/chisel-client.pid"
BRIDGE_PID_FILE="$HOME/proxy-tools/udp-bridge-client.pid"
PF_CONF="$HOME/proxy-tools/pf-rustdesk-redirect.conf"

export no_proxy="${no_proxy:+${no_proxy},}${SERVER_IP},127.0.0.1,localhost"
export NO_PROXY="$no_proxy"

TUNNELS=(
  "11436:localhost:11436"
  "60022:localhost:22"
  "21115:localhost:21115"
  "21116:localhost:21116"
  "21117:localhost:21117"
  "21118:localhost:21118"
  "21119:localhost:21119"
  "31116:localhost:31116"
)

# Reverse tunnels: expose local CodeForge to server 60
REVERSE_TUNNELS=(
  "R:18440:127.0.0.1:18440"
  "R:18441:127.0.0.1:18441"
)

needs_tunnel() {
  curl -sk --connect-timeout 3 --max-time 5 -o /dev/null "https://${SERVER_IP}:${PROBE_PORT}/" 2>/dev/null
  [ $? -ne 0 ]
}

PF_WATCHER_PID_FILE="$HOME/proxy-tools/pf-watcher.pid"
PF_WATCHER_INTERVAL=30  # 每 30 秒检查一次

PF_MERGED="/tmp/pf-merged-chisel.conf"

start_pf_rules() {
  if [ ! -f "$PF_CONF" ]; then return; fi
  # 合并系统规则 + 自定义规则，避免替换系统默认
  cat /etc/pf.conf "$PF_CONF" > "$PF_MERGED" 2>/dev/null
  sudo pfctl -ef "$PF_MERGED" 2>/dev/null
}

stop_pf_rules() {
  # 恢复系统默认规则
  sudo pfctl -f /etc/pf.conf 2>/dev/null
  rm -f "$PF_MERGED"
}

check_pf_loaded() {
  # 用 pfctl -sn 和 pfctl -sr 都检查，确保 rdr 和 pass 规则都在
  sudo pfctl -sn 2>/dev/null | grep -q "172.22.164.60" && return 0
  sudo pfctl -sr 2>/dev/null | grep -q "172.22.164.60" && return 0
  return 1
}

start_pf_watcher() {
  stop_pf_watcher  # 先清理旧的
  (
    while true; do
      sleep "$PF_WATCHER_INTERVAL"
      # chisel 已停止则退出 watcher
      if [ ! -f "$PID_FILE" ] || ! kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
        break
      fi
      # 检查 pf 规则是否还在
      if ! check_pf_loaded; then
        start_pf_rules
        echo "$(date '+%Y-%m-%d %H:%M:%S') [pf-watcher] rules reloaded" >> "$LOG"
      fi
    done
    rm -f "$PF_WATCHER_PID_FILE"
  ) &
  echo $! > "$PF_WATCHER_PID_FILE"
}

stop_pf_watcher() {
  if [ -f "$PF_WATCHER_PID_FILE" ]; then
    kill "$(cat "$PF_WATCHER_PID_FILE")" 2>/dev/null
    rm -f "$PF_WATCHER_PID_FILE"
  fi
}

start_udp_bridge() {
  nohup python3 "$BRIDGE_SCRIPT" --mode client \
    --udp-port 21116 --tcp-port 31116 \
    >> "$LOG" 2>&1 &
  sleep 1
  if [ -f "$BRIDGE_PID_FILE" ]; then
    echo "  UDP bridge: running (PID $(cat "$BRIDGE_PID_FILE"))"
  else
    echo "  WARNING: UDP bridge failed to start"
  fi
}

stop_udp_bridge() {
  if [ -f "$BRIDGE_PID_FILE" ]; then
    kill "$(cat "$BRIDGE_PID_FILE")" 2>/dev/null
    rm -f "$BRIDGE_PID_FILE"
  fi
}

do_start() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "chisel client already running (PID $(cat "$PID_FILE"))"
    return 0
  fi

  if ! needs_tunnel; then
    echo "Direct access to ${SERVER_IP}:${PROBE_PORT} OK — tunnel not needed"
    return 0
  fi

  echo "Cannot reach ${SERVER_IP}:${PROBE_PORT} directly — starting tunnel"

  > "$LOG"

  nohup "$CHISEL_BIN" client --auth "$AUTH" $TLS_SKIP_VERIFY "$CHISEL_SERVER" \
    "${TUNNELS[@]}" "${REVERSE_TUNNELS[@]}" \
    >> "$LOG" 2>&1 &
  echo $! > "$PID_FILE"

  echo -n "  Connecting"
  for _ in $(seq 1 30); do
    if grep -q "Connected" "$LOG" 2>/dev/null; then
      echo " OK"
      break
    fi
    echo -n "."
    sleep 1
  done

  if ! grep -q "Connected" "$LOG" 2>/dev/null; then
    echo " TIMEOUT"
    echo "ERROR: Check $LOG"
    tail -5 "$LOG"
    kill "$(cat "$PID_FILE")" 2>/dev/null
    rm -f "$PID_FILE"
    return 1
  fi

  echo "  Tunnels:"
  for t in "${TUNNELS[@]}"; do
    echo "    localhost:${t%%:*} -> ${t#*:}"
  done
  for t in "${REVERSE_TUNNELS[@]}"; do
    echo "    ${t}"
  done

  start_udp_bridge
  start_pf_rules
  start_pf_watcher
  echo "  pf rules: loaded from $PF_CONF (watcher every ${PF_WATCHER_INTERVAL}s)"
}

do_stop() {
  stop_pf_watcher
  stop_pf_rules
  stop_udp_bridge
  if [ -f "$PID_FILE" ]; then
    kill "$(cat "$PID_FILE")" 2>/dev/null
    rm -f "$PID_FILE"
    echo "chisel client stopped"
  else
    echo "chisel client not running"
  fi
}

do_status() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    echo "chisel client running (PID $(cat "$PID_FILE"))"
    echo "Tunnels:"
    for t in "${TUNNELS[@]}"; do
      echo "  localhost:${t%%:*} -> ${t#*:}"
    done
    for t in "${REVERSE_TUNNELS[@]}"; do
      echo "  ${t}"
    done
    if [ -f "$BRIDGE_PID_FILE" ] && kill -0 "$(cat "$BRIDGE_PID_FILE")" 2>/dev/null; then
      echo "UDP bridge: running (PID $(cat "$BRIDGE_PID_FILE"))"
    else
      echo "UDP bridge: not running"
    fi
    if check_pf_loaded; then
      echo "pf rules: active (RustDesk redirect)"
    else
      echo "pf rules: NOT loaded"
    fi
    if [ -f "$PF_WATCHER_PID_FILE" ] && kill -0 "$(cat "$PF_WATCHER_PID_FILE")" 2>/dev/null; then
      echo "pf watcher: running (PID $(cat "$PF_WATCHER_PID_FILE"), every ${PF_WATCHER_INTERVAL}s)"
    else
      echo "pf watcher: not running"
    fi
  else
    rm -f "$PID_FILE" 2>/dev/null
    if ! needs_tunnel; then
      echo "chisel client not running (direct access OK — not needed)"
    else
      echo "chisel client not running (tunnel NEEDED but not started)"
    fi
  fi
}

case "${1:-start}" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; sleep 1; do_start ;;
  status)  do_status ;;
  *)       echo "Usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac
