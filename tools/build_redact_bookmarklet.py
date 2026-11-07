"""Build a one-line browser bookmarklet from tools/demo_redact.js.

Usage: python3 tools/build_redact_bookmarklet.py
Writes tools/demo_redact.bookmarklet.txt; paste its content as the URL of a new bookmark.
"""
from pathlib import Path
import re

SRC = Path(__file__).with_name("demo_redact.js")
OUT = Path(__file__).with_name("demo_redact.bookmarklet.txt")


def main() -> None:
    code = re.sub(r"/\*.*?\*/", "", SRC.read_text(encoding="utf-8"), flags=re.S)
    code = " ".join(line.strip() for line in code.splitlines() if line.strip())
    OUT.write_text("javascript:" + code + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
