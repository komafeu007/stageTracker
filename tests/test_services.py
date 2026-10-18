"""Tests des relances : échéance comptée à partir du dernier envoi, recalcul et notifications."""
import dataclasses
import unittest
from datetime import timedelta
from unittest import mock

from app import services
from app.db import get_db, init_db, iso, now_iso
from app.services import (
    check_followups,
    company_to_dict,
    compute_due,
    recompute_followups,
)

# Comme la config locale : trois relances possibles, et on ne relance pas une candidature
# vieille de plus de 600 jours.
CONFIG = dict(followup_days=[7, 14, 28], followup_max_age_days=600)
COUNTER = [0]


def _ago(days: float) -> str:
    return iso(services.utcnow() - timedelta(days=days))


def _company(messages: list[tuple[str, str]], name: str | None = None) -> int:
    """Insère une entreprise « pending » avec ses messages (direction, date ISO)."""
    COUNTER[0] += 1
    name = name or f"Relance {COUNTER[0]}"
    thread = f"trelance{COUNTER[0]}"
    with get_db() as db:
        cid = db.execute(
            "INSERT INTO company(name, email, status, created_at) VALUES (?, ?, 'pending', ?)",
            (name, f"contact{COUNTER[0]}@exemple.fr", now_iso()),
        ).lastrowid
        db.execute("INSERT INTO thread(id, company_id, created_at) VALUES (?, ?, ?)",
                   (thread, cid, now_iso()))
        for i, (direction, sent_at) in enumerate(messages):
            db.execute(
                "INSERT INTO message(id, thread_id, company_id, direction, sent_at, subject, body) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"{thread}-m{i}", thread, cid, direction, sent_at, "Candidature", "Bonjour"),
            )
        services.recompute_company(db, cid)
    return cid


def _row(cid: int):
    with get_db() as db:
        return db.execute("SELECT * FROM company WHERE id = ?", (cid,)).fetchone()


class ComputeDueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        patcher = mock.patch.object(services, "settings", dataclasses.replace(services.settings, **CONFIG))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_due_counts_from_the_first_mail_when_no_relance_sent(self):
        cid = _company([("out", _ago(8))])
        due = compute_due(_row(cid))
        self.assertIsNotNone(due)
        self.assertEqual(_row(cid)["followup_count"], 0)
        self.assertEqual(company_to_dict(_row(cid))["status"], "followup")  # J+7 dépassé

    def test_due_counts_from_the_last_mail_not_the_first(self):
        # Candidature en août, relance envoyée il y a 2 jours : la suivante ne doit pas être due.
        cid = _company([("out", _ago(60)), ("out", _ago(30)), ("out", _ago(2))])
        row = _row(cid)
        self.assertEqual(row["followup_count"], 2)
        due = compute_due(row)
        self.assertIsNotNone(due)
        # Dernier envoi (il y a 2 jours) + J+28 : encore ~26 jours de marge.
        self.assertGreater((due - services.utcnow()).days, 20)
        self.assertEqual(company_to_dict(row)["status"], "pending")

    def test_a_reply_stops_the_followups(self):
        cid = _company([("out", _ago(40)), ("in", _ago(30))])
        self.assertIsNone(compute_due(_row(cid)))
        self.assertEqual(company_to_dict(_row(cid))["status"], "replied")

    def test_no_due_after_the_last_configured_relance(self):
        cid = _company([("out", _ago(10)), ("out", _ago(9)), ("out", _ago(8)), ("out", _ago(1))])
        self.assertEqual(_row(cid)["followup_count"], 3)
        self.assertIsNone(compute_due(_row(cid)))

    def test_stale_application_is_not_relanced(self):
        cid = _company([("out", _ago(700))])
        self.assertIsNone(compute_due(_row(cid)))


class RecomputeFollowupsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        patcher = mock.patch.object(services, "settings", dataclasses.replace(services.settings, **CONFIG))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_recompute_refreshes_anchor_and_count_of_every_company(self):
        cid = _company([("out", _ago(40)), ("out", _ago(3))])
        with get_db() as db:  # on fige des valeurs périmées, comme avant un rattrapage
            db.execute("UPDATE company SET anchor_at = ?, followup_count = 0 WHERE id = ?",
                       (_ago(40), cid))

        recompute_followups()

        row = _row(cid)
        self.assertEqual(row["followup_count"], 1)
        self.assertGreater((services.utcnow() - services.parse_iso(row["anchor_at"])).days, 2)

    def test_check_followups_notifies_then_purges_when_the_relance_is_sent(self):
        cid = _company([("out", _ago(10))], name="RelanceNotif")
        check_followups()
        with get_db() as db:
            kinds = db.execute(
                "SELECT dedupe_key FROM notification WHERE company_id = ? AND type = 'followup'", (cid,)
            ).fetchall()
        self.assertEqual(len(kinds), 1)

        # La relance part (nouveau mail envoyé) : elle n'est plus due, la notification doit disparaître.
        with get_db() as db:
            thread = db.execute("SELECT thread_id FROM message WHERE company_id = ? LIMIT 1",
                                (cid,)).fetchone()["thread_id"]
            db.execute(
                "INSERT INTO message(id, thread_id, company_id, direction, sent_at, subject, body) "
                "VALUES (?, ?, ?, 'out', ?, ?, ?)",
                (f"{thread}-relance", thread, cid, now_iso(), "Re: Candidature", "Bonjour"),
            )
        check_followups()
        with get_db() as db:
            remaining = db.execute(
                "SELECT COUNT(*) AS n FROM notification WHERE company_id = ? AND type = 'followup'", (cid,)
            ).fetchone()["n"]
        self.assertEqual(remaining, 0)
        self.assertEqual(company_to_dict(_row(cid))["status"], "pending")


if __name__ == "__main__":
    unittest.main()
