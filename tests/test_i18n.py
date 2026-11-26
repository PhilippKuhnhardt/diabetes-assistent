"""Server texts in German and English (app/i18n.py, APP_LANGUAGE)."""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from app import i18n
from app.i18n_en import EN

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def source_keys() -> dict[str, str]:
    """Every German text the code passes to tr(), with where it is."""
    keys = {}
    for path in sorted((ROOT / "app").rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "tr" and node.args \
                    and isinstance(node.args[0], ast.Constant):
                keys.setdefault(node.args[0].value, f"{path.relative_to(ROOT)}:{node.lineno}")
    return keys


class CatalogueTest(unittest.TestCase):
    def test_every_text_has_an_english_version(self):
        missing = [f"{where}: {key}" for key, where in source_keys().items() if key not in EN]
        self.assertEqual(missing, [])

    def test_placeholders_match(self):
        wrong = [key for key, value in EN.items()
                 if set(PLACEHOLDER.findall(key)) != set(PLACEHOLDER.findall(value))]
        self.assertEqual(wrong, [])

    def test_no_unused_entries(self):
        # The sport names are translated from a dict (app/episodes.py SPORT_NAMES), not a literal.
        from app import episodes
        used = set(source_keys()) | set(episodes.SPORT_LABELS.values())
        self.assertEqual(sorted(set(EN) - used), [])


class TranslateTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, i18n, "LANGUAGE", i18n.LANGUAGE)

    def test_german_is_the_source(self):
        i18n.LANGUAGE = "de"
        self.assertEqual(i18n.tr("Höchstens {count} Fotos", count=4), "Höchstens 4 Fotos")
        self.assertEqual(i18n.pick("Hallo", "Hello"), "Hallo")

    def test_english(self):
        i18n.LANGUAGE = "en"
        self.assertEqual(i18n.tr("Höchstens {count} Fotos", count=4), "At most 4 photos")
        self.assertEqual(i18n.tr("Ein Text, den es nicht gibt"), "Ein Text, den es nicht gibt")
        self.assertEqual(i18n.pick("Hallo", "Hello"), "Hello")

    def test_setting(self):
        for value, expected in (("en", "en"), (" EN ", "en"), ("en-GB", "en"), ("de", "de"), ("fr", "de"), ("", "de")):
            with self.subTest(value=value):
                os.environ["APP_LANGUAGE"] = value
                try:
                    self.assertEqual(i18n._configured(), expected)
                finally:
                    del os.environ["APP_LANGUAGE"]


class EnglishProcessTest(unittest.TestCase):
    """The whole app imported with APP_LANGUAGE=en: module-level labels and prompts in English."""

    def test_app_in_english(self):
        script = (
            "import pkgutil, importlib, app\n"
            "for module in pkgutil.walk_packages(app.__path__, 'app.'):\n"
            "    importlib.import_module(module.name)\n"
            "from app import meal_agent, mcp\n"
            "from app.services import notifications, agent, agent_tools\n"
            "from app import episodes, dishes\n"
            "assert meal_agent.SYSTEM.startswith('You are the meal agent'), meal_agent.SYSTEM[:40]\n"
            "assert mcp.INSTRUCTIONS.startswith('Diabetes diary'), mcp.INSTRUCTIONS[:40]\n"
            "assert agent.TRANSCRIBE_INSTRUCTION.startswith('Transcribe'), agent.TRANSCRIBE_INSTRUCTION\n"
            "assert notifications.LABELS['low'] == 'Low ahead', notifications.LABELS\n"
            "assert episodes.SPORT_NAMES['jogging'] == 'Jogging'\n"
            "assert 'Joggen' in episodes.NOTE_LABELS and 'Jogging' in episodes.NOTE_LABELS\n"
            "assert dishes.hint(3, 40) == 'logged 3×, usually 40 g', dishes.hint(3, 40)\n"
            "tool = next(t for t in agent_tools.TOOLS if t['name'] == 'save_basal')\n"
            "assert tool['title'] == 'Save basal', tool['title']\n"
            "assert tool['inputSchema']['properties']['units']['description'] == 'Units in steps of 0.5.'\n"
            "assert meal_agent.lookups([{'name': 'web_search', 'args': {'query': 'pho'}}]) == ['Web search: “pho”']\n"
            "print('ok')\n")
        env = {**os.environ, "APP_LANGUAGE": "en"}
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
