"""Keep the bot running in the background on Windows, with no console window.

Run with pythonw.exe (start-bot.bat / the autostart task do this). Restarts bot.py 10s after it exits.
"""
import ctypes
import os
import subprocess
import time
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
LOG = LOG_DIR / "bot.log"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


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


def main() -> None:
    # Only one runner at a time (e.g. autostart + double-clicking start-bot.bat)
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\DiscordLLMBotRunner")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return

    LOG_DIR.mkdir(exist_ok=True)
    env = dict(os.environ, PATH=fresh_path(), PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    while True:
        if LOG.exists() and LOG.stat().st_size > 5 * 1024 * 1024:
            LOG.replace(LOG_DIR / "bot.old.log")
        with open(LOG, "ab") as f:
            subprocess.run(
                [str(PYTHON), "bot.py"], cwd=ROOT, env=env,
                stdout=f, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        time.sleep(10)


if __name__ == "__main__":
    main()
