import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "h5" / "生产后台.html").read_text(encoding="utf-8")


class ProductionConsoleTests(unittest.TestCase):
    def test_task_manager_has_only_restart_action(self):
        self.assertIn('<button class="btn danger" id="restart">重启后台</button>', HTML)
        self.assertNotIn('id="push"', HTML)
        self.assertNotIn('id="ahead"', HTML)
        self.assertNotIn('id="projectCount"', HTML)
        self.assertNotIn('id="autoPush"', HTML)
        self.assertNotIn("自动推送远端", HTML)

    def test_current_and_all_task_scopes_are_explicit(self):
        self.assertIn('data-manager-tab="current">本次</button>', HTML)
        self.assertIn('data-manager-tab="tasks">全部任务</button>', HTML)
        self.assertIn("function currentTaskItems()", HTML)
        self.assertIn("!t.read_only", HTML)
        self.assertIn("renderTasks('currentTasks',currentTaskItems())", HTML)
        self.assertIn("renderTasks('tasks',taskCache)", HTML)

    def test_archive_records_cannot_push_or_delete_from_console(self):
        self.assertNotIn("/api/git/sync", HTML)
        self.assertNotIn("delete_git=1", HTML)
        self.assertIn("!t.read_only&&!active", HTML)


if __name__ == "__main__":
    unittest.main()
