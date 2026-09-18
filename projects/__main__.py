"""Command line for the project co-pilot - same brain as voice Jarvis.

    python -m projects "open homemade project"
    python -m projects "what should i work on next"
    python -m projects "add high priority task: kitchen availability screen"
    python -m projects "daily project summary"
    python -m projects --no-editor "open homemade project and resume work"
    python -m projects            # interactive prompt
"""
from __future__ import annotations

import argparse
import logging
import sys

from .assistant import get_assistant
from .intent import dispatch, route


def _run(text: str, open_editor: bool) -> None:
    assistant = get_assistant()
    intent = route(text)
    if intent.action == "open_project":
        intent.args["open_editor"] = open_editor
    if intent.action == "end_work_session" and not intent.args.get("accomplished"):
        intent.args["accomplished"] = input("What did you accomplish today? ").strip()
        intent.args["next_steps"] = input("What should be done next? ").strip()
    result = dispatch(intent, assistant)
    print(result.get("message", result))
    if result.get("content"):
        print(result["content"])


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="python -m projects", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="*", help="what to do, in plain English")
    parser.add_argument("--no-editor", action="store_true", help="don't launch VS Code")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)

    if args.command:
        _run(" ".join(args.command), not args.no_editor)
        return 0
    print("Project co-pilot. Type a command, or 'quit'.")
    while True:
        try:
            text = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            return 0
        if text.lower() in {"quit", "exit", "q"}:
            return 0
        if text:
            _run(text, not args.no_editor)


if __name__ == "__main__":
    sys.exit(main())
