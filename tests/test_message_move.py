"""Réaffectation manuelle d'un mail à une autre entreprise (correction du classement).

Aucun appel réseau : on écrit directement les lignes SQLite, puis on appelle l'API.
"""
import itertools
import unittest

from fastapi.testclient import TestClient

from app import db
from app.main import app

CSRF = {"X-Requested-With": "stagetracker"}
_next_tag = itertools.count(1)


def seed_message(thread_id: str, company_id: int, message_id: str, direction: str, sent_at: str) -> None:
    with db.get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO thread(id, company_id, subject, created_at) VALUES (?, ?, ?, ?)",
                     (thread_id, company_id, "Candidature de stage", db.now_iso()))
        conn.execute(
            "INSERT INTO message(id, thread_id, company_id, direction, sender, recipients, subject, body, "
            "sent_at, has_attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
            (message_id, thread_id, company_id, direction, "contact@acme.fr", "moi@exemple.fr",
             "Candidature de stage", "Bonjour", sent_at),
        )


class MoveMessageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def setUp(self):
        db.init_db()
        # Identifiants uniques par test : thread/message ont une clé primaire globale.
        self.tag = next(_next_tag)
        self.src = self.client.post("/api/companies", json={"name": f"SourceCo {self.tag}"}, headers=CSRF).json()["id"]
        self.dst = self.client.post("/api/companies", json={"name": f"CibleCo {self.tag}"}, headers=CSRF).json()["id"]

    def tid(self, name: str) -> str:
        return f"{name}-{self.tag}"

    def test_csrf_required(self):
        self.assertEqual(self.client.post("/api/messages/move",
                                          json={"message_ids": ["m1"], "company_id": self.dst}).status_code, 403)

    def test_unknown_company_and_message(self):
        seed_message(self.tid("t"), self.src, self.tid("m"), "in", "2026-01-01T10:00:00+00:00")
        self.assertEqual(self.client.post("/api/messages/move",
                                          json={"message_ids": [self.tid("m")], "company_id": 999999},
                                          headers=CSRF).status_code, 404)
        self.assertEqual(self.client.post("/api/messages/move",
                                          json={"message_ids": ["inconnu"], "company_id": self.dst},
                                          headers=CSRF).status_code, 404)
        self.assertEqual(self.client.post("/api/messages/move",
                                          json={"message_ids": [], "company_id": self.dst},
                                          headers=CSRF).status_code, 422)

    def test_moving_the_only_message_moves_the_thread_and_trashes_the_source(self):
        seed_message(self.tid("t"), self.src, self.tid("m"), "in", "2026-01-01T10:00:00+00:00")

        res = self.client.post("/api/messages/move",
                               json={"message_ids": [self.tid("m")], "company_id": self.dst}, headers=CSRF)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.json()["moved"], 1)

        with db.get_db() as conn:
            moved = conn.execute("SELECT company_id FROM message WHERE id = ?", (self.tid("m"),)).fetchone()
            thread = conn.execute("SELECT company_id FROM thread WHERE id = ?", (self.tid("t"),)).fetchone()
            src = conn.execute("SELECT status FROM company WHERE id = ?", (self.src,)).fetchone()
        self.assertEqual(moved["company_id"], self.dst)
        self.assertEqual(thread["company_id"], self.dst)   # le fil suit le transfert
        self.assertEqual(src["status"], "ignored")          # entreprise vidée -> corbeille

        thread_view = self.client.get(f"/api/companies/{self.dst}/thread").json()
        self.assertEqual([m["id"] for m in thread_view["messages"]], [self.tid("m")])

    def test_partial_move_keeps_the_source_company(self):
        seed_message(self.tid("t"), self.src, self.tid("a1"), "in", "2026-01-01T10:00:00+00:00")
        seed_message(self.tid("t"), self.src, self.tid("a2"), "out", "2026-01-02T10:00:00+00:00")

        self.client.post("/api/messages/move",
                         json={"message_ids": [self.tid("a1")], "company_id": self.dst}, headers=CSRF)

        with db.get_db() as conn:
            thread = conn.execute("SELECT company_id FROM thread WHERE id = ?", (self.tid("t"),)).fetchone()
            left = conn.execute("SELECT COUNT(*) AS n FROM message WHERE company_id = ?", (self.src,)).fetchone()
            src = conn.execute("SELECT status FROM company WHERE id = ?", (self.src,)).fetchone()
        self.assertEqual(thread["company_id"], self.src)    # le fil reste à la source
        self.assertEqual(left["n"], 1)
        self.assertNotEqual(src["status"], "ignored")

        listing = self.client.get("/api/companies").json()
        self.assertTrue(any(c["id"] == self.src for c in listing["active"] + listing["prospects"]))

    def test_moving_into_trashed_company_restores_it(self):
        seed_message(self.tid("t"), self.src, self.tid("m"), "in", "2026-01-01T10:00:00+00:00")
        self.client.post(f"/api/companies/{self.dst}/ignore", headers=CSRF)

        self.client.post("/api/messages/move",
                         json={"message_ids": [self.tid("m")], "company_id": self.dst}, headers=CSRF)

        thread_view = self.client.get(f"/api/companies/{self.dst}/thread").json()
        self.assertEqual([m["id"] for m in thread_view["messages"]], [self.tid("m")])
        self.assertNotEqual(thread_view["company"]["stored_status"], "ignored")


if __name__ == "__main__":
    unittest.main()
