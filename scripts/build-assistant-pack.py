"""Build the canonical assistant pack, or check it without writing files."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "listener" / "src"))

from tg_triage.assistant_pack import (  # noqa: E402
    AssistantPackError, CORE_FILE, PACK_FILE, PRINCIPLES_FILE, SOURCES, parse, render,
)
from tg_triage.assistant_core import REFERENCE_FILES, render_core


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--rules-dir", type=Path, default=ROOT / "listener" / "rules")
    parser.add_argument("--principles", type=Path,
                        default=ROOT / "listener" / "assistant" / PRINCIPLES_FILE)
    parser.add_argument("--out", type=Path,
                        default=ROOT / "listener" / "assistant" / PACK_FILE)
    parser.add_argument("--core-out", type=Path, help="Core bundle path (default: beside the full pack)")
    args = parser.parse_args()
    try:
        text = render(args.principles.read_bytes().decode("utf-8"), [
            (name, (args.rules_dir / name).read_bytes().decode("utf-8")) for name in SOURCES
        ])
        extras = {name: args.rules_dir / name for name in REFERENCE_FILES}
        core_out = args.core_out or args.out.with_name(CORE_FILE)
        if core_out.resolve() == args.out.resolve():
            raise AssistantPackError("the core and full pack paths must be distinct")
        core = (render_core(parse(text), {name: path.read_text(encoding="utf-8") for name, path in extras.items()})
                if args.core_out is not None or any(path.exists() for path in extras.values()) else None)
        if args.check:
            if parse(args.out.read_bytes().decode("utf-8")).text != text:
                raise AssistantPackError(f"{args.out}: assistant pack differs from rebuild")
            if core is not None and core_out.read_text(encoding="utf-8") != core:
                raise AssistantPackError(f"{core_out}: assistant core differs from rebuild")
        else:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(text.encode("utf-8"))
            if core is not None:
                core_out.parent.mkdir(parents=True, exist_ok=True)
                core_out.write_bytes(core.encode("utf-8"))
    except (OSError, UnicodeError, AssistantPackError) as exc:
        print(f"assistant pack: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
