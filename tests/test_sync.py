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


if __name__ == "__main__":
    unittest.main()
