"""Engineering assistant: search, TODOs, reading code, module summaries and
code-health heuristics for one project folder.

Everything here returns raw facts (paths, lines, counts). Jarvis's model
does the explaining - it can only explain what these functions hand it.

Two guards apply to every read, because whatever is read here is sent to
the Gemini API:
  * paths can never escape the project root (no "../../secrets"), and
  * secret-looking files (.env, keys, keystores, credentials) are never
    read or searched.
"""
from __future__ import annotations

import ast
import difflib
import os
import re
from collections import Counter
from pathlib import Path

SKIP_DIRS = {
    "node_modules", "build", "dist", "out", "target", "bin", "obj", "coverage",
    "__pycache__", "venv", "env", "Pods", "DerivedData", "vendor", "site-packages",
    "ephemeral", "Generated", "generated",
}
SOURCE_EXT = {
    ".py", ".dart", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts", ".swift",
    ".go", ".rs", ".cs", ".cpp", ".cc", ".c", ".h", ".hpp", ".m", ".sql", ".ps1",
    ".sh", ".rb", ".php", ".vue", ".svelte", ".html", ".css", ".scss",
}
DOC_EXT = {".md", ".rst", ".txt"}
CONFIG_NAMES = {
    "pubspec.yaml", "package.json", "requirements.txt", "pyproject.toml", "go.mod",
    "cargo.toml", "pom.xml", "build.gradle", "analysis_options.yaml", "tsconfig.json",
    "dockerfile", "docker-compose.yml", "makefile",
}
GENERATED_SUFFIXES = (
    ".g.dart", ".freezed.dart", ".gr.dart", ".mocks.dart", ".pb.dart", ".pbenum.dart",
    ".min.js", ".min.css", ".designer.cs", "_pb2.py",
)
SECRET_NAMES = {
    "id_rsa", "id_ed25519", "id_dsa", "credentials.json", "service-account.json",
    "serviceaccount.json", "google-services.json", "googleservice-info.plist",
    "key.properties", "secrets.json", "secrets.yaml", ".npmrc", ".pypirc", ".netrc",
}
SECRET_EXT = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore", ".crt", ".cer"}

MAX_FILES = 5000
MAX_FILE_BYTES = 1_000_000
READ_MAX_LINES = 150
READ_MAX_CHARS = 8000
LARGE_FILE_LINES = 400
LONG_FUNCTION_LINES = 60

_TODO_RE = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b[:(\s-]*(.*)")
_COMMENT_RE = re.compile(r"(#|//|/\*|<!--|^\s*\*|^\s*--)")
_DECL_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|internal\s+|protected\s+)?"
    r"(?:abstract\s+|final\s+|sealed\s+|static\s+|async\s+|base\s+|data\s+)*"
    r"(class|interface|enum|mixin|extension|def|function|fun|func|struct|trait|record)\s+"
    r"([A-Za-z_]\w*)"
)


def is_secret(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith(".env") or name in SECRET_NAMES or path.suffix.lower() in SECRET_EXT:
        return True
    return "secret" in name or "credential" in name or name.endswith(".env")


def is_generated(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(GENERATED_SUFFIXES) or name.endswith(".lock") or name in {
        "package-lock.json", "pubspec.lock", "yarn.lock", "poetry.lock", "uv.lock",
    }


def walk_project(root: Path, max_files: int = MAX_FILES):
    """Every file worth looking at, skipping VCS, dependencies, build output
    and hidden folders."""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS
        )
        for name in sorted(filenames):
            count += 1
            if count > max_files:
                return
            yield Path(dirpath) / name


def count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as fh:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: fh.read(65536), b""))
    except OSError:
        return 0


def read_text(path: Path, limit: int = MAX_FILE_BYTES) -> str | None:
    try:
        if path.stat().st_size > limit:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None          # binary
    return data.decode("utf-8", errors="replace")


def _stem_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


class CodeInsights:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._files: list[Path] | None = None

    # -- file inventory -------------------------------------------------

    def files(self) -> list[Path]:
        if self._files is None:
            self._files = list(walk_project(self.root))
        return self._files

    def source_files(self) -> list[Path]:
        return [
            f for f in self.files()
            if f.suffix.lower() in SOURCE_EXT and not is_generated(f) and not is_secret(f)
        ]

    def doc_files(self) -> list[Path]:
        return [f for f in self.files() if f.suffix.lower() in DOC_EXT and not is_secret(f)]

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    @staticmethod
    def is_test(path: Path) -> bool:
        name = path.name.lower()
        parts = {p.lower() for p in path.parts}
        return (
            name.startswith("test_") or re.search(r"[._](test|spec)\.\w+$", name) is not None
            or "tests" in parts or "test" in parts or "__tests__" in parts
        )

    # -- safe path handling ------------------------------------------------

    def safe_path(self, relative: str) -> Path | None:
        """Resolve a path inside the project; None if it escapes the root."""
        candidate = (self.root / relative.replace("\\", "/").lstrip("/")).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate

    def resolve(self, query: str, want_dir: bool = False) -> list[Path]:
        """A spoken or typed file/folder reference -> matching paths.

        'lib/router/app_router.dart' (exact), 'app router', 'app_router.dart'
        or 'auth' (folder) all work. Returns every equally good match so the
        caller can ask which one.
        """
        query = (query or "").strip().strip("'\"")
        if not query:
            return []
        exact = self.safe_path(query)
        if exact and exact.exists() and (exact.is_dir() == want_dir or not want_dir):
            return [exact]
        words = [w for w in re.findall(r"[A-Za-z0-9_.]+", query.lower())
                 if w not in {"file", "module", "the", "folder", "class", "code", "dart", "py"}]
        key = _stem_key("".join(words))
        if not key:
            return []
        if want_dir:
            pool = sorted({p.parent for p in self.files()} - {self.root})
            name_of = lambda p: _stem_key(p.name)
        else:
            pool = [f for f in self.files() if not is_secret(f)]
            name_of = lambda p: _stem_key(p.name.split(".")[0])
        exact_hits = [p for p in pool if name_of(p) == key]
        if exact_hits:
            return self._prefer_code(exact_hits)[:8]
        contains = [p for p in pool if key in name_of(p)]
        if contains:
            contains.sort(key=lambda p: len(name_of(p)))
            return self._prefer_code(contains)[:8]
        scored = [(difflib.SequenceMatcher(None, key, name_of(p)).ratio(), p) for p in pool]
        scored = [(s, p) for s, p in scored if s >= 0.75]
        if not scored:
            return []
        best = max(s for s, _ in scored)
        return self._prefer_code([p for s, p in scored if s >= best - 0.03])[:8]

    def _prefer_code(self, paths: list[Path]) -> list[Path]:
        """'auth' means lib/features/auth, not test/features/auth, and
        'app router' means app_router.dart, not app_router.g.dart - unless
        only those match."""
        written = [p for p in paths if not is_generated(p)] or paths
        code = [p for p in written if not self.is_test(p.relative_to(self.root))]
        return code or written

    # -- engineering assistant ---------------------------------------------

    def read(self, query: str, start: int = 1, max_lines: int = READ_MAX_LINES) -> dict:
        matches = self.resolve(query)
        if not matches:
            return {"ok": False, "message": f"No file matching '{query}' in this project."}
        if len(matches) > 1:
            return {"ok": False, "needs_choice": True,
                    "candidates": [self.rel(m) for m in matches],
                    "message": "Several files match - which one? "
                               + ", ".join(self.rel(m) for m in matches)}
        path = matches[0]
        if path.is_dir():
            return {"ok": False, "message": f"{self.rel(path)} is a folder, not a file."}
        if is_secret(path):
            return {"ok": False, "message":
                    f"{self.rel(path)} looks like it holds secrets, so I won't read it."}
        text = read_text(path)
        if text is None:
            return {"ok": False, "message": f"{self.rel(path)} is binary or too large to read."}
        lines = text.splitlines()
        start = max(1, int(start or 1))
        end = min(len(lines), start + max_lines - 1)
        body, used = [], 0
        for number in range(start, end + 1):
            line = f"{number:>4}| {lines[number - 1]}"
            if used + len(line) > READ_MAX_CHARS:
                end = number - 1
                break
            body.append(line)
            used += len(line) + 1
        more = f" (file has {len(lines)} lines; ask for line {end + 1} onwards for more)" \
            if end < len(lines) else ""
        return {
            "ok": True, "path": self.rel(path), "start": start, "end": end,
            "total_lines": len(lines), "content": "\n".join(body),
            "message": f"{self.rel(path)}, lines {start}-{end}{more}.",
        }

    def search(self, query: str, limit: int = 20) -> dict:
        query = (query or "").strip()
        if len(query) < 2:
            return {"ok": False, "message": "Tell me what to search for."}
        needle = query.lower()
        name_hits = [self.rel(f) for f in self.files()
                     if needle in f.name.lower() and not is_secret(f)][:10]
        line_hits, files_hit = [], set()
        for f in self.source_files() + self.doc_files():
            text = read_text(f)
            if text is None or needle not in text.lower():
                continue
            files_hit.add(self.rel(f))
            for number, line in enumerate(text.splitlines(), 1):
                if needle in line.lower() and len(line_hits) < limit:
                    line_hits.append({"path": self.rel(f), "line": number,
                                      "text": line.strip()[:160]})
        if not name_hits and not line_hits:
            return {"ok": True, "found": False, "matches": [], "files": [],
                    "message": f"'{query}' does not appear anywhere in the project."}
        parts = []
        if name_hits:
            parts.append(f"file names: {', '.join(name_hits[:5])}")
        if files_hit:
            parts.append(f"found in {len(files_hit)} file(s), e.g. "
                         + ", ".join(sorted(files_hit)[:5]))
        return {"ok": True, "found": True, "file_name_matches": name_hits,
                "matches": line_hits, "files": sorted(files_hit),
                "message": f"'{query}': " + "; ".join(parts) + "."}

    def find_todos(self, limit: int = 30) -> dict:
        items, total, per_file = [], 0, Counter()
        for f in self.source_files():
            text = read_text(f)
            if not text or not _TODO_RE.search(text):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                m = _TODO_RE.search(line)
                # Only inside comments, so a string or identifier that merely
                # contains "TODO" is not counted.
                if not m or not _COMMENT_RE.search(line[:m.start()]):
                    continue
                total += 1
                per_file[self.rel(f)] += 1
                if len(items) < limit:
                    items.append({"path": self.rel(f), "line": number, "tag": m.group(1),
                                  "text": m.group(2).strip()[:140]})
        if not total:
            return {"ok": True, "count": 0, "todos": [],
                    "message": "No TODO or FIXME comments in the code."}
        top = ", ".join(f"{p} ({n})" for p, n in per_file.most_common(3))
        return {"ok": True, "count": total, "todos": items,
                "by_file": dict(per_file.most_common(10)),
                "message": f"{total} TODO/FIXME comment(s); most in {top}."}

    def module_summary(self, query: str) -> dict:
        matches = self.resolve(query, want_dir=True) or self.resolve(query)
        if not matches:
            return {"ok": False, "message": f"No file or folder matching '{query}'."}
        if len(matches) > 1:
            return {"ok": False, "needs_choice": True,
                    "candidates": [self.rel(m) for m in matches],
                    "message": "Several match - which one? "
                               + ", ".join(self.rel(m) for m in matches)}
        target = matches[0]
        if target.is_dir():
            return self._folder_summary(target)
        return self._file_summary(target)

    def _folder_summary(self, folder: Path) -> dict:
        files = [f for f in self.source_files() if folder in f.parents]
        if not files:
            return {"ok": True, "path": self.rel(folder), "files": [],
                    "message": f"{self.rel(folder)} has no source files."}
        sized = sorted(((count_lines(f), f) for f in files), key=lambda x: x[0], reverse=True)
        subdirs = Counter(f.relative_to(folder).parts[0] for f in files
                          if len(f.relative_to(folder).parts) > 1)
        declared = []
        for _, f in sized[:15]:
            names = self._declarations(f)[:4]
            declared.append({"path": self.rel(f), "lines": count_lines(f), "declares": names})
        tests = sum(1 for f in files if self.is_test(f))
        sub = ", ".join(f"{d} ({n})" for d, n in subdirs.most_common(8))
        return {
            "ok": True, "path": self.rel(folder), "file_count": len(files),
            "total_lines": sum(n for n, _ in sized), "test_files": tests,
            "subfolders": dict(subdirs.most_common(15)), "files": declared,
            "message": (f"{self.rel(folder)}: {len(files)} source files, "
                        f"{sum(n for n, _ in sized)} lines"
                        + (f"; subfolders {sub}" if sub else "") + "."),
        }

    def _file_summary(self, path: Path) -> dict:
        if is_secret(path):
            return {"ok": False, "message": f"{self.rel(path)} looks like a secrets file."}
        text = read_text(path) or ""
        info = {"ok": True, "path": self.rel(path), "lines": len(text.splitlines())}
        if path.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree:
                doc = ast.get_docstring(tree)
                info["docstring"] = (doc or "").strip().split("\n\n")[0][:400]
                info["classes"] = [
                    {"name": n.name, "methods": [m.name for m in n.body
                                                 if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))][:15]}
                    for n in tree.body if isinstance(n, ast.ClassDef)
                ]
                info["functions"] = [n.name for n in tree.body
                                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                info["imports"] = sorted({
                    (a.name if isinstance(n, ast.Import) else (n.module or "")).split(".")[0]
                    for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))
                    for a in (n.names if isinstance(n, ast.Import) else [n])
                } - {""})
        else:
            info["declares"] = self._declarations(path)
            header = []
            for line in text.splitlines()[:15]:
                stripped = line.strip()
                if stripped.startswith(("//", "#", "*", "/*", "--")):
                    header.append(stripped.lstrip("/#*- ").strip())
                elif stripped and header:
                    break
            info["header_comment"] = " ".join(h for h in header if h)[:400]
            info["imports"] = re.findall(r"^\s*(?:import|using|require)\s+['\"]?([^'\";\s]+)",
                                         text, re.MULTILINE)[:20]
        names = info.get("declares") or [c["name"] for c in info.get("classes", [])] \
            + info.get("functions", [])
        info["message"] = (f"{info['path']}: {info['lines']} lines"
                           + (f", defines {', '.join(names[:6])}" if names else "") + ".")
        return info

    def _declarations(self, path: Path) -> list[str]:
        text = read_text(path) or ""
        return [m.group(2) for m in (_DECL_RE.match(line) for line in text.splitlines()) if m][:30]

    def layout(self, depth: int = 2, limit: int = 25) -> list[dict]:
        """Source files per folder, `depth` levels deep - the shape of the code."""
        counts = Counter()
        for f in self.source_files():
            parts = f.relative_to(self.root).parts[:-1]
            if parts:
                counts["/".join(parts[:depth])] += 1
        return [{"folder": d, "files": n} for d, n in counts.most_common(limit)]

    def health(self) -> dict:
        """Refactoring candidates, missing tests and technical debt."""
        sources = self.source_files()
        code = [f for f in sources if f.suffix.lower() not in {".html", ".css", ".scss", ".sql"}]
        tests = [f for f in code if self.is_test(f)]
        non_tests = [f for f in code if not self.is_test(f)]

        sized = sorted(((count_lines(f), f) for f in non_tests), key=lambda x: x[0], reverse=True)
        large = [{"path": self.rel(f), "lines": n} for n, f in sized if n >= LARGE_FILE_LINES][:6]

        long_functions = []
        for f in non_tests:
            if f.suffix != ".py":
                continue
            try:
                tree = ast.parse(read_text(f) or "")
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.end_lineno:
                    length = node.end_lineno - node.lineno + 1
                    if length >= LONG_FUNCTION_LINES:
                        long_functions.append({"path": self.rel(f), "function": node.name,
                                               "line": node.lineno, "lines": length})
        long_functions.sort(key=lambda x: x["lines"], reverse=True)

        test_keys = {_stem_key(re.sub(r"^test_|[._](test|spec)$", "", t.stem)) for t in tests}
        untested = []
        for n, f in sized:
            stem = f.stem
            if stem in {"__init__", "__main__", "main", "index", "setup", "conftest"} or n < 30:
                continue
            if _stem_key(stem) not in test_keys:
                untested.append({"path": self.rel(f), "lines": n})
        todos = self.find_todos(limit=5)

        suggestions = []
        for item in large[:3]:
            suggestions.append(f"Refactor: {item['path']} is {item['lines']} lines - "
                               "consider splitting it.")
        for item in long_functions[:3]:
            suggestions.append(f"Refactor: {item['function']}() in {item['path']} is "
                               f"{item['lines']} lines long.")
        for item in untested[:4]:
            suggestions.append(f"Missing tests: {item['path']} ({item['lines']} lines) "
                               "has no matching test file.")
        if todos["count"]:
            suggestions.append(f"Technical debt: {todos['count']} TODO/FIXME comments.")
        ratio = round(len(tests) / len(code), 2) if code else 0.0
        return {
            "ok": True, "source_files": len(code), "test_files": len(tests),
            "test_ratio": ratio, "large_files": large, "long_functions": long_functions[:6],
            "untested": untested[:10], "todo_count": todos["count"],
            "suggestions": suggestions,
            "message": (f"{len(code)} source files, {len(tests)} test files. "
                        + (" ".join(suggestions[:5]) if suggestions else
                           "Nothing stands out - no very large files, TODOs or obvious test gaps.")),
        }
