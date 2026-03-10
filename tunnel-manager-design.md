# Python SSH Tunnel Manager 设计方案

## 目标

将 `run-local-services.sh` 转换为 Python 脚本，实现：
1. 服务的统一定义（配置文件）
2. 定期自动检查隧道状态
3. 自动重启断开的隧道
4. 作为守护进程运行（替代 cron）

---

## 架构设计

```
tunnel_manager.py
    ├── 配置文件 (tunnels.yaml)
    ├── 进程管理 (启动/停止/重启 autossh)
    ├── 健康检查 (定期检查远程端口)
    ├── 日志系统 (记录状态变化)
    └── 信号处理 (优雅退出)
```

---

## 核心功能

### 1. 配置文件 (tunnels.yaml)

```yaml
tunnels:
  - name: ai-api
    local_port: 11436
    remote_host: 127.0.0.1
    remote_port: 11436
    description: AI API LLM Proxy
    
  - name: lab-redmine
    local_port: 18200
    remote_host: 172.22.164.35
    remote_port: 80
    description: Lab Redmine
    
  # ... 其他隧道配置

settings:
  check_interval: 60        # 检查间隔（秒）
  ssh_server: 172.96.254.246
  ssh_port: 27959
  ssh_user: zt
  ssh_key: ~/.ssh/studio_02
  autossh_poll: 60
  autossh_gatetime: 0
```

### 2. 核心类设计

```python
class TunnelManager:
    - load_config()         # 加载配置
    - start_all()           # 启动所有隧道
    - stop_all()            # 停止所有隧道
    - check_health()        # 检查隧道健康状态
    - restart_tunnel()      # 重启单个隧道
    - run_daemon()          # 守护进程主循环
```

### 3. 健康检查机制

```
每 60 秒执行一次：
1. 通过 SSH 检查远程端口是否监听
2. 检查本地 autossh 进程是否存活
3. 如果检测到异常，重启对应隧道
```

### 4. 日志系统

- 状态变化日志
- 错误日志
- 可选：发送通知（邮件/webhook）

---

## 文件结构

```
/Users/zhangtony/proxy/
├── tunnel_manager.py      # 主脚本
├── tunnels.yaml           # 配置文件
├── tunnel_manager.log     # 日志文件
└── run-local-services.sh  # 原脚本（保留备份）
```

---

## 使用方式

```bash
# 启动守护进程
python3 tunnel_manager.py --daemon

# 前台运行（调试）
python3 tunnel_manager.py --foreground

# 检查状态
python3 tunnel_manager.py --status

# 重启所有隧道
python3 tunnel_manager.py --restart

# 停止所有隧道
python3 tunnel_manager.py --stop
```

---

## 下一步

需要用户确认后，我将创建完整的 Python 脚本。