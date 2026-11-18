"""Build the canonical assistant pack, or check it without writing files."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "listener" / "src"))

from tg_triage.assistant_pack import (  # noqa: E402
    AssistantPackError, PACK_FILE, PRINCIPLES_FILE, SOURCES, parse, render,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rules-dir", type=Path, default=ROOT / "listener" / "rules")
    parser.add_argument("--principles", type=Path,
                        default=ROOT / "listener" / "assistant" / PRINCIPLES_FILE)
    parser.add_argument("--out", type=Path,
                        default=ROOT / "listener" / "assistant" / PACK_FILE)
    args = parser.parse_args()
    try:
        text = render(args.principles.read_bytes().decode("utf-8"), [
            (name, (args.rules_dir / name).read_bytes().decode("utf-8")) for name in SOURCES
        ])
        if args.check:
            if parse(args.out.read_bytes().decode("utf-8")).text != text:
                raise AssistantPackError(f"{args.out}: assistant pack differs from rebuild")
        else:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(text.encode("utf-8"))
    except (OSError, UnicodeError, AssistantPackError) as exc:
        print(f"assistant pack: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
