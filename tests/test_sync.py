"""Tests du pipeline de synchronisation (Gmail + Ollama simulés)."""
import unittest

from app import sync
from app.db import get_db, init_db
from app.services import get_company_dict

ME = "etudiant@example.com"


def _msg(mid, direction, subject, body, sender, sent="2026-01-10T10:00:00+00:00"):
    return {
        "id": mid, "thread_id": "t1", "direction": direction, "sender": sender,
        "from_addr": sender if direction == "in" else ME,
        "recipients": ME if direction == "in" else sender,
        "participants": [(sender, sender)],
        "subject": subject, "body": body, "sent_at": sent,
        "rfc_id": f"<{mid}@x>", "labels": set(), "has_attachments": False,
    }


def _install_thread(messages):
    sync.gm.get_thread = lambda service, tid, **kw: {"messages": messages}
    sync.gm.parse_message = lambda m, me: m


class SyncPipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self._enabled = sync.local_ai.enabled
        self._classify = sync.local_ai.classify_thread
        sync.local_ai.enabled = lambda: True

    def tearDown(self):
        sync.local_ai.enabled = self._enabled
        sync.local_ai.classify_thread = self._classify

    def test_01_new_relevant_thread_uses_local_ai(self):
        sync.local_ai.classify_thread = lambda subject, msgs: {
            "relevant": True, "company_name": "Acme Corp", "outcome": "refusal", "summary": "Refus poli.",
        }
        _install_thread([_msg("s1", "in", "Candidature pour un stage",
                              "Votre candidature est refusée.", "rh@acme.example")])
        sync.process_thread(object(), "tsync1", ME, notify=True)

        with get_db() as db:
            company = db.execute("SELECT * FROM company WHERE name = 'Acme Corp'").fetchone()
            replies = db.execute(
                "SELECT COUNT(*) c FROM notification n JOIN company c2 ON c2.id = n.company_id "
                "WHERE n.type = 'reply' AND c2.name = 'Acme Corp'").fetchone()["c"]
        self.assertIsNotNone(company)
        self.assertEqual(company["status"], "refused")
        self.assertEqual(company["ai_label"], "refusal")
        self.assertEqual(company["summary"], "Refus poli.")
        self.assertEqual(replies, 1)

    def test_02_irrelevant_thread_is_skipped(self):
        sync.local_ai.classify_thread = lambda subject, msgs: {
            "relevant": False, "company_name": "", "outcome": "none", "summary": "",
        }
        _install_thread([_msg("s2", "in", "Newsletter du jour",
                              "Promo -50% sur les chaussettes.", "news@shop.fr")])
        sync.process_thread(object(), "tsync2", ME, notify=True)

        with get_db() as db:
            skipped = db.execute("SELECT 1 FROM skipped_thread WHERE thread_id = 'tsync2'").fetchone()
            company = db.execute("SELECT 1 FROM company WHERE email = 'news@shop.fr'").fetchone()
        self.assertIsNotNone(skipped)
        self.assertIsNone(company)

    def test_03_known_thread_reclassifies_new_inbound(self):
        with get_db() as db:
            company = db.execute("SELECT * FROM company WHERE name = 'Acme Corp'").fetchone()
        self.assertIsNotNone(company, "le test 01 doit avoir créé l'entreprise")
        cid = company["id"]

        sync.local_ai.classify_thread = lambda subject, msgs: {
            "relevant": True, "company_name": "Acme Corp", "outcome": "positive", "summary": "Entretien proposé.",
        }
        _install_thread([
            _msg("s1", "in", "Candidature pour un stage", "Votre candidature est refusée.", "rh@acme.example"),
            _msg("s3", "in", "Re", "Finalement un entretien est possible.", "rh@acme.example",
                 "2026-01-12T10:00:00+00:00"),
        ])
        sync.process_thread(object(), "tsync1", ME, notify=True)

        self.assertEqual(get_company_dict(cid)["stored_status"], "replied")
        with get_db() as db:
            count = db.execute("SELECT COUNT(*) c FROM message WHERE thread_id = 'tsync1'").fetchone()["c"]
        self.assertEqual(count, 2)

    def test_04_new_thread_fills_sector_from_mail_content(self):
        sync.local_ai.classify_thread = lambda subject, msgs: {
            "relevant": True, "company_name": "RoboVision", "outcome": "none",
            "sector": "Vision / Perception", "summary": "",
        }
        _install_thread([_msg("s4", "in", "Stage en vision",
                              "Nous concevons des systèmes de vision par ordinateur.",
                              "contact@robovision.example")])
        sync.process_thread(object(), "tsync4", ME, notify=False)

        with get_db() as db:
            row = db.execute("SELECT sector FROM company WHERE name = 'RoboVision'").fetchone()
        self.assertEqual(row["sector"], "Vision / Perception")

    def test_05_sector_backfilled_by_keywords_without_local_ai(self):
        sync.local_ai.enabled = lambda: False
        _install_thread([_msg("s5", "in", "Offre de stage en cybersécurité",
                              "Nous renforçons notre équipe cybersécurité (pentest, audit).",
                              "rh@secure.example")])
        sync.process_thread(object(), "tsync5", ME, notify=False)

        with get_db() as db:
            row = db.execute("SELECT sector FROM company WHERE email = 'rh@secure.example'").fetchone()
        self.assertEqual(row["sector"], "Cybersécurité")

    def test_06_existing_company_without_sector_is_completed(self):
        sync.local_ai.enabled = lambda: True
        sync.local_ai.classify_thread = lambda subject, msgs: {
            "relevant": True, "company_name": "Factory", "outcome": "none", "sector": "", "summary": "",
        }
        _install_thread([_msg("s6", "in", "Réponse à votre candidature",
                              "Nous automatisons des chaînes de production avec des robots.",
                              "rh@factory.example")])
        sync.process_thread(object(), "tsync6", ME, notify=False)
        with get_db() as db:
            row = db.execute("SELECT id FROM company WHERE email = 'rh@factory.example'").fetchone()
            db.execute("UPDATE company SET sector = NULL WHERE id = ?", (row["id"],))

        # Deuxième passage (fil déjà connu, aucun nouveau message) : le secteur est complété.
        sync.process_thread(object(), "tsync6", ME, notify=False)
        with get_db() as db:
            sector = db.execute("SELECT sector FROM company WHERE id = ?", (row["id"],)).fetchone()["sector"]
        self.assertEqual(sector, "Robotique")

    def test_07_backfill_sectors_uses_stored_messages(self):
        sync.local_ai.enabled = lambda: False
        with get_db() as db:
            cid = db.execute(
                "INSERT INTO company(name, status, created_at) VALUES (?, ?, ?)",
                ("BackfillCo", "pending", "2026-01-01T00:00:00+00:00"),
            ).lastrowid
            db.execute("INSERT INTO thread(id, company_id, created_at) VALUES (?, ?, ?)",
                       ("tback", cid, "2026-01-02T00:00:00+00:00"))
            db.execute(
                "INSERT INTO message(id, thread_id, company_id, direction, sent_at, subject, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("b1", "tback", cid, "in", "2026-01-02T00:00:00+00:00", "Stage robotique",
                 "Nous développons un bras robotique."),
            )

        self.assertGreaterEqual(sync.backfill_sectors(), 1)
        with get_db() as db:
            sector = db.execute("SELECT sector FROM company WHERE id = ?", (cid,)).fetchone()["sector"]
        self.assertEqual(sector, "Robotique")


if __name__ == "__main__":
    unittest.main()
