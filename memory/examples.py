"""Runnable usage examples for the memory module.

    python -m memory.examples

Uses a throwaway database in the OS temp dir - never touches your real
memory - so it is safe to run any time to see the module end to end.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from . import assistant, intent


def main() -> None:
    tmp_path = Path(tempfile.gettempdir()) / "jarvis_memory_demo.db"
    tmp_path.unlink(missing_ok=True)
    assistant.configure(tmp_path)

    print("-- explicit remember commands --")
    for sentence in [
        "Remember that my daughter's school starts at 8:30",
        "Remember my office WiFi password is XYZ",
        "Remember my favorite editor is VS Code",
        "Remember that I live in Amsterdam",
    ]:
        parsed = intent.parse(sentence)
        result = assistant.remember_this(
            parsed.category or "Personal", parsed.key, parsed.value
        )
        print(f"  {sentence!r}\n    -> {result['message']}")

    print("\n-- recall by question --")
    for question in [
        "What time does my daughter's school start?",
        "Where do I live?",
    ]:
        result = assistant.recall_memory(question)
        print(f"  {question!r}\n    -> {result['message']}")

    print("\n-- automatic extraction (needs confirmation, nothing is stored) --")
    casual = "My favorite programming language is Python."
    candidate = intent.detect_storable_fact(casual)
    if candidate:
        print(
            f"  {casual!r}\n"
            f"    -> Jarvis would ask: 'Would you like me to remember that "
            f"{candidate.key} is {candidate.value}?'"
        )

    print("\n-- list everything --")
    print(" ", assistant.list_memories()["message"])

    assistant._get_manager().close()
    tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
