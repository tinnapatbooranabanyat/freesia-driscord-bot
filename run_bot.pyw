"""Keep the bot running in the background on Windows, with no console window.

Run with pythonw.exe (start-bot.bat / the autostart task do this). It
- restarts bot.py 10s after it exits or crashes,
- updates yt-dlp at startup and every day at UPDATE_HOUR (YouTube changes often and old yt-dlp versions
  stop working), restarting the bot when a new version was installed.
"""
import ctypes
import os
import subprocess
import time
import winreg
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG = LOG_DIR / "bot.log"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
UPDATE_HOUR = 5  # local time; quiet hours so a restart rarely interrupts music
NO_WINDOW = subprocess.CREATE_NO_WINDOW


def fresh_path() -> str:
    """Read PATH from the registry so ffmpeg/deno installed later are found without a reboot."""
    parts = []
    for hive, key in (
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(hive, key) as k:
                parts.append(os.path.expandvars(winreg.QueryValueEx(k, "Path")[0]))
        except OSError:
            pass
    return ";".join(parts) or os.environ.get("PATH", "")


def log(message: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} INFO runner: {message}\n")


def ytdlp_version() -> str:
    r = subprocess.run([str(PYTHON), "-c", "import yt_dlp.version as v; print(v.__version__)"],
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    return r.stdout.strip()


def update_ytdlp() -> bool:
    """pip install -U yt-dlp. Returns True if a new version was installed."""
    before = ytdlp_version()
    try:
        with open(LOG, "ab") as f:
            subprocess.run(
                [str(PYTHON), "-m", "pip", "install", "-q", "-U", "--disable-pip-version-check", "yt-dlp[default]"],
                stdout=f, stderr=subprocess.STDOUT, creationflags=NO_WINDOW, timeout=600,
            )
    except subprocess.TimeoutExpired:
        log("yt-dlp update timed out")
        return False
    after = ytdlp_version()
    if after != before:
        log(f"yt-dlp updated {before} -> {after}")
        return True
    log(f"yt-dlp is up to date ({after})")
    return False


def start_bot(env: dict) -> subprocess.Popen:
    if LOG.exists() and LOG.stat().st_size > 5 * 1024 * 1024:
        LOG.replace(LOG_DIR / "bot.old.log")
    f = open(LOG, "ab")
    proc = subprocess.Popen([str(PYTHON), "bot.py"], cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT,
                            creationflags=NO_WINDOW)
    f.close()  # the child keeps its own handle
    return proc


def stop_bot(proc: subprocess.Popen) -> None:
    # The venv python.exe is a launcher with the real interpreter as a child: kill the whole tree
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
    proc.wait(timeout=30)


def main() -> None:
    # Only one runner at a time (e.g. autostart + double-clicking start-bot.bat)
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\DiscordLLMBotRunner")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return

    LOG_DIR.mkdir(exist_ok=True)
    env = dict(os.environ, PATH=fresh_path(), PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    update_ytdlp()
    last_check = datetime.now().date()
    proc = start_bot(env)

    while True:
        time.sleep(30)
        if proc.poll() is not None:
            log(f"bot exited with code {proc.returncode}; restarting in 10s")
            time.sleep(10)
            proc = start_bot(env)
            continue
        now = datetime.now()
        if now.hour == UPDATE_HOUR and now.date() != last_check:
            last_check = now.date()
            if update_ytdlp():
                log("restarting bot to load the new yt-dlp")
                stop_bot(proc)
                proc = start_bot(env)


if __name__ == "__main__":
    main()
