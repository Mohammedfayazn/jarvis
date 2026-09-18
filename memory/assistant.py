"""Assistant-facing layer: the functions main.py wires up as Gemini tools.

Every function here takes plain tool arguments and returns a dict with a
"message" field in Hinglish, matching the contract every other tool in
this project follows (see window_manager.py / browser_tools.py) - the
system prompt tells Jarvis to always relay result["message"] rather than
assume what happened.
"""
from __future__ import annotations

import logging

from .service import MemoryManager

logger = logging.getLogger("jarvis.memory.assistant")

_manager: MemoryManager | None = None


def configure(db_path=None) -> MemoryManager:
    """(Re)point the module-level manager at a specific database.

    Used by tests and memory/examples.py so they never touch the real
    memory file; main.py never needs to call this, the default path is
    fine for real use.
    """
    global _manager
    _manager = MemoryManager(db_path)
    return _manager


def _get_manager() -> MemoryManager:
    global _manager
    if _manager is None:
        _manager = MemoryManager()
    return _manager


def remember_this(category: str, key: str, value: str) -> dict:
    key = (key or "").strip()
    value = (value or "").strip()
    if not key or not value:
        return {"ok": False, "message": "Kya yaad rakhna hai, wo saaf nahi bataya."}
    manager = _get_manager()
    existing = manager.has_exact(key)
    memory = manager.add_memory(category, key, value)
    verb = "update kar diya" if existing else "yaad rakh liya"
    logger.info("%s memory '%s'", verb, memory.key)
    return {
        "ok": True,
        "memory": memory.as_dict(),
        "message": f"{memory.label}: {memory.value} - {verb}.",
    }


def recall_memory(query: str) -> dict:
    query = (query or "").strip()
    if not query:
        return {"ok": False, "message": "Kya yaad karna hai, wo saaf nahi bataya."}
    manager = _get_manager()
    matches = manager.search_memory(query)
    if not matches:
        return {
            "ok": True, "found": False,
            "message": "Mujhe iske baare me kuch yaad nahi.",
        }
    memory, _score = matches[0]
    return {
        "ok": True,
        "found": True,
        "memory": memory.as_dict(),
        "alternatives": [m.as_dict() for m, _ in matches[1:]],
        "message": f"{memory.label}: {memory.value}",
    }


def forget_memory(key: str) -> dict:
    key = (key or "").strip()
    if not key:
        return {"ok": False, "message": "Kaunsi baat bhoolni hai, wo saaf nahi bataya."}
    manager = _get_manager()
    existing = manager.get_memory(key)
    if existing is None:
        return {"ok": False, "message": f"'{key}' ke naam se kuch yaad nahi tha."}
    manager.delete_memory(key)
    logger.info("forgot memory '%s'", existing.key)
    return {"ok": True, "message": f"'{existing.label}' bhula diya."}


def list_memories(category: str | None = None) -> dict:
    manager = _get_manager()
    memories = manager.list_memories(category)
    if not memories:
        scope = f" {category} me" if category else ""
        return {
            "ok": True, "count": 0,
            "message": f"Abhi{scope} kuch bhi yaad nahi rakha hai.",
        }
    shown = memories[:10]
    lines = [f"{m.label}: {m.value}" for m in shown]
    more = f" (aur {len(memories) - len(shown)})" if len(memories) > len(shown) else ""
    return {
        "ok": True,
        "count": len(memories),
        "memories": [m.as_dict() for m in memories],
        "message": "; ".join(lines) + more,
    }
