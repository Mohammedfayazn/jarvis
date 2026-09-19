import argparse
import array
import asyncio
import datetime
import logging
import os
import queue
import sys
import threading

import pyaudio
from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types
from websockets.exceptions import ConnectionClosed

import app_paths
import browser_tools
import islamic
import projects
import speech_coach
import ui_server
import window_manager
from audio_runtime import AudioPlayer, UtteranceRecorder
from memory import assistant as memory_assistant
from prompts import instruction

LOG_MAX_BYTES = 5_000_000


def _setup_output():
    """Console ho toh UTF-8 (Windows ka cp1252 emoji par crash karta hai).
    Jarvis.exe (--noconsole) me print() aur errors hamesha log file me - chahe
    terminal se chalaya ho: tab stdout terminal ka mil jata hai, aur wahan
    koi use padh nahi raha hota."""
    if sys.stdout is not None and not app_paths.FROZEN:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        return
    path = app_paths.log_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        path.replace(path.with_name(path.name + ".1"))
    stream = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    sys.stdout = sys.stderr = stream
    print(f"\n===== Jarvis started {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====")


_setup_output()

# API key: pehle Jarvis ke data folder ki .env (packaged app yahi padhta hai),
# phir code ke paas wali (development)
load_dotenv(app_paths.env_file())
load_dotenv()

# Tutor ke kaam ki baatein terminal me (jaise recitation me kya suna gaya);
# baaki libraries sirf warning par bolein
logging.basicConfig(level=logging.WARNING, format="%(message)s")
logging.getLogger("jarvis.islamic").setLevel(logging.INFO)

MODEL = "gemini-3.1-flash-live-preview"

# Audio Settings (Gemini ke requirements ke mutabik)
MIC_RATE   = 16_000   # Mic input rate (Gemini 16kHz standard PCM leta hai)
SPK_RATE   = 24_000   # Speaker output rate (Gemini 24kHz output deta hai)
CHUNK_SIZE = 320      # 20ms audio chunks (16000 * 0.02 = 320 frames)

# Queue limits. Bounded isliye ki agar ek taraf peeche reh jaye toh memory na
# badhe - purana audio drop karna behtar hai, kyunki realtime me woh waise bhi
# ab kisi kaam ka nahi raha.
MIC_QUEUE_MAX = 100   # ~2 second mic audio
SPK_QUEUE_MAX = 400   # Gemini ke chunks bade hote hain

# Reconnect backoff
RECONNECT_MAX_DELAY = 30
STABLE_SECONDS = 30   # Itni der chala toh backoff reset kar do


class SessionExpiring(Exception):
    """Server ka GoAway - session ki time limit aa gayi. Khud band karke
    resume handle ke saath dobara judna hai, warna server 1008 se kaat deta
    hai."""

# HUD ko har 20ms wala level bhejna bekaar hai - har teesra chunk kaafi hai
LEVEL_EVERY = 3

# "So jao" ke baad alvida ka itna intezaar - model turn khatam na kare toh
# bhi Jarvis itne second me so jayega
SLEEP_FALLBACK = 15

# Sone ke baad itne mic chunks (20ms each) anasune - Jarvis ki apni alvida
# ki goonj se wake word na bhade. 50 = 1 second.
WAKE_GRACE_CHUNKS = 50

# Window tools ke saajhe parameters
_WINDOW_NAME = types.Schema(
    type=types.Type.STRING,
    description=(
        "App ya window ka naam jaisa user ne kaha: 'excel', 'outlook', "
        "'visual studio', 'vs code', 'chrome', 'pdf', 'teams', ya title ka "
        "hissa jaise 'budget report'."
    ),
)
_CONFIRM_TOKEN = types.Schema(
    type=types.Type.STRING,
    description=(
        "needs_confirmation wale nateeje ka token - sirf user ke saaf 'haan' "
        "ke baad bhejo."
    ),
)

# Project tools ke saajhe parameters
_PROJECT = types.Schema(
    type=types.Type.STRING,
    description=(
        "Project ka naam jaisa user ne kaha ('homemade', 'jarvis'). Khali "
        "chhodo toh abhi wala (sabse aakhri khola hua) project."
    ),
)
_TEXT = lambda description: types.Schema(type=types.Type.STRING, description=description)


def _project_tools():
    """Project co-pilot (projects/) - declared separately, TOOLS lamba na ho."""
    return [
        types.FunctionDeclaration(
            name="open_project",
            description=(
                "Project dhoondh kar VS Code me kholta hai, uski yaaddasht, git "
                "history aur docs padh kar haal batata hai. 'Homemade project "
                "kholo', 'open homemade project and resume work', 'open last "
                "project'. Nateeje ka message chhote me sunao: kab kaam hua, "
                "haal ki git activity, khule tasks, aur suggested next step."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "name": _TEXT("Project ka naam; 'last' = aakhri khola hua project."),
                "mode": _TEXT(
                    "'summary' (default: kholo + haal batao), 'resume' (+ pichhli "
                    "baar kya kiya aur aage kya tha), ya 'open' (sirf kholo)."),
                "open_editor": types.Schema(
                    type=types.Type.BOOLEAN,
                    description="VS Code kholna hai? Default true. Sirf status poochha ho toh false."),
            }),
        ),
        types.FunctionDeclaration(
            name="project_overview",
            description=(
                "Project ke baare me sawaalon ka jawab, stored memory + git + "
                "docs + pichhle work sessions se. view chuno: 'status' (project "
                "ka poora haal), 'working_on' ('main kya kar raha tha?'), "
                "'changes' ('is hafte kya badla?' - days ke saath), 'blockers' "
                "('kya rok raha hai?'), 'next_actions' ('ab kya karun?' - "
                "recommendations, wajah ke saath), 'daily_summary' (sab projects "
                "ka daily standup), 'list_projects', 'open_notes' (project notes "
                "file VS Code me)."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "view": _TEXT("status | working_on | changes | blockers | next_actions | "
                              "daily_summary | list_projects | open_notes"),
                "days": types.Schema(type=types.Type.INTEGER,
                                     description="Sirf 'changes' ke liye: aaj=1, hafta=7, mahina=30."),
                "project": _PROJECT,
            }, required=["view"]),
        ),
        types.FunctionDeclaration(
            name="add_project_task",
            description="Project me naya task: 'task add karo: kitchen availability screen'.",
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "title": _TEXT("Task ka chhota naam."),
                "priority": _TEXT("High, Medium ya Low. Na bola ho toh chhod do."),
                "description": _TEXT("Optional tafseel."),
                "project": _PROJECT,
            }, required=["title"]),
        ),
        types.FunctionDeclaration(
            name="update_project_task",
            description=(
                "Task ka status/priority badalta hai: 'kitchen screen complete "
                "ho gaya', 'payments task blocked hai', 'order workflow shuru "
                "kiya'. needs_choice aaye toh poochho kaunsa task."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "title": _TEXT("Task ka naam ya uska hissa."),
                "status": _TEXT("Todo, In Progress, Blocked ya Done."),
                "priority": _TEXT("Optional: High, Medium, Low."),
                "project": _PROJECT,
            }, required=["title"]),
        ),
        types.FunctionDeclaration(
            name="list_project_tasks",
            description="Tasks dikhata hai: pending (default), done, blocked, in progress, ya all.",
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "status": _TEXT("pending | done | blocked | in progress | all"),
                "project": _PROJECT,
            }),
        ),
        types.FunctionDeclaration(
            name="record_project_note",
            description=(
                "Project ki lambi yaaddasht me likhta hai. kind: goal, decision "
                "(architecture/technical faisla), note (implementation note), "
                "blocker, problem (baar baar aane wali dikkat), preference "
                "(coding pasand). Jab main koi faisla, goal, blocker ya note "
                "batau aur saaf ho ki project ke liye hai, tab chalao."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "kind": _TEXT("goal | decision | note | blocker | problem | preference"),
                "text": _TEXT("Jo yaad rakhna hai, ek saaf jumle me."),
                "scope": _TEXT("'global' sirf preference/problem ke liye jo har project par lage; "
                               "warna chhod do."),
                "project": _PROJECT,
            }, required=["kind", "text"]),
        ),
        types.FunctionDeclaration(
            name="recall_project_notes",
            description=(
                "Project ke faisle, notes, goals, blockers, problems, "
                "preferences yaad karta hai: 'auth ke liye humne kya decide "
                "kiya tha?', 'project goals kya hain?'. found false ho toh saaf "
                "kaho ki record nahi hai - andaaza mat lagao."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "kind": _TEXT("Optional: goal | decision | note | blocker | problem | preference"),
                "query": _TEXT("Optional: kis baare me, jaise 'firebase auth'."),
                "project": _PROJECT,
            }),
        ),
        types.FunctionDeclaration(
            name="resolve_project_blocker",
            description="Blocker hal ho gaya: 'Stripe wala blocker khatam ho gaya'.",
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "text": _TEXT("Blocker ka hissa jisse pehchana jaye."),
                "project": _PROJECT,
            }, required=["text"]),
        ),
        types.FunctionDeclaration(
            name="end_work_session",
            description=(
                "Aaj ka kaam session save karta hai. Jab main kahoon kaam khatam "
                "('aaj ke liye bas', 'done for today'), PEHLE poochho 'Aaj kya "
                "accomplish kiya?', jawab suno, phir poochho 'Aage kya karna "
                "hai?', jawab suno - tab dono jawab ke saath yeh chalao. Khud se "
                "jawab mat banao."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "accomplished": _TEXT("Mere jawab ka saar: aaj kya kiya."),
                "next_steps": _TEXT("Mere jawab ka saar: aage kya karna hai."),
                "project": _PROJECT,
            }, required=["accomplished"]),
        ),
        types.FunctionDeclaration(
            name="project_code",
            description=(
                "Project ke code ke saath kaam. action: 'architecture' "
                "(structure + docs), 'search' (query dhoondho), 'todos', 'read' "
                "(ek file padho - explain karne ke liye; query = file ka naam, "
                "start = line), 'module' (folder/file ka khulasa), 'health' "
                "(refactoring, missing tests, technical debt), 'diff' (uncommitted "
                "changes). Code sirf wahi explain karo jo nateeje me aaya."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "action": _TEXT("architecture | search | todos | read | module | health | diff"),
                "query": _TEXT("search ka text, ya file/folder ka naam ('app router', 'auth')."),
                "start": types.Schema(type=types.Type.INTEGER,
                                      description="Sirf 'read': kis line se padhna hai."),
                "project": _PROJECT,
            }, required=["action"]),
        ),
    ]


def _project_overview(a):
    pa = projects.get_assistant()
    view = (a.get("view") or "status").strip().lower().replace(" ", "_")
    project = a.get("project") or None
    views = {
        "status": lambda: pa.project_status(project),
        "working_on": lambda: pa.what_was_i_working_on(project),
        "changes": lambda: pa.what_changed(int(a.get("days") or 7), project),
        "blockers": lambda: pa.blockers(project),
        "next_actions": lambda: pa.next_actions(project),
        "daily_summary": pa.daily_summary,
        "list_projects": pa.list_projects,
        "open_notes": lambda: pa.open_notes(project),
    }
    if view not in views:
        return {"ok": False, "message": f"'{view}' view nahi hai: {', '.join(views)}."}
    return views[view]()


# Sab blocking hain (git, disk, SQLite, VS Code launch) - thread me chalte hain
PROJECT_TOOLS = {
    "open_project": lambda a: projects.get_assistant().open_project(
        a.get("name") or None, a.get("mode") or "summary",
        a.get("open_editor", True) is not False),
    "project_overview": _project_overview,
    "add_project_task": lambda a: projects.get_assistant().add_task(
        a.get("title", ""), a.get("priority"), a.get("description"),
        project=a.get("project") or None),
    "update_project_task": lambda a: projects.get_assistant().update_task(
        a.get("title", ""), a.get("status"), a.get("priority"), a.get("project") or None),
    "list_project_tasks": lambda a: projects.get_assistant().list_tasks(
        a.get("status") or "pending", a.get("project") or None),
    "record_project_note": lambda a: projects.get_assistant().record(
        a.get("kind", ""), a.get("text", ""), a.get("project") or None,
        a.get("scope") or "project"),
    "recall_project_notes": lambda a: projects.get_assistant().recall(
        a.get("kind"), a.get("query"), a.get("project") or None),
    "resolve_project_blocker": lambda a: projects.get_assistant().resolve_blocker(
        a.get("text", ""), a.get("project") or None),
    "end_work_session": lambda a: projects.get_assistant().end_work_session(
        a.get("accomplished"), a.get("next_steps"), a.get("project") or None),
    "project_code": lambda a: projects.get_assistant().code(
        a.get("action", ""), a.get("query"), a.get("project") or None, a.get("start")),
}


_STUDENT = _TEXT(
    "Kaun seekh raha hai: naam ('Zunaira', 'Fayaz'), ya 'child' / 'me'. Khali "
    "chhodo toh jo abhi seekh raha tha.")
_SURAH = _TEXT("Surah ka naam ya number: 'Al-Ikhlas' ya '112'. Naam dena behtar hai.")
_INT = lambda description: types.Schema(type=types.Type.INTEGER, description=description)


def _islamic_tools():
    """Islamic tutor (islamic/) - Quran companion aur family Quran teacher."""
    return [
        types.FunctionDeclaration(
            name="islamic_sources",
            description=(
                "Verified Islamic sources. Quran ya hadith ka koi bhi hissa BOLNE "
                "SE PEHLE yeh chalao - apni yaad se kabhi nahi. action: 'verse' "
                "(surah + ayah[, to_ayah]), 'search' (Quran translation me English "
                "word, query), 'hadith' (collection: bukhari, muslim, abudawud, "
                "tirmidhi, nasai, ibnmajah, malik, nawawi + number), 'topic' "
                "(manners/duas jaise 'eating', 'parents' - pehle se verified). "
                "Nateeje me QURAN/HADITH/LESSON alag likhe hote hain - bolte waqt "
                "bhi alag rakho, aur hadith ki grading batao."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "action": _TEXT("verse | search | hadith | topic"),
                "query": _TEXT("search/topic ke liye."),
                "surah": _SURAH, "ayah": _INT("Ayah number."),
                "to_ayah": _INT("Aakhri ayah (range ke liye)."),
                "collection": _TEXT("Hadith collection."), "number": _INT("Hadith number."),
            }, required=["action"]),
        ),
        types.FunctionDeclaration(
            name="quran_lesson",
            description=(
                "Quran/Islamic lesson shuru ya jaari: 'Teach Quran', 'start "
                "child's lesson', 'Zunaira ka sabaq', 'continue yesterday's "
                "lesson', 'next lesson'. Lesson screen par dikhta hai aur "
                "tilawat khud chalti hai. track: alphabet (Arabic huroof), "
                "tajweed, surah (hifz, ayah-ba-ayah), dua, manners, ya auto. "
                "mode: continue (jahan chhoda), next (agla sabaq), repeat. "
                "Message ka lesson apne andaaz me sikhao - bachche ke saath "
                "chhota, pyaar se."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "student": _STUDENT,
                "track": _TEXT("alphabet | tajweed | surah | dua | manners | auto"),
                "mode": _TEXT("continue | next | repeat"),
                "surah": _SURAH, "ayah": _INT("Kis ayah se (surah ke saath)."),
            }),
        ),
        types.FunctionDeclaration(
            name="lesson_done",
            description=(
                "Abhi ka sabaq poora hua (quiz/recitation achhi rahi, ya user ne "
                "kaha 'ho gaya, aage chalo') - mark karke agla sabaq dikhata hai."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={"student": _STUDENT}),
        ),
        types.FunctionDeclaration(
            name="play_quran",
            description=(
                "Quran ki tilawat chalata hai (Mishary Alafasy) aur ayaat screen "
                "par dikhata hai: 'Surah Mulk sunao', 'ayat 3 teen baar repeat "
                "karo'. Tilawat ke dauran main tumhe sun nahi sakta - pehle chhota "
                "sa bata do phir chalao."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "surah": _SURAH, "from_ayah": _INT("Pehli ayah (default 1)."),
                "to_ayah": _INT("Aakhri ayah (default: surah ka end)."),
                "repeat": _INT("Har ayah kitni baar (1-10)."),
            }, required=["surah"]),
        ),
        types.FunctionDeclaration(
            name="stop_quran_audio",
            description="Chal rahi tilawat rok deta hai.",
            parameters=types.Schema(type=types.Type.OBJECT, properties={}),
        ),
        types.FunctionDeclaration(
            name="test_recitation",
            description=(
                "Recitation/hifz test: 'Test Surah Al-Ikhlas', 'Zunaira ki surah "
                "suno'. Mic recording shuru hoti hai; tum usi waqt ek chhoti line "
                "bolo ('Chalo, shuru karo!') aur phir BILKUL chup raho. Jab "
                "padhne wala ruk jayega, nateeja '[Recitation result]' ke saath "
                "aayega - tab use pyaar se, apne shabdon me batao. show_text sirf "
                "tab true jab padhne ki practice ho, hifz test me nahi."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "student": _STUDENT, "surah": _SURAH,
                "from_ayah": _INT("Pehli ayah."), "to_ayah": _INT("Aakhri ayah."),
                "show_text": types.Schema(type=types.Type.BOOLEAN,
                                          description="Screen par ayaat dikhani hain? Default false."),
            }),
        ),
        types.FunctionDeclaration(
            name="quran_quiz",
            description=(
                "Bachche ka chhota quiz (huroof, agla lafz, dua, adab). Bina "
                "answer ke = naya sawaal; bachche ka jawab mile toh answer ke "
                "saath dobara chalao (jaisa bola: 'baa', 'number two', 'doosra'). "
                "Result ka message pyaar se sunao."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "student": _STUDENT,
                "answer": _TEXT("Bachche ka jawab, jaisa usne kaha."),
                "kind": _TEXT("Optional: letter_name | letter_find | next_word | dua | manners"),
            }),
        ),
        types.FunctionDeclaration(
            name="learner",
            description=(
                "Seekhne walon ki profiles: action 'add' (naam, role child/parent, "
                "bachche ki age), 'list', 'progress' (kya seekha, kya yaad hai, "
                "kahan kamzori), 'daily_plan' ('Daily Islamic lesson' - aaj ka "
                "Quran, dua, adab aur dohrai). Parent aur child ki progress alag."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "action": _TEXT("add | list | progress | daily_plan"),
                "name": _TEXT("Learner ka naam (add/progress/daily_plan)."),
                "role": _TEXT("child | parent (sirf add)."),
                "age": _INT("Bachche ki umar (sirf add)."),
            }, required=["action"]),
        ),
    ]


_CHILD = _TEXT("Bachche ka naam ('Zunaira'). Khali chhodo toh jo abhi practice kar raha tha.")
_LANG = _TEXT("Practice ki zubaan: en (English), nl (Dutch) ya hi (Hindi). Khali = bachche ki focus language.")


def _coach_tools():
    """Child speech coach (speech_coach/) - 4-6 saal ke bachche ke liye."""
    return [
        types.FunctionDeclaration(
            name="speech_coach",
            description=(
                "Bachche ka 10-minute speech session: action 'start' (plan + greeting), "
                "'next' (agla hissa: words, say-it-with-me, baat-cheet, kahani, sitare), "
                "'status', 'end'. Har nateeje ka message batata hai ab kya karna hai - "
                "wahi karo, bachche ki zubaan me, chhote khush jumlon me."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "action": _TEXT("start | next | status | end"),
                "child": _CHILD, "language": _LANG,
            }, required=["action"]),
        ),
        types.FunctionDeclaration(
            name="coach_words",
            description=(
                "Vocabulary: action 'learn' (naye picture words, category: animals, food, "
                "school, family, colors, nature), 'review' (dohrai), 'translate' (ek "
                "lafz English/Dutch/Hindi me - text me lafz do)."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "action": _TEXT("learn | review | translate"),
                "child": _CHILD, "language": _LANG,
                "category": _TEXT("animals | food | school | family | colors | nature"),
                "text": _TEXT("Sirf translate: jo lafz translate karna hai."),
            }, required=["action"]),
        ),
        types.FunctionDeclaration(
            name="practice_word",
            description=(
                "Bachcha ek lafz bolne ki practice kare: pehle tum lafz khush aur dheere "
                "bolo, phir yeh chalao aur CHUP raho. Nateeja '[Word practice result]' ke "
                "saath aayega - use pyaar se, apne shabdon me batao. Kabhi 'wrong' nahi."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "word": _TEXT("Practice ka lafz, jaise 'rabbit', 'hond', 'kutta'."),
                "child": _CHILD, "language": _LANG,
            }, required=["word"]),
        ),
        types.FunctionDeclaration(
            name="story_time",
            description=(
                "Picture scene screen par dikhata hai ('What do you see?') aur batata hai "
                "kaise ek-ek qadam se poora jumla banwana hai. scene: park, breakfast, "
                "rain, beach, farm, school, night, birthday - ya khali (agla)."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "child": _CHILD, "language": _LANG,
                "scene": _TEXT("Optional scene ka naam."),
            }),
        ),
        types.FunctionDeclaration(
            name="sentence_practice",
            description=(
                "Jab bachcha chhota jumla bole ('Dog running'), tum use bada karke bolo "
                "('The dog is running in the park') aur yeh chalao: child_said = jo usne "
                "kaha, bigger_sentence = tumhara bada jumla. Progress me jumlon ki "
                "lambai ka hisaab rehta hai."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "child_said": _TEXT("Bachche ne jo kaha, bilkul waise."),
                "bigger_sentence": _TEXT("Tumhara thoda bada, sahi jumla - usi zubaan me."),
                "child": _CHILD, "language": _LANG,
            }, required=["child_said"]),
        ),
        types.FunctionDeclaration(
            name="child_profile",
            description=(
                "Bachche ki profile aur parent report: action 'add' (naam, age, "
                "languages jaise 'nl,en,hi', focus_language), 'set_language', "
                "'dashboard' (hafte ki report parents ke liye - naye lafz, jumlon ki "
                "lambai, pronunciation practice, suggestions)."
            ),
            parameters=types.Schema(type=types.Type.OBJECT, properties={
                "action": _TEXT("add | set_language | dashboard"),
                "name": _TEXT("Bachche ka naam."),
                "age": types.Schema(type=types.Type.INTEGER, description="Umar (add)."),
                "languages": _TEXT("Comma se: 'nl,en,hi' (add)."),
                "focus_language": _LANG,
                "days": types.Schema(type=types.Type.INTEGER, description="Dashboard kitne din ka (default 7)."),
            }, required=["action"]),
        ),
    ]


def _coach() -> speech_coach.SpeechCoach:
    if COACH is None:
        raise RuntimeError("speech coach is not set up")
    return COACH


def _coach_words(a):
    action = (a.get("action") or "learn").lower()
    if action == "translate":
        return _coach().translate(a.get("text", ""))
    return _coach().words(a.get("child") or None, a.get("category"), a.get("language"),
                          review=action == "review")


def _child_profile(a):
    action = (a.get("action") or "").lower()
    if action == "add":
        langs = [x.strip() for x in (a.get("languages") or "").split(",") if x.strip()] or None
        return _coach().add_child(a.get("name", ""), a.get("age"), langs, a.get("focus_language"))
    if action == "set_language":
        return _coach().set_language(a.get("name") or None, a.get("focus_language", ""))
    if action == "dashboard":
        return _coach().parent_dashboard(a.get("name") or None, a.get("days") or 7)
    return {"ok": False, "message": "action: add, set_language ya dashboard."}


COACH_TOOLS = {
    "speech_coach": lambda a: _coach().session(
        a.get("action") or "start", a.get("child") or None, a.get("language")),
    "coach_words": _coach_words,
    "practice_word": lambda a: _coach().practice_word(
        a.get("child") or None, a.get("word", ""), a.get("language")),
    "story_time": lambda a: _coach().story(
        a.get("child") or None, a.get("scene"), a.get("language")),
    "sentence_practice": lambda a: _coach().sentence(
        a.get("child") or None, a.get("child_said", ""), a.get("bigger_sentence"), a.get("language")),
    "child_profile": _child_profile,
}


TUTOR: islamic.IslamicTutor | None = None
COACH: speech_coach.SpeechCoach | None = None
QURAN_PLAYER: AudioPlayer | None = None


def setup_islamic_tutor(loop, bus, playback, spk_queue):
    """Tutor ko HUD, speaker aur mic se jodna. Hooks thread-safe hain:
    tools asyncio.to_thread me chalte hain, HUD event loop par."""
    global TUTOR, QURAN_PLAYER
    player = QURAN_PLAYER = AudioPlayer(is_jarvis_speaking=lambda: playback.playing or not spk_queue.empty())
    recorder = UtteranceRecorder()
    playback.quran_player, playback.recorder = player, recorder

    def show(payload):
        loop.call_soon_threadsafe(bus.publish, {"type": "panel", "payload": payload})

    def record(on_done, child, expected_seconds):
        # people pause to recall the next ayah - children a little longer
        recorder.start(on_done, pause=4.0 if child else 3.0,
                       max_seconds=min(180.0, 15.0 + expected_seconds * 2.5))
        return True

    def notify(text):
        """Recitation ka nateeja baad me aata hai - Gemini ko text ki tarah bhejo."""
        print(f"\U0001f54c {text}")

        async def send():
            session = playback.session
            if session is None or not playback.connected:
                return
            try:
                await session.send_realtime_input(text=text)
            except Exception as exc:
                print(f"\U0001f54c nateeja bhej nahi paya: {exc}")
        asyncio.run_coroutine_threadsafe(send(), loop)

    def on_command(name):
        if name == "stop_audio":
            player.stop()
        elif name == "cancel_recitation":
            recorder.cancel()
            show({"mode": "feedback", "correct": False, "title": "Recitation cancelled",
                  "text": "Stopped listening."})

    bus.on_command = on_command
    TUTOR = islamic.IslamicTutor(display=show, play=player.play, record=record, notify=notify)

    def record_word(on_done, pause, max_seconds, no_speech):
        recorder.start(on_done, pause=pause, max_seconds=max_seconds, no_speech=no_speech)
        return True

    global COACH
    COACH = speech_coach.SpeechCoach(display=show, record=record_word, notify=notify)


def _tutor() -> islamic.IslamicTutor:
    if TUTOR is None:
        raise RuntimeError("Islamic tutor is not set up")
    return TUTOR


def _learner_tool(a):
    t = _tutor()
    action = (a.get("action") or "").strip().lower()
    if action == "add":
        return t.add_learner(a.get("name", ""), a.get("role") or None, a.get("age"))
    if action == "list":
        return t.learners()
    if action == "progress":
        return t.progress(a.get("name") or None)
    if action in ("daily_plan", "plan", "daily"):
        return t.daily_plan(a.get("name") or None)
    return {"ok": False, "message": "action: add, list, progress ya daily_plan."}


def _stop_quran(_a):
    if QURAN_PLAYER is None or not QURAN_PLAYER.busy:
        return {"ok": True, "message": "Koi tilawat nahi chal rahi thi."}
    QURAN_PLAYER.stop()
    return {"ok": True, "message": "Tilawat rok di."}


ISLAMIC_TOOLS = {
    "islamic_sources": lambda a: _tutor().sources(
        a.get("action", ""), a.get("query"), a.get("surah"), a.get("ayah"),
        a.get("to_ayah"), a.get("collection"), a.get("number")),
    "quran_lesson": lambda a: _tutor().lesson(
        a.get("student") or None, a.get("track") or "auto", a.get("mode") or "continue",
        a.get("surah"), a.get("ayah")),
    "lesson_done": lambda a: _tutor().complete_lesson(a.get("student") or None),
    "play_quran": lambda a: _tutor().play(
        a.get("surah"), a.get("from_ayah") or 1, a.get("to_ayah"), a.get("repeat") or 1),
    "stop_quran_audio": _stop_quran,
    "test_recitation": lambda a: _tutor().test_recitation(
        a.get("student") or None, a.get("surah"), a.get("from_ayah"), a.get("to_ayah"),
        bool(a.get("show_text", False))),
    "quran_quiz": lambda a: _tutor().quiz(
        a.get("student") or None, a.get("answer"), a.get("kind")),
    "learner": _learner_tool,
}


# Jo tools Jarvis awaaz se chala sakta hai
TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="close_unused_browser_windows",
                description=(
                    "Har khuli browser window band karta hai, siwaye kaam "
                    "wali window aur Jarvis ke apne HUD ke. SIRF tab chalao "
                    "jab user ki abhi wali baat me saaf WINDOWS band karne ko "
                    "kaha ho: 'bekaar browser windows band kar do'. Tabs ki "
                    "baat ho toh close_browser_tabs. Dhyan rahe: poori window "
                    "jati hai, uske saare tabs ke saath. Chalane ke baad user "
                    "ko batao ki kaun si windows band ki."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={},
                ),
            ),
            types.FunctionDeclaration(
                name="close_browser_tabs",
                description=(
                    "Kaam wali browser window ke tabs band karta hai. SIRF tab "
                    "chalao jab user ki abhi wali baat me saaf taur par TABS "
                    "band karne ko kaha ho - kisi aur baat (email, gaana, "
                    "sawaal) ke beech khud se kabhi nahi. Do tareeke, jo user "
                    "ne kaha usi hisaab se: `close` - sirf yeh band ('Gmail "
                    "aur WhatsApp band karo'); `keep` - yeh rakho, baaki band "
                    "('bas GitHub rakho'). Dono ek saath nahi. 'X band karo' "
                    "= close, 'X rakho' = keep - shak ho toh poochho. "
                    "`needs_choice` aaye toh poochho kaunsa rakhna hai. "
                    "`needs_confirmation` aaye toh KUCH BAND NAHI HUA: user "
                    "ko ginti aur kuch naam batao, poochho 'band karun?', aur "
                    "saaf 'haan' mile tabhi wahi close/keep aur "
                    "`confirm_token` ke saath dobara chalao. User ko nateeje "
                    "ka `message` batao."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "close": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.STRING),
                            description=(
                                "Band karne wale tabs ke naam ke hisse, jaise "
                                "['Gmail', 'WhatsApp']. Baaki sab khule rahenge."
                            ),
                        ),
                        "keep": types.Schema(
                            type=types.Type.ARRAY,
                            items=types.Schema(type=types.Type.STRING),
                            description=(
                                "Rakhne wale tabs ke naam ke hisse, jaise "
                                "['GitHub']. Baaki sab band honge."
                            ),
                        ),
                        "confirm_token": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "needs_confirmation wale nateeje ka token - "
                                "sirf user ke saaf 'haan' ke baad bhejo."
                            ),
                        ),
                    },
                ),
            ),
            types.FunctionDeclaration(
                name="play_on_youtube",
                description=(
                    "YouTube par gaana ya video dhoondh kar browser me chala "
                    "deta hai. Tab chalao jab user kuch bajane ko kahe - "
                    "'Arijit ka tum hi ho laga do', 'koi lofi chala do', "
                    "'YouTube pe ... play karo'. Chalane ke baad user ko "
                    "chhote me batao kya laga diya."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "YouTube search - gaane ka naam aur artist, "
                                "jaise 'tum hi ho arijit singh'. User ki "
                                "baat ka matlab likho, uske poore shabd nahi "
                                "('bhai woh gaana laga do' nahi)."
                            ),
                        ),
                    },
                    required=["query"],
                ),
            ),
            types.FunctionDeclaration(
                name="go_to_sleep",
                description=(
                    "Jarvis ko sula deta hai - baatcheet ruk jati hai aur "
                    "Jarvis sunna band kar deta hai, jab tak user 'Hey "
                    "Jarvis' bol kar na jagaye. Sirf tab chalao jab user saaf "
                    "taur par JARVIS ko rukne ya sone ko kahe: 'Jarvis so "
                    "jao', 'bas karo Jarvis', 'Jarvis stop', 'good night "
                    "Jarvis'. Gaane ke bol ya background awaaz me 'stop' aaye "
                    "toh MAT chalao. Ek chhota sa alvida bolo."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={},
                ),
            ),
            # --- Window manager (window_manager.py) ---
            types.FunctionDeclaration(
                name="list_open_windows",
                description=(
                    "Computer par khuli saari windows batata hai - app aur "
                    "title. Tab chalao jab user poochhe kya khula hai, ya "
                    "kisi window ka sahi naam jaanna ho."
                ),
                parameters=types.Schema(type=types.Type.OBJECT, properties={}),
            ),
            types.FunctionDeclaration(
                name="close_window",
                description=(
                    "Naam se ek app ki window band karta hai: 'Excel band "
                    "karo', 'close Outlook', 'close visual studio', 'PDF band "
                    "karo'. 'current browser tab' do toh sirf saamne wala "
                    "browser tab band hota hai. SIRF tab chalao jab user ki "
                    "abhi wali baat me saaf band karne ko kaha ho. "
                    "`needs_confirmation` aaye toh KUCH BAND NAHI HUA - "
                    "`message` wala sawaal user se poochho, aur saaf 'haan' "
                    "mile tabhi wahi name aur `confirm_token` ke saath "
                    "dobara chalao."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "name": _WINDOW_NAME,
                        "confirm_token": _CONFIRM_TOKEN,
                    },
                    required=["name"],
                ),
            ),
            types.FunctionDeclaration(
                name="close_all_windows",
                description=(
                    "Ek app ki SAARI windows band: 'saare Chrome band karo', "
                    "'close all Excel windows', 'close all browser windows'. "
                    "Sirf jab user ne 'saari/all' kaha ho. needs_confirmation "
                    "ka niyam close_window jaisa."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "app": types.Schema(
                            type=types.Type.STRING,
                            description="App ka naam: 'chrome', 'excel', 'outlook', 'browser'.",
                        ),
                        "confirm_token": _CONFIRM_TOKEN,
                    },
                    required=["app"],
                ),
            ),
            types.FunctionDeclaration(
                name="close_all_windows_except_current",
                description=(
                    "Current window chhod kar computer ki SAARI windows band. "
                    "Sirf jab user saaf yahi kahe: 'current ke alawa sab band "
                    "karo'. Yeh HAMESHA pehle needs_confirmation deta hai - "
                    "message padh kar sunao (usme likha hai kaunsi window "
                    "bachegi), aur saaf 'haan' ke baad hi confirm_token ke "
                    "saath dobara chalao."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"confirm_token": _CONFIRM_TOKEN},
                ),
            ),
            types.FunctionDeclaration(
                name="focus_window",
                description=(
                    "App ki window aage laata hai: 'Outlook pe jao', 'switch "
                    "to VS Code', 'Chrome saamne lao'. Kuch band nahi karta."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"name": _WINDOW_NAME},
                    required=["name"],
                ),
            ),
            types.FunctionDeclaration(
                name="minimize_window",
                description="App ki window(s) minimize: 'Teams minimize karo'.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"name": _WINDOW_NAME},
                    required=["name"],
                ),
            ),
            types.FunctionDeclaration(
                name="maximize_window",
                description="App ki window maximize karke aage: 'browser maximize karo'.",
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"name": _WINDOW_NAME},
                    required=["name"],
                ),
            ),
            # --- Personal memory (memory/) ---
            types.FunctionDeclaration(
                name="remember_this",
                description=(
                    "Ek chhoti si baat hamesha ke liye yaad rakh leta hai - "
                    "sleep aur restart ke baad bhi rehti hai, hamari abhi ki "
                    "baatcheet ki tarah nahi bhoolti. Jab tak main khud 'yaad "
                    "rakho' ya 'remember' na kahoon, kabhi khud se mat "
                    "chalana. Jaise: 'yaad rakho meri beti ka school 8:30 "
                    "baje shuru hota hai', 'remember my wifi password is "
                    "XYZ', 'yaad rakho main Amsterdam me rehta hoon'. Agar "
                    "isi naam se pehle se kuch yaad hai, yeh use update kar "
                    "deta hai - dobara poochhne ki zaroorat nahi. Agar "
                    "baatcheet me tumhe khud koi yaad rakhne layak naya fact "
                    "dikhe (jaise 'mera favorite editor VS Code hai') jo "
                    "maine seedha yaad rakhne ko nahi kaha, pehle poochho "
                    "'yeh yaad rakh loon?' - saaf 'haan' milne ke baad hi "
                    "yeh tool chalao."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "category": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Personal, Family, Work, Preferences, "
                                "Projects, ya Reminders me se sabse sahi. "
                                "Pata na ho toh Personal."
                            ),
                        ),
                        "key": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Chhota sa naam jisse baad me yeh baat "
                                "dhoondhi jaye, jaise 'beti ka school', "
                                "'office wifi password', 'favorite editor'."
                            ),
                        ),
                        "value": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Asli baat jo yaad rakhni hai, jaise '8:30' "
                                "ya 'VS Code'."
                            ),
                        ),
                    },
                    required=["key", "value"],
                ),
            ),
            types.FunctionDeclaration(
                name="recall_memory",
                description=(
                    "Pehle yaad rakhi hui baaton me se jawab dhoondhta hai. "
                    "Tab chalao jab main kuch aisa poochhoon jiska jawab "
                    "tumhe yaad rakhi hui kisi baat me mil sakta ho, jaise "
                    "'meri beti ka school kab shuru hota hai?' ya 'mera "
                    "wifi password kya hai?'. `found: false` aaye toh saaf "
                    "kaho 'mujhe yaad nahi', guess mat karo."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "query": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Jo poochha gaya uska matlab, jaise 'beti ka "
                                "school time'."
                            ),
                        ),
                    },
                    required=["query"],
                ),
            ),
            types.FunctionDeclaration(
                name="forget_memory",
                description=(
                    "Ek yaad rakhi hui baat hamesha ke liye hata deta hai. "
                    "Sirf tab chalao jab main saaf kahoon 'yeh baat bhool "
                    "jao' ya 'ise yaad se hata do', naam ke saath."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "key": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Jo naam use hua tha yaad rakhte waqt, jaise "
                                "'office wifi password'."
                            ),
                        ),
                    },
                    required=["key"],
                ),
            ),
            types.FunctionDeclaration(
                name="list_memories",
                description=(
                    "Ab tak yaad rakhi saari (ya ek category ki) baatein "
                    "batata hai. Tab chalao jab main poochhoon 'tumhe mere "
                    "baare me kya kya yaad hai?' ya 'family wali baatein "
                    "batao'."
                ),
                parameters=types.Schema(
                    type=types.Type.OBJECT,
                    properties={
                        "category": types.Schema(
                            type=types.Type.STRING,
                            description=(
                                "Optional: Personal, Family, Work, "
                                "Preferences, Projects, ya Reminders me se "
                                "sirf ek. Khali chhodo toh sab dikhaye."
                            ),
                        ),
                    },
                ),
            ),
        ] + _project_tools() + _islamic_tools() + _coach_tools()
    )
]


def _list_windows_tool(_args):
    windows = window_manager.list_open_windows()
    return {"windows": windows,
            "message": window_manager.summarize_windows(windows)}


# Window tools ka ek hi rasta - sab blocking hain (Win32, COM), sab thread me
WINDOW_TOOLS = {
    "list_open_windows": _list_windows_tool,
    "close_window": lambda a: window_manager.close_window(
        a.get("name", ""), a.get("confirm_token")),
    "close_all_windows": lambda a: window_manager.close_all_windows(
        a.get("app", ""), a.get("confirm_token")),
    "close_all_windows_except_current":
        lambda a: window_manager.close_all_windows_except_current(
            a.get("confirm_token")),
    "focus_window": lambda a: window_manager.focus_window(a.get("name", "")),
    "minimize_window": lambda a: window_manager.minimize_window(a.get("name", "")),
    "maximize_window": lambda a: window_manager.maximize_window(a.get("name", "")),
}

# Personal memory tools - SQLite-backed, survives sleep/restart (memory/)
MEMORY_TOOLS = {
    "remember_this": lambda a: memory_assistant.remember_this(
        a.get("category", ""), a.get("key", ""), a.get("value", "")),
    "recall_memory": lambda a: memory_assistant.recall_memory(a.get("query", "")),
    "forget_memory": lambda a: memory_assistant.forget_memory(a.get("key", "")),
    "list_memories": lambda a: memory_assistant.list_memories(a.get("category") or None),
}


class Playback:
    """Audio threads aur HUD ke beech ka saajha haal.

    `playing` send_loop echo rokne ko padhta hai. `connected` isliye hai ki
    speaker thread reconnect ke beech me galti se "listening" na dikha de.
    """

    def __init__(self):
        self.playing = False
        self.connected = False
        # Islamic tutor: Quran audio player, recitation recorder, aur abhi ka
        # session (recitation ka nateeja baad me isi par bheja jata hai)
        self.quran_player = None
        self.recorder = None
        self.session = None
        # Session resumption handle - reconnect par wahi baatcheet; sone ke
        # baad None, taaki jaagna hamesha nayi baatcheet ho
        self.resume_handle = None


def _offer(q, item):
    """Queue full ho toh sabse purana chunk hata kar naya daalein."""
    if q.full():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(item)


def mic_worker(loop, stream, mic_queue, bus, stop_event):
    """Ek hi lamba-chalne wala thread mic padhta hai.

    Pehle har 20ms chunk par `asyncio.to_thread` call hota tha - 50 dispatch
    prati second. Ek thread + queue se event loop khali rehta hai, aur HUD ka
    level bhi yahin nikal aata hai (RMS isi thread me, loop par bojh nahi).
    """
    counter = 0

    while not stop_event.is_set():
        try:
            data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
        except Exception:
            break

        counter += 1
        level = None
        if counter % LEVEL_EVERY == 0:
            samples = array.array("h")
            samples.frombytes(data)
            if samples:
                level = max(abs(s) for s in samples) / 32768

        try:
            loop.call_soon_threadsafe(_offer, mic_queue, data)
            if level is not None:
                loop.call_soon_threadsafe(bus.level, level)
        except RuntimeError:
            break   # Event loop band ho chuka hai


def speaker_worker(loop, stream, spk_queue, playback, bus, stop_event):
    """Speaker writes apne thread me - event loop kabhi block nahi hota."""

    def announce(state):
        if playback.connected:
            try:
                loop.call_soon_threadsafe(bus.state, state)
            except RuntimeError:
                pass

    while not stop_event.is_set():
        chunk = spk_queue.get()
        if chunk is None:
            break

        if not playback.playing:
            playback.playing = True
            announce("speaking")

        try:
            stream.write(chunk)
        except Exception:
            break
        finally:
            if spk_queue.empty():
                playback.playing = False
                announce("listening")


async def send_loop(session, mic_queue, playback):
    """Mic queue se audio uthakar Gemini ko bhejta hai."""
    while True:
        data = await mic_queue.get()
        # Recitation test chal raha hai: awaaz sirf local speech model ko
        # jati hai, Gemini ko nahi - taaki Jarvis bachche ko beech me na toke.
        recorder = playback.recorder
        if recorder is not None and recorder.active:
            # Jarvis ki apni awaaz ("ab tum bolo: hond!") recording me na aaye -
            # speaker se mic tak pahunch sakti hai
            if not playback.playing:
                recorder.feed(data)
            continue
        # Agar speaker active hai toh mic data ignore karein (echo rokne ke liye)
        # - Jarvis ki awaaz ho ya Quran ki tilawat
        if playback.playing or (playback.quran_player and playback.quran_player.busy):
            continue
        await session.send_realtime_input(
            audio=types.Blob(data=data, mime_type="audio/pcm;rate=16000")
        )


async def handle_tool_call(session, tool_call, bus, sleep_event):
    """Jarvis ne koi tool maanga hai - chalao aur nateeja wapas bhejo.

    True lautata hai agar Jarvis ko sone ko kaha gaya - receive_loop phir
    alvida poora hone deta hai aur tab session band karta hai.
    """
    responses = []
    sleep_requested = False

    for call in tool_call.function_calls or []:
        if call.name == "go_to_sleep":
            sleep_requested = True
            result = {"ok": True, "message": "Jarvis ab so jayega."}
            print("\U0001f4a4 Jarvis so raha hai...")
            bus.transcript("system", "Jarvis so raha hai")
            # Model turn_complete na bheje toh bhi atke nahi
            asyncio.get_running_loop().call_later(SLEEP_FALLBACK, sleep_event.set)
        elif call.name == "close_unused_browser_windows":
            # Win32 calls blocking hain - loop ko rokne ki zaroorat nahi
            result = await asyncio.to_thread(
                browser_tools.close_unused_browser_windows
            )
            note = result.get("message") or result.get("error", "")
            print(f"\U0001f9f9 {note}")
            bus.transcript("system", note)
        elif call.name == "close_browser_tabs":
            args = call.args or {}
            # PowerShell + UI Automation - kuch second lagte hain, loop se bahar
            result = await asyncio.to_thread(
                browser_tools.close_browser_tabs,
                args.get("keep"),
                args.get("close"),
                args.get("confirm_token"),
            )
            note = result.get("message") or result.get("error", "")
            print(f"\U0001f5c2️ {note}")
            bus.transcript("system", note)
        elif call.name == "play_on_youtube":
            query = (call.args or {}).get("query", "")
            # Network + browser kholna - dono blocking, loop se bahar
            result = await asyncio.to_thread(browser_tools.play_on_youtube, query)
            note = result.get("message", "YouTube nahi khul paya")
            print(f"\U0001f3b5 {note}")
            bus.transcript("system", note)
        elif call.name in WINDOW_TOOLS:
            result = await asyncio.to_thread(WINDOW_TOOLS[call.name], call.args or {})
            note = result.get("message", "")
            print(f"\U0001fa9f {note}")
            # Model ko poora nateeja jata hai; HUD par ek line kaafi
            bus.transcript("system", note if len(note) <= 160 else note[:157] + "...")
        elif call.name in PROJECT_TOOLS:
            # git, disk walk, SQLite, VS Code - sab blocking, loop se bahar
            result = await asyncio.to_thread(PROJECT_TOOLS[call.name], call.args or {})
            note = result.get("message", "")
            print(f"\U0001f4c1 {note}")
            first = note.split("\n", 1)[0]
            bus.transcript("system", first if len(first) <= 160 else first[:157] + "...")
        elif call.name in ISLAMIC_TOOLS:
            # Network (Quran/hadith), SQLite, audio download - loop se bahar
            result = await asyncio.to_thread(ISLAMIC_TOOLS[call.name], call.args or {})
            note = result.get("message", "")
            print(f"\U0001f54c {note}")
            first = note.split("\n", 1)[0]
            bus.transcript("system", first if len(first) <= 160 else first[:157] + "...")
        elif call.name in COACH_TOOLS:
            # SQLite + (practice_word) speech model load - loop se bahar
            result = await asyncio.to_thread(COACH_TOOLS[call.name], call.args or {})
            note = result.get("message", "")
            print(f"\U0001f9f8 {note}")
            first = note.split("\n", 1)[0]
            bus.transcript("system", first if len(first) <= 160 else first[:157] + "...")
        elif call.name in MEMORY_TOOLS:
            # SQLite hai - blocking, isliye loop se bahar
            result = await asyncio.to_thread(MEMORY_TOOLS[call.name], call.args or {})
            note = result.get("message", "")
            print(f"\U0001f9e0 {note}")
            bus.transcript("system", note if len(note) <= 160 else note[:157] + "...")
        else:
            result = {"error": f"'{call.name}' naam ka koi tool nahi hai."}

        responses.append(
            types.FunctionResponse(id=call.id, name=call.name, response=result)
        )

    if responses:
        await session.send_tool_response(function_responses=responses)

    return sleep_requested


async def wait_for_playback(spk_queue, playback, timeout=SLEEP_FALLBACK):
    """Jo bol raha hai use poora bolne dein - alvida beech me na kate."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while (playback.playing or not spk_queue.empty()) and loop.time() < deadline:
        await asyncio.sleep(0.1)
    # stream.write buffer me daal kar lautta hai, speaker tak pahunchne me
    # thoda aur lagta hai
    await asyncio.sleep(0.4)


async def receive_loop(session, spk_queue, playback, bus, sleep_event):
    """Gemini se audio aur transcripts leta hai - har turn, lagataar.

    `session.receive()` sirf EK model turn deta hai aur turn_complete par ruk
    jata hai. Pehle yahan bas ek `async for` tha, toh pehle jawab ke baad
    connection koi padhta hi nahi tha: Jarvis behra ho jata tha. Upar se
    na-padhe messages jama hote-hote websockets ne socket padhna hi band kar
    diya, keepalive pong nahi dikha, aur ~20s baad connection 1011 se tut
    jata tha. Isliye bahar `while True`.
    """
    user_buf, model_buf = [], []
    sleep_pending = False

    while True:
        async for response in session.receive():
            # Server baar-baar naya "resume handle" deta hai. Connection toote
            # toh isi se wahi baatcheet aage chalti hai (sabaq beech me na
            # chhoote).
            update = response.session_resumption_update
            if update and update.resumable and update.new_handle:
                playback.resume_handle = update.new_handle

            if response.go_away:
                raise SessionExpiring(response.go_away.time_left)

            # Tool call alag rasta hai - iske saath server_content nahi aata
            if response.tool_call:
                if await handle_tool_call(
                    session, response.tool_call, bus, sleep_event
                ):
                    sleep_pending = True
                continue

            sc = response.server_content
            if not sc:
                continue

            # Audio model response speaker queue me daalein
            if sc.model_turn:
                for part in sc.model_turn.parts or []:
                    if part.inline_data:
                        try:
                            spk_queue.put_nowait(part.inline_data.data)
                        except queue.Full:
                            # Playback itna peeche hai ki catch-up nahi hoga -
                            # purana chunk chhod dein taaki latency na badhe.
                            try:
                                spk_queue.get_nowait()
                                spk_queue.put_nowait(part.inline_data.data)
                            except (queue.Empty, queue.Full):
                                pass

            # Transcriptions collect karein
            if sc.input_transcription:
                t = sc.input_transcription.text.strip()
                if t:
                    user_buf.append(t)
                    # Aapki baat pakad li - ab jawab ban raha hai. Yahi woh lamha
                    # hai jab user ko rukna chahiye, isliye HUD par alag dikhta hai.
                    if not playback.playing:
                        bus.state("thinking")

            if sc.output_transcription:
                t = sc.output_transcription.text.strip()
                if t: model_buf.append(t)

            # Turn complete hone par console aur HUD dono par bhejein
            if sc.turn_complete:
                if user_buf:
                    text = " ".join(user_buf)
                    print(f"\U0001f464 User: {text}")
                    bus.transcript("user", text)
                    if COACH is not None:
                        # speech coach session chal raha ho toh bachche ke
                        # jumlon ki lambai/zubaan note hoti hai
                        COACH.observe(text)
                    user_buf.clear()
                if model_buf:
                    text = " ".join(model_buf)
                    print(f"\U0001f916 Jarvis: {text}")
                    bus.transcript("jarvis", text)
                    model_buf.clear()

                # Jarvis ko sone ko kaha tha - alvida poora sun lein, phir
                # session band. Aage ka faisla session_once karega.
                if sleep_pending:
                    await wait_for_playback(spk_queue, playback)
                    sleep_event.set()
                    return


def load_wake_word():
    """"Hey Jarvis" sunne wala model - na mile toh Jarvis bina iske chale.

    Pehli baar model download hota hai; tab internet na ho, ya openwakeword
    install hi na ho, toh sone ka matlab purana wala rahega: program band.
    """
    try:
        from wake_word import WakeWordListener
        return WakeWordListener()
    except Exception as exc:   # ImportError, network, model file - sab
        print(
            f"⚠️  'Hey Jarvis' wake word load nahi hua ({exc.__class__.__name__}: "
            f"{exc}). Sone par Jarvis band ho jayega."
        )
        return None


async def wait_for_wake_word(mic_queue, wake):
    """Jarvis so raha hai - sirf "Hey Jarvis" ka intezaar.

    Gemini se connection is dauraan band hai; mic ka audio sirf local model
    tak jata hai. Model har 80ms par ~2ms leta hai, isliye thread ki zaroorat
    nahi - seedha loop par.
    """
    wake.reset()
    # Neend se pehle ka mic audio - Jarvis ka apna alvida bhi - phenk dein,
    # warna usi me "Jarvis" sun kar turant jaag jata. Alvida me aksar "Hey
    # Jarvis bol ke jagana" hota hai, aur speaker ki awaaz mic tak thodi der
    # se pahunchti hai - isliye shuru ka ek second bhi anasuna.
    while not mic_queue.empty():
        mic_queue.get_nowait()
    for _ in range(WAKE_GRACE_CHUNKS):
        await mic_queue.get()

    while True:
        data = await mic_queue.get()
        if wake.heard(data):
            return


async def session_once(
    client, config, mic_queue, spk_queue, playback, bus, sleep_event
):
    """Ek Gemini Live session - connect se lekar disconnect tak.

    True lautata hai agar Jarvis ko sula diya gaya (dobara connect nahi
    karna), warna False - connection gaya, reconnect karo.
    """
    print("\nJarvis (Gemini Live) se connect ho raha hai...")
    bus.state("connecting")

    config = config.model_copy(update={
        "session_resumption": types.SessionResumptionConfig(handle=playback.resume_handle)})
    if playback.resume_handle:
        print("Pichhli baatcheet wahin se aage badh rahi hai (resumed).")
    async with client.aio.live.connect(model=MODEL, config=config) as session:
        print("Connected! Bolna shuru karein (Ctrl+C se band karein)\n")
        playback.connected = True
        playback.session = session
        bus.state("listening")

        # Purana audio phenk dein - reconnect ke baad stale chunks bhejne ka
        # matlab hai purani baat dobara sunana.
        while not mic_queue.empty():
            mic_queue.get_nowait()
        while not spk_queue.empty():
            try:
                spk_queue.get_nowait()
            except queue.Empty:
                break

        sleeper = asyncio.create_task(sleep_event.wait())
        tasks = [
            asyncio.create_task(send_loop(session, mic_queue, playback)),
            asyncio.create_task(
                receive_loop(session, spk_queue, playback, bus, sleep_event)
            ),
            sleeper,
        ]
        try:
            # FIRST_COMPLETED, FIRST_EXCEPTION nahi: koi bhi loop ruke - chahe
            # gir kar, chahe chupchaap - session aadha-zinda nahi chhodna.
            # Pehle receive_loop chupchaap khatam ho jata tha aur session
            # bina sune latka rehta tha.
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Ek loop ruka toh baaki bhi rokne hain - warna woh mari hui
            # connection par likhte rahenge.
            playback.connected = False
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        if sleep_event.is_set():
            return True

        # Jo pehle gira, uska asli exception yahan dobara uthega
        for task in done:
            if task is not sleeper:
                task.result()
        return False


async def run(start_asleep: bool = False, open_browser: bool = True):
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print(f"ERROR: GOOGLE_API_KEY nahi mila - {app_paths.env_file()} me set karein!")
        return

    # Pehle ka data (memory/data, islamic/data ...) ek baar naye data folder
    # me copy - databases khulne se pehle
    copied = app_paths.migrate_legacy_data()
    if copied:
        print(f"\U0001f4e6 Data {app_paths.home()} me copy hua: {', '.join(copied)}")

    # PyAudio ko initialize karein aur streams open karein. Streams reconnect
    # ke aar-paar zinda rehte hain - device baar-baar kholna bekaar risk hai.
    p = pyaudio.PyAudio()
    # Windows ka default mic chupke se badal jata hai (headset lagaya, hataya)
    # - aur muted headset par Jarvis ko sirf khamoshi milti hai. Kaunsa mic
    # hai, shuru me hi dikh jaye.
    try:
        print(f"\U0001f3a4 Mic: {p.get_default_input_device_info()['name']}")
    except OSError:
        print("⚠️  Koi mic nahi mila!")
    mic_stream = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=MIC_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE
    )
    spk_stream = p.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SPK_RATE,
        output=True
    )

    loop = asyncio.get_running_loop()
    mic_queue = asyncio.Queue(maxsize=MIC_QUEUE_MAX)
    spk_queue = queue.Queue(maxsize=SPK_QUEUE_MAX)
    playback = Playback()
    stop_event = threading.Event()

    # HUD pehle uthta hai taaki connect hone ka intezaar screen par dikhe
    bus = ui_server.EventBus()
    await ui_server.start(bus, open_browser=open_browser)
    setup_islamic_tutor(loop, bus, playback, spk_queue)

    mic_thread = threading.Thread(
        target=mic_worker,
        args=(loop, mic_stream, mic_queue, bus, stop_event),
        name="mic",
        daemon=True,
    )
    speaker_thread = threading.Thread(
        target=speaker_worker,
        args=(loop, spk_stream, spk_queue, playback, bus, stop_event),
        name="speaker",
        daemon=True,
    )
    mic_thread.start()
    speaker_thread.start()

    # Gemini client aur configurations
    client = genai.Client(api_key=api_key)
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Laomedeia")
            )
        ),
        system_instruction=types.Content(parts=[types.Part(
            text=instruction + "\n\n" + islamic.ANSWER_POLICY)]),
        # Ghar ki zubaanein - bina iske bachche ki awaaz kabhi Portuguese ya
        # Korean likh di jaati thi
        input_audio_transcription=types.AudioTranscriptionConfig(
            language_codes=["en-US", "hi-IN", "ur-IN", "nl-NL"]),
        # Lambi sessions (poora sabaq) Live API ki time limit par na katein
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        tools=TOOLS,
    )

    wake = load_wake_word()

    # Windows start hone par Jarvis sota hua uthta hai: sirf offline wake
    # word sunta hai, Gemini tak kuch nahi jata, jab tak "Hey Jarvis" na bolein
    if start_asleep:
        if wake is None:
            print("Wake word nahi chala - isliye jaag kar shuru ho raha hai.")
        else:
            print("\U0001f4a4 Jarvis so raha hai. Jagane ke liye bolo: 'Hey Jarvis'")
            bus.state("sleeping")
            await wait_for_wake_word(mic_queue, wake)
            print("Jaag raha hoon!")

    delay = 1
    try:
        # Connection tutna normal hai - keepalive timeout, wifi, ya server side
        # se. Crash karne ke bajaye dobara connect karein. Jarvis so jaye toh
        # "Hey Jarvis" ka intezaar, phir naya session. Band sirf Ctrl+C se.
        while True:
            # Har session ka apna event. go_to_sleep ka 15s wala fallback
            # timer purane event ko pakde rehta hai - agar woh naye session
            # me bajta, toh jagte hi Jarvis phir so jata.
            sleep_event = asyncio.Event()
            started = loop.time()
            try:
                if await session_once(
                    client, config, mic_queue, spk_queue, playback, bus,
                    sleep_event,
                ):
                    if wake is None:
                        print("\n\U0001f4a4 Jarvis so gaya. Alvida!")
                        break

                    print("\n\U0001f4a4 Jarvis so gaya. Jagane ke liye bolo: 'Hey Jarvis'")
                    # Sote hue na tilawat chale, na koi recitation test sunta rahe
                    if playback.quran_player:
                        playback.quran_player.stop()
                    if playback.recorder:
                        playback.recorder.cancel()
                    # jaagne par nayi baatcheet - purani resume na ho
                    playback.resume_handle = None
                    bus.state("sleeping")
                    await wait_for_wake_word(mic_queue, wake)
                    print("\n\U0001f44b 'Hey Jarvis' suna - jaag raha hoon!")
                    delay = 1
                    continue
                print("\nSession khatam ho gaya.")
            except SessionExpiring:
                # Lambi session ke baad backoff 1s hi hai; resume handle se
                # baatcheet wahin se chalegi
                print("\nSession ki time limit - wahi baatcheet naye connection par...")
            except ConnectionClosed as exc:
                print(f"\nConnection tut gaya: {exc.__class__.__name__}")
            except genai_errors.APIError as exc:
                print(f"\nGemini ne connection band kiya: {exc.code} {exc.message}")
            except (TimeoutError, OSError) as exc:
                print(f"\nNetwork problem: {exc.__class__.__name__}: {exc}")

            # Session theek-thaak chala toh backoff reset - baar-baar aane wale
            # drops aur ek lambe session ke baad ke drop me farq hai.
            if loop.time() - started > STABLE_SECONDS:
                delay = 1

            playback.playing = False
            playback.connected = False
            bus.state("reconnecting")

            print(f"{delay}s me dobara connect kar rahe hain... (Ctrl+C se band karein)")
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_DELAY)

    finally:
        # Cleanup: threads rokein, phir streams aur PyAudio band karein
        print("\nCleaning up audio resources...")
        bus.state("offline")
        stop_event.set()
        try:
            spk_queue.put_nowait(None)   # Speaker thread ko get() se jagayein
        except queue.Full:
            pass

        # Pehle threads ruken, TAB streams band hon. Mic thread `stream.read`
        # ke andar PortAudio me baitha hota hai - us waqt stream close kiya
        # toh woh aazaad ki hui memory padhta hai aur poora process
        # segfault (exit 139) se mar jata hai. read() ~20ms me lautta hai,
        # phir stop_event dekh kar thread nikal jata hai.
        mic_thread.join(timeout=2)
        speaker_thread.join(timeout=2)

        # Koi thread phir bhi atka ho toh uska stream mat chhedo - daemon
        # thread hai, process ke saath khatam ho jayega. Crash se behtar.
        if not mic_thread.is_alive():
            mic_stream.stop_stream()
            mic_stream.close()
        if not speaker_thread.is_alive():
            spk_stream.stop_stream()
            spk_stream.close()
        if not (mic_thread.is_alive() or speaker_thread.is_alive()):
            p.terminate()
        print("Done!")


_INSTANCE_MUTEX = []


def _already_running() -> bool:
    """Ek hi Jarvis: mic aur HUD ports do copies me nahi bant sakte."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    handle = kernel32.CreateMutexW(None, False, "Local\\JarvisVoiceAssistant")
    already = ctypes.get_last_error() == 183          # ERROR_ALREADY_EXISTS
    _INSTANCE_MUTEX.append(handle)                    # process ke saath zinda rahe
    return already


def self_test() -> int:
    """Packaged build ki jaanch, bina Gemini/mic session ke: bundled files,
    API key, wake word, databases, mic, aur dono speech models. Nateeja
    console ya (Jarvis.exe me) log file me. Exit code 0 = sab theek."""
    import time
    results = []

    def check(name, fn):
        start = time.time()
        try:
            detail = fn() or ""
            results.append(True)
            print(f"  OK    {name} {detail} ({time.time() - start:.1f}s)")
        except Exception as exc:
            results.append(False)
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")

    print(f"Jarvis self-test - frozen={app_paths.FROZEN}, code={app_paths.CODE_DIR}")
    print(f"Data folder: {app_paths.home()}")

    def bundled_files():
        for path in (ui_server.UI_DIR / "index.html", ui_server.UI_DIR / "jarvis.svg",
                     browser_tools.TABS_SCRIPT):
            if not path.is_file():
                raise FileNotFoundError(path)
    check("bundled HUD + scripts", bundled_files)
    check("GOOGLE_API_KEY present", lambda: None if os.environ.get("GOOGLE_API_KEY")
          else (_ for _ in ()).throw(RuntimeError(f"not set - put it in {app_paths.env_file()}")))

    def wake():
        from wake_word import WakeWordListener
        listener = WakeWordListener()
        return f"score on silence {listener.feed(bytes(2560 * 5)):.2f}"
    check("wake word model", wake)

    def databases():
        from memory import db as mdb
        from projects import db as pdb
        from islamic import db as idb
        from speech_coach import db as sdb
        for mod in (mdb, pdb, idb, sdb):
            mod.connect(mod.DEFAULT_DB_PATH).close()
        return str(app_paths.home())
    check("databases", databases)

    def microphone():
        audio = pyaudio.PyAudio()
        try:
            return audio.get_default_input_device_info()["name"]
        finally:
            audio.terminate()
    check("microphone", microphone)

    def quran_model():
        from islamic.recitation import Transcriber
        t = Transcriber()
        if not t.load():
            raise RuntimeError(t.error)
        return repr(t.transcribe(bytes(32000)))
    check("Quran speech model", quran_model)

    def coach_model():
        from speech_coach.pronunciation import MultilingualTranscriber
        t = MultilingualTranscriber()
        if not t.load():
            raise RuntimeError(t.error)
        return repr(t.transcribe(bytes(32000), "en"))
    check("child coach speech model", coach_model)

    ok = all(results)
    print(f"Self-test {'PASSED' if ok else 'FAILED'}: {sum(results)}/{len(results)} checks")
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="Jarvis voice assistant")
    parser.add_argument("--start-asleep", action="store_true",
                        help="sirf 'Hey Jarvis' ka intezaar karke shuru karo (Windows startup)")
    parser.add_argument("--no-browser", action="store_true",
                        help="HUD ka browser tab khud mat kholo")
    parser.add_argument("--self-test", action="store_true",
                        help="build ki jaanch karke band ho jao (exit code 0 = theek)")
    args = parser.parse_args(argv)

    if args.self_test:
        sys.exit(self_test())

    if _already_running():
        # Shortcut dobara dabaya - naya Jarvis nahi, chalte hue ka HUD kholo
        import webbrowser
        webbrowser.open(f"http://{ui_server.HTTP_HOST}:{ui_server.HTTP_PORT}/")
        print("Jarvis pehle se chal raha hai - HUD khol diya.")
        return
    try:
        asyncio.run(run(start_asleep=args.start_asleep, open_browser=not args.no_browser))
    except KeyboardInterrupt:
        print("\nJarvis Stopped. Alvida!")


if __name__ == "__main__":
    main()
