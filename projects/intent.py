"""IntentRouter: plain-English project commands -> assistant calls.

In the live voice session Gemini itself picks the tool and fills in the
arguments (see main.py's TOOLS), in whatever language you speak. This
router is the deterministic path for everything else: the command line
(`python -m projects "open homemade project"`), tests, and any text front
end. Order matters - the first matching rule wins.
"""
from __future__ import annotations

import dataclasses
import re


@dataclasses.dataclass(frozen=True)
class Intent:
    action: str
    args: dict = dataclasses.field(default_factory=dict)


_MODE = r"(?:\s+and\s+(?P<mode>summari[sz]e(?:\s+(?:its|the))?(?:\s+status)?|resume(?:\s+work)?|show status))?"

_RULES: list[tuple[str, str, dict]] = [
    (r"^open (?:the )?(?:last|previous|recent) project" + _MODE + "$", "open_project", {"name": "last"}),
    (r"^open (?:the )?(?:project notes|notes)(?: (?:for|of) (?P<project>.+?))?(?: project)?$", "open_notes", {}),
    (r"^open (?:the )?(?P<project>.+?) (?:project )?notes$", "open_notes", {}),
    (r"^open (?:the )?project (?P<name>.+?)" + _MODE + "$", "open_project", {}),
    (r"^open (?:the |my )?(?P<name>.+?) project" + _MODE + "$", "open_project", {}),
    (r"^(?:resume|continue) (?:work(?:ing)? on )?(?:the )?(?P<name>.+?)(?: project)?$", "open_project", {"mode": "resume"}),
    (r"^(?:list|show) (?:my |all )?projects$", "list_projects", {}),
    (r"^(?:daily (?:project )?(?:summary|standup)|standup|daily standup)$", "daily_summary", {}),
    (r"^what (?:was|am) i working on(?: in (?P<project>.+))?$", "what_was_i_working_on", {}),
    (r"^(?:show |what are )?(?:the )?(?:pending|open|next|remaining) tasks(?: in (?P<project>.+))?$", "list_tasks", {"status": "pending"}),
    (r"^show (?:the )?next tasks$", "list_tasks", {"status": "pending"}),
    (r"^(?:show |list )?(?:the )?(?:completed|done|finished) tasks$", "list_tasks", {"status": "done"}),
    (r"^(?:show |list )?(?:the )?blocked tasks$", "list_tasks", {"status": "blocked"}),
    (r"^(?:show |list )?(?:the )?tasks in progress$", "list_tasks", {"status": "in progress"}),
    (r"^what(?:'s| is) blocking (?:progress|me|us)$", "blockers", {}),
    (r"^(?:show |list |what are )?(?:the )?blockers$", "blockers", {}),
    (r"^what changed (?P<period>today|this week|this month|recently)$", "what_changed", {}),
    (r"^what should i (?:do|work on) next$", "next_actions", {}),
    (r"^(?:suggest|recommend) (?:the )?next (?:steps?|actions?)$", "next_actions", {}),
    (r"^(?:summari[sz]e|show) (?:the )?project status$|^project status$|^(?:what is|what's) the (?:project )?status$", "project_status", {}),
    (r"^(?:explain|describe) (?:the )?(?:project )?architecture$", "code", {"action": "architecture"}),
    (r"^(?:find|show|list) (?:all )?(?:the )?todo(?:s| comments)?$", "code", {"action": "todos"}),
    (r"^(?:search|grep) (?:the )?(?:project|code|files)? ?for (?P<query>.+)$", "code", {"action": "search"}),
    (r"^(?:summari[sz]e|describe) (?:the )?(?:module|folder|file) (?P<query>.+)$", "code", {"action": "module"}),
    (r"^(?:explain|show|read) (?:the )?(?:code in |file )(?P<query>.+)$", "code", {"action": "read"}),
    (r"^(?:suggest refactor\w*|suggest (?:missing )?tests|identify (?:technical|tech) debt|code health)(?: opportunities)?$", "code", {"action": "health"}),
    (r"^add (?:a )?(?:(?P<priority>high|low|medium)[- ]priority )?task:? (?P<title>.+)$", "add_task", {}),
    (r"^(?:mark|set) (?:task )?(?P<title>.+?) (?:as )?(?:complete|completed|done|finished)$", "update_task", {"status": "done"}),
    (r"^(?:complete|finish) task (?P<title>.+)$", "update_task", {"status": "done"}),
    (r"^(?:mark|set) (?:task )?(?P<title>.+?) (?:as )?blocked$", "update_task", {"status": "blocked"}),
    (r"^(?:start|begin) (?:task|working on) (?P<title>.+)$", "update_task", {"status": "in progress"}),
    (r"^(?:we |i )?decided (?:to |that )?(?P<text>.+)$", "record", {"kind": "decision"}),
    (r"^(?:decision|note|blocker|goal):? (?P<text>.+)$", "record", {}),
    (r"^(?:note that|remember that) (?P<text>.+)$", "record", {"kind": "note"}),
    (r"^(?:i'?m |i am )?(?:done|finished) (?:for today|working)$|^end (?:work )?session$|^finish work$", "end_work_session", {}),
]
_COMPILED = [(re.compile(rx, re.IGNORECASE), action, fixed) for rx, action, fixed in _RULES]
_PERIOD_DAYS = {"today": 1, "this week": 7, "recently": 7, "this month": 30}


def _clean(text: str) -> str:
    text = re.sub(r"^\s*(?:hey |ok |okay )?jarvis[,:]?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^(?:please |can you |could you )+", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s+(?:please)$", "", text, flags=re.IGNORECASE)
    return " ".join(text.strip().rstrip("?.!").split())


def route(text: str) -> Intent:
    cleaned = _clean(text)
    for rx, action, fixed in _COMPILED:
        m = rx.match(cleaned)
        if not m:
            continue
        args = dict(fixed)
        groups = {k: v for k, v in m.groupdict().items() if v}
        if "mode" in groups:
            mode = groups.pop("mode").lower()
            args["mode"] = "resume" if mode.startswith("resume") else "summary"
        if "period" in groups:
            args["days"] = _PERIOD_DAYS[groups.pop("period").lower()]
        if action == "record" and "kind" not in args:
            args["kind"] = re.match(r"\w+", cleaned).group(0).lower()
        args.update(groups)
        if action == "end_work_session":
            # the two answers come from asking the user, not from this line
            args.setdefault("accomplished", None)
        return Intent(action, args)
    return Intent("unknown", {"text": cleaned})


def dispatch(intent: Intent, assistant) -> dict:
    """Run a routed intent against a ProjectAssistant."""
    if intent.action == "unknown":
        return {"ok": False, "message": f"I don't know how to do '{intent.args.get('text')}'."}
    args = dict(intent.args)
    if intent.action == "end_work_session" and not args.get("accomplished"):
        return {"ok": False, "needs_input": ["accomplished", "next_steps"],
                "message": "What did you accomplish today? And what should be done next?"}
    return getattr(assistant, intent.action)(**args)
