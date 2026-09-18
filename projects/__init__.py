"""Project Intelligence System: Jarvis as engineering co-pilot.

Layers (outer depends on inner, never the reverse):

    intent.py        IntentRouter       text command -> use case (CLI, tests)
    assistant.py     ProjectAssistant   use cases; what main.py's tools call
    recommend.py     RecommendationEngine
    brain.py         ProjectBrain       per-project memory (+ tasks.py TaskManager)
    git_analyzer.py  GitAnalyzer        read-only git
    indexer.py       ProjectIndexer     README/docs/manifests -> summary
    code_insights.py CodeInsights       search, TODOs, read, module summary, health
    locator.py       ProjectLocator     spoken name -> folder
    vscode.py        VSCodeController
    models.py        entities            db.py  SQLite schema

Personal facts ("my daughter's school starts at 8:30") stay in the
separate `memory` package's MemoryManager; this package remembers work.
"""
from .assistant import ProjectAssistant, configure, get_assistant
from .brain import ProjectBrain
from .git_analyzer import GitAnalyzer
from .indexer import ProjectIndexer
from .recommend import RecommendationEngine
from .tasks import TaskManager
from .vscode import VSCodeController

__all__ = [
    "ProjectAssistant", "ProjectBrain", "GitAnalyzer", "ProjectIndexer",
    "RecommendationEngine", "TaskManager", "VSCodeController",
    "configure", "get_assistant",
]
