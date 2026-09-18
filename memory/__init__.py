"""Jarvis's personal memory module.

Layers (see each file's docstring for detail):
    db.py         - raw SQLite access: schema + CRUD SQL, no business logic
    models.py     - Memory dataclass and the fixed category vocabulary
    embeddings.py - optional sentence-transformers semantic search
    service.py    - MemoryManager: fuzzy + semantic recall on top of db.py
    intent.py     - rule-based "remember ..." / question understanding
    assistant.py  - dict-in/dict-out functions main.py wires up as tools

Import MemoryManager for direct use, or `assistant` for the tool-shaped
functions. See examples.py for a runnable walkthrough
(`python -m memory.examples`).
"""
from .models import CATEGORIES, Memory
from .service import MemoryManager

__all__ = ["MemoryManager", "Memory", "CATEGORIES"]
