"""Native OS folder / file pickers, opened by the (local) server on the user's desktop.

- macOS: the standard Finder chooser via ``osascript``.
- Linux: ``zenity`` or ``kdialog`` if installed.
- Windows, or Linux without those: Tk's native dialog (``tkinter``).

Each function returns the chosen path(s), ``None`` if the user cancelled, and
raises ``NotSupported`` when no dialog can be shown (e.g. a headless server);
the UI then falls back to its in-page folder browser.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

VIDEO_UTI = '{"public.movie", "public.mpeg-4", "com.apple.quicktime-movie", "public.avi", "public.mpeg"}'


class NotSupported(RuntimeError):
    pass


def _osa_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _run_osascript(script: str) -> str | None:
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        if "-128" in r.stderr:  # user cancelled
            return None
        raise NotSupported(r.stderr.strip() or "osascript failed")
    return r.stdout


def _mac_pick(kind: str, title: str, start: str | None) -> list[str] | None:
    loc = f" default location (POSIX file {_osa_str(start)})" if start and Path(start).is_dir() else ""
    if kind == "folder":
        body = f"set f to choose folder with prompt {_osa_str(title)}{loc}\nreturn POSIX path of f"
    else:
        body = (f"set fs to choose file with prompt {_osa_str(title)}{loc} of type {VIDEO_UTI} "
                "with multiple selections allowed\n"
                "set out to \"\"\nrepeat with f in fs\nset out to out & POSIX path of f & linefeed\nend repeat\nreturn out")
    # `activate` brings the chooser in front of the browser (no automation permission needed).
    out = _run_osascript(f"activate\n{body}")
    if out is None:
        return None
    return [p.rstrip("/") or "/" for p in out.splitlines() if p.strip()]


def _linux_pick(kind: str, title: str, start: str | None) -> list[str] | None:
    if shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", f"--title={title}"]
        if kind == "folder":
            cmd.append("--directory")
        else:
            cmd += ["--multiple", "--separator=\n", "--file-filter=Videos | *.mp4 *.avi *.mov *.mpg *.mkv"]
        if start:
            cmd.append(f"--filename={start.rstrip('/')}/")
    elif shutil.which("kdialog"):
        if kind == "folder":
            cmd = ["kdialog", "--title", title, "--getexistingdirectory", start or str(Path.home())]
        else:
            cmd = ["kdialog", "--title", title, "--multiple", "--separate-output",
                   "--getopenfilename", start or str(Path.home()), "*.mp4 *.avi *.mov *.mpg *.mkv"]
    else:
        return _tk_pick(kind, title, start)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 1:  # cancelled
        return None
    if r.returncode != 0:
        raise NotSupported(r.stderr.strip() or f"{cmd[0]} failed")
    return [p for p in r.stdout.splitlines() if p.strip()]


_TK_SCRIPT = r"""
import sys, tkinter as tk
from tkinter import filedialog
kind, title, start = sys.argv[1], sys.argv[2], sys.argv[3] or None
root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
if kind == "folder":
    p = filedialog.askdirectory(title=title, initialdir=start, mustexist=True)
    print(p or "")
else:
    ps = filedialog.askopenfilenames(title=title, initialdir=start,
        filetypes=[("Videos", "*.mp4 *.avi *.mov *.mpg *.mkv"), ("All files", "*.*")])
    print("\n".join(ps))
"""


def _tk_pick(kind: str, title: str, start: str | None) -> list[str] | None:
    # A separate process: Tk must own its main thread, and the server's isn't free.
    r = subprocess.run([sys.executable, "-c", _TK_SCRIPT, kind, title, start or ""], capture_output=True, text=True)
    if r.returncode != 0:
        raise NotSupported(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "Tk dialog failed")
    out = [p for p in r.stdout.splitlines() if p.strip()]
    return out or None


def pick(kind: str, title: str = "Choose", start: str | None = None) -> list[str] | None:
    """kind: "folder" (returns [path]) or "videos" (one or more video files)."""
    system = platform.system()
    if system == "Darwin" and shutil.which("osascript"):
        return _mac_pick(kind, title, start)
    if system == "Linux":
        return _linux_pick(kind, title, start)
    return _tk_pick(kind, title, start)


def reveal(path: str | Path) -> None:
    """Show a folder in the OS file manager (Finder, the desktop's file browser, Explorer)."""
    path = str(path)
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", path])
    elif system == "Windows":
        import os

        os.startfile(path)  # type: ignore[attr-defined]
    elif shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", path])
    else:
        raise NotSupported("no file manager found (xdg-open)")
