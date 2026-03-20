# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

本仓库是一组网络代理和 SSH 隧道管理工具，用于连接多个内网环境（实验室、DevOps 服务器、云 VPS）。核心场景：通过 VPS 跳板机暴露内部服务、管理反向/本地 SSH 隧道、chisel 隧道转发、RustDesk 远程桌面流量重定向。

## 架构

### 两套隧道方案并行运行

1. **SSH 隧道管理器** (`tunnel_manager.py` + `tunnels.yaml`)
   - Python 守护进程，基于 autossh 管理多服务器 SSH 隧道
   - 支持 remote (-R) 和 local (-L) 两种隧道类型
   - 自动健康检查 + 自动重启断开的隧道
   - 配置支持新版（多服务器 `servers:` 块）和旧版（单服务器 `settings:` 内联）格式

2. **Chisel 隧道** (`chisel-client.sh` / `chisel-server.sh`)
   - 用于无法直连时的备用方案（通过 HTTPS 隧道）
   - 配合 `udp-tcp-bridge.py` 解决 chisel 不支持 UDP 的问题
   - 配合 `pf-rustdesk-redirect.conf` (macOS pf 防火墙) 实现 RustDesk 流量重定向

### 关键服务器

| 名称 | 地址 | 角色 |
|------|------|------|
| yizhao | 172.96.254.246:27959 | VPS 跳板机，运行代理服务 |
| devops | 172.22.164.60:22 | Mac Studio，运行 DevOps 服务 |
| lab-gateway | 172.22.164.35 | 实验室 NAT 网关，端口转发到内网 |

### 配置文件

- `tunnels.yaml` — 主配置，定义所有服务器和隧道（多服务器格式）
- `local.yaml` — 旧版单服务器格式的配置（仅 yizhao）
- `devops-60.yaml` — 旧版单服务器格式（仅 devops CodeForge 隧道）

## 常用命令

```bash
# SSH 隧道管理
python3 tunnel_manager.py --status                    # 查看所有隧道状态
python3 tunnel_manager.py --status --server yizhao    # 查看指定服务器
python3 tunnel_manager.py --foreground                # 前台守护模式（调试）
python3 tunnel_manager.py --restart                   # 重启所有隧道
python3 tunnel_manager.py --restart --server devops   # 重启指定服务器隧道
python3 tunnel_manager.py --stop                      # 停止所有
python3 tunnel_manager.py -c tunnels.yaml devops-60.yaml --status  # 加载多配置

# Chisel 隧道（在无法直连 devops 时使用）
./chisel-client.sh start|stop|restart|status
./chisel-server.sh start|stop|restart|status    # 在 devops 服务器上运行

# 代理环境变量
source setproxy.sh    # 设置 http_proxy/https_proxy (127.0.0.1:4001)
source unsetproxy.sh  # 取消代理
```

## 依赖

- `autossh` — SSH 隧道自动重连
- `chisel` — HTTP 隧道（二进制文件在仓库根目录）
- Python 3 + `pyyaml`
- macOS `pfctl` — RustDesk 流量重定向（仅 chisel 方案）

## 注意事项

- `tunnel_manager.py` 使用 `pgrep -f` 匹配进程，通过端口号+服务器地址的组合作为唯一标识
- 隧道启动后有 30 秒 grace period，期间不会因端口未就绪而触发重启
- `rustdesk/` 目录是独立的 RustDesk 子项目（Rust + Flutter），有自己的 CLAUDE.md
- `lab-infra.md` 记录了实验室网络拓扑和端口转发配置，包含服务器凭据
