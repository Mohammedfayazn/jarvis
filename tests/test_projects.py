"""Project intelligence tests - throwaway git repos and folders in a temp
dir; never touches real projects, never launches VS Code."""
import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from projects import db
from projects.assistant import ProjectAssistant
from projects.brain import ProjectBrain
from projects.code_insights import CodeInsights
from projects.git_analyzer import Commit, GitAnalyzer, summarize_commits
from projects.indexer import ProjectIndexer, summary_text
from projects.intent import dispatch, route
from projects.locator import ProjectLocator
from projects.models import Priority, TaskStatus, ago, normalize_kind
from projects.recommend import ProjectContext, RecommendationEngine
from projects.tasks import TaskManager

HAS_GIT = shutil.which("git") is not None


def git(repo: Path, *args, date: str | None = None):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "t@example.com"}
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def make_flutter_project(root: Path) -> Path:
    write(root / "pubspec.yaml",
          "name: homemade\ndescription: Home-cooked food marketplace.\n"
          "dependencies:\n  flutter:\n    sdk: flutter\n  flutter_riverpod: ^2.0.0\n"
          "  supabase_flutter: ^2.0.0\n  go_router: ^14.0.0\n"
          "dev_dependencies:\n  flutter_test:\n    sdk: flutter\n")
    write(root / "README.md",
          "# Homemade\n\nFind home cooks near you and pick up food cooked today.\n\n"
          "## Tech stack\n\nFlutter and Supabase.\n\n"
          "## Architecture\n\nFeature-first folders with data/domain/presentation layers.\n\n"
          "## Roadmap\n\n1. ~~Auth~~ done\n2. **Kitchen availability** <- current\n\n"
          "- [ ] Add order placement workflow\n- [x] Login\n")
    write(root / "lib/features/auth/sign_in_screen.dart",
          "// Sign in screen.\nclass SignInScreen extends StatelessWidget {}\n"
          "// TODO: validate phone numbers\n")
    write(root / "lib/features/auth/auth_repository.dart",
          "abstract class AuthRepository {}\n" + "// line\n" * 450)
    write(root / "lib/features/auth/user.g.dart", "// generated\n// TODO: ignored\n")
    write(root / "test/features/auth/sign_in_screen_test.dart", "void main() {}\n")
    write(root / ".env", "SUPABASE_KEY=super-secret-value\n")
    write(root / "lib/core/config.dart", 'const url = "TODO in a string";\n')
    return root


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


@unittest.skipUnless(HAS_GIT, "git not installed")
class GitAnalyzerTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        write(self.repo / "a.py", "print(1)\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "Add login screen", date="2026-01-01T10:00:00+01:00")
        write(self.repo / "b.py", "print(2)\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "Fix login validation")
        self.git = GitAnalyzer(self.repo)

    def test_recent_commits_newest_first(self):
        commits = self.git.get_recent_commits(5)
        self.assertEqual([c.subject for c in commits],
                         ["Fix login validation", "Add login screen"])
        self.assertEqual(commits[1].when.year, 2026)

    def test_branches(self):
        self.assertEqual(self.git.get_current_branch(), "main")
        git(self.repo, "branch", "feature/x")
        self.assertEqual(sorted(self.git.get_branches()), ["feature/x", "main"])

    def test_uncommitted_changes(self):
        write(self.repo / "a.py", "print('changed')\n")
        write(self.repo / "src/new.py", "x = 1\n")
        write(self.repo / "b.py", "staged\n")
        git(self.repo, "add", "b.py")
        changes = self.git.get_uncommitted_changes()
        self.assertEqual(changes.branch, "main")
        self.assertIn("a.py", changes.unstaged)
        self.assertIn("b.py", changes.staged)
        self.assertEqual(changes.untracked, ["src/"])
        self.assertEqual(changes.total, 3)
        self.assertIn("changed", changes.diff_stat)
        self.assertIn("print('changed')", self.git.get_diff())

    def test_clean_tree(self):
        self.assertEqual(self.git.get_uncommitted_changes().total, 0)

    def test_last_work_session_uses_latest_edit(self):
        write(self.repo / "a.py", "edited\n")
        info = self.git.get_last_work_session()
        self.assertEqual(info["last_edit_file"], "a.py")
        self.assertEqual(info["uncommitted_files"], 1)
        self.assertIsNotNone(info["last_activity"])

    def test_commits_since(self):
        self.assertEqual([c.subject for c in self.git.commits_since(7)], ["Fix login validation"])

    def test_empty_repo_and_non_repo(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        git(empty, "init", "-q", "-b", "main")
        analyzer = GitAnalyzer(empty)
        self.assertTrue(analyzer.is_repo())
        self.assertEqual(analyzer.get_recent_commits(), [])
        self.assertEqual(analyzer.get_current_branch(), "main")
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertFalse(GitAnalyzer(plain).is_repo())


class SummarizeCommitsTest(unittest.TestCase):
    def commits(self, *subjects):
        return [Commit("abc", "t", "2026-01-01T00:00:00+00:00", s) for s in subjects]

    def test_themes(self):
        text = summarize_commits(self.commits(
            "Add login flow", "Fix sign-in validation", "Profile editing",
            "Auth guard in router", "Edit profile avatar"))
        self.assertEqual(text, "The last 5 commits focused on authentication and profile management.")

    def test_no_theme_lists_subjects(self):
        self.assertIn("Wibble", summarize_commits(self.commits("Wibble", "Wobble")))

    def test_empty(self):
        self.assertEqual(summarize_commits([]), "There are no commits yet.")


class TaskManagerTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.brain = ProjectBrain(self.conn)
        self.pid = self.brain.register("demo", tempfile.gettempdir()).id
        self.tasks = TaskManager(self.conn)

    def test_add_dedupes_open_tasks(self):
        first, created = self.tasks.add(self.pid, "Kitchen availability screen")
        again, created_again = self.tasks.add(self.pid, "kitchen availability screen")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.id, again.id)

    def test_status_lifecycle(self):
        task, _ = self.tasks.add(self.pid, "Order workflow")
        done = self.tasks.set_status(task.id, "complete")
        self.assertIs(done.status, TaskStatus.DONE)
        self.assertIsNotNone(done.completed_at)
        reopened = self.tasks.set_status(task.id, "todo")
        self.assertIsNone(reopened.completed_at)

    def test_ordering(self):
        self.tasks.add(self.pid, "low one", priority="low")
        self.tasks.add(self.pid, "high one", priority="high")
        doing, _ = self.tasks.add(self.pid, "doing one")
        self.tasks.set_status(doing.id, TaskStatus.IN_PROGRESS)
        self.assertEqual([t.title for t in self.tasks.open_tasks(self.pid)],
                         ["doing one", "high one", "low one"])

    def test_find_partial_and_ambiguous(self):
        self.tasks.add(self.pid, "Complete kitchen availability screen")
        self.tasks.add(self.pid, "Add order placement workflow")
        self.tasks.add(self.pid, "Order history screen")
        found = self.tasks.find(self.pid, "kitchen screen")
        self.assertEqual([t.title for t in found], ["Complete kitchen availability screen"])
        self.assertEqual(len(self.tasks.find(self.pid, "order")), 2)
        self.assertEqual(self.tasks.find(self.pid, "payments"), [])

    def test_invalid_status_rejected_by_schema(self):
        task, _ = self.tasks.add(self.pid, "x")
        with self.assertRaises(ValueError):
            self.tasks.set_status(task.id, "maybe")


class ProjectBrainTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect(":memory:")
        self.brain = ProjectBrain(self.conn)
        self.project = self.brain.register("homemade", tempfile.gettempdir())

    def test_register_capitalises_and_is_idempotent(self):
        self.assertEqual(self.project.name, "Homemade")
        again = self.brain.register("other", tempfile.gettempdir())
        self.assertEqual(again.id, self.project.id)

    def test_find_by_spoken_name(self):
        self.assertEqual(self.brain.find("homemade project")[0].id, self.project.id)
        self.assertEqual(self.brain.find("home made")[0].id, self.project.id)
        self.assertEqual(self.brain.find("jarvis"), [])

    def test_entries_and_global_preferences(self):
        self.brain.add_entry(self.project.id, "architecture decision", "Use Supabase for auth")
        self.brain.add_entry(None, "coding preference", "Small commits")
        kinds = {e.kind for e in self.brain.entries(self.project.id)}
        self.assertEqual(kinds, {"decision", "preference"})
        self.assertEqual(len(self.brain.entries(self.project.id, include_global=False)), 1)

    def test_resolve_blocker(self):
        entry = self.brain.add_entry(self.project.id, "blocker", "Waiting for Stripe account approval")
        match = self.brain.find_entries(self.project.id, "stripe approval", ["blocker"])
        self.assertEqual([e.id for e in match], [entry.id])
        self.brain.resolve_entry(entry.id)
        self.assertEqual(self.brain.entries(self.project.id, ["blocker"]), [])

    def test_session_end_replaces_next_steps(self):
        self.brain.start_session(self.project.id)
        self.brain.end_session(self.project.id, "Built kitchen form", "Wire the API")
        self.brain.start_session(self.project.id)
        session = self.brain.end_session(self.project.id, "Wired the API", "Add tests")
        self.assertEqual(session.accomplished, "Wired the API")
        steps = self.brain.entries(self.project.id, ["next_step"])
        self.assertEqual([e.text for e in steps], ["Add tests"])
        project = self.brain.get(self.project.id)
        self.assertEqual(project.current_status, "Wired the API")
        self.assertIsNotNone(project.last_worked_on)

    def test_stale_session_closed_quietly(self):
        old = (dt.datetime.now() - dt.timedelta(days=2)).isoformat(timespec="seconds")
        self.conn.execute("INSERT INTO work_sessions (project_id, started_at) VALUES (?, ?)",
                          (self.project.id, old))
        fresh = self.brain.start_session(self.project.id)
        self.assertNotEqual(fresh.started_at, old)
        self.assertIsNone(self.brain.last_session(self.project.id))

    def test_memory_snapshot_fields(self):
        self.brain.add_entry(self.project.id, "goal", "Launch in one neighbourhood")
        self.brain.tasks.add(self.project.id, "Payments", status="blocked")
        memory = self.brain.memory(self.project).as_dict()
        for field in ("project_name", "project_path", "project_description", "goals",
                      "current_status", "completed_tasks", "pending_tasks", "blockers",
                      "decisions", "notes", "last_worked_on", "next_steps"):
            self.assertIn(field, memory)
        self.assertEqual(memory["goals"], ["Launch in one neighbourhood"])
        self.assertEqual(memory["blockers"], ["Payments"])


class IndexerAndInsightsTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.root = make_flutter_project(self.tmp / "homemade")

    def test_index(self):
        index = ProjectIndexer(self.root).index()
        self.assertEqual(index["description"], "Home-cooked food marketplace.")
        for tech in ("Flutter", "Riverpod", "Supabase", "go_router"):
            self.assertIn(tech, index["stack"])
        self.assertEqual(index["current_focus"], ["Kitchen availability"])
        self.assertEqual(index["doc_open_items"], ["Add order placement workflow"])
        self.assertTrue(index["architecture_notes"].startswith("[README.md - Architecture]"))
        self.assertEqual(index["test_files"], 1)
        json.dumps(index)   # must be storable
        self.assertIn("Built with Flutter", summary_text(index))

    def test_secret_files_never_read_or_searched(self):
        insights = CodeInsights(self.root)
        self.assertFalse(insights.read(".env")["ok"])
        self.assertFalse(insights.search("super-secret")["found"])

    def test_path_traversal_blocked(self):
        write(self.tmp / "outside.py", "secret = 1\n")
        insights = CodeInsights(self.root)
        self.assertIsNone(insights.safe_path("../outside.py"))
        self.assertFalse(insights.read("../outside.py")["ok"])

    def test_read_by_spoken_name(self):
        result = CodeInsights(self.root).read("sign in screen")
        self.assertTrue(result["ok"], result["message"])
        self.assertEqual(result["path"], "lib/features/auth/sign_in_screen.dart")
        self.assertIn("class SignInScreen", result["content"])

    def test_generated_twin_does_not_make_read_ambiguous(self):
        write(self.root / "lib/router/app_router.dart", "class AppRouter {}\n")
        write(self.root / "lib/router/app_router.g.dart", "// generated\n")
        result = CodeInsights(self.root).read("app router")
        self.assertTrue(result["ok"], result["message"])
        self.assertEqual(result["path"], "lib/router/app_router.dart")

    def test_todos_only_in_comments_and_not_generated(self):
        todos = CodeInsights(self.root).find_todos()
        self.assertEqual(todos["count"], 1)
        self.assertEqual(todos["todos"][0]["text"], "validate phone numbers")

    def test_module_summary_folder(self):
        result = CodeInsights(self.root).module_summary("auth")
        self.assertEqual(result["path"], "lib/features/auth")
        self.assertEqual(result["file_count"], 2)

    def test_health(self):
        health = CodeInsights(self.root).health()
        self.assertEqual(health["large_files"][0]["path"], "lib/features/auth/auth_repository.dart")
        untested = [u["path"] for u in health["untested"]]
        self.assertIn("lib/features/auth/auth_repository.dart", untested)
        self.assertNotIn("lib/features/auth/sign_in_screen.dart", untested)


class LocatorTest(TempDirTest):
    def setUp(self):
        super().setUp()
        write(self.tmp / "dev/homemade/pubspec.yaml", "name: homemade\n")
        (self.tmp / "dev/homemade/.git").mkdir()
        write(self.tmp / "docs/Jarvis/jarvis/requirements.txt", "x\n")
        write(self.tmp / "docs/notes/readme.txt", "not a project\n")
        write(self.tmp / "dev/kitchen-web/package.json", "{}")
        write(self.tmp / "dev/kitchen-mobile/package.json", "{}")
        self.locator = ProjectLocator([self.tmp / "dev", self.tmp / "docs"])

    def test_exact_and_nested(self):
        self.assertEqual([p.name for p in self.locator.locate("Homemade project")], ["homemade"])
        self.assertEqual([p.name for p in self.locator.locate("jarvis")], ["jarvis"])

    def test_fuzzy(self):
        self.assertEqual([p.name for p in self.locator.locate("home made")], ["homemade"])

    def test_ambiguous_and_missing(self):
        self.assertEqual(len(self.locator.locate("kitchen")), 2)
        self.assertEqual(self.locator.locate("notes"), [])
        self.assertEqual(self.locator.locate("banana"), [])


class RecommendationTest(unittest.TestCase):
    def setUp(self):
        conn = db.connect(":memory:")
        self.brain = ProjectBrain(conn)
        self.project = self.brain.register("demo", tempfile.gettempdir())

    def recommend(self):
        ctx = ProjectContext(
            project=self.brain.get(self.project.id),
            tasks=self.brain.tasks.list(self.project.id),
            blockers=self.brain.entries(self.project.id, ["blocker"]),
            next_steps=self.brain.entries(self.project.id, ["next_step"]),
            goals=self.brain.entries(self.project.id, ["goal"]),
        )
        return RecommendationEngine().recommend(ctx)

    def test_blocked_then_in_progress_then_plan_then_todo(self):
        t = self.brain.tasks
        t.add(self.project.id, "Write docs", priority="low")
        t.add(self.project.id, "Payments", status="blocked")
        t.add(self.project.id, "Kitchen screen", status="in progress")
        self.brain.end_session(self.project.id, "did stuff", "Wire order API")
        titles = [r.title for r in self.recommend()]
        self.assertEqual(titles[:4], ["Unblock: Payments", "Finish: Kitchen screen",
                                      "Wire order API", "Start: Write docs"])

    def test_empty_project_asks_for_goals(self):
        self.assertEqual(self.recommend()[0].source, "setup")


class IntentRouterTest(unittest.TestCase):
    CASES = {
        "Open Homemade project": ("open_project", {"name": "Homemade"}),
        "Hey Jarvis, open the homemade project and summarize status":
            ("open_project", {"name": "homemade", "mode": "summary"}),
        "open homemade project and resume work": ("open_project", {"name": "homemade", "mode": "resume"}),
        "open project homemade": ("open_project", {"name": "homemade"}),
        "Open last project": ("open_project", {"name": "last"}),
        "Open project notes": ("open_notes", {}),
        "Show next tasks": ("list_tasks", {"status": "pending"}),
        "What was I working on?": ("what_was_i_working_on", {}),
        "What are the pending tasks?": ("list_tasks", {"status": "pending"}),
        "Show completed tasks": ("list_tasks", {"status": "done"}),
        "Show blocked tasks": ("list_tasks", {"status": "blocked"}),
        "What changed this week?": ("what_changed", {"days": 7}),
        "What is blocking progress?": ("blockers", {}),
        "What should I do next?": ("next_actions", {}),
        "What should I work on next?": ("next_actions", {}),
        "Summarize project status": ("project_status", {}),
        "Explain project architecture": ("code", {"action": "architecture"}),
        "Daily project summary": ("daily_summary", {}),
        "Add task: kitchen availability screen": ("add_task", {"title": "kitchen availability screen"}),
        "add high priority task order workflow": ("add_task", {"title": "order workflow", "priority": "high"}),
        "Mark kitchen availability screen as complete":
            ("update_task", {"title": "kitchen availability screen", "status": "done"}),
        "find TODO comments": ("code", {"action": "todos"}),
        "search the code for supabase": ("code", {"action": "search", "query": "supabase"}),
        "suggest refactoring opportunities": ("code", {"action": "health"}),
        "we decided to use Supabase for auth": ("record", {"kind": "decision", "text": "use Supabase for auth"}),
        "I'm done for today": ("end_work_session", {"accomplished": None}),
    }

    def test_commands(self):
        for text, (action, args) in self.CASES.items():
            with self.subTest(text=text):
                intent = route(text)
                self.assertEqual(intent.action, action)
                for key, value in args.items():
                    self.assertEqual(intent.args.get(key), value)

    def test_unknown(self):
        self.assertEqual(route("make me a sandwich").action, "unknown")


class FakeEditor:
    def __init__(self):
        self.opened = []

    def open(self, path, line=None):
        self.opened.append(Path(path))
        return True, "ok"


@unittest.skipUnless(HAS_GIT, "git not installed")
class AssistantEndToEndTest(TempDirTest):
    def setUp(self):
        super().setUp()
        self.root = make_flutter_project(self.tmp / "dev" / "homemade")
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "Add sign-in screen and auth repository")
        write(self.root / "lib/features/kitchen/availability.dart", "class Availability {}\n")
        self.editor = FakeEditor()
        self.assistant = ProjectAssistant(self.tmp / "data" / "projects.db",
                                          roots=[self.tmp / "dev"], vscode=self.editor)

    def tearDown(self):
        self.assistant.close()
        super().tearDown()

    def test_open_project_full_flow(self):
        result = self.assistant.open_project("Homemade")
        self.assertTrue(result["ok"], result["message"])
        self.assertEqual(self.editor.opened, [self.root])
        msg = result["message"]
        self.assertTrue(msg.startswith("Homemade project opened in VS Code."))
        for part in ("Last active:", "Branch main: 1 uncommitted", "Recent Git activity:",
                     "authentication", "Suggested next step:"):
            self.assertIn(part, msg)

    def test_follow_ups_use_current_project(self):
        self.assistant.open_project("homemade", open_editor=False)
        self.assistant.add_task("Complete kitchen availability screen", priority="high")
        self.assistant.add_task("Add order placement workflow")
        self.assertIn("Complete kitchen availability screen",
                      self.assistant.list_tasks()["message"])
        done = self.assistant.update_task("kitchen availability", status="done")
        self.assertIn("marked complete", done["message"])
        self.assertIn("Complete kitchen availability screen",
                      self.assistant.list_tasks("completed")["message"])
        titles = [r["title"] for r in self.assistant.next_actions()["recommendations"]]
        # a fresh commit + 1 changed file ranks below a real open task
        self.assertEqual(titles[0], "Start: Add order placement workflow")
        self.assertIn("Commit your uncommitted work (1 files in lib/features/kitchen)", titles)
        # the docs checklist item duplicates the task, so it is not repeated
        self.assertEqual(sum("order placement" in t.lower() for t in titles), 1)

    def test_work_session_then_resume(self):
        self.assistant.open_project("homemade", open_editor=False)
        saved = self.assistant.end_work_session(
            "Implemented kitchen profile form", "Kitchen availability scheduling")
        self.assertTrue(saved["ok"])
        resumed = self.assistant.open_project("last", mode="resume", open_editor=False)
        self.assertIn("you finished: Implemented kitchen profile form", resumed["message"])
        self.assertIn("You planned to do next: Kitchen availability scheduling",
                      resumed["message"])
        self.assertIn("Implemented kitchen profile form",
                      self.assistant.what_changed(7)["message"])
        self.assertIn("Projects worked on:\n* Homemade", self.assistant.daily_summary()["message"])

    def test_decisions_remembered(self):
        self.assistant.open_project("homemade", open_editor=False)
        self.assistant.record("decision", "Use Supabase instead of Firebase for authentication")
        recalled = self.assistant.recall(query="firebase authentication")
        self.assertIn("Use Supabase instead of Firebase", recalled["message"])

    def test_architecture_and_notes(self):
        self.assistant.open_project("homemade", open_editor=False)
        arch = self.assistant.code("architecture")
        self.assertIn("Feature-first folders", arch["message"])
        notes = self.assistant.open_notes()
        self.assertTrue(Path(notes["path"]).is_file())

    def test_unknown_and_no_current_project(self):
        self.assertIn("Which project?", self.assistant.project_status()["message"])
        self.assertTrue(self.assistant.open_project("banana")["not_found"])

    def test_router_dispatch(self):
        result = dispatch(route("open homemade project"), self.assistant)
        self.assertTrue(result["ok"])
        ask = dispatch(route("I'm done for today"), self.assistant)
        self.assertEqual(ask["needs_input"], ["accomplished", "next_steps"])


class ModelsTest(unittest.TestCase):
    def test_ago(self):
        ref = dt.datetime(2026, 1, 10, 12, 0, 0)
        self.assertEqual(ago("2026-01-08T12:00:00", ref), "2 days ago")
        self.assertEqual(ago("2026-01-09T11:00:00", ref), "yesterday")
        self.assertEqual(ago(None, ref), "never")

    def test_parsers(self):
        self.assertIs(TaskStatus.parse("in progress"), TaskStatus.IN_PROGRESS)
        self.assertIs(Priority.parse("urgent"), Priority.HIGH)
        self.assertEqual(normalize_kind("next steps"), "next_step")
        self.assertIsNone(normalize_kind("banana"))


if __name__ == "__main__":
    unittest.main()
