"""Verified source text: the Quran and hadith, fetched and cached.

Nothing here is ever generated or typed from memory. The Quran comes from
api.alquran.cloud, which serves the Tanzil project's verified Uthmani text
(with Sahih International translation and a transliteration); recitation
audio from the Islamic Network CDN (Mishary Alafasy by default); hadith
from the open fawazahmed0/hadith-api dataset. Everything fetched is cached
in SQLite, so a lesson that worked once works offline after that.

If a source cannot be reached and nothing is cached, callers get a
SourceUnavailable error - they must say so, never fill the gap.
"""
from __future__ import annotations

import dataclasses
import difflib
import json
import logging
import re
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

from .db import DATA_DIR

logger = logging.getLogger("jarvis.islamic.sources")

QURAN_API = "https://api.alquran.cloud/v1"
QURAN_EDITIONS = "quran-uthmani,en.sahih,en.transliteration"
AUDIO_URL = "https://cdn.islamic.network/quran/audio/128/{reciter}/{number}.mp3"
DEFAULT_RECITER = "ar.alafasy"
HADITH_API = "https://cdn.jsdelivr.net/gh/fawazahmed0/hadith-api@1/editions"
TIMEOUT = 20
USER_AGENT = "Jarvis-IslamicTutor/1.0"

# alquran.cloud glues the basmala onto ayah 1 of every surah except
# Al-Fatiha (where it IS ayah 1) and At-Tawbah (which has none). For
# memorisation ayah 1 of Al-Ikhlas must start at "qul", so it is removed.
# The basmala to remove is taken from the source's own 1:1, never typed
# here - a hand-typed copy differed from the source in invisible marks.
BASMALA_GLOBAL_NUMBER = 1   # 1:1 - also used to play the basmala before a surah

HADITH_COLLECTIONS = {
    "bukhari": "Sahih al-Bukhari",
    "muslim": "Sahih Muslim",
    "abudawud": "Sunan Abi Dawud",
    "tirmidhi": "Jami' at-Tirmidhi",
    "nasai": "Sunan an-Nasa'i",
    "ibnmajah": "Sunan Ibn Majah",
    "malik": "Muwatta Malik",
    "nawawi": "An-Nawawi's Forty Hadith",
}
# Scholars treat these two collections as authentic in their entirety; the
# dataset carries no per-hadith grade for them.
CONSENSUS_SAHIH = {"bukhari", "muslim"}


class SourceUnavailable(RuntimeError):
    """The verified source could not be reached and nothing is cached."""


@dataclasses.dataclass(frozen=True)
class Surah:
    number: int
    name_ar: str
    name_en: str
    meaning: str
    ayah_count: int
    revelation: str

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class Ayah:
    surah: int
    ayah: int
    global_number: int
    arabic: str
    translation: str
    transliteration: str

    @property
    def ref(self) -> str:
        return f"{self.surah}:{self.ayah}"

    def as_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data["ref"] = self.ref
        return data


@dataclasses.dataclass(frozen=True)
class Hadith:
    collection: str
    number: int
    text_en: str
    text_ar: str | None
    grades: list[dict]

    @property
    def collection_name(self) -> str:
        return HADITH_COLLECTIONS.get(self.collection, self.collection)

    @property
    def reference(self) -> str:
        return f"{self.collection_name} {self.number}"

    @property
    def grading(self) -> str:
        if self.collection in CONSENSUS_SAHIH:
            return "Sahih (the whole collection is accepted as authentic by scholarly consensus)"
        if not self.grades:
            return "Grade not given in the source - treat with care"
        return "; ".join(f"{g.get('grade')} ({g.get('name')})" for g in self.grades[:3])

    def as_dict(self) -> dict:
        data = dataclasses.asdict(self)
        data.update(reference=self.reference, grading=self.grading)
        return data


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise SourceUnavailable(f"could not reach {url.split('/')[2]}: {exc}") from exc


def clean_arabic(text: str, surah: int, ayah: int, basmala: str | None = None) -> str:
    """Strip the BOM, and the basmala the API prefixes to ayah 1."""
    text = text.lstrip("\ufeff").strip()
    if ayah == 1 and surah not in (1, 9) and basmala:
        from .recitation import normalize
        prefix = basmala.split()
        head = text.split()
        if len(head) > len(prefix) and                 [normalize(w) for w in head[:len(prefix)]] == [normalize(w) for w in prefix]:
            text = " ".join(head[len(prefix):])
    return text


class QuranSource:
    def __init__(self, conn: sqlite3.Connection, audio_dir: Path | None = None,
                 reciter: str = DEFAULT_RECITER):
        self._conn = conn
        self.reciter = reciter
        self.audio_dir = audio_dir or (DATA_DIR / "audio")

    # -- surah list ------------------------------------------------------

    def surahs(self) -> list[Surah]:
        rows = self._conn.execute("SELECT * FROM quran_surahs ORDER BY number").fetchall()
        if len(rows) < 114:
            data = _get_json(f"{QURAN_API}/meta")["data"]["surahs"]["references"]
            self._conn.executemany(
                "INSERT OR REPLACE INTO quran_surahs VALUES (?, ?, ?, ?, ?, ?)",
                [(s["number"], s["name"], s["englishName"], s["englishNameTranslation"],
                  s["numberOfAyahs"], s["revelationType"]) for s in data],
            )
            self._conn.commit()
            rows = self._conn.execute("SELECT * FROM quran_surahs ORDER BY number").fetchall()
        return [Surah(**dict(r)) for r in rows]

    def surah(self, number) -> Surah:
        """A surah by number (112, "112") or by name ("Al-Ikhlas", "ikhlaas")."""
        text = str(number).strip()
        if not text.isdigit():
            return self.surahs()[self.resolve_surah(text) - 1]
        if not 1 <= int(text) <= 114:
            raise ValueError("a surah number is between 1 and 114")
        return self.surahs()[int(text) - 1]

    def resolve_surah(self, name: str) -> int:
        def key(text: str) -> str:
            text = re.sub(r"[^a-z]", "", text.lower().replace("surah", "").replace("sura", ""))
            text = re.sub(r"^(al|an|at|ash|as|ad|az|ar|adh)(?=[a-z]{3})", "", text)
            return re.sub(r"(.)\1+", r"\1", text)      # ikhlaas -> ikhlas
        wanted = key(name)
        if not wanted:
            raise ValueError("which surah?")
        names = {s.number: key(s.name_en) for s in self.surahs()}
        for number, have in names.items():
            if have == wanted:
                return number
        close = difflib.get_close_matches(wanted, list(names.values()), n=2, cutoff=0.8)
        if len(close) == 1 or (close and difflib.SequenceMatcher(None, wanted, close[0]).ratio() >= 0.9):
            return next(n for n, v in names.items() if v == close[0])
        raise ValueError(f"I don't know a surah called '{name}' - say its number or full name")

    # -- text --------------------------------------------------------------

    def ayahs(self, surah: int, start: int = 1, end: int | None = None) -> list[Ayah]:
        info = self.surah(surah)
        end = min(end or info.ayah_count, info.ayah_count)
        start = max(1, start)
        if start > end:
            raise ValueError(f"{info.name_en} has {info.ayah_count} ayahs")
        rows = self._rows(surah, start, end)
        if len(rows) < end - start + 1:
            self._download_surah(surah)
            rows = self._rows(surah, start, end)
        return [Ayah(**dict(r)) for r in rows]

    def ayah(self, surah: int, ayah: int) -> Ayah:
        return self.ayahs(surah, ayah, ayah)[0]

    def _rows(self, surah, start, end):
        return self._conn.execute(
            "SELECT * FROM quran_ayahs WHERE surah = ? AND ayah BETWEEN ? AND ? ORDER BY ayah",
            (surah, start, end),
        ).fetchall()

    def _download_surah(self, surah: int) -> None:
        editions = _get_json(f"{QURAN_API}/surah/{surah}/editions/{QURAN_EDITIONS}")["data"]
        by_id = {e["edition"]["identifier"]: e["ayahs"] for e in editions}
        arabic, english, translit = (by_id["quran-uthmani"], by_id["en.sahih"],
                                     by_id["en.transliteration"])
        if not (len(arabic) == len(english) == len(translit)):
            raise SourceUnavailable("the Quran source returned editions of different lengths")
        basmala = None if surah in (1, 9) else self.ayah(1, 1).arabic
        rows = []
        for ar, en, tr in zip(arabic, english, translit):
            n = ar["numberInSurah"]
            rows.append((surah, n, ar["number"], clean_arabic(ar["text"], surah, n, basmala),
                         en["text"].strip(), tr["text"].strip()))
        self._conn.executemany("INSERT OR REPLACE INTO quran_ayahs VALUES (?, ?, ?, ?, ?, ?)", rows)
        self._conn.commit()
        logger.info("cached surah %s (%d ayahs)", surah, len(rows))

    def search(self, keyword: str, limit: int = 8) -> list[Ayah]:
        """English-translation keyword search (Sahih International)."""
        keyword = " ".join((keyword or "").split())
        if len(keyword) < 3:
            return []
        url = f"{QURAN_API}/search/{urllib.request.quote(keyword)}/all/en.sahih"
        data = _get_json(url)
        if not isinstance(data.get("data"), dict):
            return []            # the API answers 404 "no matches" as a string
        out = []
        for match in data["data"].get("matches", [])[:limit]:
            out.append(self.ayah(match["surah"]["number"], match["numberInSurah"]))
        return out

    # -- audio -------------------------------------------------------------

    def audio_url(self, ayah: Ayah | int) -> str:
        number = ayah.global_number if isinstance(ayah, Ayah) else int(ayah)
        return AUDIO_URL.format(reciter=self.reciter, number=number)

    def audio_file(self, ayah: Ayah | int) -> Path:
        """Local copy of an ayah's recitation (downloaded once)."""
        number = ayah.global_number if isinstance(ayah, Ayah) else int(ayah)
        path = self.audio_dir / self.reciter / f"{number}.mp3"
        if path.is_file() and path.stat().st_size > 1024:
            return path
        req = urllib.request.Request(self.audio_url(number), headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                if "audio" not in resp.headers.get("Content-Type", ""):
                    raise SourceUnavailable("the audio source did not return audio")
                data = resp.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SourceUnavailable(f"could not download recitation audio: {exc}") from exc
        if len(data) < 1024:
            raise SourceUnavailable("the recitation audio file was empty")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        tmp.replace(path)
        return path


class HadithSource:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def get(self, collection: str, number: int) -> Hadith:
        collection = (collection or "").lower().replace(" ", "").replace("-", "").replace("'", "")
        collection = {"sahihbukhari": "bukhari", "sahihalbukhari": "bukhari",
                      "sahihmuslim": "muslim", "abudaud": "abudawud", "sunanabidawud": "abudawud",
                      "jamiattirmidhi": "tirmidhi", "nawawi40": "nawawi"}.get(collection, collection)
        if collection not in HADITH_COLLECTIONS:
            raise ValueError(f"unknown collection; use one of {', '.join(HADITH_COLLECTIONS)}")
        number = int(number)
        row = self._conn.execute(
            "SELECT * FROM hadith_cache WHERE collection = ? AND number = ?", (collection, number)
        ).fetchone()
        if row is None:
            english = _get_json(f"{HADITH_API}/eng-{collection}/{number}.json")["hadiths"]
            if not english or not english[0].get("text", "").strip():
                raise SourceUnavailable(f"{HADITH_COLLECTIONS[collection]} {number} has no text in the source")
            try:
                arabic = _get_json(f"{HADITH_API}/ara-{collection}/{number}.json")["hadiths"][0]["text"]
            except (SourceUnavailable, KeyError, IndexError):
                arabic = None
            grades = [{"name": g.get("name"), "grade": g.get("grade")}
                      for g in english[0].get("grades", [])]
            from .tracker import now_iso   # local import: tracker imports nothing from here
            self._conn.execute(
                "INSERT OR REPLACE INTO hadith_cache VALUES (?, ?, ?, ?, ?, ?)",
                (collection, number, english[0]["text"].strip(), arabic, json.dumps(grades), now_iso()),
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM hadith_cache WHERE collection = ? AND number = ?", (collection, number)
            ).fetchone()
        return Hadith(collection=row["collection"], number=row["number"], text_en=row["text_en"],
                      text_ar=row["text_ar"], grades=json.loads(row["grades"]))
