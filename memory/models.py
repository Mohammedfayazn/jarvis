"""Data shapes and the fixed category vocabulary for the memory module."""
from __future__ import annotations

import dataclasses
import datetime as _dt
import difflib

CATEGORIES = ("Personal", "Family", "Work", "Preferences", "Projects", "Reminders")
DEFAULT_CATEGORY = "Personal"


def now_iso() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def normalize_category(category: str | None) -> str:
    """Map free-form category text onto the fixed vocabulary above.

    Unknown or missing input falls back to Personal rather than raising -
    a category is a filing aid, not something worth failing a save over.
    """
    if not category:
        return DEFAULT_CATEGORY
    category = category.strip()
    for known in CATEGORIES:
        if known.lower() == category.lower():
            return known
    match = difflib.get_close_matches(category, CATEGORIES, n=1, cutoff=0.6)
    return match[0] if match else DEFAULT_CATEGORY


@dataclasses.dataclass(frozen=True)
class Memory:
    id: int
    category: str
    key: str
    value: str
    created_at: str
    updated_at: str

    @classmethod
    def from_row(cls, row) -> "Memory":
        return cls(
            id=row["id"],
            category=row["category"],
            key=row["key"],
            value=row["value"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @property
    def label(self) -> str:
        return self.key.replace("_", " ")

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)
