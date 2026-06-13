import os
import sys
import glob
import time
import queue
import threading
import subprocess
import logging
import zipfile
import io
import re
import shutil
import json
import argparse
from datetime import datetime
from pathlib import Path
from functools import wraps
from flask import Flask, render_template_string, request, jsonify, send_file, Response, stream_with_context, g
from flask_httpauth import HTTPBasicAuth

app = Flask(__name__)
auth = HTTPBasicAuth()
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

# ================== 全局配置（由命令行参数覆盖） ==================
SERVER_PATH = None
START_COMMAND = None
WORLD_FOLDER = None
LOCAL_LLM_BASE_URL = None
LOCAL_LLM_MODEL = None
LOCAL_LLM_API_KEY = None
ROOT_PASSWORD = None   # root 用户密码，仅从命令行读取

# ================== 依赖路径 ==================
MODS_FOLDER = None
LOG_FILE = None
CONSOLE_LOG = None
BACKUP_FOLDER = None
CONFIG_FOLDER = None
OPTIM_MOD_FOLDER = None
SERVER_PROPERTIES_FILE = None
BANNED_PLAYERS_FILE = None
OPS_FILE = None
USERS_FILE = 'users.json'

# ================== 全局状态 ==================
server_process = None
server_status = "stopped"
server_pid = None
console_queue = queue.Queue()
stop_event = threading.Event()
read_thread = None

online_players_set = set()
online_players_lock = threading.Lock()
player_info_cache = {}
cache_lock = threading.Lock()

# ================== 用户管理 ==================
def load_users():
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_users(users):
    try:
        with open(USERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(users, f, indent=2)
        return True
    except:
        return False

def authenticate_user(username, password):
    if username == 'root':
        return ('root', None) if password == ROOT_PASSWORD else (None, '用户名或密码错误')
    users = load_users()
    user = users.get(username)
    if user and user.get('password') == password:
        return user.get('role'), None
    else:
        return None, '用户名或密码错误'

# ================== 登录失败限制 ==================
LOGIN_FAIL_FILE = 'login_fails.json'

def load_login_fails():
    if not os.path.exists(LOGIN_FAIL_FILE):
        return {}
    try:
        with open(LOGIN_FAIL_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return {}

def save_login_fails(data):
    try:
        with open(LOGIN_FAIL_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
    except:
        pass

def is_ip_locked(ip):
    data = load_login_fails()
    if ip not in data:
        return False
    locked_until = data[ip].get('locked_until', 0)
    if locked_until > time.time():
        return True
    if ip in data:
        del data[ip]
        save_login_fails(data)
    return False

def record_fail(ip):
    data = load_login_fails()
    now = time.time()
    if ip not in data:
        data[ip] = {'count': 1, 'locked_until': 0}
    else:
        data[ip]['count'] += 1
        if data[ip]['count'] >= 3:
            data[ip]['locked_until'] = now + 86400
            data[ip]['count'] = 0
    save_login_fails(data)

def clear_fail(ip):
    data = load_login_fails()
    if ip in data:
        del data[ip]
        save_login_fails(data)

# ================== 认证与权限装饰器 ==================
@auth.verify_password
def verify_password(username, password):
    ip = request.remote_addr
    if is_ip_locked(ip):
        return False
    role, error = authenticate_user(username, password)
    if role:
        clear_fail(ip)
        g.username = username
        g.role = role
        return True
    else:
        record_fail(ip)
        return False

def role_required(allowed_roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not hasattr(g, 'role') or g.role not in allowed_roles:
                return jsonify({'error': '权限不足'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

# ================== 辅助函数 ==================
def send_stdin_command(command):
    if server_process is None or server_process.poll() is not None:
        return False, "服务器未运行"
    try:
        server_process.stdin.write((command + "\n").encode())
        server_process.stdin.flush()
        return True, "命令已发送"
    except Exception as e:
        return False, str(e)

def list_mods():
    return [os.path.basename(f) for f in glob.glob(os.path.join(MODS_FOLDER, '*.jar'))]

def list_optim_mods():
    return [os.path.basename(f) for f in glob.glob(os.path.join(OPTIM_MOD_FOLDER, '*.jar'))]

def get_log_content(max_lines=500):
    if not os.path.exists(LOG_FILE):
        return "日志文件不存在"
    try:
        with open(LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
            return ''.join(lines[-max_lines:])
    except Exception as e:
        return f"读取日志失败: {e}"

def is_llm_configured():
    return (LOCAL_LLM_BASE_URL != 'http://localhost:8000/v1' or LOCAL_LLM_MODEL != 'mc-analyst-v1') and LOCAL_LLM_BASE_URL and LOCAL_LLM_MODEL

def analyze_with_local_llm(log_text):
    if not is_llm_configured():
        return "LLM 未配置"
    if not log_text:
        return "没有日志内容可分析"
    max_chars = 8000
    if len(log_text) > max_chars:
        log_text = log_text[:max_chars] + "\n...(日志过长，已截断)"
    try:
        import openai
        client = openai.OpenAI(
            base_url=LOCAL_LLM_BASE_URL,
            api_key=LOCAL_LLM_API_KEY or "not-needed"
        )
        response = client.chat.completions.create(
            model=LOCAL_LLM_MODEL,
            messages=[
                {"role": "system", "content": "你是一个 Minecraft 服务器管理助手，请分析以下服务器日志，指出可能的错误、警告或异常，并给出简短建议。"},
                {"role": "user", "content": log_text}
            ],
            temperature=0.5,
            max_tokens=1000
        )
        return response.choices[0].message.content
    except ImportError:
        return "请安装 openai 库：pip install openai"
    except Exception as e:
        return f"调用本地 LLM 服务失败: {e}"

# ================== 玩家信息解析 ==================
def parse_player_info(line):
    global player_info_cache
    login_ip_pattern = re.compile(r'\[.*?\]\s+(\w+)\[/([0-9.]+):\d+\]\s+logged in')
    join_pattern = re.compile(r'(\w+)\s+joined the game')
    uuid_pattern = re.compile(r'UUID of player (\w+) is ([0-9a-f-]+)')
    forge_login_pattern = re.compile(r'(\w+)\s+\(UUID:\s+([0-9a-f-]+)\)\s+logged in')
    leave_pattern = re.compile(r'(\w+)\s+left the game')

    with cache_lock:
        m = login_ip_pattern.search(line)
        if m:
            name, ip = m.groups()
            if name not in player_info_cache:
                player_info_cache[name] = {}
            player_info_cache[name]['ip'] = ip
            with online_players_lock:
                online_players_set.add(name)
        m = join_pattern.search(line)
        if m:
            name = m.group(1)
            with online_players_lock:
                online_players_set.add(name)
        m = forge_login_pattern.search(line)
        if m:
            name, uuid = m.groups()
            if name not in player_info_cache:
                player_info_cache[name] = {}
            player_info_cache[name]['uuid'] = uuid
            with online_players_lock:
                online_players_set.add(name)
        m = uuid_pattern.search(line)
        if m:
            name, uuid = m.groups()
            if name not in player_info_cache:
                player_info_cache[name] = {}
            player_info_cache[name]['uuid'] = uuid
        m = leave_pattern.search(line)
        if m:
            name = m.group(1)
            with online_players_lock:
                online_players_set.discard(name)

def get_current_online_players():
    with online_players_lock:
        return list(online_players_set)

def get_player_details():
    with online_players_lock:
        names = list(online_players_set)
    result = []
    with cache_lock:
        for name in names:
            info = player_info_cache.get(name, {})
            result.append({'name': name, 'uuid': info.get('uuid', '未知'), 'ip': info.get('ip', '未知')})
    return result

# ================== server.properties 管理 ==================
def get_server_properties_content():
    if not os.path.exists(SERVER_PROPERTIES_FILE):
        return None, "server.properties 文件不存在"
    try:
        with open(SERVER_PROPERTIES_FILE, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read(), None
    except Exception as e:
        return None, str(e)

def save_server_properties_content(content):
    try:
        with open(SERVER_PROPERTIES_FILE, 'w', encoding='utf-8') as f:
            f.write(content)
        return True, None
    except Exception as e:
        return False, str(e)

# ================== 黑名单管理 ==================
def load_banned_players():
    if not os.path.exists(BANNED_PLAYERS_FILE):
        return []
    try:
        with open(BANNED_PLAYERS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except:
        return []

def save_banned_players(bans):
    try:
        with open(BANNED_PLAYERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(bans, f, indent=2)
        return True
    except Exception as e:
        logging.error(f"保存 banned-players.json 失败: {e}")
        return False

def add_ban(uuid, name, reason=None, source=None):
    bans = load_banned_players()
    for ban in bans:
        if ban.get('uuid') == uuid:
            return False, "该玩家已在封禁列表中"
    new_ban = {"uuid": uuid, "name": name, "created": datetime.now().isoformat(), "source": source or "面板", "reason": reason or "未提供原因"}
    bans.append(new_ban)
    if save_banned_players(bans):
        return True, "添加成功"
    else:
        return False, "保存失败"

def remove_ban(uuid):
    bans = load_banned_players()
    new_bans = [b for b in bans if b.get('uuid') != uuid]
    if len(new_bans) == len(bans):
        return False, "未找到该封禁记录"
    if save_banned_players(new_bans):
        return True, "删除成功"
    else:
        return False, "保存失败"

# ================== OP 名单管理 ==================
def load_ops():
    if not os.path.exists(OPS_FILE):
        return []
    try:
        with open(OPS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except:
        return []

def save_ops(ops_list):
    try:
        with open(OPS_FILE, 'w', encoding='utf-8') as f:
            json.dump(ops_list, f, indent=2)
        return True
    except Exception as e:
        logging.error(f"保存 ops.json 失败: {e}")
        return False

def add_op(uuid, name, level=4):
    ops = load_ops()
    for op in ops:
        if op.get('uuid') == uuid:
            return False, "该玩家已是 OP"
    new_op = {"uuid": uuid, "name": name, "level": level}
    ops.append(new_op)
    if save_ops(ops):
        return True, "添加成功"
    else:
        return False, "保存失败"

def remove_op(uuid):
    ops = load_ops()
    new_ops = [o for o in ops if o.get('uuid') != uuid]
    if len(new_ops) == len(ops):
        return False, "未找到该 OP 记录"
    if save_ops(new_ops):
        return True, "删除成功"
    else:
        return False, "保存失败"

# ================== 备份功能 ==================
def create_backup():
    world_path = os.path.join(SERVER_PATH, WORLD_FOLDER)
    if not os.path.exists(world_path):
        return None, f"存档目录不存在: {WORLD_FOLDER}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_name = f"{WORLD_FOLDER}_{timestamp}.zip"
    backup_path = os.path.join(BACKUP_FOLDER, backup_name)
    try:
        with zipfile.ZipFile(backup_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(world_path):
                for file in files:
                    full_path = os.path.join(root, file)
                    arcname = os.path.relpath(full_path, os.path.dirname(world_path))
                    zipf.write(full_path, arcname)
        return backup_name, None
    except Exception as e:
        return None, str(e)

def list_backups():
    backups = []
    for f in glob.glob(os.path.join(BACKUP_FOLDER, '*.zip')):
        stat = os.stat(f)
        backups.append({'name': os.path.basename(f), 'size': stat.st_size, 'modified': stat.st_mtime})
    backups.sort(key=lambda x: x['modified'], reverse=True)
    return backups

def delete_backup(filename):
    file_path = os.path.join(BACKUP_FOLDER, filename)
    if os.path.exists(file_path) and filename.endswith('.zip'):
        os.remove(file_path)
        return True
    return False

def restore_backup(filename):
    backup_path = os.path.join(BACKUP_FOLDER, filename)
    if not os.path.exists(backup_path):
        return False, f"备份文件不存在: {filename}"
    if server_process and server_process.poll() is None:
        logging.info("正在停止服务器以恢复存档...")
        stop_server()
        timeout = 30
        start_time = time.time()
        while server_process and server_process.poll() is None and (time.time() - start_time) < timeout:
            time.sleep(0.5)
        if server_process and server_process.poll() is None:
            return False, "无法停止服务器，恢复操作已取消"
    world_path = os.path.join(SERVER_PATH, WORLD_FOLDER)
    if os.path.exists(world_path):
        try:
            shutil.rmtree(world_path)
        except Exception as e:
            start_server()
            return False, f"无法清空世界目录: {e}"
    else:
        Path(world_path).mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(backup_path, 'r') as zipf:
            for member in zipf.namelist():
                target_path = os.path.join(world_path, member)
                if not os.path.realpath(target_path).startswith(os.path.realpath(world_path)):
                    continue
                zipf.extract(member, world_path)
        logging.info(f"备份 {filename} 已成功恢复到 {world_path}")
    except Exception as e:
        start_server()
        return False, f"解压备份失败: {e}"
    success, msg = start_server()
    if not success:
        return False, f"恢复后启动服务器失败: {msg}"
    return True, "世界已成功恢复并重启服务器"

# ================== 配置文件管理 ==================
def get_config_files():
    files = []
    for root, dirs, names in os.walk(CONFIG_FOLDER):
        for name in names:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, CONFIG_FOLDER)
            if any(name.endswith(ext) for ext in ['.cfg', '.toml', '.json', '.properties', '.txt', '.yaml', '.yml', '.hocon']):
                files.append(rel)
    return sorted(files)

def get_config_content(filepath):
    full = os.path.join(CONFIG_FOLDER, filepath)
    if not os.path.exists(full):
        return None, "文件不存在", None
    try:
        stat = os.stat(full)
        mtime = stat.st_mtime
        with open(full, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        return content, None, mtime
    except Exception as e:
        return None, str(e), None

def get_config_mtime(filepath):
    full = os.path.join(CONFIG_FOLDER, filepath)
    if not os.path.exists(full):
        return None, "文件不存在"
    try:
        stat = os.stat(full)
        return stat.st_mtime, None
    except Exception as e:
        return None, str(e)

def save_config_content(filepath, content):
    full = os.path.join(CONFIG_FOLDER, filepath)
    try:
        with open(full, 'w', encoding='utf-8') as f:
            f.write(content)
        return True, None
    except Exception as e:
        return False, str(e)

# ================== 模组打包 ==================
def pack_mods_zip(include_optim=False):
    mod_files = list_mods()
    if include_optim:
        optim_files = list_optim_mods()
    else:
        optim_files = []
    if not mod_files and not optim_files:
        return None, "没有找到任何模组文件"
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for mod in mod_files:
            zipf.write(os.path.join(MODS_FOLDER, mod), mod)
        for optim in optim_files:
            zipf.write(os.path.join(OPTIM_MOD_FOLDER, optim), optim)
    zip_buffer.seek(0)
    return zip_buffer, None

# ================== 服务器进程管理 ==================
def read_output(pipe, log_file):
    with open(log_file, 'a', encoding='utf-8') as f:
        for line in iter(pipe.readline, b''):
            if not line:
                break
            try:
                text = line.decode('utf-8', errors='replace').rstrip()
            except:
                text = str(line)
            f.write(text + '\n')
            f.flush()
            console_queue.put(text)
            parse_player_info(text)
    pipe.close()

def start_server():
    global server_process, server_status, server_pid, read_thread, stop_event
    if server_process and server_process.poll() is None:
        return False, "服务器已在运行中"
    cmd = START_COMMAND
    logging.info(f"启动命令: {cmd}")
    try:
        server_process = subprocess.Popen(
            cmd,
            cwd=SERVER_PATH,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            shell=True,
            text=False,
        )
        server_status = "starting"
        server_pid = server_process.pid
        stop_event.clear()
        read_thread = threading.Thread(target=read_output, args=(server_process.stdout, CONSOLE_LOG), daemon=True)
        read_thread.start()
        time.sleep(2)
        if server_process.poll() is not None:
            server_status = "stopped"
            return False, "服务器启动后立即退出，请检查控制台日志"
        server_status = "running"
        return True, "服务器启动成功"
    except Exception as e:
        server_status = "stopped"
        return False, f"启动失败: {e}"

def stop_server():
    global server_process, server_status, read_thread
    if not server_process or server_process.poll() is not None:
        server_status = "stopped"
        return False, "服务器未运行"
    server_status = "stopping"
    try:
        server_process.stdin.write("stop\n".encode())
        server_process.stdin.flush()
        logging.info("通过stdin发送stop命令")
    except Exception as e:
        logging.warning(f"stdin发送stop失败: {e}")
    for _ in range(10):
        if server_process.poll() is not None:
            break
        time.sleep(1)
    if server_process.poll() is None:
        server_process.terminate()
        logging.warning("服务器未响应，已强制终止")
        time.sleep(2)
        if server_process.poll() is None:
            server_process.kill()
            logging.warning("强制kill服务器进程")
    with online_players_lock:
        online_players_set.clear()
    server_process = None
    server_status = "stopped"
    console_queue.put("[系统] 服务器已停止")
    return True, "服务器已停止"

def restart_server():
    success, msg = stop_server()
    if not success:
        return False, msg
    time.sleep(2)
    return start_server()

# ================== 路由（权限控制） ==================
@app.route('/')
@auth.login_required
def index():
    return render_template_string(HTML_TEMPLATE, role=g.role)

@app.route('/api/server/status')
@auth.login_required
def server_status_api():
    return jsonify({'status': server_status, 'pid': server_pid, 'running': server_process is not None and server_process.poll() is None})

@app.route('/api/server/start', methods=['POST'])
@auth.login_required
def server_start():
    success, msg = start_server()
    return jsonify({'success': success, 'message': msg})

@app.route('/api/server/stop', methods=['POST'])
@auth.login_required
def server_stop():
    success, msg = stop_server()
    return jsonify({'success': success, 'message': msg})

@app.route('/api/server/restart', methods=['POST'])
@auth.login_required
def server_restart():
    success, msg = restart_server()
    return jsonify({'success': success, 'message': msg})

@app.route('/api/console/stream')
@auth.login_required
def console_stream():
    def generate():
        if os.path.exists(CONSOLE_LOG):
            try:
                with open(CONSOLE_LOG, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()
                    for line in lines[-10:]:
                        yield f"data: {line.strip()}\n\n"
            except:
                pass
        while True:
            try:
                line = console_queue.get(timeout=1)
                yield f"data: {line}\n\n"
            except queue.Empty:
                if server_process and server_process.poll() is not None:
                    yield f"event: close\ndata: 服务器已关闭\n\n"
                    break
                continue
    return Response(stream_with_context(generate()), mimetype="text/event-stream")

@app.route('/api/command', methods=['POST'])
@auth.login_required
@role_required(['root', 'administrator'])
def execute_command():
    data = request.json
    command = data.get('command', '').strip()
    if not command:
        return jsonify({'error': '命令不能为空'}), 400
    success, msg = send_stdin_command(command)
    if not success:
        return jsonify({'error': msg}), 500
    return jsonify({'success': True, 'message': msg})

@app.route('/api/players', methods=['GET'])
@auth.login_required
def get_players():
    return jsonify({'players': get_player_details()})

@app.route('/api/players/kick', methods=['POST'])
@auth.login_required
@role_required(['root', 'administrator'])
def kick_player():
    data = request.json
    player = data.get('player', '').strip()
    if not player:
        return jsonify({'error': '玩家名不能为空'}), 400
    success, msg = send_stdin_command(f"kick {player}")
    if not success:
        return jsonify({'error': msg}), 500
    return jsonify({'success': True, 'message': msg})

@app.route('/api/players/ban', methods=['POST'])
@auth.login_required
@role_required(['root', 'administrator'])
def ban_player():
    data = request.json
    player = data.get('player', '').strip()
    if not player:
        return jsonify({'error': '玩家名不能为空'}), 400
    success, msg = send_stdin_command(f"ban {player}")
    if not success:
        return jsonify({'error': msg}), 500
    with cache_lock:
        info = player_info_cache.get(player, {})
        uuid = info.get('uuid', '')
    if uuid:
        add_ban(uuid, player, source="面板")
    return jsonify({'success': True, 'message': msg})

@app.route('/api/players/banip', methods=['POST'])
@auth.login_required
@role_required(['root', 'administrator'])
def ban_ip():
    data = request.json
    ip = data.get('ip', '').strip()
    if not ip:
        return jsonify({'error': 'IP地址不能为空'}), 400
    success, msg = send_stdin_command(f"ban-ip {ip}")
    if not success:
        return jsonify({'error': msg}), 500
    return jsonify({'success': True, 'message': msg})

@app.route('/api/bans', methods=['GET'])
@auth.login_required
def get_bans():
    return jsonify({'bans': load_banned_players()})

@app.route('/api/bans', methods=['POST'])
@auth.login_required
@role_required(['root', 'administrator'])
def add_ban_api():
    data = request.json
    uuid = data.get('uuid', '').strip()
    name = data.get('name', '').strip() or "未知"
    reason = data.get('reason', '')
    if not uuid:
        return jsonify({'error': 'UUID 不能为空'}), 400
    success, msg = add_ban(uuid, name, reason, source="面板")
    if not success:
        return jsonify({'error': msg}), 500
    if name != "未知":
        send_stdin_command(f"ban {name}")
    return jsonify({'success': True, 'message': msg})

@app.route('/api/bans/<uuid>', methods=['DELETE'])
@auth.login_required
@role_required(['root', 'administrator'])
def remove_ban_api(uuid):
    success, msg = remove_ban(uuid)
    if not success:
        return jsonify({'error': msg}), 500
    return jsonify({'success': True, 'message': msg})

@app.route('/api/ops', methods=['GET'])
@auth.login_required
def get_ops():
    return jsonify({'ops': load_ops()})

@app.route('/api/ops', methods=['POST'])
@auth.login_required
@role_required(['root', 'administrator'])
def add_op_api():
    data = request.json
    uuid = data.get('uuid', '').strip()
    name = data.get('name', '').strip() or "未知"
    level = data.get('level', 4)
    if not uuid:
        return jsonify({'error': 'UUID 不能为空'}), 400
    success, msg = add_op(uuid, name, level)
    if not success:
        return jsonify({'error': msg}), 500
    if name != "未知":
        send_stdin_command(f"op {name}")
    return jsonify({'success': True, 'message': msg})

@app.route('/api/ops/<uuid>', methods=['DELETE'])
@auth.login_required
@role_required(['root', 'administrator'])
def remove_op_api(uuid):
    ops = load_ops()
    name = next((op.get('name') for op in ops if op.get('uuid') == uuid), None)
    success, msg = remove_op(uuid)
    if not success:
        return jsonify({'error': msg}), 500
    if name:
        send_stdin_command(f"deop {name}")
    return jsonify({'success': True, 'message': msg})

@app.route('/api/server_properties', methods=['GET'])
@auth.login_required
@role_required(['root', 'administrator'])
def get_server_properties():
    content, err = get_server_properties_content()
    if err:
        return jsonify({'error': err}), 500
    return jsonify({'content': content})

@app.route('/api/server_properties', methods=['POST'])
@auth.login_required
@role_required(['root', 'administrator'])
def save_server_properties():
    data = request.json
    content = data.get('content', '')
    success, err = save_server_properties_content(content)
    if not success:
        return jsonify({'error': err}), 500
    return jsonify({'success': True})

@app.route('/api/mods', methods=['GET'])
@auth.login_required
@role_required(['root', 'administrator'])
def get_mods():
    return jsonify({'mods': list_mods()})

@app.route('/api/mods', methods=['POST'])
@auth.login_required
@role_required(['root', 'administrator'])
def upload_mod():
    if 'file' not in request.files:
        return jsonify({'error': '没有上传文件'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '文件名为空'}), 400
    if not file.filename.lower().endswith('.jar'):
        return jsonify({'error': '只允许上传 .jar 模组文件'}), 400
    safe_filename = os.path.basename(file.filename)
    file.save(os.path.join(MODS_FOLDER, safe_filename))
    return jsonify({'success': True, 'filename': safe_filename})

@app.route('/api/mods/batch', methods=['POST'])
@auth.login_required
@role_required(['root', 'administrator'])
def upload_mods_batch():
    if 'files' not in request.files:
        return jsonify({'error': '没有上传文件'}), 400
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': '没有选择文件'}), 400
    uploaded, failed = [], []
    for file in files:
        if file.filename == '':
            continue
        if not file.filename.lower().endswith('.jar'):
            failed.append({'filename': file.filename, 'reason': '不是 .jar 文件'})
            continue
        safe_filename = os.path.basename(file.filename)
        try:
            file.save(os.path.join(MODS_FOLDER, safe_filename))
            uploaded.append(safe_filename)
        except Exception as e:
            failed.append({'filename': safe_filename, 'reason': str(e)})
    return jsonify({'success': True, 'uploaded': uploaded, 'failed': failed})

@app.route('/api/mods/<filename>', methods=['DELETE'])
@auth.login_required
@role_required(['root', 'administrator'])
def delete_mod(filename):
    if '..' in filename or '/' in filename or '\\' in filename:
        return jsonify({'error': '非法文件名'}), 400
    file_path = os.path.join(MODS_FOLDER, filename)
    if not os.path.exists(file_path):
        return jsonify({'error': '文件不存在'}), 404
    os.remove(file_path)
    return jsonify({'success': True})

@app.route('/api/mods/download', methods=['GET'])
@auth.login_required
@role_required(['root', 'administrator'])
def mods_download():
    include_optim = request.args.get('include_optim', 'false').lower() == 'true'
    zip_buffer, err = pack_mods_zip(include_optim=include_optim)
    if err:
        return jsonify({'error': err}), 500
    suffix = "_with_optim" if include_optim else ""
    return send_file(zip_buffer, as_attachment=True,
                     download_name=f'mods{suffix}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip',
                     mimetype='application/zip')

@app.route('/api/optim_mods', methods=['GET'])
@auth.login_required
@role_required(['root', 'administrator'])
def get_optim_mods():
    return jsonify({'mods': list_optim_mods()})

@app.route('/api/optim_mods', methods=['POST'])
@auth.login_required
@role_required(['root', 'administrator'])
def upload_optim_mod():
    if 'file' not in request.files:
        return jsonify({'error': '没有上传文件'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': '文件名为空'}), 400
    if not file.filename.lower().endswith('.jar'):
        return jsonify({'error': '只允许上传 .jar 模组文件'}), 400
    safe_filename = os.path.basename(file.filename)
    file.save(os.path.join(OPTIM_MOD_FOLDER, safe_filename))
    return jsonify({'success': True, 'filename': safe_filename})

@app.route('/api/optim_mods/<filename>', methods=['DELETE'])
@auth.login_required
@role_required(['root', 'administrator'])
def delete_optim_mod(filename):
    if '..' in filename or '/' in filename or '\\' in filename:
        return jsonify({'error': '非法文件名'}), 400
    file_path = os.path.join(OPTIM_MOD_FOLDER, filename)
    if not os.path.exists(file_path):
        return jsonify({'error': '文件不存在'}), 404
    os.remove(file_path)
    return jsonify({'success': True})

@app.route('/api/log', methods=['GET'])
@auth.login_required
def get_log():
    if not os.path.exists(LOG_FILE):
        return jsonify({'error': '日志文件不存在'}), 404
    return send_file(LOG_FILE, as_attachment=True, download_name='latest.log', mimetype='text/plain')

@app.route('/api/analyze_log', methods=['POST'])
@auth.login_required
def analyze_log():
    if not is_llm_configured():
        return jsonify({'error': '未配置 LLM 服务，请通过命令行参数 --local-llm-base-url 和 --local-llm-model 启用'}), 400
    log_text = get_log_content()
    analysis = analyze_with_local_llm(log_text)
    if analysis == "LLM 未配置":
        return jsonify({'error': '未配置 LLM 服务'}), 400
    return jsonify({'analysis': analysis})

@app.route('/api/backup/create', methods=['POST'])
@auth.login_required
@role_required(['root', 'administrator'])
def backup_create():
    backup_name, err = create_backup()
    if err:
        return jsonify({'success': False, 'error': err}), 500
    return jsonify({'success': True, 'backup': backup_name})

@app.route('/api/backup/list', methods=['GET'])
@auth.login_required
@role_required(['root', 'administrator'])
def backup_list():
    return jsonify({'backups': list_backups()})

@app.route('/api/backup/download/<filename>', methods=['GET'])
@auth.login_required
@role_required(['root', 'administrator'])
def backup_download(filename):
    if '..' in filename or not filename.endswith('.zip'):
        return jsonify({'error': '非法文件名'}), 400
    file_path = os.path.join(BACKUP_FOLDER, filename)
    if not os.path.exists(file_path):
        return jsonify({'error': '文件不存在'}), 404
    return send_file(file_path, as_attachment=True, download_name=filename)

@app.route('/api/backup/delete/<filename>', methods=['DELETE'])
@auth.login_required
@role_required(['root', 'administrator'])
def backup_delete(filename):
    if '..' in filename or not filename.endswith('.zip'):
        return jsonify({'error': '非法文件名'}), 400
    if delete_backup(filename):
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '删除失败'}), 500

@app.route('/api/backup/restore/<filename>', methods=['POST'])
@auth.login_required
@role_required(['root', 'administrator'])
def backup_restore(filename):
    if '..' in filename or not filename.endswith('.zip'):
        return jsonify({'error': '非法文件名'}), 400
    success, msg = restore_backup(filename)
    if not success:
        return jsonify({'success': False, 'error': msg}), 500
    return jsonify({'success': True, 'message': msg})

@app.route('/api/config/list', methods=['GET'])
@auth.login_required
@role_required(['root', 'administrator'])
def config_list():
    return jsonify({'files': get_config_files()})

@app.route('/api/config/mtime', methods=['GET'])
@auth.login_required
@role_required(['root', 'administrator'])
def config_mtime():
    filepath = request.args.get('path', '')
    if not filepath or '..' in filepath:
        return jsonify({'error': '无效路径'}), 400
    mtime, err = get_config_mtime(filepath)
    if err:
        return jsonify({'error': err}), 500
    return jsonify({'mtime': mtime})

@app.route('/api/config/get', methods=['GET'])
@auth.login_required
@role_required(['root', 'administrator'])
def config_get():
    filepath = request.args.get('path', '')
    if not filepath or '..' in filepath:
        return jsonify({'error': '无效路径'}), 400
    content, err, mtime = get_config_content(filepath)
    if err:
        return jsonify({'error': err}), 500
    return jsonify({'content': content, 'mtime': mtime})

@app.route('/api/config/save', methods=['POST'])
@auth.login_required
@role_required(['root', 'administrator'])
def config_save():
    data = request.json
    filepath = data.get('path', '')
    content = data.get('content', '')
    if not filepath or '..' in filepath:
        return jsonify({'error': '无效路径'}), 400
    success, err = save_config_content(filepath, content)
    if not success:
        return jsonify({'error': err}), 500
    return jsonify({'success': True})

# ================== 用户管理（仅 root） ==================
@app.route('/api/users', methods=['GET'])
@auth.login_required
@role_required(['root'])
def get_users():
    users = load_users()
    safe_users = {u: {'role': users[u]['role']} for u in users}
    return jsonify({'users': safe_users})

@app.route('/api/users', methods=['POST'])
@auth.login_required
@role_required(['root'])
def add_user():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    role = data.get('role', 'politician')
    if not username or not password:
        return jsonify({'error': '用户名和密码不能为空'}), 400
    if role not in ['administrator', 'politician']:
        return jsonify({'error': '角色必须是 administrator 或 politician'}), 400
    users = load_users()
    if username in users:
        return jsonify({'error': '用户名已存在'}), 400
    users[username] = {'password': password, 'role': role}
    if save_users(users):
        return jsonify({'success': True})
    else:
        return jsonify({'error': '保存失败'}), 500

@app.route('/api/users/<username>', methods=['DELETE'])
@auth.login_required
@role_required(['root'])
def delete_user(username):
    if username == 'root':
        return jsonify({'error': '不能删除 root 用户'}), 400
    users = load_users()
    if username not in users:
        return jsonify({'error': '用户不存在'}), 404
    del users[username]
    if save_users(users):
        return jsonify({'success': True})
    else:
        return jsonify({'error': '保存失败'}), 500

@app.route('/api/users/<username>/password', methods=['PUT'])
@auth.login_required
@role_required(['root'])
def change_user_password(username):
    data = request.json
    new_password = data.get('password', '').strip()
    if not new_password:
        return jsonify({'error': '新密码不能为空'}), 400
    users = load_users()
    if username not in users:
        return jsonify({'error': '用户不存在'}), 404
    users[username]['password'] = new_password
    if save_users(users):
        return jsonify({'success': True})
    else:
        return jsonify({'error': '保存失败'}), 500

# ================== 访客路由 ==================
@app.route('/guests')
def guests_index():
    return render_template_string(GUESTS_TEMPLATE)

@app.route('/api/guests/players')
def guests_get_players():
    return jsonify({'players': get_current_online_players()})

@app.route('/api/guests/mods/download')
def guests_mods_download():
    include_optim = request.args.get('include_optim', 'false').lower() == 'true'
    zip_buffer, err = pack_mods_zip(include_optim=include_optim)
    if err:
        return jsonify({'error': err}), 500
    suffix = "_with_optim" if include_optim else ""
    return send_file(zip_buffer, as_attachment=True,
                     download_name=f'mods{suffix}_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip',
                     mimetype='application/zip')

# ================== 前端模板（管理面板） ==================
HTML_TEMPLATE = r'''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Minecraft 服务器管理器</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { padding-top: 2rem; background-color: #f8f9fa; }
        .container { max-width: 1600px; }
        .card { margin-bottom: 1.5rem; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .mod-list, .backup-list, .config-list, .ban-list, .op-list { max-height: 300px; overflow-y: auto; }
        .analysis-result { white-space: pre-wrap; background-color: #f1f1f1; padding: 1rem; border-radius: 5px; }
        .console-box { background-color: #000; color: #0f0; font-family: monospace; height: 400px; overflow-y: auto; padding: 10px; border-radius: 5px; }
        .status-indicator { display: inline-block; width: 12px; height: 12px; border-radius: 50%; margin-right: 5px; }
        .status-running { background-color: #28a745; }
        .status-stopped { background-color: #dc3545; }
        .status-starting, .status-stopping { background-color: #ffc107; }
        .config-editor { font-family: monospace; }
        .player-table { font-size: 0.9rem; }
        .search-box { margin-bottom: 10px; }
    </style>
</head>
<body>
<div class="container">
    <h1 class="mb-4">📦 Minecraft 服务器管理器</h1>

    <!-- 服务器控制卡片 -->
    <div class="card">
        <div class="card-header bg-dark text-white">🖥️ 服务器控制</div>
        <div class="card-body">
            <div class="row align-items-center">
                <div class="col-md-4">
                    <h5>状态: <span id="statusText">未知</span> <span id="statusIndicator" class="status-indicator"></span></h5>
                    <p>PID: <span id="pid">-</span></p>
                </div>
                <div class="col-md-8 text-end">
                    <button id="startBtn" class="btn btn-success">▶ 启动</button>
                    <button id="stopBtn" class="btn btn-danger">⏹️ 停止</button>
                    <button id="restartBtn" class="btn btn-warning">🔄 重启</button>
                </div>
            </div>
        </div>
    </div>

    <!-- 在线玩家卡片 -->
    <div class="card">
        <div class="card-header bg-primary text-white">👥 在线玩家</div>
        <div class="card-body">
            <div class="search-box"><input type="text" id="playerSearch" class="form-control" placeholder="搜索玩家名或 UUID..."></div>
            <div class="table-responsive">
                <table class="table table-striped table-hover player-table">
                    <thead><tr><th>玩家名</th><th>UUID</th><th>IP地址</th><th>操作</th></tr></thead>
                    <tbody id="playerTableBody"><tr><td colspan="4" class="text-center">暂无玩家在线</td></tr></tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- 权限管理卡片 -->
    <div class="card">
        <div class="card-header bg-danger text-white">🚫 权限管理</div>
        <div class="card-body">
            <div class="row">
                <div class="col-md-6">
                    <h5>📛 黑名单</h5>
                    <div class="search-box"><input type="text" id="banSearch" class="form-control" placeholder="搜索玩家名或 UUID..."></div>
                    <ul id="banList" class="list-group ban-list"></ul>
                    <hr>
                    <div id="banAddPanel"><input type="text" id="banUuid" class="form-control mb-1" placeholder="玩家 UUID（必填）"><input type="text" id="banName" class="form-control mb-1" placeholder="玩家名（可选）"><input type="text" id="banReason" class="form-control mb-1" placeholder="封禁原因（可选）"><button id="addBanBtn" class="btn btn-danger btn-sm">➕ 添加封禁</button></div>
                </div>
                <div class="col-md-6">
                    <h5>⭐ OP 名单</h5>
                    <div class="search-box"><input type="text" id="opSearch" class="form-control" placeholder="搜索玩家名或 UUID..."></div>
                    <ul id="opList" class="list-group op-list"></ul>
                    <hr>
                    <div id="opAddPanel"><input type="text" id="opUuid" class="form-control mb-1" placeholder="玩家 UUID（必填）"><input type="text" id="opName" class="form-control mb-1" placeholder="玩家名（可选）"><select id="opLevel" class="form-select mb-1"><option value="4">OP 等级 4</option><option value="3">OP 等级 3</option><option value="2">OP 等级 2</option><option value="1">OP 等级 1</option></select><button id="addOpBtn" class="btn btn-primary btn-sm">➕ 添加 OP</button></div>
                </div>
            </div>
        </div>
    </div>

    <!-- 用户管理卡片（仅 root 可见） -->
    <div id="userManagementCard" class="card" style="display: none;">
        <div class="card-header bg-dark text-white">👥 用户管理</div>
        <div class="card-body">
            <div class="row">
                <div class="col-md-6">
                    <h5>现有用户</h5>
                    <ul id="userList" class="list-group"></ul>
                </div>
                <div class="col-md-6">
                    <h5>添加新用户</h5>
                    <input type="text" id="newUsername" class="form-control mb-1" placeholder="用户名">
                    <input type="password" id="newPassword" class="form-control mb-1" placeholder="密码">
                    <select id="newRole" class="form-select mb-1">
                        <option value="politician">Politician</option>
                        <option value="administrator">Administrator</option>
                    </select>
                    <button id="addUserBtn" class="btn btn-primary btn-sm">➕ 添加用户</button>
                </div>
            </div>
        </div>
    </div>

    <!-- 高级模块（root 和 administrator 可见） -->
    <div id="advancedModules" style="display: none;">
        <div class="card"><div class="card-header bg-info text-white">📺 控制台输出 (实时)</div><div class="card-body"><div id="console" class="console-box"></div></div></div>
        <div class="card"><div class="card-header bg-primary text-white">💬 执行服务器指令</div><div class="card-body"><div class="input-group mb-3"><input type="text" id="commandInput" class="form-control" placeholder="例如 /say Hello"><button id="sendCommandBtn" class="btn btn-primary">发送</button></div><div id="commandResult" class="alert alert-secondary mt-2" style="display:none;"></div></div></div>
        <div class="card"><div class="card-header bg-success text-white">🧩 模组管理</div><div class="card-body"><div class="row"><div class="col-md-6"><h5>已安装模组</h5><ul id="modList" class="list-group mod-list"></ul></div><div class="col-md-6"><h5>上传新模组</h5><form id="uploadForm" enctype="multipart/form-data"><input type="file" class="form-control mb-2" name="file" accept=".jar" required><button type="submit" class="btn btn-success">上传</button><button type="button" id="downloadModsBtn" class="btn btn-secondary ms-2">📦 打包下载所有模组</button></form><hr><h5>批量上传模组</h5><form id="batchUploadForm" enctype="multipart/form-data"><input type="file" class="form-control mb-2" name="files" accept=".jar" multiple required><button type="submit" class="btn btn-primary">🚀 批量上传</button><div id="batchUploadProgress" class="mt-2" style="display:none;"><div class="spinner-border spinner-border-sm"></div> 上传中...</div><div id="batchUploadResult" class="mt-2"></div></form></div></div><hr><div class="row mt-3"><div class="col-md-6"><h5>优化模组</h5><ul id="optimModList" class="list-group mod-list"></ul></div><div class="col-md-6"><h5>上传优化模组</h5><form id="uploadOptimForm" enctype="multipart/form-data"><input type="file" class="form-control mb-2" name="file" accept=".jar" required><button type="submit" class="btn btn-warning">上传优化模组</button></form></div></div></div></div>
        <div class="card"><div class="card-header bg-secondary text-white">⚙️ server.properties 编辑</div><div class="card-body"><textarea id="serverPropsEditor" class="form-control config-editor" rows="15" style="font-family:monospace;"></textarea><button id="saveServerPropsBtn" class="btn btn-primary mt-2">保存修改</button><div id="serverPropsSaveMsg" class="mt-2"></div><div class="alert alert-info mt-2">修改后需要重启服务器生效。</div></div></div>
        <div class="card"><div class="card-header bg-secondary text-white">💾 存档备份</div><div class="card-body"><div class="row"><div class="col-md-6"><button id="createBackupBtn" class="btn btn-primary">📀 立即备份世界</button></div><div class="col-md-6"><ul id="backupList" class="list-group backup-list"></ul></div></div></div></div>
        <div class="card"><div class="card-header bg-warning text-dark">⚙️ 模组配置文件修改</div><div class="card-body"><div class="row"><div class="col-md-4"><ul id="configFileList" class="list-group config-list"></ul></div><div class="col-md-8"><textarea id="configEditor" class="form-control config-editor" rows="15" style="font-family:monospace;"></textarea><div class="mt-2"><button id="saveConfigBtn" class="btn btn-primary" style="display:none;">保存修改</button><button id="refreshConfigBtn" class="btn btn-secondary" style="display:none;">刷新</button></div><div id="configSaveMsg" class="mt-2"></div></div></div></div></div>
        <div class="card"><div class="card-header bg-info text-white">📄 日志与 AI 分析</div><div class="card-body"><div class="mb-3"><a href="/api/log" class="btn btn-secondary" download>📥 下载最新日志</a><button id="analyzeLogBtn" class="btn btn-warning ms-2">🤖 调用本地 AI 分析日志</button></div><div id="analysisResult" class="analysis-result" style="display:none;"><strong>分析结果：</strong><div id="analysisText"></div></div><div id="analysisLoading" style="display:none;" class="text-center mt-3"><div class="spinner-border text-primary"></div><p>AI 正在分析，请稍候...</p></div></div></div>
    </div>
</div>

<script>
    let currentPlayers = [], currentBans = [], currentOps = [];
    let userRole = '{{ role }}';
    let isAdmin = (userRole === 'root' || userRole === 'administrator');
    let isRoot = (userRole === 'root');

    function applyRoleVisibility() {
        if (isRoot) document.getElementById('userManagementCard').style.display = 'block';
        if (isAdmin) document.getElementById('advancedModules').style.display = 'block';
    }

    function escapeHtml(s){return s.replace(/[&<>]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;'})[m]);}

    async function updateStatus(){const d=await(await fetch('/api/server/status')).json(); document.getElementById('statusText').innerText=d.status; const ind=document.getElementById('statusIndicator'); ind.className='status-indicator'; if(d.status==='running')ind.classList.add('status-running'); else if(d.status==='stopped')ind.classList.add('status-stopped'); else ind.classList.add('status-starting'); document.getElementById('pid').innerText=d.pid||'-';}
    async function loadPlayers(){const d=await(await fetch('/api/players')).json(); currentPlayers=d.players||[]; filterPlayers();}
    function filterPlayers(){const kw=document.getElementById('playerSearch').value.toLowerCase(); const f=currentPlayers.filter(p=>p.name.toLowerCase().includes(kw)||p.uuid.toLowerCase().includes(kw)); const tbody=document.getElementById('playerTableBody'); if(!f.length){tbody.innerHTML='<tr><td colspan="4" class="text-center">暂无匹配玩家</td></tr>'; return;} let html=''; for(let p of f){ let btns=''; if(isAdmin) btns=`<button class="btn btn-sm btn-warning" onclick="kickPlayer('${escapeHtml(p.name)}')">踢出</button> <button class="btn btn-sm btn-danger" onclick="banPlayer('${escapeHtml(p.name)}')">封禁</button> <button class="btn btn-sm btn-danger" onclick="banIP('${escapeHtml(p.name)}','${escapeHtml(p.ip)}')">封禁IP</button> <button class="btn btn-sm btn-info" onclick="addOpFromOnline('${escapeHtml(p.name)}','${escapeHtml(p.uuid)}')">设为OP</button>`; else btns='仅查看'; html+=`<tr><td>${escapeHtml(p.name)}</td><td>${escapeHtml(p.uuid)}</td><td>${escapeHtml(p.ip)}</td><td>${btns}</td></tr>`;} tbody.innerHTML=html;}
    async function kickPlayer(p){if(!confirm(`踢出 ${p}？`))return; const d=await(await fetch('/api/players/kick',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({player:p})})).json(); alert(d.success?'踢出成功':'错误:'+d.error); setTimeout(loadPlayers,2000);}
    async function banPlayer(p){if(!confirm(`封禁 ${p}？`))return; const d=await(await fetch('/api/players/ban',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({player:p})})).json(); alert(d.success?'封禁成功':'错误:'+d.error); loadBans(); setTimeout(loadPlayers,2000);}
    async function banIP(p,ip){if(ip==='未知'||!ip){alert('无法获取IP');return;} if(!confirm(`封禁IP ${ip}？`))return; const d=await(await fetch('/api/players/banip',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ip})})).json(); alert(d.success?'封禁IP成功':'错误:'+d.error); setTimeout(loadPlayers,2000);}
    async function addOpFromOnline(name,uuid){if(!confirm(`将 ${name} 设为 OP？`))return; const d=await(await fetch('/api/ops',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({uuid,name,level:4})})).json(); alert(d.success?'添加 OP 成功':'添加失败:'+d.error); loadOps();}
    async function loadBans(){const d=await(await fetch('/api/bans')).json(); currentBans=d.bans||[]; filterBans();}
    function filterBans(){const kw=document.getElementById('banSearch').value.toLowerCase(); const f=currentBans.filter(b=>b.name.toLowerCase().includes(kw)||b.uuid.toLowerCase().includes(kw)); const list=document.getElementById('banList'); if(!f.length){list.innerHTML='<li class="list-group-item text-muted">暂无封禁记录</li>'; return;} list.innerHTML=f.map(b=>`<li class="list-group-item d-flex justify-content-between align-items-start"><div><strong>${escapeHtml(b.name)}</strong><br>UUID: ${escapeHtml(b.uuid)}<br>原因: ${escapeHtml(b.reason)}<br>时间: ${new Date(b.created).toLocaleString()}</div>${isAdmin?`<button class="btn btn-sm btn-danger" onclick="removeBan('${b.uuid}')">解封</button>`:'仅查看'}</li>`).join('');}
    window.removeBan=async(uuid)=>{if(!confirm('确定解封？'))return; const d=await(await fetch(`/api/bans/${uuid}`,{method:'DELETE'})).json(); if(d.success){alert('解封成功'); loadBans();}else alert('解封失败:'+d.error);};
    async function loadOps(){const d=await(await fetch('/api/ops')).json(); currentOps=d.ops||[]; filterOps();}
    function filterOps(){const kw=document.getElementById('opSearch').value.toLowerCase(); const f=currentOps.filter(o=>o.name.toLowerCase().includes(kw)||o.uuid.toLowerCase().includes(kw)); const list=document.getElementById('opList'); if(!f.length){list.innerHTML='<li class="list-group-item text-muted">暂无 OP</li>'; return;} list.innerHTML=f.map(o=>`<li class="list-group-item d-flex justify-content-between align-items-start"><div><strong>${escapeHtml(o.name)}</strong><br>UUID: ${escapeHtml(o.uuid)}<br>等级: ${o.level}</div>${isAdmin?`<button class="btn btn-sm btn-danger" onclick="removeOp('${o.uuid}')">移除OP</button>`:'仅查看'}</li>`).join('');}
    window.removeOp=async(uuid)=>{if(!confirm('确定移除OP？'))return; const d=await(await fetch(`/api/ops/${uuid}`,{method:'DELETE'})).json(); if(d.success){alert('移除成功'); loadOps();}else alert('移除失败:'+d.error);};

    // 用户管理
    async function loadUsers(){if(!isRoot)return; const resp=await fetch('/api/users'); const data=await resp.json(); const list=document.getElementById('userList'); list.innerHTML=''; for(const [uname,info] of Object.entries(data.users)){const li=document.createElement('li'); li.className='list-group-item d-flex justify-content-between align-items-center'; li.innerHTML=`${uname} (${info.role}) <div><button class="btn btn-sm btn-danger" onclick="deleteUser('${uname}')">删除</button> <button class="btn btn-sm btn-secondary" onclick="changePassword('${uname}')">改密码</button></div>`; list.appendChild(li);}}
    window.deleteUser=async(username)=>{if(!confirm(`删除用户 ${username}？`))return; const resp=await fetch(`/api/users/${username}`,{method:'DELETE'}); const data=await resp.json(); if(data.success){alert('删除成功'); loadUsers();}else alert('删除失败:'+data.error);};
    window.changePassword=async(username)=>{const newPass=prompt('输入新密码：'); if(!newPass)return; const resp=await fetch(`/api/users/${username}/password`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:newPass})}); const data=await resp.json(); if(data.success)alert('密码修改成功'); else alert('修改失败:'+data.error);};
    document.getElementById('addUserBtn').onclick=async()=>{const username=document.getElementById('newUsername').value.trim(); const password=document.getElementById('newPassword').value.trim(); const role=document.getElementById('newRole').value; if(!username||!password){alert('用户名和密码不能为空');return;} const resp=await fetch('/api/users',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username,password,role})}); const data=await resp.json(); if(data.success){alert('添加成功'); loadUsers(); document.getElementById('newUsername').value=''; document.getElementById('newPassword').value='';}else alert('添加失败:'+data.error);};

    // 其他高级功能函数（仅当 isAdmin 时存在元素）
    async function loadMods(){if(!isAdmin)return; const d=await(await fetch('/api/mods')).json(); const list=document.getElementById('modList'); if(!list)return; list.innerHTML=''; d.mods.forEach(mod=>{const li=document.createElement('li'); li.className='list-group-item d-flex justify-content-between align-items-center'; li.innerHTML=`${mod} <button class="btn btn-sm btn-danger" onclick="deleteMod('${mod}')">删除</button>`; list.appendChild(li);});}
    window.deleteMod=async(filename)=>{if(confirm(`删除模组 ${filename}？`)){const resp=await fetch(`/api/mods/${filename}`,{method:'DELETE'}); if(resp.ok)loadMods(); else alert('删除失败');}};
    document.getElementById('uploadForm') && document.getElementById('uploadForm').addEventListener('submit',async(e)=>{e.preventDefault(); const fd=new FormData(e.target); const resp=await fetch('/api/mods',{method:'POST',body:fd}); if(resp.ok){alert('上传成功'); loadMods(); e.target.reset();}else alert('上传失败');});
    document.getElementById('batchUploadForm') && document.getElementById('batchUploadForm').addEventListener('submit',async(e)=>{e.preventDefault(); const fd=new FormData(e.target); const progress=document.getElementById('batchUploadProgress'); const resultDiv=document.getElementById('batchUploadResult'); progress.style.display='block'; resultDiv.innerHTML=''; try{const resp=await fetch('/api/mods/batch',{method:'POST',body:fd}); const data=await resp.json(); if(data.success){let msg=`<div class="alert alert-success">上传完成！成功:${data.uploaded.length},失败:${data.failed.length}</div>`; if(data.uploaded.length)msg+=`<div><strong>成功：</strong>${data.uploaded.join(', ')}</div>`; if(data.failed.length)msg+=`<div><strong>失败：</strong><ul>${data.failed.map(f=>`<li>${f.filename} - ${f.reason}</li>`).join('')}</ul></div>`; resultDiv.innerHTML=msg; loadMods(); e.target.reset();}else resultDiv.innerHTML=`<div class="alert alert-danger">上传失败:${data.error}</div>`;}catch(err){resultDiv.innerHTML=`<div class="alert alert-danger">请求失败:${err}</div>`;}finally{progress.style.display='none';}});
    async function loadOptimMods(){if(!isAdmin)return; const d=await(await fetch('/api/optim_mods')).json(); const list=document.getElementById('optimModList'); if(!list)return; list.innerHTML=''; d.mods.forEach(mod=>{const li=document.createElement('li'); li.className='list-group-item d-flex justify-content-between align-items-center'; li.innerHTML=`${mod} <button class="btn btn-sm btn-danger" onclick="deleteOptimMod('${mod}')">删除</button>`; list.appendChild(li);});}
    window.deleteOptimMod=async(filename)=>{if(confirm(`删除优化模组 ${filename}？`)){const resp=await fetch(`/api/optim_mods/${filename}`,{method:'DELETE'}); if(resp.ok)loadOptimMods(); else alert('删除失败');}};
    document.getElementById('uploadOptimForm') && document.getElementById('uploadOptimForm').addEventListener('submit',async(e)=>{e.preventDefault(); const fd=new FormData(e.target); const resp=await fetch('/api/optim_mods',{method:'POST',body:fd}); if(resp.ok){alert('上传成功'); loadOptimMods(); e.target.reset();}else alert('上传失败');});
    document.getElementById('downloadModsBtn') && (document.getElementById('downloadModsBtn').onclick=()=>{const includeOptim=confirm('是否将优化模组也一起打包？'); window.location.href='/api/mods/download?include_optim='+(includeOptim?'true':'false');});
    async function loadServerProperties(){if(!isAdmin)return; const d=await(await fetch('/api/server_properties')).json(); if(d.error)document.getElementById('serverPropsEditor').value='加载失败:'+d.error; else document.getElementById('serverPropsEditor').value=d.content;}
    document.getElementById('saveServerPropsBtn') && (document.getElementById('saveServerPropsBtn').onclick=async()=>{const content=document.getElementById('serverPropsEditor').value; const d=await(await fetch('/api/server_properties',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content})})).json(); const msgDiv=document.getElementById('serverPropsSaveMsg'); msgDiv.innerHTML=d.success?'<div class="alert alert-success">保存成功！请重启服务器生效。</div>':'<div class="alert alert-danger">保存失败:'+d.error+'</div>';});
    async function loadBackups(){if(!isAdmin)return; const d=await(await fetch('/api/backup/list')).json(); const list=document.getElementById('backupList'); if(!list)return; list.innerHTML=''; d.backups.forEach(b=>{const sizeMB=(b.size/1024/1024).toFixed(2); const li=document.createElement('li'); li.className='list-group-item d-flex justify-content-between align-items-center'; li.innerHTML=`${b.name} (${sizeMB} MB) <div><a href="/api/backup/download/${b.name}" class="btn btn-sm btn-info" download>下载</a> <button class="btn btn-sm btn-danger" onclick="deleteBackup('${b.name}')">删除</button> <button class="btn btn-sm btn-warning" onclick="restoreBackup('${b.name}')">恢复</button></div>`; list.appendChild(li);});}
    window.deleteBackup=async(filename)=>{if(confirm(`删除备份 ${filename}？`)){const resp=await fetch(`/api/backup/delete/${filename}`,{method:'DELETE'}); if(resp.ok)loadBackups(); else alert('删除失败');}};
    window.restoreBackup=async(filename)=>{if(confirm(`⚠️ 恢复操作将覆盖当前世界，服务器将自动重启。确定？`)){const btn=event.target; const orig=btn.innerText; btn.innerText='恢复中...'; btn.disabled=true; try{const resp=await fetch(`/api/backup/restore/${filename}`,{method:'POST'}); const d=await resp.json(); if(d.success){alert('恢复成功！服务器已重启。'); updateStatus(); loadPlayers();}else alert('恢复失败:'+d.error);}catch(err){alert('请求失败:'+err);}finally{btn.innerText=orig; btn.disabled=false; loadBackups();}}};
    document.getElementById('createBackupBtn') && (document.getElementById('createBackupBtn').onclick=async()=>{const d=await(await fetch('/api/backup/create',{method:'POST'})).json(); if(d.success){alert('备份创建成功:'+d.backup); loadBackups();}else alert('备份失败:'+d.error);});
    let configCache={},currentConfigFile=null;
    async function loadConfigFiles(){if(!isAdmin)return; const d=await(await fetch('/api/config/list')).json(); const list=document.getElementById('configFileList'); if(!list)return; list.innerHTML=''; d.files.forEach(f=>{const li=document.createElement('li'); li.className='list-group-item list-group-item-action'; li.textContent=f; li.onclick=()=>loadConfigFile(f); list.appendChild(li);});}
    async function loadConfigFile(fp,force=false){if(!isAdmin)return; currentConfigFile=fp; const editor=document.getElementById('configEditor'); const saveBtn=document.getElementById('saveConfigBtn'); const refreshBtn=document.getElementById('refreshConfigBtn'); if(!editor)return; editor.value='加载中...'; saveBtn.style.display='none'; refreshBtn.style.display='none'; try{if(!force&&configCache[fp]){const mt=await fetch(`/api/config/mtime?path=${encodeURIComponent(fp)}`); const mtData=await mt.json(); if(!mtData.error&&mtData.mtime===configCache[fp].mtime){editor.value=configCache[fp].content; saveBtn.style.display='inline-block'; refreshBtn.style.display='inline-block'; return;}} const resp=await fetch(`/api/config/get?path=${encodeURIComponent(fp)}`); const data=await resp.json(); if(data.error){editor.value='加载失败:'+data.error; return;} editor.value=data.content; configCache[fp]={content:data.content,mtime:data.mtime}; saveBtn.style.display='inline-block'; refreshBtn.style.display='inline-block';}catch(err){editor.value='请求失败:'+err;}}
    document.getElementById('refreshConfigBtn') && (document.getElementById('refreshConfigBtn').onclick=async()=>{if(currentConfigFile){delete configCache[currentConfigFile]; await loadConfigFile(currentConfigFile,true);}});
    document.getElementById('saveConfigBtn') && (document.getElementById('saveConfigBtn').onclick=async()=>{if(!currentConfigFile)return; const content=document.getElementById('configEditor').value; const data=await(await fetch('/api/config/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:currentConfigFile,content})})).json(); const msgDiv=document.getElementById('configSaveMsg'); if(data.success){msgDiv.innerHTML='<div class="alert alert-success">保存成功！请重启服务器生效。</div>'; if(configCache[currentConfigFile]){configCache[currentConfigFile].content=content; const mt=await fetch(`/api/config/mtime?path=${encodeURIComponent(currentConfigFile)}`); const mtData=await mt.json(); if(!mtData.error)configCache[currentConfigFile].mtime=mtData.mtime;}}else msgDiv.innerHTML='<div class="alert alert-danger">保存失败:'+data.error+'</div>';});
    document.getElementById('analyzeLogBtn') && (document.getElementById('analyzeLogBtn').onclick=async()=>{const loading=document.getElementById('analysisLoading'); const resultDiv=document.getElementById('analysisResult'); const textDiv=document.getElementById('analysisText'); loading.style.display='block'; resultDiv.style.display='none'; try{const resp=await fetch('/api/analyze_log',{method:'POST'}); const data=await resp.json(); if(data.error){alert(data.error); return;} textDiv.textContent=data.analysis; resultDiv.style.display='block';}catch(err){alert('分析失败:'+err);}finally{loading.style.display='none';}});
    document.getElementById('sendCommandBtn') && (document.getElementById('sendCommandBtn').onclick=async()=>{const cmd=document.getElementById('commandInput').value.trim(); if(!cmd)return alert('请输入指令'); const resultDiv=document.getElementById('commandResult'); resultDiv.style.display='block'; resultDiv.textContent='发送中...'; try{const resp=await fetch('/api/command',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({command:cmd})}); const data=await resp.json(); if(data.error){resultDiv.textContent='错误:'+data.error; resultDiv.className='alert alert-danger';}else{resultDiv.textContent='命令已发送'; resultDiv.className='alert alert-success';}}catch(err){resultDiv.textContent='请求失败:'+err; resultDiv.className='alert alert-danger';}});
    const consoleDiv=document.getElementById('console'); if(consoleDiv){const evtSource=new EventSource('/api/console/stream'); evtSource.onmessage=e=>{consoleDiv.innerHTML+=e.data+'<br>'; consoleDiv.scrollTop=consoleDiv.scrollHeight;}; evtSource.addEventListener('close',e=>{consoleDiv.innerHTML+='[系统] '+e.data+'<br>'; evtSource.close();}); evtSource.onerror=err=>console.error("SSE 错误:",err);}
    document.getElementById('startBtn').onclick=async()=>{const d=await(await fetch('/api/server/start',{method:'POST'})).json(); alert(d.message); updateStatus();};
    document.getElementById('stopBtn').onclick=async()=>{const d=await(await fetch('/api/server/stop',{method:'POST'})).json(); alert(d.message); updateStatus();};
    document.getElementById('restartBtn').onclick=async()=>{const d=await(await fetch('/api/server/restart',{method:'POST'})).json(); alert(d.message); updateStatus();};
    applyRoleVisibility();
    updateStatus();
    loadPlayers();
    loadBans();
    loadOps();
    if(isAdmin){ loadMods(); loadOptimMods(); loadBackups(); loadConfigFiles(); loadServerProperties(); }
    if(isRoot) loadUsers();
    setInterval(updateStatus,3000);
</script>
</body>
</html>
'''

# ================== 访客页面模板 ==================
GUESTS_TEMPLATE = r'''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Minecraft 服务器 - 访客页面</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>body{padding-top:2rem;background-color:#f8f9fa}.container{max-width:800px}.card{margin-bottom:1.5rem}.player-list{max-height:300px;overflow-y:auto}</style>
</head>
<body>
<div class="container">
    <h1 class="mb-4">🎮 Minecraft 服务器 - 访客入口</h1>
    <div class="card">
        <div class="card-header bg-primary text-white">📦 模组下载</div>
        <div class="card-body text-center">
            <div class="form-check mb-3"><input class="form-check-input" type="checkbox" id="includeOptimCheckbox"> <label class="form-check-label" for="includeOptimCheckbox">包含优化模组</label></div>
            <a id="downloadModsLink" href="/api/guests/mods/download" class="btn btn-success btn-lg">⬇️ 下载服务端模组包</a>
            <p class="mt-2 text-muted">包含服务器当前使用的所有模组（可选优化模组）</p>
        </div>
    </div>
    <div class="card"><div class="card-header bg-info text-white">👥 在线玩家</div><div class="card-body"><ul id="playerList" class="list-group player-list"><li class="list-group-item text-center">加载中...</li></ul></div></div>
</div>
<script>
    async function loadPlayers(){try{const resp=await fetch('/api/guests/players'); const data=await resp.json(); const list=document.getElementById('playerList'); if(!data.players||data.players.length===0){list.innerHTML='<li class="list-group-item text-center">暂无玩家在线</li>'; return;} list.innerHTML=data.players.map(name=>`<li class="list-group-item">${escapeHtml(name)}</li>`).join('');}catch(err){console.error(err); document.getElementById('playerList').innerHTML='<li class="list-group-item text-danger">加载失败</li>';}}
    function escapeHtml(s){return s.replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'})[m]);}
    const cb=document.getElementById('includeOptimCheckbox'); const link=document.getElementById('downloadModsLink'); function updateLink(){link.href='/api/guests/mods/download?include_optim='+(cb.checked?'true':'false');} cb.addEventListener('change',updateLink); updateLink(); loadPlayers(); setInterval(loadPlayers,5000);
</script>
</body>
</html>
'''

# ================== 命令行参数解析 ==================
def parse_args():
    parser = argparse.ArgumentParser(description='Minecraft 服务器管理面板')
    parser.add_argument('--server-path', default=os.getcwd(), help='服务器工作目录')
    parser.add_argument('--start-command', default='java -Xmx2G -Xms1G -jar server.jar nogui', help='启动命令')
    parser.add_argument('--world-folder', default='world', help='世界文件夹名称')
    parser.add_argument('--local-llm-base-url', default='http://localhost:8000/v1', help='本地 LLM 服务地址（可选）')
    parser.add_argument('--local-llm-model', default='mc-analyst-v1', help='本地 LLM 模型名称（可选）')
    parser.add_argument('--local-llm-api-key', default='', help='本地 LLM API Key（可选）')
    parser.add_argument('--root-password', required=True, help='root 用户密码（必填）')
    parser.add_argument('--host', default='0.0.0.0', help='监听地址')
    parser.add_argument('--port', type=int, default=5000, help='监听端口')
    parser.add_argument('--debug', action='store_true', help='调试模式')
    return parser.parse_args()

if __name__ == '__main__':
    args = parse_args()
    SERVER_PATH = args.server_path
    START_COMMAND = args.start_command
    WORLD_FOLDER = args.world_folder
    LOCAL_LLM_BASE_URL = args.local_llm_base_url
    LOCAL_LLM_MODEL = args.local_llm_model
    LOCAL_LLM_API_KEY = args.local_llm_api_key
    ROOT_PASSWORD = args.root_password

    # 打印启动配置
    print("=" * 50)
    print("Minecraft 服务器管理面板启动")
    print(f"服务器目录: {SERVER_PATH}")
    print(f"root 密码: {'*' * len(ROOT_PASSWORD)}")
    print(f"本地 LLM: {LOCAL_LLM_BASE_URL} (模型: {LOCAL_LLM_MODEL})")
    print(f"LLM 启用: {'是' if is_llm_configured() else '否'}")
    print("=" * 50)

    MODS_FOLDER = os.path.join(SERVER_PATH, 'mods')
    LOG_FILE = os.path.join(SERVER_PATH, 'logs', 'latest.log')
    CONSOLE_LOG = os.path.join(SERVER_PATH, 'logs', 'console.log')
    BACKUP_FOLDER = os.path.join(SERVER_PATH, 'backups')
    CONFIG_FOLDER = os.path.join(SERVER_PATH, 'config')
    OPTIM_MOD_FOLDER = os.path.join(os.path.dirname(__file__), 'optimmod')
    SERVER_PROPERTIES_FILE = os.path.join(SERVER_PATH, 'server.properties')
    BANNED_PLAYERS_FILE = os.path.join(SERVER_PATH, 'banned-players.json')
    OPS_FILE = os.path.join(SERVER_PATH, 'ops.json')

    for p in [MODS_FOLDER, BACKUP_FOLDER, CONFIG_FOLDER, OPTIM_MOD_FOLDER]:
        Path(p).mkdir(parents=True, exist_ok=True)
    Path(LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

    app.run(host=args.host, port=args.port, debug=args.debug)
