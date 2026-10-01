"""A non-interactive $EDITOR for scripted demos and tests.

Set EDITOR to "python scripts/demo_editor.py", plus:
  DEMO_EDIT_FIND / DEMO_EDIT_REPLACE   replace the first occurrence of FIND (regex) with REPLACE, and/or
  DEMO_EDIT_APPEND                     append this text to the end of the file.
The edited file is written back in place, exactly as a human editor would save it.
"""
import os
import re
import sys
from pathlib import Path


def main() -> None:
    path = Path(sys.argv[1])
    text = path.read_text(encoding="utf-8")
    find, replace = os.environ.get("DEMO_EDIT_FIND"), os.environ.get("DEMO_EDIT_REPLACE", "")
    if find:
        new, n = re.subn(find, replace, text, count=1)
        if n == 0:
            sys.exit(f"demo_editor: pattern not found: {find!r}")
        text = new
    if os.environ.get("DEMO_EDIT_APPEND"):
        text = text.rstrip("\n") + "\n\n" + os.environ["DEMO_EDIT_APPEND"] + "\n"
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
