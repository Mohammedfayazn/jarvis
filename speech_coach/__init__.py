"""Child Speech Coach: playful language practice for a young multilingual
child (English, Dutch, Hindi). A practice companion - not a medical or
diagnostic tool.

    coach.py          SpeechCoach             use cases + the 10-minute daily session
    conversation.py   ConversationEngine      starters, follow-ups, sentence analysis
    pronunciation.py  PronunciationAnalyzer   local Whisper + gentle word comparison
    vocabulary.py     VocabularyTrainer       48 picture words in 3 languages
    storytelling.py   StorytellingModule      emoji picture scenes to talk about
    tracker.py        ProgressTracker         local SQLite progress
    dashboard.py      ParentDashboard         weekly summary for parents
"""
from .coach import SpeechCoach
from .dashboard import DISCLAIMER

__all__ = ["SpeechCoach", "DISCLAIMER"]
