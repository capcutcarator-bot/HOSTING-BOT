# Termux Hosting Bot

## Setup (Termux)
```bash
pkg install python -y
pip install -r requirements.txt --break-system-packages
```

Open `bot.py`, edit the config block near the top (`BOT_TOKEN`, `ADMIN_ID`, etc.) — everything hardcoded, no `.env` needed.

Get your `ADMIN_ID` from @userinfobot on Telegram.

## Run (survives closing Termux app)
```bash
pkg install tmux -y
tmux new -s hostbot
python3 bot.py
```
Detach with `Ctrl+B` then `D`. Bot keeps running in background.
Reattach anytime: `tmux attach -t hostbot`

## Keep phone from killing Termux
```bash
termux-wake-lock
```
Run once per session (needs `termux-api` package + Termux:API app installed).

## Auto-start on phone reboot (optional)
Install **Termux:Boot** app from F-Droid, then create:
`~/.termux/boot/start-hostbot.sh`
```bash
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
cd ~/hosting_bot
tmux new -d -s hostbot 'python3 bot.py'
```
```bash
chmod +x ~/.termux/boot/start-hostbot.sh
```

## Usage in Telegram
- Send any `.py` file or `.zip` of a project → bot auto-detects entry file (main.py/bot.py/app.py etc.)
- If not auto-detected, tap **⚙️ Set Entry** and reply with the filename
- **▶️ Run / ⏹ Stop / 🔄 Restart / 📜 Logs / 🗑 Delete** — all via inline buttons
- `/list` — see all hosted projects with live 🟢/🔴 status
- `/status` — total projects, running count, storage used

## Notes
- Each project runs as its own process group (`setsid`) so stop/restart kills all its children too.
- `requirements.txt` inside an uploaded project is auto pip-installed on first run.
- State (pids, entry files) persists in `data.json` — safe across bot restarts (stopped processes just show as 🔴 until you hit Run again).

## Troubleshooting hosted projects

The bot now checks whether a process survives startup before showing it as running. If a project exits because of a missing package, missing file, syntax error, or another exception, the Run action reports the startup error and the Logs button shows the latest traceback.

If the project is marked running but does not answer Telegram messages, open **Logs** first. Also verify that the hosted project has its own valid bot token, that the token is not already being used by another running process, and that the entry file is the correct one.

### Automatic requirements installation

When you press **Run**, the bot checks the uploaded project for `requirements.txt` and automatically runs pip for every listed package before starting the entry file. Successful installations are remembered using a hash of the requirements file; if you change that file, the bot automatically installs the new or changed dependencies on the next Run. Installation failures stop the project from being marked as running and display the pip error so it can be corrected.

### Automatic module detection

The bot now scans every uploaded `.py` file using Python’s AST parser without executing the code. It ignores local modules and standard-library imports, maps common import names such as `cv2` → `opencv-python`, `PIL` → `Pillow`, `yaml` → `PyYAML`, `telegram` → `python-telegram-bot`, and `telebot` → `pyTelegramBotAPI`. Detected third-party packages are installed automatically immediately after upload, and `requirements.txt` entries are installed as well.

Import names and pip package names are not always identical. The bot includes common mappings, but unusual libraries, private packages, system-level dependencies, Git URLs, and packages with special build requirements may still need a manual `requirements.txt` entry or Termux package.
