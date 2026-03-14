#!/usr/bin/env python3
"""
SSH Tunnel Manager - 自动管理多个 SSH 反向隧道和本地转发

功能：
1. 统一定义隧道配置（支持 -R 反向隧道和 -L 本地转发）
2. 定期自动检查隧道状态
3. 自动重启断开的隧道
4. 作为守护进程运行

使用方法：
    python3 tunnel_manager.py --daemon      # 后台守护进程
    python3 tunnel_manager.py --foreground  # 前台运行（调试）
    python3 tunnel_manager.py --status      # 查看状态
    python3 tunnel_manager.py --restart     # 重启所有隧道
    python3 tunnel_manager.py --stop        # 停止所有隧道
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml


@dataclass
class Tunnel:
    """隧道配置"""
    name: str
    tunnel_type: str  # 'remote' 或 'local'
    # 远程隧道 (-R) 使用
    local_port: int = 0
    remote_host: str = ""
    remote_port: int = 0
    # 本地转发 (-L) 使用
    bind_address: str = ""
    bind_port: int = 0
    # 通用
    description: str = ""
    pid: Optional[int] = None
    start_time: Optional[float] = None  # 隧道启动时间戳


class TunnelManager:
    """SSH 隧道管理器"""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.tunnels: Dict[str, Tunnel] = {}
        self.settings: Dict = {}
        self.running = False
        self.log_file = Path(__file__).parent / "tunnel_manager.log"
        
        self._setup_logging()
        self._load_config()

    def _setup_logging(self):
        """配置日志"""
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=[
                logging.FileHandler(self.log_file),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger(__name__)

    def _load_config(self):
        """加载配置文件"""
        config_file = Path(self.config_path)
        if not config_file.exists():
            self.logger.error(f"配置文件不存在: {self.config_path}")
            sys.exit(1)

        with open(config_file, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)

        self.settings = config.get('settings', {})
        
        for t in config.get('tunnels', []):
            tunnel = Tunnel(
                name=t['name'],
                tunnel_type=t.get('type', 'remote'),
                local_port=t.get('local_port', 0),
                remote_host=t.get('remote_host', ''),
                remote_port=t.get('remote_port', 0),
                bind_address=t.get('bind_address', ''),
                bind_port=t.get('bind_port', 0),
                description=t.get('description', '')
            )
            self.tunnels[tunnel.name] = tunnel

        self.logger.info(f"已加载 {len(self.tunnels)} 个隧道配置")

    def _get_ssh_key_path(self) -> str:
        """获取 SSH 密钥路径"""
        key = self.settings.get('ssh_key', '~/.ssh/studio_02')
        return os.path.expanduser(key)

    def _build_autossh_cmd(self, tunnel: Tunnel) -> List[str]:
        """构建 autossh 命令"""
        ssh_server = self.settings.get('ssh_server', '172.96.254.246')
        ssh_port = self.settings.get('ssh_port', 27959)
        ssh_user = self.settings.get('ssh_user', 'zt')
        ssh_key = self._get_ssh_key_path()
        
        poll = self.settings.get('autossh_poll', 60)

        if tunnel.tunnel_type == 'remote':
            # 反向隧道 (-R): 远程端口 → 本地端口
            forward_arg = f'-R 127.0.0.1:{tunnel.local_port}:{tunnel.remote_host}:{tunnel.remote_port}'
        else:
            # 本地转发 (-L): 本地端口 → 远程端口
            forward_arg = f'-L {tunnel.bind_address}:{tunnel.bind_port}:{tunnel.remote_host}:{tunnel.remote_port}'

        cmd = [
            'autossh', '-M', '0',
            forward_arg,
            '-f', '-N',
            '-o', 'TCPKeepAlive=yes',
            '-o', f'ServerAliveInterval={poll}',
            '-o', 'ServerAliveCountMax=3',
            '-o', 'ExitOnForwardFailure=yes',
            '-o', 'StrictHostKeyChecking=no',
            '-i', ssh_key,
            '-p', str(ssh_port),
            f'{ssh_user}@{ssh_server}'
        ]

        # 添加绑定地址参数 (-b)，用于指定从哪个 IP 出站
        bind_address = self.settings.get('bind_address', '')
        if bind_address:
            cmd.insert(1, '-b')
            cmd.insert(2, bind_address)

        return cmd

    def _get_process_identifier(self, tunnel: Tunnel) -> str:
        """获取用于进程查找的唯一标识符
        
        对于本地转发隧道，使用 bind_address:bind_port 组合来唯一标识
        """
        if tunnel.tunnel_type == 'remote':
            return str(tunnel.local_port)
        else:
            # 本地转发使用完整的绑定地址和端口组合
            return f"{tunnel.bind_address}:{tunnel.bind_port}"

    def _find_autossh_pid(self, tunnel: Tunnel) -> Optional[int]:
        """查找隧道对应的 autossh 进程 PID"""
        identifier = self._get_process_identifier(tunnel)
        try:
            # 使用更精确的模式匹配
            result = subprocess.run(
                ['pgrep', '-f', f'autossh.*{identifier}'],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                pids = result.stdout.strip().split('\n')
                if pids and pids[0]:
                    return int(pids[0])
        except Exception as e:
            self.logger.error(f"查找进程失败: {e}")
        return None

    def _check_tunnel_port(self, tunnel: Tunnel) -> bool:
        """检查隧道端口状态"""
        if tunnel.tunnel_type == 'remote':
            # 远程隧道：检查远程服务器上的端口
            return self._check_remote_port(tunnel.local_port)
        else:
            # 本地转发：通过代理访问 google.com 验证隧道
            return self._check_local_proxy(tunnel.bind_address, tunnel.bind_port)

    def _check_remote_port(self, port: int) -> bool:
        """检查远程服务器上的端口是否监听"""
        ssh_server = self.settings.get('ssh_server', '172.96.254.246')
        ssh_port = self.settings.get('ssh_port', 27959)
        ssh_user = self.settings.get('ssh_user', 'zt')
        ssh_key = self._get_ssh_key_path()

        try:
            result = subprocess.run(
                [
                    'ssh', '-i', ssh_key, '-p', str(ssh_port),
                    '-o', 'ConnectTimeout=5',
                    '-o', 'StrictHostKeyChecking=no',
                    f'{ssh_user}@{ssh_server}',
                    f'ss -tlnp | grep -q :{port}'
                ],
                capture_output=True,
                timeout=10
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            self.logger.warning(f"检查远程端口 {port} 超时")
            return False
        except Exception as e:
            self.logger.error(f"检查远程端口失败: {e}")
            return False

    def _check_local_proxy(self, bind_address: str, bind_port: int) -> bool:
        """检查本地代理隧道是否可用 - 通过 TCP 端口检测"""
        import socket
        
        # 处理 0.0.0.0 的情况，优先使用 127.0.0.1 检测
        check_addr = bind_address if bind_address != '0.0.0.0' else '127.0.0.1'
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(3)
        try:
            result = sock.connect_ex((check_addr, bind_port))
            if result == 0:
                self.logger.debug(f"代理 {check_addr}:{bind_port} 端口检测成功")
                return True
            else:
                self.logger.warning(f"代理 {bind_address}:{bind_port} 端口检测失败")
                return False
        except socket.timeout:
            self.logger.warning(f"代理 {bind_address}:{bind_port} 连接超时")
            return False
        except Exception as e:
            self.logger.warning(f"代理 {bind_address}:{bind_port} 检测异常: {e}")
            return False
        finally:
            sock.close()

    def start_tunnel(self, tunnel: Tunnel) -> bool:
        """启动单个隧道"""
        pid = self._find_autossh_pid(tunnel)
        if pid:
            self.logger.info(f"隧道 {tunnel.name} 已在运行 (PID: {pid})")
            tunnel.pid = pid
            return True

        cmd = self._build_autossh_cmd(tunnel)
        self.logger.info(f"启动隧道 {tunnel.name}...")

        env = os.environ.copy()
        env['AUTOSSH_GATETIME'] = str(self.settings.get('autossh_gatetime', 0))
        env['AUTOSSH_POLL'] = str(self.settings.get('autossh_poll', 10))

        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=30)
            if result.returncode == 0:
                # 等待并检查端口是否就绪（最多重试10次，每次1秒）
                max_retries = 10
                retry_interval = 1
                for attempt in range(max_retries):
                    time.sleep(retry_interval)
                    tunnel.pid = self._find_autossh_pid(tunnel)
                    if tunnel.pid:
                        # 检查端口是否已监听
                        if self._check_tunnel_port(tunnel):
                            tunnel.start_time = time.time()
                            self.logger.info(
                                f"隧道 {tunnel.name} 启动成功 (PID: {tunnel.pid}, "
                                f"等待 {attempt + 1} 秒后端口就绪)"
                            )
                            return True
                        else:
                            self.logger.debug(
                                f"隧道 {tunnel.name} 进程已启动，"
                                f"等待端口就绪 (尝试 {attempt + 1}/{max_retries})..."
                            )
                    else:
                        self.logger.debug(
                            f"隧道 {tunnel.name} 进程尚未就绪 "
                            f"(尝试 {attempt + 1}/{max_retries})..."
                        )
                
                # 达到最大重试次数，启动失败
                self.logger.error(
                    f"隧道 {tunnel.name} 启动失败: "
                    f"等待 {max_retries} 秒后端口仍未就绪"
                )
                return False
            else:
                self.logger.error(f"隧道 {tunnel.name} 启动失败: {result.stderr}")
                return False
        except Exception as e:
            self.logger.error(f"启动隧道 {tunnel.name} 异常: {e}")
            return False

    def stop_tunnel(self, tunnel: Tunnel) -> bool:
        """停止单个隧道"""
        pid = self._find_autossh_pid(tunnel)
        if not pid:
            self.logger.info(f"隧道 {tunnel.name} 未运行")
            return True

        try:
            subprocess.run(['kill', '-9', str(pid)], check=True)
            time.sleep(1)
            
            # 也杀掉可能残留的 ssh 进程
            identifier = self._get_process_identifier(tunnel)
            result = subprocess.run(
                ['pgrep', '-f', f'ssh.*{identifier}'],
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                for ssh_pid in result.stdout.strip().split('\n'):
                    if ssh_pid:
                        subprocess.run(['kill', '-9', ssh_pid])
            
            tunnel.pid = None
            self.logger.info(f"隧道 {tunnel.name} 已停止")
            return True
        except Exception as e:
            self.logger.error(f"停止隧道 {tunnel.name} 失败: {e}")
            return False

    def start_all(self):
        """启动所有隧道"""
        self.logger.info("=" * 60)
        self.logger.info("启动所有隧道...")
        self.logger.info("=" * 60)
        success = 0
        for tunnel in self.tunnels.values():
            if self.start_tunnel(tunnel):
                success += 1
        self.logger.info(f"启动完成: {success}/{len(self.tunnels)}")

    def stop_all(self):
        """停止所有隧道"""
        self.logger.info("停止所有隧道...")
        for tunnel in self.tunnels.values():
            self.stop_tunnel(tunnel)

    def check_health(self) -> Dict[str, bool]:
        """检查所有隧道健康状态
        
        对于刚启动的隧道（30秒内），给予宽限期，避免误判。
        只有当进程和端口都异常时才报告不健康。
        """
        status = {}
        grace_period = 30
        
        for tunnel in self.tunnels.values():
            pid = self._find_autossh_pid(tunnel)
            process_ok = pid is not None
            port_ok = self._check_tunnel_port(tunnel)
            
            # 基础健康状态
            is_healthy = process_ok and port_ok
            
            # 检查是否在宽限期内
            if tunnel.start_time is not None:
                elapsed = time.time() - tunnel.start_time
                in_grace_period = elapsed < grace_period
                
                # 宽限期内：进程存在但端口未就绪时，暂不判定为异常
                if not is_healthy and in_grace_period and process_ok and not port_ok:
                    self.logger.debug(
                        f"隧道 {tunnel.name} 在宽限期内 "
                        f"(已启动 {elapsed:.1f}s)，暂不判定为异常"
                    )
                    is_healthy = True
            
            status[tunnel.name] = is_healthy
            
            if not is_healthy:
                self.logger.warning(
                    f"隧道 {tunnel.name} 异常: "
                    f"进程={'✓' if process_ok else '✗'}, "
                    f"连接={'✓' if port_ok else '✗'}"
                )
        
        return status

    def run_daemon(self):
        """守护进程主循环"""
        self.logger.info("=" * 60)
        self.logger.info("隧道管理器启动 (守护模式)")
        self.logger.info("=" * 60)
        self.running = True
        
        def signal_handler(signum, frame):
            if not self.running:
                # 已经在退出过程中，直接返回避免重复处理
                return
            self.logger.info("收到停止信号，正在清理进程...")
            self.running = False
            # 立即清理所有隧道进程
            self.stop_all()
            self.logger.info("隧道管理器已停止")
            sys.exit(0)
        
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        self.start_all()

        check_interval = self.settings.get('check_interval', 30)
        self.logger.info(f"健康检查间隔: {check_interval} 秒")
        self.logger.info("按 Ctrl+C 退出")

        # 主循环：使用小间隔循环，以便快速响应退出信号
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
                    time.sleep(2)
                    self.start_tunnel(tunnel)

        self.stop_all()
        self.logger.info("隧道管理器已停止")

    def show_status(self):
        """显示隧道状态"""
        print("\n" + "=" * 100)
        print("SSH 隧道状态")
        print("=" * 100)
        print(f"{'名称':<25} {'类型':<10} {'端口':<25} {'进程':<14} {'状态'}")
        print("-" * 100)
        
        status = self.check_health()
        for name, tunnel in self.tunnels.items():
            pid = self._find_autossh_pid(tunnel)
            process_status = f"运行({pid})" if pid else "未运行"
            is_healthy = status.get(name, False)
            health_status = "✅ 正常" if is_healthy else "❌ 异常"
            
            if tunnel.tunnel_type == 'remote':
                port_info = f"远程:{tunnel.local_port} ← {tunnel.remote_host}:{tunnel.remote_port}"
            else:
                port_info = f"本地:{tunnel.bind_address}:{tunnel.bind_port} → {tunnel.remote_host}:{tunnel.remote_port}"
            
            type_label = "远程(-R)" if tunnel.tunnel_type == 'remote' else "本地(-L)"
            print(f"{name:<25} {type_label:<10} {port_info:<25} {process_status:<14} {health_status}")
        
        print("=" * 100)


def main():
    parser = argparse.ArgumentParser(description='SSH Tunnel Manager')
    parser.add_argument('--config', '-c', 
                        default=str(Path(__file__).parent / 'tunnels.yaml'),
                        help='配置文件路径')
    parser.add_argument('--daemon', '-d', action='store_true',
                        help='后台守护进程模式运行')
    parser.add_argument('--foreground', '-f', action='store_true',
                        help='前台运行（调试模式）')
    parser.add_argument('--status', '-s', action='store_true',
                        help='显示隧道状态')
    parser.add_argument('--start', action='store_true',
                        help='启动所有隧道')
    parser.add_argument('--stop', action='store_true',
                        help='停止所有隧道')
    parser.add_argument('--restart', '-r', action='store_true',
                        help='重启所有隧道')
    
    args = parser.parse_args()
    
    manager = TunnelManager(args.config)
    
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


if __name__ == '__main__':
    main()
