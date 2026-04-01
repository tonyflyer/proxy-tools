#!/usr/bin/env python3
"""
SSH Tunnel Manager - 管理多服务器 SSH 反向隧道和本地转发

支持在单个配置文件中定义多个 SSH 服务器及其隧道，也支持加载多个配置文件。

使用方法：
    python3 tunnel_manager.py --status                  # 所有隧道状态
    python3 tunnel_manager.py --status --server devops   # 只看 devops 服务器
    python3 tunnel_manager.py --restart                  # 重启所有
    python3 tunnel_manager.py --restart --server devops   # 只重启 devops
    python3 tunnel_manager.py --foreground               # 前台守护所有
    python3 tunnel_manager.py --config a.yaml b.yaml     # 加载多个配置
    python3 tunnel_manager.py --stop                     # 停止所有

配置格式 (新版 — 多服务器):
    servers:
      yizhao:
        ssh_server: 172.96.254.246
        ssh_port: 27959
        ssh_user: zt
        ssh_key: ~/.ssh/id_ed25519_m4_to_yizhao
        vpn_interface: utun4
        autossh_poll: 10
      devops:
        ssh_server: 172.22.164.60
        ssh_port: 22
        ssh_user: zhangtony
        ssh_key: ~/.ssh/id_ed25519_m4_to_yizhao

    tunnels:
      - name: ai-api
        server: yizhao
        type: remote
        local_port: 11436
        remote_host: 127.0.0.1
        remote_port: 11436

    settings:
      check_interval: 30
      autossh_gatetime: 0

配置格式 (旧版 — 单服务器, 完全兼容):
    tunnels:
      - name: ai-api
        type: remote
        ...
    settings:
      ssh_server: 172.96.254.246
      ...
"""

import argparse
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import yaml


@dataclass
class ServerConfig:
    name: str
    ssh_server: str
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_key: str = "~/.ssh/id_ed25519"
    bind_address: str = ""
    vpn_interface: str = ""  # auto-detect if empty, e.g. "utun4"
    autossh_poll: int = 30

    def resolved_key(self) -> str:
        return os.path.expanduser(self.ssh_key)


@dataclass
class Tunnel:
    name: str
    tunnel_type: str  # 'remote' or 'local'
    server_name: str = ""
    local_port: int = 0
    remote_host: str = ""
    remote_port: int = 0
    bind_address: str = ""
    bind_port: int = 0
    description: str = ""
    pid: Optional[int] = None
    start_time: Optional[float] = None
    fail_count: int = 0


class TunnelManager:
    def __init__(self, config_paths: List[str], server_filter: Optional[str] = None):
        self.config_paths = config_paths
        self.server_filter = server_filter
        self.servers: Dict[str, ServerConfig] = {}
        self.tunnels: Dict[str, Tunnel] = {}
        self.global_settings: Dict = {}
        self.running = False
        self.log_file = Path(__file__).parent / "tunnel_manager.log"

        self._setup_logging()
        self._load_all_configs()

    def _setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[logging.FileHandler(self.log_file), logging.StreamHandler(sys.stdout)],
        )
        self.logger = logging.getLogger(__name__)

    # =========================================================================
    # Config Loading — supports both legacy (single server) and new (multi)
    # =========================================================================

    def _load_all_configs(self):
        for path in self.config_paths:
            self._load_config(path)

        if self.server_filter:
            filtered = {k: v for k, v in self.tunnels.items() if v.server_name == self.server_filter}
            skipped = len(self.tunnels) - len(filtered)
            self.tunnels = filtered
            if skipped:
                self.logger.info(f"过滤服务器 '{self.server_filter}': 保留 {len(filtered)} 个隧道, 跳过 {skipped} 个")

        self.logger.info(f"已加载 {len(self.servers)} 个服务器, {len(self.tunnels)} 个隧道")

    def _load_config(self, config_path: str):
        config_file = Path(config_path)
        if not config_file.exists():
            self.logger.error(f"配置文件不存在: {config_path}")
            sys.exit(1)

        with open(config_file, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

        settings = config.get("settings", {})
        for k, v in settings.items():
            if k not in self.global_settings:
                self.global_settings[k] = v

        servers_cfg = config.get("servers", {})
        if servers_cfg:
            for srv_name, srv_data in servers_cfg.items():
                self.servers[srv_name] = ServerConfig(
                    name=srv_name,
                    ssh_server=srv_data.get("ssh_server", ""),
                    ssh_port=int(srv_data.get("ssh_port", 22)),
                    ssh_user=srv_data.get("ssh_user", ""),
                    ssh_key=srv_data.get("ssh_key", "~/.ssh/id_ed25519"),
                    bind_address=srv_data.get("bind_address", ""),
                    vpn_interface=srv_data.get("vpn_interface", ""),
                    autossh_poll=int(srv_data.get("autossh_poll", settings.get("autossh_poll", 30))),
                )
        else:
            default_name = self._infer_server_name(config_path, settings)
            if default_name not in self.servers:
                self.servers[default_name] = ServerConfig(
                    name=default_name,
                    ssh_server=settings.get("ssh_server", ""),
                    ssh_port=int(settings.get("ssh_port", 22)),
                    ssh_user=settings.get("ssh_user", ""),
                    ssh_key=settings.get("ssh_key", "~/.ssh/id_ed25519"),
                    bind_address=settings.get("bind_address", ""),
                    vpn_interface=settings.get("vpn_interface", ""),
                    autossh_poll=int(settings.get("autossh_poll", 30)),
                )

        for t in config.get("tunnels", []):
            tunnel_server = t.get("server", "")
            if not tunnel_server:
                if servers_cfg:
                    tunnel_server = list(servers_cfg.keys())[0]
                else:
                    tunnel_server = self._infer_server_name(config_path, settings)

            tunnel = Tunnel(
                name=t["name"],
                tunnel_type=t.get("type", "remote"),
                server_name=tunnel_server,
                local_port=t.get("local_port", 0),
                remote_host=t.get("remote_host", ""),
                remote_port=t.get("remote_port", 0),
                bind_address=t.get("bind_address", ""),
                bind_port=t.get("bind_port", 0),
                description=t.get("description", ""),
            )
            self.tunnels[tunnel.name] = tunnel

    def _infer_server_name(self, config_path: str, settings: dict) -> str:
        stem = Path(config_path).stem.replace(".yaml", "").replace(".yml", "")
        if stem == "tunnels":
            host = settings.get("ssh_server", "default")
            return host.replace(".", "-")
        return stem

    def _get_server(self, tunnel: Tunnel) -> ServerConfig:
        srv = self.servers.get(tunnel.server_name)
        if not srv:
            self.logger.error(f"隧道 {tunnel.name} 引用了不存在的服务器 '{tunnel.server_name}'")
            self.logger.error(f"  可用服务器: {list(self.servers.keys())}")
            sys.exit(1)
        return srv

    # =========================================================================
    # VPN route management
    # =========================================================================

    def _detect_vpn_interface(self) -> Optional[str]:
        try:
            result = subprocess.run(
                ["ifconfig"], capture_output=True, text=True, timeout=5
            )
            for block in result.stdout.split("\n\n"):
                m = re.match(r"(\S+):", block)
                if m:
                    iface = m.group(1)
                    if iface.startswith("utun") and re.search(r"inet\s+[\d.]+", block):
                        return iface
        except Exception:
            pass
        return None

    def _check_reachability(self, host: str, port: int, timeout: float = 3.0) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            return True
        except Exception:
            return False
        finally:
            sock.close()

    def _ensure_vpn_route(self, srv: ServerConfig) -> bool:
        if not self._check_reachability(srv.ssh_server, srv.ssh_port):
            vpn_iface = srv.vpn_interface
            if not vpn_iface:
                vpn_iface = self._detect_vpn_interface()

            if not vpn_iface:
                self.logger.warning(
                    f"服务器 {srv.name} ({srv.ssh_server}) 不可达，且未检测到 VPN 接口，跳过路由设置"
                )
                return False

            self.logger.info(f"检测到 VPN 接口 {vpn_iface}，尝试添加路由: {srv.ssh_server} → {vpn_iface}")
            try:
                subprocess.run(
                    ["route", "-n", "add", srv.ssh_server, "-interface", vpn_iface],
                    capture_output=True, text=True, timeout=5
                )
                time.sleep(1)
                if self._check_reachability(srv.ssh_server, srv.ssh_port, timeout=5):
                    self.logger.info(f"路由添加成功，{srv.ssh_server} 现已可达")
                    return True
                else:
                    self.logger.warning(f"路由添加完成但 {srv.ssh_server} 仍不可达")
                    return False
            except Exception as e:
                self.logger.error(f"添加 VPN 路由失败: {e}")
                return False
        return True

    # =========================================================================
    # autossh command building
    # =========================================================================

    def _build_autossh_cmd(self, tunnel: Tunnel) -> List[str]:
        srv = self._get_server(tunnel)
        poll = srv.autossh_poll

        if tunnel.tunnel_type == "remote":
            forward_arg = f"-R 127.0.0.1:{tunnel.local_port}:{tunnel.remote_host}:{tunnel.remote_port}"
        else:
            forward_arg = f"-L {tunnel.bind_address}:{tunnel.bind_port}:{tunnel.remote_host}:{tunnel.remote_port}"

        cmd = [
            "autossh",
            "-M",
            "0",
            forward_arg,
            "-f",
            "-N",
            "-o",
            "TCPKeepAlive=yes",
            "-o",
            f"ServerAliveInterval={poll}",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "StrictHostKeyChecking=no",
            "-i",
            srv.resolved_key(),
            "-p",
            str(srv.ssh_port),
            f"{srv.ssh_user}@{srv.ssh_server}",
        ]

        if srv.bind_address:
            cmd.insert(1, "-b")
            cmd.insert(2, srv.bind_address)

        return cmd

    # =========================================================================
    # Process management
    # =========================================================================

    def _get_process_identifier(self, tunnel: Tunnel) -> str:
        if tunnel.tunnel_type == "remote":
            return str(tunnel.local_port)
        else:
            return f"{tunnel.bind_address}:{tunnel.bind_port}"

    def _cleanup_tunnel_process(self, tunnel: Tunnel) -> None:
        identifier = self._get_process_identifier(tunnel)
        srv = self._get_server(tunnel)
        pattern = f"{identifier}.*{srv.ssh_server}"

        for proc_type in ["autossh", "ssh"]:
            try:
                result = subprocess.run(
                    ["pgrep", "-f", f"{proc_type}.*{pattern}"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0 and result.stdout.strip():
                    for pid in result.stdout.strip().split("\n"):
                        if pid:
                            try:
                                # 先 SIGTERM，再 SIGKILL
                                subprocess.run(["kill", pid], check=False)
                                time.sleep(1)
                                subprocess.run(["kill", "-9", pid], check=False)
                                self.logger.info(f"清理残余进程: {proc_type} (PID: {pid}) for {tunnel.name}")
                            except subprocess.CalledProcessError:
                                pass
            except Exception as e:
                self.logger.error(f"清理 {proc_type} 进程失败: {e}")

    def _find_autossh_pid(self, tunnel: Tunnel) -> Optional[int]:
        identifier = self._get_process_identifier(tunnel)
        srv = self._get_server(tunnel)
        pattern = f"autossh.*{identifier}.*{srv.ssh_server}"
        try:
            result = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
            if result.returncode == 0:
                pids = result.stdout.strip().split("\n")
                if pids and pids[0]:
                    return int(pids[0])
        except Exception as e:
            self.logger.error(f"查找进程失败: {e}")
        return None

    # =========================================================================
    # Health checks
    # =========================================================================

    def _check_tunnel_port(self, tunnel: Tunnel) -> Optional[bool]:
        """返回 True=正常, False=异常, None=未知(超时等)"""
        if tunnel.tunnel_type == "remote":
            return self._check_remote_port(tunnel)
        else:
            return self._check_local_proxy(tunnel.bind_address, tunnel.bind_port)

    def _check_remote_port(self, tunnel: Tunnel) -> Optional[bool]:
        """返回 True=端口在监听, False=端口未监听, None=检查超时(不确定)"""
        srv = self._get_server(tunnel)
        port = tunnel.local_port
        check_cmd = f"ss -tlnp 2>/dev/null | grep -q :{port} || lsof -i :{port} 2>/dev/null | grep -q LISTEN"

        try:
            result = subprocess.run(
                [
                    "ssh",
                    "-i",
                    srv.resolved_key(),
                    "-p",
                    str(srv.ssh_port),
                    "-o",
                    "ConnectTimeout=5",
                    "-o",
                    "StrictHostKeyChecking=no",
                    f"{srv.ssh_user}@{srv.ssh_server}",
                    check_cmd,
                ],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            self.logger.warning(f"检查远程端口 {port}@{srv.name} 超时 (视为未知)")
            return None
        except Exception as e:
            self.logger.error(f"检查远程端口失败: {e}")
            return None

    def _check_local_proxy(self, bind_address: str, bind_port: int) -> bool:
        import socket

        check_addr = bind_address if bind_address != "0.0.0.0" else "127.0.0.1"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        try:
            result = sock.connect_ex((check_addr, bind_port))
            return result == 0
        except Exception:
            return False
        finally:
            sock.close()

    # =========================================================================
    # Start / Stop
    # =========================================================================

    def start_tunnel(self, tunnel: Tunnel) -> bool:
        srv = self._get_server(tunnel)
        self._ensure_vpn_route(srv)

        self._cleanup_tunnel_process(tunnel)
        time.sleep(1)

        pid = self._find_autossh_pid(tunnel)
        if pid:
            self.logger.info(f"隧道 {tunnel.name} 已在运行 (PID: {pid})")
            tunnel.pid = pid
            return True

        cmd = self._build_autossh_cmd(tunnel)
        self.logger.info(f"启动隧道 {tunnel.name} → {self._get_server(tunnel).ssh_server}...")

        env = os.environ.copy()
        env["AUTOSSH_GATETIME"] = str(self.global_settings.get("autossh_gatetime", 0))
        env["AUTOSSH_POLL"] = str(self._get_server(tunnel).autossh_poll)

        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                for attempt in range(12):
                    time.sleep(1)
                    tunnel.pid = self._find_autossh_pid(tunnel)
                    if tunnel.pid and self._check_tunnel_port(tunnel):
                        tunnel.start_time = time.time()
                        self.logger.info(f"隧道 {tunnel.name} 启动成功 (PID: {tunnel.pid}, {attempt + 1}s)")
                        return True

                self.logger.error(f"隧道 {tunnel.name} 启动失败: 端口未就绪")
                return False
            else:
                self.logger.error(f"隧道 {tunnel.name} 启动失败: {result.stderr}")
                return False
        except Exception as e:
            self.logger.error(f"启动隧道 {tunnel.name} 异常: {e}")
            return False

    def stop_tunnel(self, tunnel: Tunnel) -> bool:
        pid = self._find_autossh_pid(tunnel)
        if not pid:
            self.logger.info(f"隧道 {tunnel.name} 未运行")
            return True

        try:
            # 先 SIGTERM 优雅退出，让 SSH 正常关闭远程端口
            subprocess.run(["kill", str(pid)], check=False)
            for _ in range(5):
                time.sleep(1)
                if not self._find_autossh_pid(tunnel):
                    break
            else:
                # 5 秒后仍未退出，强制 SIGKILL
                subprocess.run(["kill", "-9", str(pid)], check=False)
                time.sleep(1)
            self._cleanup_tunnel_process(tunnel)
            tunnel.pid = None
            tunnel.fail_count = 0
            self.logger.info(f"隧道 {tunnel.name} 已停止")
            return True
        except Exception as e:
            self.logger.error(f"停止隧道 {tunnel.name} 失败: {e}")
            return False

    def start_all(self):
        self.logger.info("=" * 60)
        self.logger.info("启动所有隧道...")
        self.logger.info("=" * 60)
        success = 0
        for tunnel in self.tunnels.values():
            if self.start_tunnel(tunnel):
                success += 1
        self.logger.info(f"启动完成: {success}/{len(self.tunnels)}")

    def stop_all(self):
        self.logger.info("停止所有隧道...")
        for tunnel in self.tunnels.values():
            self.stop_tunnel(tunnel)

    # =========================================================================
    # Health check loop
    # =========================================================================

    def check_health(self) -> Dict[str, bool]:
        status = {}
        grace_period = 30
        max_fail_count = 3  # 连续失败多少次才触发重启

        for tunnel in self.tunnels.values():
            pid = self._find_autossh_pid(tunnel)
            process_ok = pid is not None
            port_result = self._check_tunnel_port(tunnel)  # True/False/None

            # 进程不存在 → 一定不健康
            if not process_ok:
                is_healthy = False
                tunnel.fail_count += 1
            elif port_result is None:
                # 端口检查超时/未知 → 进程还在，不算失败，保持当前状态
                is_healthy = True
                # 不重置 fail_count，也不增加，让它自然衰减
            elif port_result:
                # 进程在 + 端口正常
                is_healthy = True
                tunnel.fail_count = 0
            else:
                # 进程在但端口确认不通
                is_healthy = False
                tunnel.fail_count += 1

            # Grace period: 刚启动的隧道给宽限
            if tunnel.start_time is not None:
                elapsed = time.time() - tunnel.start_time
                if not is_healthy and elapsed < grace_period and process_ok:
                    is_healthy = True
                    tunnel.fail_count = 0

            # 连续失败次数不够，暂不触发重启
            if not is_healthy and tunnel.fail_count < max_fail_count:
                srv = self._get_server(tunnel)
                self.logger.warning(
                    f"隧道 {tunnel.name}@{srv.name} 检查异常 ({tunnel.fail_count}/{max_fail_count}): "
                    f"进程={'✓' if process_ok else '✗'}, 连接={self._port_status_str(port_result)}"
                )
                is_healthy = True  # 还没达到阈值，暂不重启

            if not is_healthy:
                srv = self._get_server(tunnel)
                self.logger.warning(
                    f"隧道 {tunnel.name}@{srv.name} 连续 {tunnel.fail_count} 次异常，需要重启: "
                    f"进程={'✓' if process_ok else '✗'}, 连接={self._port_status_str(port_result)}"
                )

            status[tunnel.name] = is_healthy

        return status

    @staticmethod
    def _port_status_str(port_result: Optional[bool]) -> str:
        if port_result is True:
            return '✓'
        elif port_result is False:
            return '✗'
        else:
            return '?超时'

    def run_daemon(self):
        self.logger.info("=" * 60)
        self.logger.info("隧道管理器启动 (守护模式)")
        self.logger.info(f"  服务器: {', '.join(self.servers.keys())}")
        self.logger.info(f"  隧道数: {len(self.tunnels)}")
        self.logger.info("=" * 60)
        self.running = True

        def signal_handler(signum, frame):
            if not self.running:
                return
            self.logger.info("收到停止信号，正在清理...")
            self.running = False
            self.stop_all()
            self.logger.info("隧道管理器已停止")
            sys.exit(0)

        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        self.start_all()

        check_interval = int(self.global_settings.get("check_interval", 30))
        self.logger.info(f"健康检查间隔: {check_interval}s | Ctrl+C 退出")

        while self.running:
            for _ in range(check_interval):
                if not self.running:
                    break
                time.sleep(1)

            if not self.running:
                break

            status = self.check_health()
            for name, is_healthy in status.items():
                if not is_healthy:
                    self.logger.warning(f"重启隧道 {name}...")
                    tunnel = self.tunnels[name]
                    self.stop_tunnel(tunnel)
                    time.sleep(5)  # 多等几秒让远程端口释放
                    self.start_tunnel(tunnel)
                    tunnel.fail_count = 0

        self.stop_all()
        self.logger.info("隧道管理器已停止")

    # =========================================================================
    # Status display
    # =========================================================================

    def show_status(self):
        status = self.check_health()

        current_server = None
        sorted_tunnels = sorted(self.tunnels.values(), key=lambda t: (t.server_name, t.name))

        print()
        print("=" * 110)
        print("SSH 隧道状态")
        print("=" * 110)

        for tunnel in sorted_tunnels:
            srv = self._get_server(tunnel)
            if current_server != tunnel.server_name:
                current_server = tunnel.server_name
                print(f"\n  [{srv.name}] {srv.ssh_user}@{srv.ssh_server}:{srv.ssh_port}")
                print(f"  {'─' * 104}")
                print(f"  {'名称':<28} {'类型':<8} {'端口映射':<35} {'进程':<14} {'状态'}")
                print(f"  {'─' * 104}")

            pid = self._find_autossh_pid(tunnel)
            process_str = f"运行({pid})" if pid else "未运行"
            is_healthy = status.get(tunnel.name, False)
            health_str = "✅ 正常" if is_healthy else "❌ 异常"

            if tunnel.tunnel_type == "remote":
                port_info = f"远程:{tunnel.local_port} ← 本地:{tunnel.remote_host}:{tunnel.remote_port}"
            else:
                port_info = (
                    f"本地:{tunnel.bind_address}:{tunnel.bind_port} → 远程:{tunnel.remote_host}:{tunnel.remote_port}"
                )

            type_label = "-R" if tunnel.tunnel_type == "remote" else "-L"
            print(f"  {tunnel.name:<28} {type_label:<8} {port_info:<35} {process_str:<14} {health_str}")

        print()
        print("=" * 110)
        total = len(self.tunnels)
        healthy = sum(1 for v in status.values() if v)
        print(f"  合计: {total} 隧道, {healthy} 正常, {total - healthy} 异常 | 服务器: {len(self.servers)}")
        print("=" * 110)


def main():
    parser = argparse.ArgumentParser(
        description="SSH Tunnel Manager (多服务器支持)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s --status                          # 所有隧道状态
  %(prog)s --status --server devops          # 只看 devops
  %(prog)s --restart                         # 重启所有
  %(prog)s --restart --server devops         # 只重启 devops
  %(prog)s --config tunnels.yaml devops.yaml # 加载多个配置
  %(prog)s --foreground                      # 前台守护
  %(prog)s --stop --server yizhao            # 只停 yizhao
        """,
    )
    parser.add_argument(
        "--config",
        "-c",
        nargs="+",
        default=[str(Path(__file__).parent / "tunnels.yaml")],
        help="配置文件路径（可指定多个）",
    )
    parser.add_argument("--server", "-S", default=None, help="只操作指定服务器的隧道")
    parser.add_argument("--daemon", "-d", action="store_true", help="后台守护进程")
    parser.add_argument("--foreground", "-f", action="store_true", help="前台守护（调试）")
    parser.add_argument("--status", "-s", action="store_true", help="显示状态")
    parser.add_argument("--start", action="store_true", help="启动")
    parser.add_argument("--stop", action="store_true", help="停止")
    parser.add_argument("--restart", "-r", action="store_true", help="重启")

    args = parser.parse_args()

    manager = TunnelManager(args.config, server_filter=args.server)

    if args.status:
        manager.show_status()
    elif args.start:
        manager.start_all()
    elif args.stop:
        manager.stop_all()
    elif args.restart:
        manager.stop_all()
        time.sleep(2)
        manager.start_all()
    elif args.daemon or args.foreground:
        manager.run_daemon()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
