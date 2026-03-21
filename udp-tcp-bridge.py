#!/usr/bin/env python3
"""
UDP <-> TCP Bridge for chisel tunnel.

Chisel only supports TCP. This script bridges UDP traffic through a TCP tunnel.

Mode A (client side): UDP listener → TCP forwarder
  Receives UDP on a local port, forwards each packet over TCP.

Mode B (server side): TCP listener → UDP forwarder
  Receives TCP connections, forwards data as UDP to a local port.

Usage:
  Client:  python3 udp-tcp-bridge.py --mode client --udp-port 21116 --tcp-port 31116
  Server:  python3 udp-tcp-bridge.py --mode server --tcp-port 31116 --udp-target 127.0.0.1:21116
"""

import argparse
import socket
import threading
import sys
import signal
import os

BUFFER_SIZE = 65535
running = True


def signal_handler(sig, frame):
    global running
    running = False
    print("\nShutting down...")
    sys.exit(0)


def client_mode(udp_port, tcp_host, tcp_port):
    """Receive UDP packets, forward over TCP to chisel tunnel."""
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    udp_sock.bind(("0.0.0.0", udp_port))
    udp_sock.settimeout(1.0)
    print(f"[client] UDP listening on :{udp_port} -> TCP {tcp_host}:{tcp_port}")

    while running:
        try:
            data, addr = udp_sock.recvfrom(BUFFER_SIZE)
            if data:
                try:
                    tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    tcp_sock.settimeout(5)
                    tcp_sock.connect((tcp_host, tcp_port))
                    # Send length-prefixed message
                    length = len(data).to_bytes(4, "big")
                    tcp_sock.sendall(length + data)
                    # Read response
                    resp_len_bytes = tcp_sock.recv(4)
                    if resp_len_bytes and len(resp_len_bytes) == 4:
                        resp_len = int.from_bytes(resp_len_bytes, "big")
                        if 0 < resp_len <= BUFFER_SIZE:
                            resp_data = b""
                            while len(resp_data) < resp_len:
                                chunk = tcp_sock.recv(resp_len - len(resp_data))
                                if not chunk:
                                    break
                                resp_data += chunk
                            if resp_data:
                                udp_sock.sendto(resp_data, addr)
                    tcp_sock.close()
                except Exception as e:
                    print(f"[client] TCP forward error: {e}")
        except socket.timeout:
            continue
        except Exception as e:
            if running:
                print(f"[client] UDP recv error: {e}")


def server_mode(tcp_port, udp_target_host, udp_target_port):
    """Receive TCP connections from chisel, forward as UDP."""
    tcp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    tcp_sock.bind(("0.0.0.0", tcp_port))
    tcp_sock.listen(32)
    tcp_sock.settimeout(1.0)
    print(f"[server] TCP listening on :{tcp_port} -> UDP {udp_target_host}:{udp_target_port}")

    def handle_connection(conn):
        try:
            conn.settimeout(5)
            # Read length-prefixed message
            len_bytes = conn.recv(4)
            if not len_bytes or len(len_bytes) < 4:
                conn.close()
                return
            msg_len = int.from_bytes(len_bytes, "big")
            if msg_len <= 0 or msg_len > BUFFER_SIZE:
                conn.close()
                return
            data = b""
            while len(data) < msg_len:
                chunk = conn.recv(msg_len - len(data))
                if not chunk:
                    break
                data += chunk

            # Forward as UDP
            udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp_sock.settimeout(3)
            udp_sock.sendto(data, (udp_target_host, udp_target_port))
            try:
                resp, _ = udp_sock.recvfrom(BUFFER_SIZE)
                if resp:
                    resp_len = len(resp).to_bytes(4, "big")
                    conn.sendall(resp_len + resp)
            except socket.timeout:
                pass
            udp_sock.close()
            conn.close()
        except Exception as e:
            print(f"[server] handle error: {e}")
            try:
                conn.close()
            except:
                pass

    while running:
        try:
            conn, addr = tcp_sock.accept()
            t = threading.Thread(target=handle_connection, args=(conn,), daemon=True)
            t.start()
        except socket.timeout:
            continue
        except Exception as e:
            if running:
                print(f"[server] accept error: {e}")


def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    parser = argparse.ArgumentParser(description="UDP <-> TCP bridge for chisel")
    parser.add_argument("--mode", required=True, choices=["client", "server"])
    parser.add_argument("--udp-port", type=int, default=21116, help="UDP port (client mode)")
    parser.add_argument("--tcp-port", type=int, default=31116, help="TCP port")
    parser.add_argument("--tcp-host", default="127.0.0.1", help="TCP target host (client mode)")
    parser.add_argument("--udp-target", default="127.0.0.1:21116", help="UDP target host:port (server mode)")
    args = parser.parse_args()

    # Write PID file
    pid_file = os.path.expanduser(f"~/proxy-tools/udp-bridge-{args.mode}.pid")
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    if args.mode == "client":
        client_mode(args.udp_port, args.tcp_host, args.tcp_port)
    else:
        host, port = args.udp_target.rsplit(":", 1)
        server_mode(args.tcp_port, host, int(port))


if __name__ == "__main__":
    main()
