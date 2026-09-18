"""ProjectIndexer: what a project is, from its own files.

Reads README / CLAUDE.md / docs, the manifests (package.json,
requirements.txt, pyproject.toml, pubspec.yaml, ...) and the source layout,
and produces a JSON-serialisable index plus a one-paragraph summary. No
network, no code execution - only reads text files.
"""
from __future__ import annotations

import json
import re
import tomllib
from collections import Counter
from pathlib import Path

from .code_insights import CodeInsights, is_secret, read_text
from .models import now_iso

DOC_NAMES = ("README.md", "README.rst", "README.txt", "README", "CLAUDE.md",
             "ARCHITECTURE.md", "CONTRIBUTING.md", "AGENTS.md")
MAX_DOCS = 20
DOC_BYTES = 60_000
SECTION_CHARS = 1500

LANGUAGES = {
    ".py": "Python", ".dart": "Dart", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java", ".kt": "Kotlin",
    ".swift": "Swift", ".go": "Go", ".rs": "Rust", ".cs": "C#", ".cpp": "C++",
    ".c": "C", ".rb": "Ruby", ".php": "PHP", ".sql": "SQL", ".ps1": "PowerShell",
    ".vue": "Vue", ".svelte": "Svelte",
}

# dependency name (lower-case) -> technology worth mentioning
TECH = {
    "flutter": "Flutter", "flutter_riverpod": "Riverpod", "hooks_riverpod": "Riverpod",
    "riverpod": "Riverpod", "go_router": "go_router", "freezed": "freezed",
    "supabase_flutter": "Supabase", "supabase": "Supabase", "@supabase/supabase-js": "Supabase",
    "firebase_core": "Firebase", "firebase_messaging": "Firebase Cloud Messaging",
    "firebase": "Firebase", "firebase-admin": "Firebase", "flutter_stripe": "Stripe",
    "stripe": "Stripe", "dio": "dio", "react": "React", "next": "Next.js", "vue": "Vue",
    "@angular/core": "Angular", "svelte": "Svelte", "express": "Express",
    "@nestjs/core": "NestJS", "django": "Django", "flask": "Flask", "fastapi": "FastAPI",
    "google-genai": "Gemini API", "openai": "OpenAI API", "anthropic": "Claude API",
    "pyaudio": "PyAudio", "sqlalchemy": "SQLAlchemy", "pandas": "pandas",
    "torch": "PyTorch", "tensorflow": "TensorFlow", "langchain": "LangChain",
    "langgraph": "LangGraph", "prisma": "Prisma", "tailwindcss": "Tailwind CSS",
    "pywin32": "pywin32", "openwakeword": "openWakeWord", "websockets": "websockets",
}

_CURRENT_RE = re.compile(r"(←|<-+)\s*current\b|\(current\)|\bcurrent (phase|milestone|focus)\b",
                         re.IGNORECASE)
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[ \]\s+(.+)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_ARCH_RANK = [re.compile(rx, re.IGNORECASE) for rx in (
    r"architecture", r"structure|layers?", r"how it works|design|overview", r"tech stack|stack",
)]


def _clean_md(text: str) -> str:
    text = re.sub(r"~~(.*?)~~", "", text)                 # struck-through = done
    text = re.sub(r"[*_`]+", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)  # links -> text
    text = re.sub(r"(←|<-+)\s*current\b|\(current\)", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*(\d+\.|[-*])\s+", "", text)
    return " ".join(text.split()).strip(" .:-")


def _first_paragraph(markdown: str) -> str | None:
    para = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            if para:
                break
            continue
        if stripped.startswith(("#", "![", "[![", "<", "---", "```", "|", ">")):
            if para:
                break
            continue
        para.append(stripped)
    text = _clean_md(" ".join(para))
    return text[:300] if len(text) > 20 else None


def _sections(markdown: str) -> list[tuple[str, str]]:
    """(heading, body) for every heading, body running to the next heading
    of the same or a higher level."""
    lines = markdown.splitlines()
    heads = [(i, len(m.group(1)), m.group(2)) for i, line in enumerate(lines)
             if (m := _HEADING_RE.match(line))]
    out = []
    for n, (i, level, title) in enumerate(heads):
        end = len(lines)
        for j, other_level, _ in heads[n + 1:]:
            if other_level <= level:
                end = j
                break
        out.append((title.strip(), "\n".join(lines[i + 1:end]).strip()))
    return out


def _pubspec(text: str) -> dict:
    """Just enough YAML for pubspec: name, description, dependency names."""
    info = {"name": None, "description": None, "dependencies": [], "dev_dependencies": []}
    block = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        top = re.match(r"^([A-Za-z_]+):\s*(.*)$", line)
        if top:
            key, value = top.group(1), top.group(2).strip().strip("'\"")
            block = key if key in ("dependencies", "dev_dependencies") else None
            if key in ("name", "description") and value:
                info[key] = value
            continue
        dep = re.match(r"^  ([A-Za-z0-9_]+):", line)
        if block and dep:
            info[block].append(dep.group(1))
    return info


def _requirements(text: str) -> list[str]:
    names = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if m:
            names.append(m.group(1).lower())
    return names


class ProjectIndexer:
    def __init__(self, path, insights: CodeInsights | None = None):
        self.root = Path(path).resolve()
        self.insights = insights or CodeInsights(self.root)

    def index(self) -> dict:
        manifests, dependencies, stack = {}, [], []
        description = None

        pubspec = self._read("pubspec.yaml")
        if pubspec is not None:
            info = _pubspec(pubspec)
            manifests["pubspec.yaml"] = {k: v for k, v in info.items() if v}
            dependencies += info["dependencies"]
            stack += ["Flutter", "Dart"] if "flutter" in info["dependencies"] else ["Dart"]
            if info["description"] and "new flutter project" not in info["description"].lower():
                description = info["description"]

        package = self._read("package.json")
        if package is not None:
            try:
                data = json.loads(package)
            except ValueError:
                data = {}
            deps = list((data.get("dependencies") or {})) + list((data.get("devDependencies") or {}))
            manifests["package.json"] = {"name": data.get("name"),
                                         "scripts": sorted((data.get("scripts") or {}))[:15],
                                         "dependencies": deps[:40]}
            dependencies += deps
            stack.append("TypeScript" if (self.root / "tsconfig.json").exists() else "Node.js")
            description = description or data.get("description")

        requirements = self._read("requirements.txt")
        if requirements is not None:
            reqs = _requirements(requirements)
            manifests["requirements.txt"] = reqs[:40]
            dependencies += reqs
            stack.append("Python")

        pyproject = self._read("pyproject.toml")
        if pyproject:
            try:
                data = tomllib.loads(pyproject)
            except tomllib.TOMLDecodeError:
                data = {}
            project = data.get("project") or {}
            deps = [_requirements(d)[0] for d in project.get("dependencies", []) if _requirements(d)]
            manifests["pyproject.toml"] = {"name": project.get("name"), "dependencies": deps}
            dependencies += deps
            stack.append("Python")
            description = description or project.get("description")

        for marker, tech in (("go.mod", "Go"), ("Cargo.toml", "Rust"), ("pom.xml", "Java"),
                             ("build.gradle", "Gradle"), ("supabase", "Supabase"),
                             ("firebase.json", "Firebase"), ("Dockerfile", "Docker")):
            if (self.root / marker).exists():
                stack.append(tech)
        if any(self.root.glob("*.sln")) or any(self.root.glob("*.csproj")):
            stack.append(".NET")

        for dep in dependencies:
            tech = TECH.get(dep.lower())
            if tech:
                stack.append(tech)

        docs = self._docs()
        readme = next((text for name, text in docs if name.lower().startswith("readme")), None)
        if not description and readme:
            description = _first_paragraph(readme)
        if not description:
            claude = next((text for name, text in docs if name == "CLAUDE.md"), None)
            if claude:
                description = _first_paragraph(claude)

        languages = Counter()
        for f in self.insights.source_files():
            lang = LANGUAGES.get(f.suffix.lower())
            if lang:
                languages[lang] += 1
        tests = [f for f in self.insights.source_files() if CodeInsights.is_test(f)]

        return {
            "name": self.root.name,
            "path": str(self.root),
            "description": description,
            "stack": list(dict.fromkeys(stack)),
            "languages": dict(languages.most_common(6)),
            "dependencies": list(dict.fromkeys(dependencies))[:60],
            "manifests": manifests,
            "docs": [name for name, _ in docs],
            "readme_excerpt": (readme or "")[:1200],
            "architecture_notes": self._architecture(docs),
            "current_focus": self._current_focus(docs),
            "doc_open_items": self._open_items(docs),
            "layout": self.insights.layout(depth=2, limit=15),
            "source_files": sum(languages.values()),
            "test_files": len(tests),
            "indexed_at": now_iso(),
        }

    def _read(self, name: str) -> str | None:
        path = self.root / name
        if not path.is_file() or is_secret(path):
            return None
        return read_text(path, DOC_BYTES)

    def _docs(self) -> list[tuple[str, str]]:
        found = []
        for name in DOC_NAMES:
            text = self._read(name)
            if text is not None:
                found.append((name, text))
        docs_dir = self.root / "docs"
        if docs_dir.is_dir():
            for path in sorted(docs_dir.rglob("*.md"))[:MAX_DOCS]:
                if is_secret(path):
                    continue
                text = read_text(path, DOC_BYTES)
                if text is not None:
                    found.append((path.relative_to(self.root).as_posix(), text))
        return found[:MAX_DOCS]

    def _architecture(self, docs) -> str:
        # an ARCHITECTURE.md / docs/architecture*.md is the best source
        for name, text in docs:
            if "architecture" in name.lower():
                return text[:SECTION_CHARS]
        found = []
        for name, text in docs:
            for title, body in _sections(text):
                if not body:
                    continue
                for rank, rx in enumerate(_ARCH_RANK):
                    if rx.search(title):
                        found.append((rank, name, title, body))
                        break
        found.sort(key=lambda item: item[0])      # architecture before tech stack
        chunks, used = [], 0
        for _, name, title, body in found:
            if used >= SECTION_CHARS:
                break
            piece = f"[{name} - {title}]\n{body}"[: min(SECTION_CHARS - used, SECTION_CHARS // 2)]
            chunks.append(piece)
            used += len(piece)
        return "\n\n".join(chunks)

    def _current_focus(self, docs) -> list[str]:
        focus = []
        for _, text in docs:
            for line in text.splitlines():
                if _CURRENT_RE.search(line):
                    cleaned = _clean_md(line)
                    if cleaned and cleaned not in focus:
                        focus.append(cleaned)
        return focus[:5]

    def _open_items(self, docs) -> list[str]:
        items = []
        for _, text in docs:
            for line in text.splitlines():
                m = _CHECKBOX_RE.match(line)
                if m:
                    items.append(_clean_md(m.group(1)))
        return items[:15]


def summary_text(index: dict) -> str:
    """One paragraph: what it is, what it is built with, how big it is."""
    if not index:
        return "Not indexed yet."
    parts = []
    name = index.get("name", "This project")
    if index.get("description"):
        parts.append(f"{name}: {index['description'].rstrip('.')}.")
    if index.get("stack"):
        parts.append(f"Built with {', '.join(index['stack'][:8])}.")
    files = index.get("source_files", 0)
    if files:
        langs = ", ".join(f"{k} {v}" for k, v in list(index.get("languages", {}).items())[:3])
        parts.append(f"{files} source files ({langs}), {index.get('test_files', 0)} of them tests.")
    top = [item["folder"] for item in index.get("layout", [])[:5]]
    if top:
        parts.append(f"Main code in {', '.join(top)}.")
    if index.get("current_focus"):
        parts.append(f"Docs mark as current: {index['current_focus'][0]}.")
    return " ".join(parts) or f"{name} has no README or manifest I can read."
