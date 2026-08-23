import os
import sys
import json
import ast
import importlib.util
import time
import uuid
import signal
import shutil
import zipfile
import subprocess
import html
import hashlib
import socket
import re
import urllib.request
import urllib.parse
import urllib.error

import telebot
from telebot import types

# ================= HARDCODED CONFIG (edit here) =================

BOT_TOKEN = "8924492918:AAEiJafRy_Xi0Hz4XyCFOSZ-Or70AY_0jdI"

# Your Telegram numeric user id (get from @userinfobot). Only this id can control the bot.
ADMIN_ID = 8600328303

# Where hosted projects live (relative to bot.py, auto-created)
STORAGE_DIR = "hosted_projects"

# Where state (pids, entry files, etc) is saved
DATA_FILE = "data.json"
GITHUB_TOKEN_FILE = ".github_token"

# Max upload size accepted (Telegram bot API hard cap is 20MB for bots anyway)
MAX_FILE_MB = 20

# Python binary used to run hosted projects (Termux default is python3)
PYTHON_BIN = "python3"

# Common entry-file names to auto-detect, in priority order
ENTRY_CANDIDATES = [
    "main.py", "bot.py", "app.py", "run.py", "server.py", "start.py",
    "index.js", "server.js", "app.js", "start.js", "index.mjs", "server.mjs", "app.mjs",
]

# API hosting defaults. Projects that read PORT will receive an available port.
DEFAULT_API_PORT = 8000
API_PORT_RANGE = range(8000, 9000)
PORT_DETECT_SECONDS = 8
# Optional template for a real public URL, e.g. "https://example.com:{port}".
# Leave empty to show the local URL only.
PUBLIC_URL_TEMPLATE = ""
ENABLE_CLOUDFLARED_TUNNEL = True

# ===================================================================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_PATH = os.path.join(BASE_DIR, STORAGE_DIR)
DATA_PATH = os.path.join(BASE_DIR, DATA_FILE)

os.makedirs(STORAGE_PATH, exist_ok=True)

# in-memory: waiting for user input
awaiting_entry = {}
awaiting_github = set()
awaiting_github_token = set()


def admin_ids(data=None):
    data = data or load_data()
    return {ADMIN_ID, *[int(uid) for uid in data.get("admins", []) if str(uid).isdigit()]}


def hosting_user_ids(data=None):
    data = data or load_data()
    return {int(uid) for uid in data.get("hosting_users", []) if str(uid).isdigit()}

# ---------------- data.json helpers ----------------

def load_data():
    if not os.path.exists(DATA_PATH):
        return {"projects": {}}
    try:
        with open(DATA_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"projects": {}}


def save_data(data):
    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2)


def is_admin(uid):
    return uid in admin_ids()


def can_host(uid):
    return is_admin(uid) or uid in hosting_user_ids()


# ---------------- process helpers ----------------

def is_alive(pid):
    """Return True only when pid belongs to a live, non-zombie process."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError, TypeError):
        return False

    # kill(pid, 0) also succeeds for zombie processes. Those have already
    # exited and must not be shown as running in the Telegram UI.
    stat_path = f"/proc/{int(pid)}/stat"
    try:
        stat = open(stat_path, "r").read()
        state = stat.rsplit(") ", 1)[1].split()[0]
        return state != "Z"
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError, OSError):
        return False


def read_log_tail(proj_id, max_chars=3500):
    path = log_path(proj_id)
    if not os.path.exists(path):
        return "(no log file yet)"
    try:
        with open(path, "r", errors="ignore") as f:
            text = "".join(f.readlines()[-80:]).strip()
        if not text:
            return "(empty)"
        return text[-max_chars:]
    except OSError as exc:
        return f"Unable to read log: {exc}"


def project_path(pid):
    return os.path.join(STORAGE_PATH, pid)


def log_path(pid):
    return os.path.join(project_path(pid), "run.log")


def normalize_github_url(value):
    value = value.strip()
    if value.startswith("git@") or not re.match(r"^https?://github\.com/[^/\s]+/[^/\s]+(?:\.git)?/?$", value):
        return None
    return value.rstrip("/") if value.endswith(".git") else value.rstrip("/") + ".git"


def github_token_path():
    return os.path.join(BASE_DIR, GITHUB_TOKEN_FILE)


def save_github_token(token):
    path = github_token_path()
    with open(path, "w", encoding="utf-8") as f:
        f.write(token.strip())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load_github_token():
    try:
        with open(github_token_path(), "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def github_api(path, token, method="GET", body=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2026-03-10",
        "User-Agent": "HostingBot/1.0",
    }
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request("https://api.github.com" + path, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode()) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode())
        except Exception:
            detail = {"message": str(exc)}
        return exc.code, detail
    except Exception as exc:
        return 0, {"message": str(exc)}


def github_profile(token):
    status, payload = github_api("/user", token)
    return (payload if status == 200 else None), payload.get("message", "GitHub authentication failed")


def github_repositories(token):
    status, payload = github_api("/user/repos?per_page=100&sort=updated", token)
    return (payload if status == 200 and isinstance(payload, list) else None), payload.get("message", "Unable to list repositories") if isinstance(payload, dict) else "Unable to list repositories"


def clone_github_repo(url, destination, token=None):
    normalized = normalize_github_url(url)
    if not normalized:
        return False, "Please send a public GitHub repository URL like https://github.com/user/repository"
    env = os.environ.copy()
    askpass = None
    try:
        if token:
            askpass = destination + ".askpass"
            with open(askpass, "w", encoding="utf-8") as f:
                f.write("#!/bin/sh\nprintf '%s\\n' \"$GITHUB_CLONE_TOKEN\"\n")
            os.chmod(askpass, 0o700)
            env["GIT_ASKPASS"] = askpass
            env["GIT_TERMINAL_PROMPT"] = "0"
            env["GITHUB_CLONE_TOKEN"] = token
        clone_url = normalized.replace("https://github.com/", "https://x-access-token@github.com/") if token else normalized
        result = subprocess.run(
            ["git", "clone", "--depth", "1", clone_url, destination],
            capture_output=True, text=True, timeout=600, env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "GitHub clone timed out after 10 minutes."
    except OSError as exc:
        return False, f"Git is not available in the hosting runtime: {exc}"
    finally:
        if askpass:
            try:
                os.remove(askpass)
            except OSError:
                pass
    if result.returncode != 0:
        return False, f"GitHub clone failed:\n{(result.stderr or result.stdout)[-2000:]}"
    return True, "Repository cloned successfully."


def detect_project_bot_username(folder):
    token_pattern = re.compile(r"\b(\d{8,12}:[A-Za-z0-9_-]{35})\b")
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in {".git", "node_modules", "__pycache__", ".venv", "venv"}]
        for filename in files:
            if not filename.endswith((".py", ".js", ".mjs", ".cjs", ".json", ".env", ".txt")):
                continue
            try:
                text = open(os.path.join(root, filename), "r", encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            match = token_pattern.search(text)
            if not match:
                continue
            try:
                with urllib.request.urlopen(f"https://api.telegram.org/bot{match.group(1)}/getMe", timeout=8) as response:
                    payload = json.loads(response.read().decode())
                user = payload.get("result", {})
                if user.get("username"):
                    return "@" + user["username"]
            except Exception:
                return None
    return None


def manifest_commands(folder, filename):
    path = os.path.join(folder, filename)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            manifest = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    raw = manifest.get("processes", manifest.get("commands", []))
    if isinstance(raw, dict):
        raw = list(raw.values())
    commands = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str) and item.strip():
            commands.append(item.strip())
        elif isinstance(item, dict) and isinstance(item.get("command"), str) and item["command"].strip():
            commands.append(item["command"].strip())
    return commands


def detect_entry(folder):
    for manifest_name in ("run.json", "project.json"):
        if manifest_commands(folder, manifest_name):
            return manifest_name
    package_json = os.path.join(folder, "package.json")
    if os.path.isfile(package_json):
        try:
            with open(package_json, "r", encoding="utf-8", errors="ignore") as f:
                manifest = json.load(f)
            if manifest.get("scripts", {}).get("start") or manifest.get("main"):
                main = manifest.get("main")
                if main and os.path.isfile(os.path.join(folder, main)):
                    return main
                return "package.json"
        except (OSError, json.JSONDecodeError):
            pass
    for name in ENTRY_CANDIDATES:
        if os.path.isfile(os.path.join(folder, name)):
            return name
    source_files = [
        f for f in os.listdir(folder)
        if f.endswith((".py", ".js", ".mjs", ".cjs")) and os.path.isfile(os.path.join(folder, f))
    ]
    if len(source_files) == 1:
        return source_files[0]
    return None


def runtime_command(folder, entry):
    if not entry:
        return None, "No entry file set."
    if entry in {"run.json", "project.json"}:
        commands = manifest_commands(folder, entry)
        if not commands:
            return None, f"{entry} has no valid commands/processes list."
        script = "trap 'kill 0' TERM INT; " + " & ".join(f"({command})" for command in commands) + " & wait"
        return ["bash", "-lc", script], f"Mixed process manifest ({len(commands)} processes)"
    if not entry:
        return None, "No entry file set."
    if entry == "package.json":
        if not shutil.which("npm"):
            return None, "Node.js/npm is not installed in the hosting runtime."
        return ["npm", "start"], "Node.js package.json start script"
    extension = os.path.splitext(entry)[1].lower()
    if extension == ".py":
        return [PYTHON_BIN, entry], "Python"
    if extension in {".js", ".mjs", ".cjs"}:
        if not shutil.which("node"):
            return None, "Node.js is not installed in the hosting runtime."
        return ["node", entry], "Node.js"
    return None, f"Unsupported entry type: {entry}"


IMPORT_TO_PACKAGE = {
    "PIL": "Pillow",
    "cv2": "opencv-python",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "bs4": "beautifulsoup4",
    "telegram": "python-telegram-bot",
    "telebot": "pyTelegramBotAPI",
    "discord": "discord.py",
    "cv": "opencv-python",
    "Crypto": "pycryptodome",
    "jwt": "PyJWT",
    "fitz": "PyMuPDF",
    "multipart": "python-multipart",
    "dateutil": "python-dateutil",
    "OpenSSL": "pyOpenSSL",
    "yt_dlp": "yt-dlp",
    "youtube_dl": "youtube_dl",
}

# Standard-library modules that must never be sent to pip.
STDLIB_MODULES = set(getattr(sys, "stdlib_module_names", ())) | {
    "__future__", "typing_extensions", "_thread", "winreg", "msvcrt"
}


def discover_imports(folder):
    """Find top-level imports in .py files without running project code."""
    imported = set()
    local_modules = set()
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv", "venv", "env"}]
        for filename in files:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(root, filename)
            rel = os.path.relpath(path, folder)
            module_name = os.path.splitext(rel.replace(os.sep, "."))[0]
            local_modules.add(module_name.split(".")[0])
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    tree = ast.parse(f.read(), filename=path)
            except (SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
    imported -= local_modules
    imported -= STDLIB_MODULES
    return sorted(imported)


JS_BUILTINS = {
    "assert", "buffer", "child_process", "cluster", "console", "crypto", "dgram", "dns",
    "events", "fs", "http", "https", "module", "net", "os", "path", "perf_hooks",
    "process", "querystring", "readline", "stream", "string_decoder", "timers", "tls",
    "url", "util", "v8", "vm", "worker_threads", "zlib"
}


def discover_js_imports(folder):
    """Find common npm imports without executing JavaScript code."""
    imported = set()
    patterns = [
        re.compile(r"(?:require\s*\(\s*|from\s*['\"]|import\s*['\"])([^'\"/]+(?:/[^'\" ]+)*)"),
    ]
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in {"node_modules", ".git", "dist", "build"}]
        for filename in files:
            if not filename.endswith((".js", ".mjs", ".cjs")):
                continue
            path = os.path.join(root, filename)
            try:
                text = open(path, "r", encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            for pattern in patterns:
                for name in pattern.findall(text):
                    top = name.split("/")[0]
                    if not top.startswith((".", "node:")) and top not in JS_BUILTINS:
                        imported.add(name if name.startswith("@") else top)
    return sorted(imported)


def package_names_for_imports(imports):
    return sorted({IMPORT_TO_PACKAGE.get(name, name) for name in imports})


def missing_imports(imports):
    missing = []
    for name in imports:
        try:
            available = importlib.util.find_spec(name) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            available = False
        if not available:
            missing.append(name)
    return sorted(missing)


def requirement_lines(folder):
    lines = []
    req = os.path.join(folder, "requirements.txt")
    if os.path.isfile(req):
        try:
            with open(req, "r", encoding="utf-8", errors="ignore") as f:
                lines.extend(line.strip() for line in f if line.strip() and not line.lstrip().startswith(("#", "-")))
        except OSError:
            pass

    # Optional JSON manifests for projects that do not use requirements.txt.
    for manifest_name in ("requirements.json", "dependencies.json"):
        manifest_path = os.path.join(folder, manifest_name)
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8", errors="ignore") as f:
                manifest = json.load(f)
            values = manifest.get("python", manifest.get("pip", manifest.get("pythonPackages", [])))
            if isinstance(values, dict):
                values = [f"{name}{spec if isinstance(spec, str) else ''}" for name, spec in values.items()]
            if isinstance(values, list):
                lines.extend(str(value).strip() for value in values if str(value).strip())
        except (OSError, json.JSONDecodeError):
            pass
    return lines


def json_node_dependencies(folder):
    values = []
    for manifest_name in ("requirements.json", "dependencies.json"):
        manifest_path = os.path.join(folder, manifest_name)
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8", errors="ignore") as f:
                manifest = json.load(f)
            node_values = manifest.get("node", manifest.get("npm", manifest.get("nodePackages", [])))
            if isinstance(node_values, dict):
                values.extend(f"{name}@{spec}" if isinstance(spec, str) and spec else name for name, spec in node_values.items())
            elif isinstance(node_values, list):
                values.extend(str(value).strip() for value in node_values if str(value).strip())
        except (OSError, json.JSONDecodeError):
            pass
    return values


def dependency_fingerprint(folder, imports, packages):
    digest = hashlib.sha256()
    req = os.path.join(folder, "requirements.txt")
    if os.path.isfile(req):
        with open(req, "rb") as f:
            digest.update(f.read())
    digest.update("\n".join(imports).encode())
    digest.update("\n".join(packages).encode())
    return digest.hexdigest()


def install_dependencies(folder, imports, packages):
    """Install declared requirements plus detected packages in one pip call."""
    declared = requirement_lines(folder)
    combined = list(declared)
    installed = {line.split("[", 1)[0].split("==", 1)[0].split(">=", 1)[0].split("<=", 1)[0].strip().lower() for line in declared}
    for package in packages:
        normalized = package.lower().replace("_", "-")
        if normalized not in installed:
            combined.append(package)
            installed.add(normalized)
    if not combined:
        return True, "No third-party modules detected."

    req_file = os.path.join(folder, ".auto_requirements.txt")
    try:
        with open(req_file, "w", encoding="utf-8") as f:
            f.write("\n".join(combined) + "\n")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--break-system-packages", "-r", req_file],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return False, "Automatic module installation timed out after 10 minutes."
    except Exception as exc:
        return False, f"Automatic module installation failed: {exc}"
    finally:
        try:
            os.remove(req_file)
        except OSError:
            pass
    if result.returncode != 0:
        output = (result.stderr or result.stdout or "unknown pip error").strip()
        return False, f"Automatic module installation failed:\n{output[-2500:]}"
    return True, f"Installed or verified {len(combined)} package(s). Detected imports: {', '.join(imports) or 'none'}."


def install_node_dependencies(folder, js_imports):
    if not os.path.isfile(os.path.join(folder, "package.json")) and not js_imports and not json_node_dependencies(folder):
        return True, "No Node.js dependencies detected."
    if not shutil.which("npm"):
        return False, "Node.js/npm is not installed in the hosting runtime."

    try:
        manifest_result = None
        if os.path.isfile(os.path.join(folder, "package.json")):
            manifest_result = subprocess.run(
                ["npm", "install", "--no-audit", "--no-fund"],
                cwd=folder, capture_output=True, text=True, timeout=900,
            )
        detected = [name for name in js_imports if name not in JS_BUILTINS]
        detected.extend(json_node_dependencies(folder))
        if detected:
            extra = subprocess.run(
                ["npm", "install", "--no-save", "--no-audit", "--no-fund", *sorted(set(detected))],
                cwd=folder, capture_output=True, text=True, timeout=900,
            )
            if extra.returncode != 0:
                output = (extra.stderr or extra.stdout or "unknown npm error").strip()
                return False, f"Node.js dependency installation failed:\n{output[-2500:]}"
        if manifest_result and manifest_result.returncode != 0:
            output = (manifest_result.stderr or manifest_result.stdout or "unknown npm error").strip()
            return False, f"Node.js package installation failed:\n{output[-2500:]}"
        return True, f"Node.js dependencies ready; detected imports: {', '.join(js_imports) or 'none'}."
    except subprocess.TimeoutExpired:
        return False, "Node.js dependency installation timed out after 15 minutes."
    except Exception as exc:
        return False, f"Node.js dependency installation failed: {exc}"


def ensure_dependencies(proj_id, data):
    proj = data["projects"][proj_id]
    folder = project_path(proj_id)
    imports = discover_imports(folder)
    missing = missing_imports(imports)
    packages = package_names_for_imports(missing)
    js_imports = discover_js_imports(folder)
    fingerprint = dependency_fingerprint(folder, imports + js_imports, packages + json_node_dependencies(folder))
    proj["detected_imports"] = imports
    proj["missing_imports"] = missing
    proj["detected_packages"] = packages
    proj["detected_js_imports"] = js_imports
    if proj.get("dependency_fingerprint") == fingerprint and proj.get("requirements_status") == "installed":
        save_data(data)
        return True, "Dependencies already installed."

    proj["requirements_status"] = "installing"
    save_data(data)
    ok, message = install_dependencies(folder, imports, packages)
    if ok:
        ok, node_message = install_node_dependencies(folder, js_imports)
        message = f"{message} {node_message}"
    proj["requirements_status"] = "installed" if ok else "failed"
    proj["requirements_error"] = None if ok else message
    if ok:
        proj["dependency_fingerprint"] = fingerprint
    save_data(data)
    return ok, message


def railway_environment():
    return bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RAILWAY_PUBLIC_DOMAIN"))


def railway_public_url():
    domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()
    if not domain:
        return None
    if not domain.startswith(("http://", "https://")):
        domain = "https://" + domain
    return domain.rstrip("/")


def find_available_port():
    # Railway injects PORT and routes the service domain to that one port.
    # All child API processes in this service must use it.
    if railway_environment():
        try:
            return int(os.environ.get("PORT", DEFAULT_API_PORT))
        except ValueError:
            return DEFAULT_API_PORT
    for port in API_PORT_RANGE:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return DEFAULT_API_PORT


def detect_listening_port(preferred_port):
    candidates = [preferred_port] + [p for p in API_PORT_RANGE if p != preferred_port]
    for port in candidates:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return port
        except OSError:
            continue
    return None


def wait_for_api_port(pid, preferred_port):
    deadline = time.time() + PORT_DETECT_SECONDS
    while time.time() < deadline:
        if not is_alive(pid):
            return None
        port = detect_listening_port(preferred_port)
        if port:
            return port
        time.sleep(0.25)
    return None


def start_cloudflared_tunnel(port):
    if not ENABLE_CLOUDFLARED_TUNNEL or PUBLIC_URL_TEMPLATE or not shutil.which("cloudflared"):
        return None, None
    try:
        tunnel = subprocess.Popen(
            [shutil.which("cloudflared"), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            preexec_fn=os.setsid,
        )
        deadline = time.time() + 15
        pattern = re.compile(r"https://[a-z0-9-]+\\.trycloudflare\\.com")
        while time.time() < deadline and tunnel.poll() is None:
            line = tunnel.stdout.readline()
            match = pattern.search(line or "")
            if match:
                return match.group(0), tunnel.pid
        os.killpg(tunnel.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass
    return None, None


def stop_cloudflared_tunnel(proj):
    tunnel_pid = proj.get("tunnel_pid")
    if tunnel_pid and is_alive(tunnel_pid):
        try:
            os.killpg(tunnel_pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass
    proj["tunnel_pid"] = None


def project_url_text(proj):
    public_url = proj.get("public_url")
    local_url = proj.get("local_url")
    if public_url:
        return f"🌐 <b>Public URL:</b> <a href=\"{html.escape(public_url, quote=True)}\">{html.escape(public_url)}</a>"
    if local_url:
        return f"🌐 <b>Local URL:</b> <code>{html.escape(local_url)}</code>\n<i>Set PUBLIC_URL_TEMPLATE or a tunnel to make it public.</i>"
    return ""


def start_project(proj_id, data):
    proj = data["projects"][proj_id]
    folder = project_path(proj_id)
    entry = proj.get("entry")
    entry_path = os.path.join(folder, entry) if entry else ""
    if not entry:
        return False, "No entry file set. Use the ⚙️ Set Entry button first."
    if not os.path.isfile(entry_path):
        return False, f"Entry file not found: {entry}"
    if is_alive(proj.get("pid")):
        return False, "Already running."

    ok, install_error = ensure_dependencies(proj_id, data)
    if not ok:
        return False, install_error

    command, runtime = runtime_command(folder, entry)
    if not command:
        return False, runtime
    logf = open(log_path(proj_id), "w")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.Popen(
            command,
            cwd=folder,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
            preexec_fn=os.setsid,  # new process group so we can kill children too
        )
    finally:
        logf.close()

    # Give imports and argument validation a moment to fail before recording
    # the PID as running. The complete traceback is available through Logs.
    time.sleep(0.75)
    if proc.poll() is not None or not is_alive(proc.pid):
        error = read_log_tail(proj_id)
        proj["pid"] = None
        proj["pgid"] = None
        save_data(data)
        return False, f"Project exited during startup.\n<pre>{html.escape(error)}</pre>"

    proj["pid"] = proc.pid
    proj["pgid"] = proc.pid  # since setsid, pgid == pid
    proj["started_at"] = time.time()
    proj["runtime"] = runtime
    proj["command"] = command
    proj["port"] = None
    proj["local_url"] = None
    proj["public_url"] = None
    proj["tunnel_pid"] = None
    save_data(data)
    return True, f"Started (PID {proc.pid})."


def stop_project(proj_id, data):
    proj = data["projects"][proj_id]
    pid = proj.get("pid")
    if not is_alive(pid):
        proj["pid"] = None
        save_data(data)
        return False, "Not running."
    try:
        os.killpg(proj.get("pgid", pid), signal.SIGTERM)
        time.sleep(1)
        if is_alive(pid):
            os.killpg(proj.get("pgid", pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    proj["pid"] = None
    save_data(data)
    return True, "Stopped."


# ---------------- keyboards ----------------

def styled_button(text, callback_data=None, url=None, style=None):
    """Use Bot API 10.2 button colors when the installed client supports them."""
    if style is None:
        if any(word in text for word in ("Delete", "Stop", "Remove", "Deny")):
            style = "danger"
        elif any(word in text for word in ("Run", "Restart", "GitHub", "Add", "Approve", "URL")):
            style = "success"
        else:
            style = "primary"
    kwargs = {"text": text}
    if callback_data is not None:
        kwargs["callback_data"] = callback_data
    if url is not None:
        kwargs["url"] = url
    try:
        return types.InlineKeyboardButton(style=style, **kwargs)
    except TypeError:
        return types.InlineKeyboardButton(**kwargs)


def github_manage_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(styled_button("👤 Profile", callback_data="ghprofile"), styled_button("📚 Repositories", callback_data="ghlist:0"))
    kb.add(styled_button("🔌 Disconnect", callback_data="ghdisconnect"), styled_button("⬅️ Back", callback_data="home"))
    return kb


def main_menu_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        styled_button("📁 List Projects", callback_data="list"),
        styled_button("📊 Live Status", callback_data="status"),
    )
    if load_github_token():
        kb.add(styled_button("🔗 Manage GitHub", callback_data="ghmanage"))
    else:
        kb.add(styled_button("🔗 Connect GitHub", callback_data="ghconnect"))
    kb.add(styled_button("👥 Admin Help", callback_data="adminhelp"))
    return kb


def project_list_kb(data):
    kb = types.InlineKeyboardMarkup(row_width=1)
    if not data["projects"]:
        kb.add(styled_button("No projects yet — send a file/zip", callback_data="noop"))
        return kb
    for pid, proj in data["projects"].items():
        running = is_alive(proj.get("pid"))
        dot = "🟢" if running else "🔴"
        kb.add(styled_button(f"{dot} {proj['name']}", callback_data=f"open:{pid}"))
    kb.add(styled_button("⬅️ Back", callback_data="home"))
    return kb


def project_ctrl_kb(pid, data):
    proj = data["projects"][pid]
    running = is_alive(proj.get("pid"))
    kb = types.InlineKeyboardMarkup(row_width=2)
    if running:
        kb.add(
            styled_button("⏹ Stop", callback_data=f"stop:{pid}"),
            styled_button("🔄 Restart", callback_data=f"restart:{pid}"),
        )
    else:
        kb.add(styled_button("▶️ Run", callback_data=f"run:{pid}"))
    kb.add(
        styled_button("📜 Logs", callback_data=f"logs:{pid}"),
        styled_button("⚙️ Set Entry", callback_data=f"setentry:{pid}"),
    )
    kb.add(styled_button("🗑 Delete", callback_data=f"del:{pid}"))
    kb.add(styled_button("⬅️ Back", callback_data="list"))
    return kb


def register_project_folder(chat_id, folder, name, source=None):
    proj_id = os.path.basename(folder)
    data = load_data()
    entry = detect_entry(folder)
    project = {
        "name": name,
        "entry": entry,
        "pid": None,
        "created": time.time(),
        "source": source,
        "telegram_bot": detect_project_bot_username(folder),
    }
    data["projects"][proj_id] = project
    save_data(data)
    dep_ok, dep_message = ensure_dependencies(proj_id, data)
    run_ok, run_message = (start_project(proj_id, load_data()) if dep_ok and entry else (False, "Not started: entry file is missing."))
    data = load_data()
    entry_note = f"Entry: <code>{html.escape(entry)}</code>" if entry else "⚠️ Entry file not detected — use ⚙️ Set Entry."
    bot_name = f"\n🤖 Hosted bot: <b>{html.escape(project['telegram_bot'])}</b>" if project.get("telegram_bot") else ""
    dep_note = "✅ Dependencies ready." if dep_ok else f"⚠️ <b>Dependency error:</b>\n<pre>{html.escape(dep_message[-1800:])}</pre>"
    run_note = f"\n🚀 <b>Auto-start:</b> {html.escape(run_message)}" if run_ok else f"\n❌ <b>Auto-start failed:</b>\n<pre>{html.escape(run_message[-1800:])}</pre>"
    bot.send_message(chat_id, f"✅ <b>Project imported and processed</b>\n<blockquote>{html.escape(name)}</blockquote>\n{entry_note}{bot_name}\n\n{dep_note}{run_note}", reply_markup=project_ctrl_kb(proj_id, data))


def github_repo_text(repo):
    visibility = "private" if repo.get("private") else "public"
    return (
        f"📦 <b>{html.escape(repo.get('full_name', repo.get('name', 'repository')))}</b>\n"
        f"<blockquote>{html.escape(repo.get('description') or 'No description')}</blockquote>\n"
        f"⭐ <b>Stars:</b> {repo.get('stargazers_count', 0)}  |  🍴 <b>Forks:</b> {repo.get('forks_count', 0)}\n"
        f"🔒 <b>Visibility:</b> {visibility}\n"
        f"🗓 <b>Updated:</b> {html.escape(repo.get('updated_at', 'unknown'))}"
    )


def github_profile_text(profile):
    return (
        "🔗 <b>GitHub Connected</b>\n"
        f"<blockquote><b>{html.escape(profile.get('name') or profile.get('login', 'Unknown'))}</b> (@{html.escape(profile.get('login', 'unknown'))})</blockquote>\n"
        f"🆔 <b>ID:</b> {profile.get('id', 'unknown')}\n"
        f"📦 <b>Public repositories:</b> {profile.get('public_repos', 0)}\n"
        f"👥 <b>Followers:</b> {profile.get('followers', 0)}\n"
        f"📍 <b>Location:</b> {html.escape(profile.get('location') or 'Not set')}\n"
        f"🔗 <a href=\"{html.escape(profile.get('html_url', ''), quote=True)}\">Open GitHub profile</a>"
    )


def github_repo_list_kb(repos):
    kb = types.InlineKeyboardMarkup(row_width=1)
    for index, repo in enumerate(repos[:100]):
        visibility = "🔒" if repo.get("private") else "🌐"
        label = f"{visibility} {repo.get('full_name', repo.get('name', 'repository'))}"
        kb.add(styled_button(label[:60], callback_data=f"ghrepo:{index}"))
    kb.add(styled_button("🔄 Refresh repositories", callback_data="ghlist:0"))
    kb.add(styled_button("⬅️ Manage GitHub", callback_data="ghmanage"))
    return kb


def github_repo_actions_kb(full_name):
    safe_name = html.escape(full_name, quote=True)
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(styled_button("▶️ Run Repo", callback_data=f"ghrun:{full_name}"), styled_button("🗑 Delete Repo", callback_data=f"ghdelete:{full_name}"))
    kb.add(styled_button("🔗 Open on GitHub", url=f"https://github.com/{safe_name}"))
    kb.add(styled_button("⬅️ Repositories", callback_data="ghlist:0"))
    return kb


def process_github_repo(m, url):
    status = bot.reply_to(m, "🐙 Cloning GitHub repository...")
    proj_id = uuid.uuid4().hex[:8]
    folder = project_path(proj_id)
    ok, message = clone_github_repo(url, folder, load_github_token())
    if not ok:
        bot.edit_message_text(f"❌ <b>GitHub import failed</b>\n<pre>{html.escape(message[-2500:])}</pre>", m.chat.id, status.message_id)
        shutil.rmtree(folder, ignore_errors=True)
        return
    name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    bot.edit_message_text(f"✅ Cloned <b>{html.escape(name)}</b>. Installing dependencies and detecting entry...", m.chat.id, status.message_id)
    register_project_folder(m.chat.id, folder, name, source=url)


def connect_github_prompt(chat_id, user_id):
    awaiting_github_token.add(user_id)
    bot.send_message(chat_id, "🔐 <b>Connect GitHub</b>\n<blockquote>Send a GitHub Personal Access Token. It will be validated and stored locally with restricted file permissions. Never share it in a public chat.</blockquote>")


@bot.message_handler(func=lambda m: m.from_user.id in awaiting_github_token, content_types=["text"])
def handle_github_token(m):
    awaiting_github_token.discard(m.from_user.id)
    token = m.text.strip()
    if len(token) < 20 or " " in token:
        bot.reply_to(m, "❌ Invalid-looking token. Please try again.")
        return
    profile, error = github_profile(token)
    if not profile:
        bot.reply_to(m, f"❌ <b>GitHub connection failed:</b>\n<pre>{html.escape(error)}</pre>")
        return
    save_github_token(token)
    bot.reply_to(m, "✅ <b>GitHub connected successfully.</b>", reply_markup=github_manage_kb())


@bot.message_handler(commands=["start", "help"])
def cmd_start(m):
    if not can_host(m.from_user.id):
        bot.reply_to(m, "🚫 <b>Not authorized.</b>")
        return
    bot.send_message(
        m.chat.id,
        "🤖 <b>Hosting Bot</b>\n\n"
        "<blockquote>Upload a .py/.js file, .zip project, or use /github with a public repository URL.</blockquote>\n\n"
        "<b>Commands</b>\n"
        "/list — all projects\n"
        "/status — live status\n"
        "/github — clone a public GitHub repository\n"
        "/grant 123456789 — admin only\n"
        "/revoke 123456789 — admin only\n",
        reply_markup=main_menu_kb(),
    )


@bot.message_handler(commands=["connect_github"])
def cmd_connect_github(m):
    if can_host(m.from_user.id):
        connect_github_prompt(m.chat.id, m.from_user.id)


@bot.message_handler(commands=["github"])
def cmd_github(m):
    if not can_host(m.from_user.id):
        bot.reply_to(m, "🚫 <b>Not authorized.</b>")
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) == 2:
        process_github_repo(m, parts[1].strip())
    else:
        awaiting_github.add(m.from_user.id)
        bot.reply_to(m, "🐙 <b>GitHub import</b>\n<blockquote>Send a public repository URL, for example: https://github.com/user/repository</blockquote>")


@bot.message_handler(func=lambda m: m.from_user.id in awaiting_github, content_types=["text"])
def handle_github_reply(m):
    awaiting_github.discard(m.from_user.id)
    if can_host(m.from_user.id):
        process_github_repo(m, m.text.strip())


@bot.message_handler(func=lambda m: can_host(m.from_user.id) and bool(normalize_github_url(m.text.strip())), content_types=["text"])
def handle_direct_github_link(m):
    process_github_repo(m, m.text.strip())


@bot.message_handler(commands=["grant", "revoke"])
def cmd_permission(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "🚫 Admin only.")
        return
    parts = m.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        bot.reply_to(m, "Usage: <code>/grant 123456789</code> or <code>/revoke 123456789</code>")
        return
    uid = int(parts[1])
    data = load_data()
    users = set(data.get("hosting_users", []))
    if m.text.split()[0].lower() == "/grant":
        users.add(uid)
        data["hosting_users"] = sorted(users)
        save_data(data)
        bot.reply_to(m, f"✅ Hosting permission granted to <code>{uid}</code>.")
    else:
        users.discard(uid)
        data["hosting_users"] = sorted(users)
        save_data(data)
        bot.reply_to(m, f"✅ Hosting permission revoked from <code>{uid}</code>.")


@bot.message_handler(commands=["list"])
def cmd_list(m):
    if not can_host(m.from_user.id):
        return
    data = load_data()
    bot.send_message(m.chat.id, "📁 <b>Your Projects</b>", reply_markup=project_list_kb(data))


@bot.message_handler(commands=["status"])
def cmd_status(m):
    if not can_host(m.from_user.id):
        return
    bot.send_message(m.chat.id, status_text(), reply_markup=main_menu_kb())


def status_text():
    data = load_data()
    total = len(data["projects"])
    running = sum(1 for p in data["projects"].values() if is_alive(p.get("pid")))
    size = 0
    for root, _, files in os.walk(STORAGE_PATH):
        for f in files:
            try:
                size += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    size_mb = size / (1024 * 1024)
    return (
        f"📊 <b>LIVE HOSTING STATUS</b>\n"
        f"<blockquote>🟢 <b>Running:</b> {running}\n"
        f"🔴 <b>Stopped:</b> {total - running}\n"
        f"📦 <b>Total projects:</b> {total}\n"
        f"💾 <b>Storage:</b> {size_mb:.1f} MB</blockquote>"
    )


# ---------------- file upload ----------------

@bot.message_handler(content_types=["document"])
def handle_doc(m):
    if not can_host(m.from_user.id):
        return
    doc = m.document
    if doc.file_size > MAX_FILE_MB * 1024 * 1024:
        bot.reply_to(m, f"❌ File too big (max {MAX_FILE_MB}MB).")
        return

    msg = bot.reply_to(m, "⬇️ Downloading...")

    file_info = bot.get_file(doc.file_id)
    file_bytes = bot.download_file(file_info.file_path)

    proj_id = uuid.uuid4().hex[:8]
    folder = project_path(proj_id)
    os.makedirs(folder, exist_ok=True)

    orig_name = doc.file_name or f"file_{proj_id}"
    save_path = os.path.join(folder, orig_name)
    with open(save_path, "wb") as f:
        f.write(file_bytes)

    is_zip = orig_name.lower().endswith(".zip")
    if is_zip:
        try:
            with zipfile.ZipFile(save_path, "r") as z:
                z.extractall(folder)
            os.remove(save_path)
            # flatten if zip had a single root folder
            entries = [e for e in os.listdir(folder) if not e.startswith(".")]
            if len(entries) == 1 and os.path.isdir(os.path.join(folder, entries[0])):
                inner = os.path.join(folder, entries[0])
                for item in os.listdir(inner):
                    shutil.move(os.path.join(inner, item), folder)
                os.rmdir(inner)
        except zipfile.BadZipFile:
            bot.edit_message_text("❌ Invalid zip file.", m.chat.id, msg.message_id)
            shutil.rmtree(folder, ignore_errors=True)
            return

    name = orig_name.rsplit(".", 1)[0] if not is_zip else orig_name[:-4]
    entry = detect_entry(folder)

    data = load_data()
    data["projects"][proj_id] = {
        "name": name,
        "entry": entry,
        "pid": None,
        "created": time.time(),
        "telegram_bot": detect_project_bot_username(folder),
    }
    save_data(data)
    bot.edit_message_text("⬇️ Downloaded. Detecting Python modules and installing dependencies...", m.chat.id, msg.message_id)
    dep_ok, dep_message = ensure_dependencies(proj_id, data)
    dep_note = "✅ Dependencies ready." if dep_ok else f"⚠️ <b>Dependency installation failed:</b>\n<pre>{html.escape(dep_message[-1800:])}</pre>"

    entry_note = f"Entry file: <code>{entry}</code>" if entry else "⚠️ Couldn't auto-detect entry file — set it with ⚙️ Set Entry."
    run_ok, run_message = (start_project(proj_id, load_data()) if dep_ok and entry else (False, "Not started: entry file is missing."))
    data = load_data()
    run_note = f"\n🚀 <b>Auto-start:</b> {html.escape(run_message)}" if run_ok else f"\n❌ <b>Auto-start failed:</b>\n<pre>{html.escape(run_message[-1800:])}</pre>"
    bot.edit_message_text(
        f"✅ Uploaded and processed: <b>{html.escape(name)}</b>\n{entry_note}\n\n{dep_note}{run_note}",
        m.chat.id,
        msg.message_id,
        reply_markup=project_ctrl_kb(proj_id, data),
    )


@bot.message_handler(func=lambda m: m.from_user.id in awaiting_entry, content_types=["text"])
def handle_entry_reply(m):
    pid = awaiting_entry.pop(m.from_user.id)
    data = load_data()
    if pid not in data["projects"]:
        return
    filename = m.text.strip()
    folder = project_path(pid)
    if not os.path.exists(os.path.join(folder, filename)):
        bot.reply_to(m, f"❌ <code>{filename}</code> not found in project folder.")
        return
    data["projects"][pid]["entry"] = filename
    save_data(data)
    bot.reply_to(m, f"✅ Entry file set to <code>{filename}</code>", reply_markup=project_ctrl_kb(pid, data))


# ---------------- callbacks ----------------

@bot.callback_query_handler(func=lambda c: True)
def handle_callback(c):
    if not can_host(c.from_user.id):
        bot.answer_callback_query(c.id, "Not authorized.")
        return

    data = load_data()
    action = c.data

    if action == "home":
        bot.edit_message_text("🤖 <b>Hosting Bot</b>", c.message.chat.id, c.message.message_id, reply_markup=main_menu_kb())

    elif action == "list":
        bot.edit_message_text("📁 <b>Your Projects</b>", c.message.chat.id, c.message.message_id, reply_markup=project_list_kb(data))

    elif action == "status":
        bot.edit_message_text(status_text(), c.message.chat.id, c.message.message_id, reply_markup=main_menu_kb())

    elif action == "noop":
        bot.answer_callback_query(c.id)

    elif action == "github":
        awaiting_github.add(c.from_user.id)
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, "🐙 <b>GitHub import</b>\n<blockquote>Send a public GitHub repository URL.</blockquote>")

    elif action == "adminhelp":
        bot.answer_callback_query(c.id)
        if is_admin(c.from_user.id):
            bot.send_message(c.message.chat.id, "👥 <b>Admin controls</b>\n<blockquote>/grant USER_ID — allow hosting\n/revoke USER_ID — remove hosting\nOnly the main admin can change permissions.</blockquote>")
        else:
            bot.send_message(c.message.chat.id, "ℹ️ Your hosting permission is managed by the administrator.")

    elif action == "ghconnect":
        bot.answer_callback_query(c.id)
        connect_github_prompt(c.message.chat.id, c.from_user.id)

    elif action == "ghmanage":
        token = load_github_token()
        profile, error = github_profile(token) if token else (None, "Not connected")
        bot.answer_callback_query(c.id)
        if profile:
            bot.send_message(c.message.chat.id, github_profile_text(profile), reply_markup=github_manage_kb())
        else:
            bot.send_message(c.message.chat.id, f"❌ <b>GitHub connection unavailable:</b> {html.escape(error)}", reply_markup=main_menu_kb())

    elif action == "ghprofile":
        token = load_github_token()
        profile, error = github_profile(token) if token else (None, "Not connected")
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, github_profile_text(profile) if profile else f"❌ {html.escape(error)}", reply_markup=github_manage_kb() if profile else main_menu_kb())

    elif action.startswith("ghlist:"):
        token = load_github_token()
        repos, error = github_repositories(token) if token else (None, "Connect GitHub first")
        bot.answer_callback_query(c.id)
        if repos is None:
            bot.send_message(c.message.chat.id, f"❌ <b>Repository list failed:</b> {html.escape(error)}", reply_markup=github_manage_kb())
        else:
            data["github_repo_cache"] = repos
            save_data(data)
            bot.send_message(c.message.chat.id, f"📚 <b>Repositories</b> — {len(repos)} found", reply_markup=github_repo_list_kb(repos))

    elif action.startswith("ghrepo:"):
        bot.answer_callback_query(c.id)
        try:
            index = int(action.split(":", 1)[1])
            repo = data.get("github_repo_cache", [])[index]
        except (ValueError, IndexError, TypeError):
            bot.send_message(c.message.chat.id, "❌ Repository list expired. Press Refresh repositories.")
        else:
            bot.send_message(c.message.chat.id, github_repo_text(repo), reply_markup=github_repo_actions_kb(repo.get("full_name", "")))

    elif action.startswith("ghdelete:"):
        if not is_admin(c.from_user.id):
            bot.answer_callback_query(c.id, "Admin only.")
            return
        full_name = action.split(":", 1)[1]
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(styled_button("✅ Confirm delete", callback_data=f"ghdelete_confirm:{full_name}", style="danger"), styled_button("Cancel", callback_data="ghlist:0"))
        bot.answer_callback_query(c.id)
        bot.send_message(c.message.chat.id, f"⚠️ <b>Delete repository?</b>\n<blockquote>This permanently deletes <code>{html.escape(full_name)}</code> from GitHub.</blockquote>", reply_markup=kb)

    elif action.startswith("ghdelete_confirm:"):
        full_name = action.split(":", 1)[1]
        parts = full_name.split("/", 1)
        bot.answer_callback_query(c.id, "Deleting...")
        if len(parts) != 2:
            bot.send_message(c.message.chat.id, "❌ Invalid repository name.")
        else:
            status, payload = github_api(f"/repos/{urllib.parse.quote(parts[0])}/{urllib.parse.quote(parts[1])}", load_github_token(), method="DELETE")
            if status == 204:
                bot.send_message(c.message.chat.id, f"✅ Deleted <code>{html.escape(full_name)}</code> from GitHub.", reply_markup=github_manage_kb())
            else:
                bot.send_message(c.message.chat.id, f"❌ Delete failed: {html.escape(str(payload.get('message', payload)))}", reply_markup=github_manage_kb())

    elif action.startswith("ghrun:"):
        full_name = action.split(":", 1)[1]
        bot.answer_callback_query(c.id, "Importing repository...")
        process_github_repo(c.message, f"https://github.com/{full_name}")

    elif action == "ghdisconnect":
        if not is_admin(c.from_user.id):
            bot.answer_callback_query(c.id, "Admin only.")
            return
        try:
            os.remove(github_token_path())
        except OSError:
            pass
        bot.answer_callback_query(c.id, "Disconnected.")
        bot.send_message(c.message.chat.id, "✅ GitHub disconnected.", reply_markup=main_menu_kb())

    elif ":" in action:
        cmd, pid = action.split(":", 1)
        if pid not in data["projects"]:
            bot.answer_callback_query(c.id, "Project not found (deleted?).")
            return
        proj = data["projects"][pid]

        if cmd == "open":
            running = is_alive(proj.get("pid"))
            txt = f"📦 <b>{html.escape(proj['name'])}</b>\nStatus: {'🟢 running' if running else '🔴 stopped'}\nEntry: <code>{html.escape(proj.get('entry') or 'not set')}</code>"
            bot.edit_message_text(txt, c.message.chat.id, c.message.message_id, reply_markup=project_ctrl_kb(pid, data))

        elif cmd == "run":
            ok, msg = start_project(pid, data)
            bot.answer_callback_query(c.id, msg[:190])
            data = load_data()
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=project_ctrl_kb(pid, data))
            if ok and project_url_text(data["projects"][pid]):
                bot.send_message(c.message.chat.id, project_url_text(data["projects"][pid]))

        elif cmd == "stop":
            ok, msg = stop_project(pid, data)
            bot.answer_callback_query(c.id, msg)
            data = load_data()
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=project_ctrl_kb(pid, data))

        elif cmd == "restart":
            stop_project(pid, data)
            data = load_data()
            ok, msg = start_project(pid, data)
            bot.answer_callback_query(c.id, "Restarted." if ok else msg)
            data = load_data()
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=project_ctrl_kb(pid, data))

        elif cmd == "logs":
            lp = log_path(pid)
            if not os.path.exists(lp):
                bot.answer_callback_query(c.id, "No logs yet.")
                return
            with open(lp, "r", errors="ignore") as f:
                lines = f.readlines()[-40:]
            text = "".join(lines).strip() or "(empty)"
            if len(text) > 3500:
                text = text[-3500:]
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, f"📜 <b>Last logs — {html.escape(proj['name'])}</b>\n<pre>{html.escape(text)}</pre>")

        elif cmd == "setentry":
            awaiting_entry[c.from_user.id] = pid
            bot.answer_callback_query(c.id)
            bot.send_message(c.message.chat.id, f"Send the entry filename for <b>{proj['name']}</b> (e.g. main.py):")

        elif cmd == "del":
            stop_project(pid, data)
            shutil.rmtree(project_path(pid), ignore_errors=True)
            del data["projects"][pid]
            save_data(data)
            bot.answer_callback_query(c.id, "Deleted.")
            bot.edit_message_text("📁 <b>Your Projects</b>", c.message.chat.id, c.message.message_id, reply_markup=project_list_kb(data))


if __name__ == "__main__":
    print("Hosting bot running...")
    bot.infinity_polling(timeout=30, long_polling_timeout=30)
