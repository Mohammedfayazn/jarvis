"""SpeechCoach: the application layer Jarvis talks to.

Composes the vocabulary trainer, conversation engine, pronunciation
analyzer, storytelling module, progress tracker and parent dashboard into
use cases: a 10-minute daily session, word practice, story time, sentence
building, and the weekly parent summary. Every method returns a dict with a
"message" - what Jarvis should do or say next - like every other tool.

Hooks (so it runs the same live, in the CLI and in tests):
    display(payload)                        show something on the HUD
    record(on_done, pause, max_seconds, no_speech)   capture one utterance
    notify(text)                            tell Jarvis a result that arrives later

Tone rules live here as much as in the prompt: the child never hears
"wrong", every result starts with praise or "good try", and anything
unclear is the microphone's fault, not hers.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from . import db
from .conversation import ConversationEngine, analyze_utterance
from .dashboard import ParentDashboard
from .pronunciation import MultilingualTranscriber, compare, feedback
from .storytelling import StorytellingModule
from .tracker import Child, ProgressTracker
from .vocabulary import (
    CATEGORIES, LANGUAGE_NAMES, WORDS, Word, VocabularyTrainer, find_word, normalize_language,
)

logger = logging.getLogger("jarvis.speech_coach")

SESSION_PARTS = (
    ("greeting", 1, "Say hello"),
    ("vocabulary", 2, "New words"),
    ("pronunciation", 2, "Say it with me"),
    ("conversation", 2, "Let's talk"),
    ("story", 2, "Story time"),
    ("reward", 1, "Stars!"),
)
GREETING = {
    "en": "Hello {name}! Ready to play with words today? How are you?",
    "nl": "Hallo {name}! Zin om met woordjes te spelen? Hoe gaat het met je?",
    "hi": "नमस्ते {name}! आज शब्दों से खेलें? तुम कैसी हो?",
}
REWARD = {
    "en": "You did it, {name}! {stars} new stars - you have {total} stars now!",
    "nl": "Je hebt het gedaan, {name}! {stars} nieuwe sterren - nu heb je er {total}!",
    "hi": "शाबाश {name}! {stars} नए सितारे - अब तुम्हारे पास {total} सितारे हैं!",
}
STICKERS = ("🌟", "🦄", "🐬", "🌈", "🦋", "🏆", "🎈")


def _noop(*_args, **_kwargs):
    return False


class SpeechCoach:
    def __init__(self, db_path=None, *, display=None, record=None, notify=None,
                 transcriber: MultilingualTranscriber | None = None):
        path = Path(db_path) if db_path else db.DEFAULT_DB_PATH
        self._conn = db.connect(path)
        self.tracker = ProgressTracker(self._conn)
        self.vocabulary = VocabularyTrainer(self.tracker)
        self.conversation = ConversationEngine()
        self.stories = StorytellingModule(self.tracker)
        self.dashboard = ParentDashboard(self.tracker)
        self.transcriber = transcriber or MultilingualTranscriber()
        self.display = display or _noop
        self.record = record or _noop
        self.notify = notify or _noop
        self._session: dict | None = None     # {"child": id, "id": session id}
        self._active_child: int | None = None

    def close(self) -> None:
        self._conn.close()

    # -- who ---------------------------------------------------------------

    def _child(self, name: str | None):
        children = self.tracker.children()
        if not children:
            return None, {"ok": False, "needs_profile": True, "message":
                          "There's no child profile yet. Ask the parent for the child's name, "
                          "age and languages, then add the profile."}
        key = " ".join((name or "").lower().replace("'s", "").split())
        if key and key not in ("child", "my child", "kid", "daughter", "beti", "the child", "her"):
            child = self.tracker.get_child(key)
            if child is None:
                return None, {"ok": False, "needs_profile": True,
                              "message": f"No profile for {name} yet - add one first."}
            self._active_child = child.id
            return child, None
        if self._active_child:
            child = next((c for c in children if c.id == self._active_child), None)
            if child:
                return child, None
        if len(children) == 1:
            self._active_child = children[0].id
            return children[0], None
        return None, {"ok": False, "needs_choice": True,
                      "message": "Which child? " + ", ".join(c.name for c in children)}

    def _with(self, name, fn):
        child, error = self._child(name)
        if error:
            return error
        try:
            return fn(child)
        except Exception as exc:
            logger.exception("speech coach failed")
            return {"ok": False, "message": f"Something went wrong: {exc}"}

    def add_child(self, name: str, age: int | None = None, languages=None,
                  focus_language: str | None = None) -> dict:
        name = " ".join((name or "").split())
        if name.lower() in ("", "child", "kid", "user", "daughter", "son", "name"):
            return {"ok": False, "message": "I need the child's real name."}
        langs = [normalize_language(l, "") for l in (languages or ["nl", "en", "hi"])]
        langs = [l for l in dict.fromkeys(langs) if l] or ["nl", "en", "hi"]
        focus = normalize_language(focus_language, langs[0])
        child, created = self.tracker.add_child(name, int(age) if age else None, langs, focus)
        self._active_child = child.id
        spoken = ", ".join(LANGUAGE_NAMES[l] for l in child.languages)
        return {"ok": True, "created": created, "child": child.as_dict(),
                "message": (f"{'Added' if created else 'Updated'} {child.name}"
                            + (f" ({child.age})" if child.age else "")
                            + f" - languages {spoken}; lessons mainly in "
                              f"{LANGUAGE_NAMES[child.focus_language]}.")}

    def set_language(self, name: str | None, language: str) -> dict:
        def run(child: Child) -> dict:
            child2 = self.tracker.set_focus(child, normalize_language(language, child.focus_language))
            return {"ok": True, "message": f"Lessons for {child2.name} will now be mainly in "
                                           f"{LANGUAGE_NAMES[child2.focus_language]}."}
        return self._with(name, run)

    # -- the 10-minute daily session ----------------------------------------

    def session(self, action: str = "start", name: str | None = None,
                language: str | None = None) -> dict:
        action = (action or "start").lower()

        def run(child: Child) -> dict:
            lang = normalize_language(language, child.focus_language)
            if action == "start":
                plan = [{"part": p, "minutes": m, "title": t} for p, m, t in SESSION_PARTS]
                sid = self.tracker.start_session(child, lang, plan)
                self._session = {"child": child.id, "id": sid, "language": lang, "stars": 0}
                self.display({"mode": "plan", "title": f"{child.name}'s 10 minutes",
                              "student": child.name,
                              "items": [{"part": p["part"], "title": p["title"],
                                         "detail": f"{p['minutes']} min"} for p in plan]})
                return {"ok": True, "session": plan, "message":
                        "Today's 10-minute session: hello, new words, say-it-with-me, talking, a "
                        "story, then stars. Start with the greeting - say: "
                        + GREETING[lang].format(name=child.name)
                        + " Then call speech_coach with action 'next'."}
            if not self._session or self._session["child"] != child.id:
                return {"ok": False, "message": f"No session running for {child.name} - start one first."}
            state = self.tracker.session(self._session["id"])
            if action == "status":
                part = state["plan"][min(state["step"], len(state["plan"]) - 1)]
                return {"ok": True, "message": f"Now: {part['title']} "
                                               f"({state['step'] + 1} of {len(state['plan'])})."}
            if action == "end":
                return self._reward(child, finished_early=True)
            # next part
            self.tracker.advance_session(self._session["id"])
            self._session["stars"] += 1
            state = self.tracker.session(self._session["id"])
            if state["step"] >= len(state["plan"]) - 1:    # the last part is the reward
                return self._reward(child)
            part = state["plan"][state["step"]]["part"]
            return self._part(child, part, self._session["language"])
        return self._with(name, run)

    def _part(self, child: Child, part: str, lang: str) -> dict:
        if part == "vocabulary":
            result = self.words(child.name, language=lang)
            result["message"] = "New words: " + result["message"] + " Then call 'next'."
            return result
        if part == "pronunciation":
            words = self.vocabulary.review_words(child, lang, 2) or self.vocabulary.next_words(child, lang, count=2)
            hard = self.tracker.practising_sounds(child)
            if hard:
                sound = hard[0]["sound"]
                extra = [w for w in WORDS if sound in w.text(hard[0]["language"]).lower()]
                words = (extra[:1] + words)[:2]
            names = ", ".join(w.say(lang) for w in words)
            return {"ok": True, "words": [w.key for w in words], "message":
                    f"Say-it-with-me: practise {names}. For each word, say it slowly and cheerfully "
                    f"yourself, then call practice_word for it and stay quiet while {child.name} "
                    "tries. Then call 'next'."}
        if part == "conversation":
            topic, question = self.conversation.starter(lang)
            self.display({"mode": "feedback", "correct": True, "title": "Let's talk!",
                          "text": question})
            return {"ok": True, "topic": topic, "message":
                    f"Conversation: ask \"{question}\" Listen, praise, and ask a follow-up like "
                    f"\"{self.conversation.follow_up(lang)}\" to get a longer answer. When "
                    f"{child.name}'s sentence is short, say it back bigger and log it with "
                    "sentence_practice. "
                    "About two minutes, then call 'next'."}
        if part == "story":
            result = self.story(child.name, language=lang)
            result["message"] += " Then call 'next'."
            return result
        return {"ok": True, "message": "Keep going, then call 'next'."}

    def _reward(self, child: Child, finished_early: bool = False) -> dict:
        lang = self._session["language"] if self._session else child.focus_language
        stars = max(1, self._session["stars"] if self._session else 1)
        total = self.tracker.add_stars(child, stars)
        if self._session:
            self.tracker.end_session(self._session["id"], stars)
            self._session = None
        sticker = STICKERS[total % len(STICKERS)]
        text = REWARD[lang].format(name=child.name, stars=stars, total=total)
        self.display({"mode": "reward", "title": f"{sticker} {child.name}", "stars": stars,
                      "total": total, "text": text})
        return {"ok": True, "stars": stars, "total": total,
                "message": ("Session ended early - still a great effort. " if finished_early else "")
                           + f"Say, with lots of joy: {text}"}

    # -- vocabulary ------------------------------------------------------

    def words(self, name: str | None = None, category: str | None = None,
              language: str | None = None, review: bool = False) -> dict:
        def run(child: Child) -> dict:
            lang = normalize_language(language, child.focus_language)
            cat = category.lower() if category and category.lower() in CATEGORIES else None
            chosen = (self.vocabulary.review_words(child, lang, 3) if review else []) \
                or self.vocabulary.next_words(child, lang, cat, 3)
            for w in chosen:
                self.tracker.word_seen(child, w.key, lang)
            others = [l for l in child.languages if l != lang]
            self.display({"mode": "words", "title": f"{LANGUAGE_NAMES[lang]} words",
                          "student": child.name,
                          "items": [{"emoji": w.emoji, "word": w.say(lang),
                                     "also": [w.say(o) for o in others]} for w in chosen]})
            cat_name = chosen[0].category if chosen else "words"
            if chosen and not review:
                self.tracker.complete_lesson(child, f"vocab:{cat_name}:{lang}")
            listing = "; ".join(f"{w.emoji} {w.say(lang)}" for w in chosen)
            return {"ok": True, "words": [w.as_dict() for w in chosen], "message":
                    f"{cat_name.title()} in {LANGUAGE_NAMES[lang]}: {listing}. Show each one, say it "
                    f"clearly, ask {child.name} to say it back, and cheer every try."}
        return self._with(name, run)

    def translate(self, text: str) -> dict:
        w = find_word(text)
        if w is None:
            return {"ok": True, "found": False, "message":
                    f"'{text}' isn't in my picture-word list - translate it yourself, keeping "
                    "it simple, and say you're not completely sure if you aren't."}
        return {"ok": True, "found": True, "word": w.as_dict(), "message":
                f"{w.emoji} English: {w.en} · Dutch: {w.text('nl', True)} · Hindi: {w.say('hi')}"}

    # -- pronunciation -----------------------------------------------------

    def practice_word(self, name: str | None, word: str, language: str | None = None) -> dict:
        def run(child: Child) -> dict:
            lang = normalize_language(language, child.focus_language)
            target = find_word(word)
            if target is None:
                text = " ".join((word or "").split())
                if not text:
                    return {"ok": False, "message": "Which word?"}
                target = Word(text.lower(), "extra", "🗣️", text, text, None, text, text)
            if not self.transcriber.available():
                return {"ok": False, "message":
                        f"Listening needs the local speech model ({self.transcriber.error}). "
                        "Practise by saying it together instead."}
            if not self.transcriber.load():
                return {"ok": False, "message": f"The speech model didn't load: {self.transcriber.error}"}
            self.display({"mode": "say", "title": "Your turn!", "student": child.name,
                          "emoji": target.emoji, "word": target.say(lang)})

            def done(status: str, pcm: bytes) -> None:
                threading.Thread(target=self._finish_word, args=(child, target, lang, status, pcm),
                                 name="word-check", daemon=True).start()

            if not self.record(done, pause=1.5, max_seconds=8.0, no_speech=10.0):
                return {"ok": False, "message": "I can't listen here - word practice needs the live assistant."}
            return {"ok": True, "listening": True, "message":
                    f"Listening for {target.say(lang)}. If you haven't yet, say the word once, "
                    f"slowly and happily, then stay quiet - {child.name}'s result comes in a moment."}
        return self._with(name, run)

    def check_word(self, child: Child, target: Word, lang: str, transcript: str) -> dict:
        attempt = compare(target, lang, transcript)
        if attempt.status in ("clear", "close"):
            # only fair evidence counts toward sounds: unclear audio is ignored
            self.tracker.word_attempt(child, target.key, lang, attempt.recognized)
            for s in attempt.sounds_ok:
                self.tracker.sound_attempt(child, lang, s, True)
            for s in attempt.sounds_missed:
                self.tracker.sound_attempt(child, lang, s, False)
        elif attempt.status == "different":
            self.tracker.word_attempt(child, target.key, lang, False)
        text = feedback(attempt)
        self.display({"mode": "feedback", "correct": attempt.recognized,
                      "title": "⭐ " + target.say(lang) if attempt.recognized else "Good try!",
                      "text": text})
        return {"ok": True, "attempt": attempt.as_dict(), "message": text}

    def _finish_word(self, child: Child, target: Word, lang: str, status: str, pcm: bytes) -> None:
        if status != "ok":
            self.notify(f"[Word practice] {child.name} didn't say anything - that's fine. "
                        "Encourage gently and offer to try together.")
            return
        try:
            transcript = self.transcriber.transcribe(pcm, lang)
            logger.info("word practice: %s tried %r, heard %r", child.name, target.say(lang), transcript)
            result = self.check_word(child, target, lang, transcript)
            self.notify("[Word practice result - say it warmly in your own words, never 'wrong'] "
                        + result["message"])
        except Exception as exc:
            logger.exception("word check failed")
            self.notify(f"[Word practice check failed ({exc}) - just praise the try and move on.]")

    # -- conversation ------------------------------------------------------

    def observe(self, text: str) -> None:
        """Called with each thing heard during a session - builds the
        sentence-length and language-mix picture without any tool call."""
        if not self._session or not text or not text.strip():
            return
        child = next((c for c in self.tracker.children() if c.id == self._session["child"]), None)
        if child is None:
            return
        info = analyze_utterance(text, self._session["language"])
        if info.words:
            self.tracker.log_utterance(child, info.text, info.words, info.languages)

    def sentence(self, name: str | None, child_said: str, bigger: str | None = None,
                 language: str | None = None) -> dict:
        """Log a sentence the child said, and the bigger one Jarvis modelled."""
        def run(child: Child) -> dict:
            lang = normalize_language(language, child.focus_language)
            info = analyze_utterance(child_said, lang)
            self.tracker.log_utterance(child, info.text, info.words, info.languages, bigger)
            if bigger:
                self.display({"mode": "sentence", "title": "A bigger sentence!",
                              "said": info.text, "bigger": bigger})
            tips = f" Ideas: {'; '.join(info.hints)}." if info.hints and not bigger else ""
            mixed = (f" {child.name} mixed " + " and ".join(LANGUAGE_NAMES[l] for l in info.languages)
                     + " - that's fine; gently echo it back in one language.") if info.mixed else ""
            return {"ok": True, "analysis": info.as_dict(), "message":
                    (self.conversation.expansion_message(lang, bigger) if bigger
                     else f"{info.words} word(s).") + tips + mixed}
        return self._with(name, run)

    def conversation_starter(self, name: str | None = None, topic: str | None = None,
                             language: str | None = None) -> dict:
        def run(child: Child) -> dict:
            lang = normalize_language(language, child.focus_language)
            topic_key, question = self.conversation.starter(lang, topic)
            return {"ok": True, "topic": topic_key, "message":
                    f"Ask: \"{question}\" Follow up with: \"{self.conversation.follow_up(lang)}\""}
        return self._with(name, run)

    # -- stories -----------------------------------------------------------

    def story(self, name: str | None = None, scene: str | None = None,
              language: str | None = None) -> dict:
        def run(child: Child) -> dict:
            lang = normalize_language(language, child.focus_language)
            chosen = self.stories.next_scene(child, lang, scene)
            self.display({**self.stories.display(chosen, lang), "student": child.name})
            self.tracker.complete_lesson(child, f"story:{chosen.key}:{lang}")
            return {"ok": True, "scene": chosen.key,
                    "message": self.stories.guide(chosen, lang, child.name)}
        return self._with(name, run)

    # -- parents -----------------------------------------------------------

    def parent_dashboard(self, name: str | None = None, days: int = 7) -> dict:
        def run(child: Child) -> dict:
            report = self.dashboard.week(child, max(1, min(int(days or 7), 60)))
            text = self.dashboard.text(report)
            self.display({"mode": "dashboard", "title": f"{child.name} · this week", "text": text})
            return {"ok": True, "report": report, "message": text}
        return self._with(name, run)

