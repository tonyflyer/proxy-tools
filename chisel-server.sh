#!/bin/bash
# Chisel Server - run on 172.22.164.60
# Listens on port 8080, enables reverse tunnelling with authentication
#
# Also runs udp-tcp-bridge.py to convert TCP 31116 back to UDP 21116
# (because chisel only tunnels TCP, but RustDesk needs UDP 21116)

CHISEL_BIN="$HOME/proxy-tools/chisel"
BRIDGE_SCRIPT="$HOME/proxy-tools/udp-tcp-bridge.py"
HOST=172.22.164.60
PORT=8080
AUTH="zt:Chisel@2026!"
TLS_CERT="$HOME/proxy-tools/chisel.crt"
TLS_KEY="$HOME/proxy-tools/chisel.key"

export no_proxy="${no_proxy:+${no_proxy},}172.22.164.60,127.0.0.1,localhost"
export NO_PROXY="$no_proxy"
LOG="$HOME/proxy-tools/chisel-server.log"
PID_FILE="$HOME/proxy-tools/chisel-server.pid"
BRIDGE_PID_FILE="$HOME/proxy-tools/udp-bridge-server.pid"

start_udp_bridge() {
  nohup python3 "$BRIDGE_SCRIPT" --mode server \
    --tcp-port 31116 --udp-target 127.0.0.1:21116 \
    >> "$LOG" 2>&1 &
  sleep 1
  if [ -f "$BRIDGE_PID_FILE" ]; then
    echo "  UDP bridge: TCP:31116 -> UDP:21116 (PID $(cat "$BRIDGE_PID_FILE"))"
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

case "${1:-start}" in
  start)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "chisel server already running (PID $(cat "$PID_FILE"))"
      exit 0
    fi
    nohup "$CHISEL_BIN" server --host "$HOST" --port "$PORT" --reverse --auth "$AUTH" \
      --tls-cert "$TLS_CERT" --tls-key "$TLS_KEY" \
      >> "$LOG" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 1
    if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "chisel server started on ${HOST}:${PORT} (PID $(cat "$PID_FILE"))"
      start_udp_bridge
    else
      echo "ERROR: chisel server failed to start. Check $LOG"
      exit 1
    fi
    ;;
  stop)
    stop_udp_bridge
    if [ -f "$PID_FILE" ]; then
      kill "$(cat "$PID_FILE")" 2>/dev/null
      rm -f "$PID_FILE"
      echo "chisel server stopped"
    else
      echo "chisel server not running"
    fi
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  status)
    if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      echo "chisel server running (PID $(cat "$PID_FILE"))"
      if [ -f "$BRIDGE_PID_FILE" ] && kill -0 "$(cat "$BRIDGE_PID_FILE")" 2>/dev/null; then
        echo "  UDP bridge: running (PID $(cat "$BRIDGE_PID_FILE"))"
      else
        echo "  UDP bridge: not running"
      fi
    else
      echo "chisel server not running"
      rm -f "$PID_FILE" 2>/dev/null
    fi
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status}"
    exit 1
    ;;
esac
