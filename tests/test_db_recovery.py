"""Robustesse SQLite : le schéma est recréé si la base a été vidée en cours d'exécution.

C'est l'incident qui faisait croire que Gmail était déconnecté : toutes les requêtes
renvoyaient « no such table » et le statut partait en 500.
"""
import sqlite3
import unittest

from app import db
from app.config import settings
from app.main import ensure_schema


class SchemaRecoveryTest(unittest.TestCase):
    def setUp(self):
        db.init_db()

    def test_added_columns_are_applied_idempotently(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE company (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        db._apply_added_columns(conn)
        db._apply_added_columns(conn)  # une deuxième passe ne doit rien casser
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(company)")}
        conn.close()
        self.assertIn("sector", cols)

    def test_schema_is_recreated_after_truncation(self):
        db.kv_set("cle", "valeur")
        # Simule un fichier de base tronqué pendant que l'app tourne.
        settings.db_path.write_bytes(b"")

        ensure_schema()

        with db.get_db() as conn:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
        self.assertIn("kv", tables)
        self.assertIn("company", tables)
        # Base recréée donc vide : la donnée est perdue, mais l'app refonctionne.
        self.assertIsNone(db.kv_get("cle"))

    def test_ensure_schema_is_a_noop_on_healthy_db(self):
        db.kv_set("cle", "valeur")
        ensure_schema()
        self.assertEqual(db.kv_get("cle"), "valeur")


if __name__ == "__main__":
    unittest.main()
