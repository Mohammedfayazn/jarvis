"""Islamic tutor: Quran learning companion and family Quran teacher.

    knowledge.py  IslamicKnowledgeBase  verified Quran/hadith, labelled; answer policy
    teacher.py    QuranTeacher         alphabet, tajweed, verse-by-verse, duas, manners
    child.py      ChildLearningMode    short quizzes and praise for ages 4-6
    recitation.py RecitationAnalyzer   local speech model + word/letter comparison
    tracker.py    LearningTracker      per-learner progress, memorisation, weak areas
    planner.py    LessonPlanner        daily plan: Quran, dua, manners, revision
    assistant.py  IslamicTutor         use cases main.py's tools call
    sources.py    verified sources (alquran.cloud / Tanzil, hadith-api), cached
    curriculum.py what is taught - references only, never scripture text
"""
from .assistant import IslamicTutor
from .knowledge import ANSWER_POLICY

__all__ = ["IslamicTutor", "ANSWER_POLICY"]
