"""Spoken command -> which system_control tool runs, with which arguments.

    python examples/system_intents_demo.py            # dry run, nothing happens
    python examples/system_intents_demo.py --run      # actually run the safe ones

In Jarvis itself there is no table like this: Gemini hears the sentence and
calls the tool (the declarations live in main.py `_system_tools`, the rules
in prompts.py). This file is the same mapping written out by hand, so the
routing can be read, tried and argued with without a microphone or an API
key - and it doubles as the list of phrases worth testing after a change.

Only read-only tools run with --run. Anything that changes the machine
(volume, brightness, files, power) is printed, never executed here: with
`delete` and `shutdown` the first call is only a question anyway, and the
token comes back to the caller.
"""
import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import system_control  # noqa: E402


def read_only(tool: str, call_args: dict) -> bool:
    """Does this call only look at the machine?"""
    action = (call_args.get("action") or "").lower()
    return (tool == "system_status"
            or (tool == "clipboard" and action == "read")
            or (tool == "manage_apps" and action == "list")
            or (tool == "manage_files" and action == "list")
            or action == "status")


NUMBER = r"(?P<n>\d{1,3})"

# (pattern, tool, how to build the arguments). First match wins, so the
# specific phrases come before the general ones.
INTENTS = [
    (rf"(?:set|rakho|karo)?\s*volume\s*(?:to|par|pe)?\s*{NUMBER}\s*%?",
     "control_volume", lambda m: {"action": "set", "percent": int(m["n"])}),
    (rf"volume\s*(?:up|badhao|increase)(?:\s*by)?\s*{NUMBER}?",
     "control_volume", lambda m: {"action": "up", "percent": int(m["n"] or 10)}),
    (rf"(?:volume|awaaz)\s*(?:down|kam karo|decrease)(?:\s*by)?\s*{NUMBER}?",
     "control_volume", lambda m: {"action": "down", "percent": int(m["n"] or 10)}),
    (r"\b(mute|chup karo)\b", "control_volume", lambda m: {"action": "mute"}),
    (r"\bunmute\b", "control_volume", lambda m: {"action": "unmute"}),

    (rf"brightness\s*(?:to|par|pe)?\s*{NUMBER}\s*%?",
     "control_brightness", lambda m: {"action": "set", "percent": int(m["n"])}),
    (rf"(?:screen|brightness)\s*(?:up|brighter|badhao)(?:\s*by)?\s*{NUMBER}?",
     "control_brightness", lambda m: {"action": "up", "percent": int(m["n"] or 10)}),
    (rf"(?:screen|brightness)\s*(?:down|dimmer|kam karo)(?:\s*by)?\s*{NUMBER}?",
     "control_brightness", lambda m: {"action": "down", "percent": int(m["n"] or 10)}),

    (r"(?:create|make|banao)\s+(?:a\s+)?(?:folder|directory)\s+(?P<path>.+)",
     "manage_files", lambda m: {"action": "create_folder", "path": m["path"].strip()}),
    (r"(?:what(?:'s| is) in|list|dikhao)\s+(?P<path>[a-zA-Z]:\\.+|/.+)",
     "manage_files", lambda m: {"action": "list", "path": m["path"].strip()}),
    (r"(?:move)\s+(?P<path>.+?)\s+(?:to|into)\s+(?P<destination>.+)",
     "manage_files", lambda m: {"action": "move", "path": m["path"].strip(),
                                "destination": m["destination"].strip()}),
    (r"(?:delete|remove|hatao)\s+(?P<path>[a-zA-Z]:\\.+|/.+)",
     "manage_files", lambda m: {"action": "delete", "path": m["path"].strip()}),

    (r"(?:cancel|stop|roko)\s+(?:the\s+)?shutdown",
     "power_control", lambda m: {"action": "cancel"}),
    (rf"(?:shutdown|shut down|band karo)\s*(?:pc|computer|laptop)?(?:\s*in\s*{NUMBER}\s*(?:seconds|minutes)?)?",
     "power_control", lambda m: {"action": "shutdown", "seconds": int(m["n"] or 60)}),
    (rf"restart\s*(?:pc|computer|laptop)?(?:\s*in\s*{NUMBER})?",
     "power_control", lambda m: {"action": "restart", "seconds": int(m["n"] or 60)}),
    (r"\block\b", "power_control", lambda m: {"action": "lock"}),
    (r"\b(sleep|so jao)\b", "power_control", lambda m: {"action": "sleep"}),
    (r"\bhibernate\b", "power_control", lambda m: {"action": "hibernate"}),

    (r"\b(pause|play|gaana roko|chalao)\b", "media_control",
     lambda m: {"action": "playpause"}),
    (r"\b(next|skip|agla)\b", "media_control", lambda m: {"action": "next"}),
    (r"\b(previous|pichla)\b", "media_control", lambda m: {"action": "previous"}),

    (r"(?:open|start|kholo)\s+(?P<name>[\w .+-]+)", "manage_apps",
     lambda m: {"action": "open", "name": m["name"].strip()}),
    (r"(?:force[- ]?close|kill|zabardasti band)\s+(?P<name>[\w .+-]+)", "manage_apps",
     lambda m: {"action": "force_close", "name": m["name"].strip()}),
    (r"(?:what(?:'s| is) running|running apps|kya chal raha)", "manage_apps",
     lambda m: {"action": "list"}),

    (r"(?:clipboard|copied).*(?:read|what|kya)|(?:read|what).*clipboard",
     "clipboard", lambda m: {"action": "read"}),
    (r"(?:copy|clipboard me daalo)\s+(?P<text>.+)", "clipboard",
     lambda m: {"action": "write", "text": m["text"].strip()}),

    (r"(?:battery|charge|pc kaisa|system status)", "system_status", lambda m: {}),
]

SAID = [
    "set volume to 30",
    "volume up by 15",
    "awaaz kam karo",
    "mute",
    "set brightness to 50%",
    "screen down by 20",
    "create folder D:\\Projects\\Jarvis_Logs",
    "what's in C:\\Users\\syeda\\Documents",
    "delete C:\\Users\\syeda\\Downloads\\old.zip",
    "shutdown pc in 120",
    "cancel shutdown",
    "lock",
    "next track",
    "open notepad",
    "what's running",
    "how much battery do I have",
    "make me a sandwich",
]


def route(said: str):
    r"""(tool name, arguments) for a spoken line, or (None, None).

    Matching is case-insensitive, but the captured text comes from the
    ORIGINAL line: `D:\Projects` must not turn into `d:\projects`.
    """
    text = " ".join((said or "").split())
    for pattern, tool, build in INTENTS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            groups = {name: (text[slice(*match.span(name))] if match.span(name) != (-1, -1)
                             else None)
                      for name in match.groupdict()}
            groups.setdefault("n", None)      # patterns without a number
            return tool, build(groups)
    return None, None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true",
                        help="actually call the read-only tools")
    parser.add_argument("said", nargs="*", help="a line to route (default: the examples)")
    args = parser.parse_args()

    for said in [" ".join(args.said)] if args.said else SAID:
        tool, call_args = route(said)
        print(f'\n🗣️  "{said}"')
        if tool is None:
            print("    no system control matches - Jarvis would just answer")
            continue
        print(f"    -> {tool}({call_args})")
        if args.run and read_only(tool, call_args):
            result = system_control.VOICE_TOOLS[tool](call_args)
            print(f"       {result['message'][:120]}")
        elif args.run:
            print("       (not run here: it would change the machine)")


if __name__ == "__main__":
    main()
