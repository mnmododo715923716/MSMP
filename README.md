
# MSMP – Minecraft Server Management Panel

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8%2B-blue)](https://www.python.org/)

**MSMP** is a lightweight, cross-platform web control panel for Minecraft servers (Forge, NeoForge, Fabric).  
**MSMP** 是一个轻量级、跨平台的 Minecraft 服务器网页管理面板（支持 Forge、NeoForge、Fabric）。

It allows you to manage your server entirely from a browser – start/stop/restart, mod management, online player list, ban/OP control, world backups, configuration editing, and local AI log analysis.  
您可以通过浏览器完全管理服务器 – 启动/停止/重启、模组管理、在线玩家列表、封禁/OP 控制、世界备份、配置文件编辑以及本地 AI 日志分析。

**Key highlights:** multi‑user role system (root/administrator/politician), stdin‑based server control (no RCON required), login failure limit, guest mod download page.  
**主要亮点：** 多用户角色系统（root/administrator/politician）、基于 stdin 的服务器控制（无需 RCON）、登录失败限制、访客模组下载页面。

---

## Features / 功能特点

- **Server Control** – Start, stop, restart your server as a subprocess (stdin‑based).  
  **服务器控制** – 以子进程方式启动/停止/重启服务器（基于 stdin 命令）。
- **Mod Management** – Upload, delete, batch upload, and download all mods (including optimisation mods).  
  **模组管理** – 上传、删除、批量上传、打包下载普通模组和优化模组。
- **Online Player List** – Real‑time list based on log parsing (no RCON). Displays name, UUID, IP.  
  **在线玩家列表** – 基于日志解析实时更新，展示玩家名、UUID、IP（无需 RCON）。
- **Ban & OP Management** – Manage `banned-players.json` and `ops.json` directly, with real‑time effect via stdin.  
  **封禁与 OP 管理** – 直接编辑封禁和 OP 列表，并通过 stdin 实时生效。
- **World Backup & Restore** – Create zip backups of your world folder and restore them (auto stop/start).  
  **世界备份与恢复** – 打包存档文件夹为 zip 备份，支持恢复（自动停服和重启）。
- **Configuration Editing** – Edit `server.properties` and mod config files (`.cfg`, `.toml`, `.json`, `.properties`, etc.).  
  **配置文件编辑** – 在线编辑 `server.properties` 以及模组配置文件。
- **Local LLM Log Analysis** – Send server log to a local OpenAI‑compatible LLM for intelligent analysis (optional).  
  **AI 日志分析** – 将服务器日志发送到 OpenAI 兼容的大模型进行智能分析（可选）。
- **Guest Page** – Public page for mod downloads (optional include optimisation mods) and online player list (names only).  
  **访客页面** – 公开的模组下载页面（可选择是否包含优化模组）和在线玩家列表（仅显示名称）。
- **Multi‑User Role System** – Three roles: `root`, `administrator`, `politician`.  
  **多用户角色系统** – 三种角色：`root`、`administrator`、`politician`。
  - `root` – Full access (all modules + user management).  
  - `administrator` – Mod management, backups, config editing, commands, kick/ban/OP actions.  
  - `politician` – Only view online players and read‑only ban/OP lists.  
  - `root` can add/delete other users from the web panel.  
  - `root` password is set only via command line.
- **Login Failure Limit** – IP locked for 24 hours after 3 consecutive failed login attempts.  
  **登录失败限制** – 连续 3 次登录失败后锁定 IP 24 小时。
- **No RCON Required** – All commands are sent via stdin to the server process.  
  **无需 RCON** – 所有命令通过 stdin 发送到服务器进程。
- **Cross‑platform & Command‑line Configuration** – All settings passed via command‑line arguments.  
  **跨平台且无需环境变量** – 所有配置通过命令行参数传递。

---

## Requirements / 环境要求

- Python 3.8+
- A running Minecraft server (Forge/NeoForge/Fabric) – **no RCON setup needed**
- (Optional) A LLM service with OpenAI‑compatible API (e.g., [LocalAI](https://github.com/mudler/LocalAI), [Ollama](https://ollama.com/))

---

## Installation & Usage / 安装与使用

### 1. Clone / 克隆

```bash
git clone https://github.com/yourusername/MSMP.git
cd MSMP
```

### 2. Install Dependencies / 安装依赖

```bash
pip install flask flask-httpauth openai
```

### 3. Prepare Your Server / 准备您的服务器

Place your Minecraft server files (jar, `mods/`, `logs/`, `config/`, `world/`, etc.) in a directory (e.g., `C:\MinecraftServer`). No need to enable RCON.

### 4. Configuration & Launch / 配置与启动

All options are passed via command line. Create a start script.

**Windows example (`start.bat`):**

```batch
@echo off
python app.py ^
    --server-path "%CD%" ^
    --start-command "java @user_jvm_args.txt @libraries/net/minecraftforge/forge/1.20.1-47.4.10/win_args.txt nogui" ^
    --root-password your_strong_root_password ^
    --host 127.0.0.1 ^
    --port 5000
pause
```

**Linux/macOS example (`start.sh`):**

```bash
#!/bin/bash
python3 app.py \
    --server-path /path/to/server \
    --start-command "java -Xmx4G -Xms2G -jar fabric-server-launch.jar nogui" \
    --root-password your_strong_root_password \
    --host 0.0.0.0 \
    --port 5000
```

Optional LLM arguments (omit if not needed):  
`--local-llm-base-url http://localhost:8000/v1 --local-llm-model mc-analyst-v1 --local-llm-api-key your_key`

Run your script. The panel will be available at `http://127.0.0.1:5000` (or the host/port you set). Log in with username `root` and the `--root-password` you provided.

After logging in as root, you can add `administrator` and `politician` users via the **User Management** card.

---

## Command‑line Arguments / 命令行参数

| Argument | Default | Description |
|----------|---------|-------------|
| `--server-path` | current directory | Path to Minecraft server folder |
| `--start-command` | `java -Xmx2G -Xms1G -jar server.jar nogui` | Full command to start the server |
| `--world-folder` | `world` | Name of the world folder |
| `--llm-base-url` | `http://localhost:8000/v1` | URL of local LLM service (optional) |
| `--llm-model` | `mc-analyst-v1` | Model name for log analysis (optional) |
| `--llm-api-key` | (empty) | API Key for LLM service (optional) |
| `--root-password` | **required** | Password for the `root` user |
| `--host` | `0.0.0.0` | Flask listening address |
| `--port` | `5000` | Flask listening port |
| `--debug` | `False` | Enable Flask debug mode |

---

## User Roles / 用户角色

| Role | Permissions |
|------|-------------|
| **root** | All modules + user management. |
| **administrator** | Mod management, backups, config editing, server.properties, commands, kick/ban/OP actions. |
| **politician** | View online players and read‑only ban/OP lists. |

- `root` can add/delete/modify passwords of `administrator` and `politician` users via the web panel.  
- `root` password can only be set via command line (`--root-password`); it cannot be changed from the panel.

---

## File Structure / 文件结构

When you run the panel, the following files/folders are created:

```
MSMP/
├── app.py                  # Main application
├── users.json              # Non‑root users (username, password, role)
├── login_fails.json        # IP login failure records
├── optimmod/               # Folder for optimisation mods
├── backups/                # World backups (relative to SERVER_PATH)
└── (your server folder)    # mods/, logs/, config/, world/, etc.
```

---

## Troubleshooting / 常见问题

**Q: Can't log in?**  
A: Use username `root` and the password from `--root-password`. For other users, ensure correct credentials.

**Q: Server doesn't start?**  
A: Check the console output. Verify `--start-command` and the working directory (`--server-path`).

**Q: Online player list is empty?**  
A: The list updates automatically via log parsing when players join/leave. No manual refresh needed.

**Q: I want to reset the login failure lock for an IP.**  
A: Delete or edit `login_fails.json` (remove the entry for that IP) and restart the panel.

---

## License / 许可证

This project is licensed under the **MIT License**. See [LICENSE](LICENSE) for details.  
本项目采用 **MIT 许可证**。

---

## Acknowledgements / 致谢

- [Flask](https://flask.palletsprojects.com/)
- [Flask-HTTPAuth](https://flask-httpauth.readthedocs.io/)
- [OpenAI Python Library](https://github.com/openai/openai-python)

---

**Enjoy managing your Minecraft server with MSMP!**  
**祝您使用 MSMP 愉快地管理 Minecraft 服务器！**


