"""Open text in the user's editor ($VISUAL / $EDITOR, else Notepad on Windows, vi elsewhere) and read it back."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _editor_cmd() -> list[str]:
    cmd = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    parts = shlex.split(cmd, posix=os.name != "nt") if cmd else (["notepad"] if sys.platform == "win32" else ["vi"])
    parts = [p[1:-1] if len(p) > 1 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    # On Windows, CreateProcess won't find "code" (code.cmd) or "venv/Scripts/python" (no .exe) by itself.
    exe = shutil.which(parts[0]) or parts[0]
    return [exe, *parts[1:]]


def edit_text(text: str, suffix: str = ".txt") -> str | None:
    """Returns the edited text, or None if the file was left unchanged. Blocks until the editor exits."""
    fd, name = tempfile.mkstemp(suffix=suffix, prefix="story_")
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        subprocess.run([*_editor_cmd(), str(path)], check=True)
        edited = path.read_text(encoding="utf-8-sig")  # Notepad may add a BOM
    finally:
        path.unlink(missing_ok=True)
    return None if edited.replace("\r\n", "\n") == text else edited.replace("\r\n", "\n")
