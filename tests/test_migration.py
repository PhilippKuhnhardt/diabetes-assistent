import json
import tempfile
import unittest
from pathlib import Path

from app.store import config, db


class PriorInsulinPercentileTests(unittest.TestCase):
    """The migration that came with counting earlier insulin in the meal factor (2026-11-14)."""

    def setUp(self):
        self.old_db = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp.name) / "app.sqlite3"
        db.init()

    def tearDown(self):
        db.DB_PATH = self.old_db
        self.temp.cleanup()

    def migrate(self, percentile):
        config.save_settings({**config.default_settings(), "auto_percentile": percentile, "auto_half_life_days": "14"})
        with db.connect() as connection:
            db._prior_insulin_percentile(connection)
            return [json.loads(row["payload"]) for row in
                    connection.execute("SELECT payload FROM settings_versions ORDER BY id")]

    def test_percentile_40_becomes_35_as_a_new_version(self):
        versions = self.migrate("40")
        self.assertEqual([item["auto_percentile"] for item in versions], ["40", "35"])
        self.assertEqual(versions[1]["auto_half_life_days"], "14")
        self.assertEqual(config.get_settings()[1]["auto_percentile"], "35")

    def test_other_percentiles_stay(self):
        self.assertEqual([item["auto_percentile"] for item in self.migrate("37.5")], ["37.5"])

    def test_fresh_database_gets_no_settings_version(self):
        with db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM settings_versions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
