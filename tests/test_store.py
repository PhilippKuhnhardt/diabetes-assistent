import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.store import config, db


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.old_path = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp.name) / "app.sqlite3"

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.temp.cleanup()

    def version(self):
        with db.connect() as connection:
            return connection.execute("PRAGMA user_version").fetchone()[0]

    def test_new_database_runs_every_migration_once(self):
        db.init()
        db.init()
        self.assertEqual(self.version(), len(db.MIGRATIONS))
        self.assertEqual(config.get_settings()[0], 0)

    def test_database_from_before_migrations_keeps_its_data_and_gets_new_columns(self):
        connection = sqlite3.connect(db.DB_PATH)
        connection.executescript("""
            CREATE TABLE settings_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                effective_at TEXT NOT NULL, payload TEXT NOT NULL);
            INSERT INTO settings_versions(created_at,effective_at,payload)
                VALUES('2026-09-01T00:00:00+00:00','1970-01-01T00:00:00+00:00','{"window_minutes":170}');
            CREATE TABLE nightscout_profiles (start_at TEXT PRIMARY KEY, profile_id TEXT NOT NULL,
                profile_name TEXT NOT NULL, timezone TEXT NOT NULL, isf_schedule TEXT NOT NULL, synced_at TEXT NOT NULL);
            CREATE TABLE analysis_sport_marks (meal_id TEXT PRIMARY KEY, status TEXT NOT NULL);
        """)
        connection.close()
        db.init()
        self.assertEqual(self.version(), len(db.MIGRATIONS))
        version, settings = config.get_settings()
        self.assertEqual((version, settings["window_minutes"]), (1, 170))
        with db.connect() as connection:
            profile_columns = {row["name"] for row in connection.execute("PRAGMA table_info(nightscout_profiles)")}
            mark_columns = {row["name"] for row in connection.execute("PRAGMA table_info(analysis_sport_marks)")}
        self.assertTrue({"ic_schedule", "target_schedule"} <= profile_columns)
        self.assertIn("effect_mgdl", mark_columns)

    def test_places_drop_stored_positions_and_auswaerts_becomes_the_fallback(self):
        connection = sqlite3.connect(db.DB_PATH)
        connection.row_factory = sqlite3.Row
        for migration in db.MIGRATIONS[:-1]:
            migration(connection)
        connection.execute(f"PRAGMA user_version={len(db.MIGRATIONS) - 1}")
        connection.executescript("""
            INSERT INTO places(id,name,created_at) VALUES(1,'Kantine','x'),(2,'auswärts','x');
            INSERT INTO entry_locations(event_id,at,lat,lon,accuracy_m,place_id,updated_at)
                VALUES('a','x',49.0,8.4,20,1,'x'),('b','x',49.1,8.4,20,NULL,'x');
        """)
        connection.commit()
        connection.close()
        db.init()
        with db.connect() as connection:
            places = [tuple(row) for row in connection.execute("SELECT name,fallback FROM places ORDER BY id")]
            locations = [tuple(row) for row in connection.execute("SELECT event_id,lat,lon,accuracy_m,place_id FROM entry_locations")]
        self.assertEqual(places, [("Kantine", 0), ("auswärts", 1)])
        self.assertEqual(locations, [("a", None, None, None, 1)])


if __name__ == "__main__":
    unittest.main()
