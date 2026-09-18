"""ParentDashboard: a weekly summary a parent can read in a minute.

Describes practice, not ability: what was practised, what is growing,
and what to try next. No scores against other children, no labels - and
it says so, pointing to a speech therapist (logopedist) for any concern.
"""
from __future__ import annotations

import statistics

from .tracker import Child, ProgressTracker, days_ago
from .vocabulary import LANGUAGE_NAMES, WORDS_BY_KEY

DISCLAIMER = ("This is a practice log, not an assessment. If you have any concern about "
              "speech or language development, a speech therapist (logopedist) is the "
              "right person to talk to.")


def _avg(values):
    return round(statistics.mean(values), 1) if values else None


class ParentDashboard:
    def __init__(self, tracker: ProgressTracker):
        self.tracker = tracker

    def week(self, child: Child, days: int = 7) -> dict:
        since, before = days_ago(days - 1), days_ago(2 * days - 1)
        learned = self.tracker.words_learned_since(child, since)
        seen = self.tracker.words_seen_since(child, since)
        this_week = self.tracker.utterances(child, since)
        last_week = self.tracker.utterances(child, before, until=since)
        avg_now = _avg([u["words"] for u in this_week])
        avg_before = _avg([u["words"] for u in last_week])
        longest = max(this_week, key=lambda u: u["words"], default=None)
        expanded = [u for u in this_week if u["expanded"]]
        mixed = [u for u in this_week if len(u["languages"]) >= 2]
        sounds = self.tracker.sounds(child, since)
        attempts = self.tracker.words_practised_since(child, since)
        sessions = [s for s in self.tracker.sessions(child, since) if s["ended_at"]]
        active_days = len({u["day"] for u in this_week} | {s["day"] for s in sessions})
        lessons = self.tracker.lessons_since(child, since)
        practising = self.tracker.practising_sounds(child)

        def word_list(items):
            by_lang = {}
            for item in items:
                w = WORDS_BY_KEY.get(item["word_key"])
                if w:
                    by_lang.setdefault(item["language"], []).append(w.text(item["language"]))
            return by_lang

        return {
            "child": child.as_dict(), "days": days, "active_days": active_days,
            "sessions": len(sessions), "stars": child.stars,
            "new_words_learned": word_list(learned), "words_introduced": word_list(seen),
            "sentences": {"count": len(this_week), "average_words": avg_now,
                          "average_words_before": avg_before,
                          "longest": longest["text"] if longest else None,
                          "expanded_together": len(expanded), "mixed_language": len(mixed)},
            "pronunciation": {"attempts": attempts,
                              "sounds": [{**s, "rate": round(s["successes"] / s["attempts"], 2)}
                                         for s in sounds]},
            "keep_practising": practising, "lessons": lessons,
            "recommendations": self.recommend(child, avg_now, avg_before, mixed, this_week, practising),
        }

    def recommend(self, child: Child, avg_now, avg_before, mixed, utterances, practising) -> list[str]:
        recs = []
        for s in practising[:2]:
            examples = [w.text(s["language"]) for w in WORDS_BY_KEY.values()
                        if s["sound"] in w.text(s["language"]).lower()][:3]
            recs.append(f"Play a naming game with words that have the '{s['sound']}' sound in "
                        f"{LANGUAGE_NAMES[s['language']]}: {', '.join(examples)}. Say them slowly "
                        "together - no correcting, just fun.")
        if utterances and avg_now is not None and avg_now < 4:
            recs.append(f"When {child.name} says a short sentence, repeat it back one step bigger "
                        "(\"Dog running!\" -> \"Yes, the dog is running fast!\") - no need to "
                        "ask for a repeat.")
        if mixed and len(mixed) >= max(3, len(utterances) // 3):
            recs.append("Mixing languages is a normal part of growing up multilingual. Try "
                        "one-language moments: a Dutch story at bedtime, English at breakfast.")
        if not utterances:
            recs.append("No conversation practice this week - five minutes of 'tell me about "
                        "your day' is a great start.")
        focus = LANGUAGE_NAMES[child.focus_language]
        recs.append(f"Read a picture book in {focus} together and ask 'what do you see?' on each page.")
        return recs[:4]

    def text(self, report: dict) -> str:
        c, s, p = report["child"], report["sentences"], report["pronunciation"]
        lines = [f"This week with {c['name']}: {report['active_days']} active day(s), "
                 f"{report['sessions']} full session(s), {c['stars']} stars so far."]
        learned = report["new_words_learned"]
        if learned:
            parts = [f"{LANGUAGE_NAMES[lang]}: {', '.join(words)}" for lang, words in learned.items()]
            lines.append("New words learned - " + "; ".join(parts) + ".")
        elif report["words_introduced"]:
            n = sum(len(v) for v in report["words_introduced"].values())
            lines.append(f"{n} new words introduced (not yet said clearly twice).")
        if s["count"]:
            trend = ""
            if s["average_words_before"] is not None:
                trend = f" (last week {s['average_words_before']})"
            lines.append(f"Sentences: {s['count']} in practice chats, about {s['average_words']} "
                         f"words each{trend}; longest: \"{s['longest']}\". Made bigger together "
                         f"{s['expanded_together']} time(s).")
            if s["mixed_language"]:
                lines.append(f"Mixed languages in {s['mixed_language']} sentence(s) - normal "
                             "for a child growing up with three languages.")
        if p["attempts"]:
            lines.append(f"Pronunciation practice: {p['attempts']} word(s) tried.")
            keep = report["keep_practising"]
            if keep:
                lines.append("Sounds to keep playing with: " + ", ".join(
                    f"'{k['sound']}' ({LANGUAGE_NAMES[k['language']]})" for k in keep) + ".")
        if report["recommendations"]:
            lines.append("Ideas for this week:")
            lines.extend(f"* {r}" for r in report["recommendations"])
        lines.append(DISCLAIMER)
        return "\n".join(lines)

