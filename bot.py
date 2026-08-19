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

# Max upload size accepted (Telegram bot API hard cap is 20MB for bots anyway)
MAX_FILE_MB = 20

# Python binary used to run hosted projects (Termux default is python3)
PYTHON_BIN = "python3"

# Common entry-file names to auto-detect, in priority order
ENTRY_CANDIDATES = ["main.py", "bot.py", "app.py", "run.py", "server.py", "start.py"]

# ===================================================================

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORAGE_PATH = os.path.join(BASE_DIR, STORAGE_DIR)
DATA_PATH = os.path.join(BASE_DIR, DATA_FILE)

os.makedirs(STORAGE_PATH, exist_ok=True)

# in-memory: waiting for user to type entry filename -> project_id
awaiting_entry = {}

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
    return uid == ADMIN_ID


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


def detect_entry(folder):
    for name in ENTRY_CANDIDATES:
        if os.path.exists(os.path.join(folder, name)):
            return name
    py_files = [f for f in os.listdir(folder) if f.endswith(".py")]
    if len(py_files) == 1:
        return py_files[0]
    return None


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
    req = os.path.join(folder, "requirements.txt")
    if not os.path.isfile(req):
        return []
    try:
        with open(req, "r", encoding="utf-8", errors="ignore") as f:
            return [line.strip() for line in f if line.strip() and not line.lstrip().startswith(("#", "-"))]
    except OSError:
        return []


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


def ensure_dependencies(proj_id, data):
    proj = data["projects"][proj_id]
    folder = project_path(proj_id)
    imports = discover_imports(folder)
    missing = missing_imports(imports)
    packages = package_names_for_imports(missing)
    fingerprint = dependency_fingerprint(folder, imports, packages)
    proj["detected_imports"] = imports
    proj["missing_imports"] = missing
    proj["detected_packages"] = packages
    if proj.get("dependency_fingerprint") == fingerprint and proj.get("requirements_status") == "installed":
        save_data(data)
        return True, "Dependencies already installed."

    proj["requirements_status"] = "installing"
    save_data(data)
    ok, message = install_dependencies(folder, imports, packages)
    proj["requirements_status"] = "installed" if ok else "failed"
    proj["requirements_error"] = None if ok else message
    if ok:
        proj["dependency_fingerprint"] = fingerprint
    save_data(data)
    return ok, message


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

    logf = open(log_path(proj_id), "w")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.Popen(
            [PYTHON_BIN, entry],
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

def main_menu_kb():
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("📁 List Projects", callback_data="list"),
        types.InlineKeyboardButton("📊 Status", callback_data="status"),
    )
    return kb


def project_list_kb(data):
    kb = types.InlineKeyboardMarkup(row_width=1)
    if not data["projects"]:
        kb.add(types.InlineKeyboardButton("No projects yet — send a file/zip", callback_data="noop"))
        return kb
    for pid, proj in data["projects"].items():
        running = is_alive(proj.get("pid"))
        dot = "🟢" if running else "🔴"
        kb.add(types.InlineKeyboardButton(f"{dot} {proj['name']}", callback_data=f"open:{pid}"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="home"))
    return kb


def project_ctrl_kb(pid, data):
    proj = data["projects"][pid]
    running = is_alive(proj.get("pid"))
    kb = types.InlineKeyboardMarkup(row_width=2)
    if running:
        kb.add(
            types.InlineKeyboardButton("⏹ Stop", callback_data=f"stop:{pid}"),
            types.InlineKeyboardButton("🔄 Restart", callback_data=f"restart:{pid}"),
        )
    else:
        kb.add(types.InlineKeyboardButton("▶️ Run", callback_data=f"run:{pid}"))
    kb.add(
        types.InlineKeyboardButton("📜 Logs", callback_data=f"logs:{pid}"),
        types.InlineKeyboardButton("⚙️ Set Entry", callback_data=f"setentry:{pid}"),
    )
    kb.add(types.InlineKeyboardButton("🗑 Delete", callback_data=f"del:{pid}"))
    kb.add(types.InlineKeyboardButton("⬅️ Back", callback_data="list"))
    return kb


# ---------------- commands ----------------

@bot.message_handler(commands=["start", "help"])
def cmd_start(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "🚫 Not authorized.")
        return
    bot.send_message(
        m.chat.id,
        "🤖 <b>Hosting Bot</b>\n\n"
        "Send me a <b>.py file</b> or a <b>.zip</b> of your project and I'll host it.\n\n"
        "Commands:\n"
        "/list - all projects\n"
        "/status - overall status\n",
        reply_markup=main_menu_kb(),
    )


@bot.message_handler(commands=["list"])
def cmd_list(m):
    if not is_admin(m.from_user.id):
        return
    data = load_data()
    bot.send_message(m.chat.id, "📁 <b>Your Projects</b>", reply_markup=project_list_kb(data))


@bot.message_handler(commands=["status"])
def cmd_status(m):
    if not is_admin(m.from_user.id):
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
        f"📊 <b>Status</b>\n\n"
        f"Total projects: <b>{total}</b>\n"
        f"🟢 Running: <b>{running}</b>\n"
        f"🔴 Stopped: <b>{total - running}</b>\n"
        f"💾 Storage used: <b>{size_mb:.1f} MB</b>"
    )


# ---------------- file upload ----------------

@bot.message_handler(content_types=["document"])
def handle_doc(m):
    if not is_admin(m.from_user.id):
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
    }
    save_data(data)
    bot.edit_message_text("⬇️ Downloaded. Detecting Python modules and installing dependencies...", m.chat.id, msg.message_id)
    dep_ok, dep_message = ensure_dependencies(proj_id, data)
    dep_note = "✅ Dependencies ready." if dep_ok else f"⚠️ <b>Dependency installation failed:</b>\n<pre>{html.escape(dep_message[-1800:])}</pre>"

    entry_note = f"Entry file: <code>{entry}</code>" if entry else "⚠️ Couldn't auto-detect entry file — set it with ⚙️ Set Entry."
    bot.edit_message_text(
        f"✅ Uploaded: <b>{name}</b>\n{entry_note}\n\n{dep_note}",
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
    if not is_admin(c.from_user.id):
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

    elif ":" in action:
        cmd, pid = action.split(":", 1)
        if pid not in data["projects"]:
            bot.answer_callback_query(c.id, "Project not found (deleted?).")
            return
        proj = data["projects"][pid]

        if cmd == "open":
            running = is_alive(proj.get("pid"))
            txt = f"📦 <b>{proj['name']}</b>\nStatus: {'🟢 running' if running else '🔴 stopped'}\nEntry: <code>{proj.get('entry') or 'not set'}</code>"
            bot.edit_message_text(txt, c.message.chat.id, c.message.message_id, reply_markup=project_ctrl_kb(pid, data))

        elif cmd == "run":
            ok, msg = start_project(pid, data)
            bot.answer_callback_query(c.id, msg)
            data = load_data()
            bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=project_ctrl_kb(pid, data))

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
