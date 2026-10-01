import sys

from .cli import app

# Windows consoles and redirected output default to cp1252; story text and the ✓/✗/→ glyphs need UTF-8.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

app(prog_name="story")
