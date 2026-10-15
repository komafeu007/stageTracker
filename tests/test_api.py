"""Tests des routes HTTP (aucun appel réseau : Gmail et IA simulés)."""
import unittest

from fastapi.testclient import TestClient

from app.main import app

CSRF = {"X-Requested-With": "stagetracker"}


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    def test_health(self):
        self.assertEqual(self.client.get("/api/health").json(), {"ok": True})

    def test_status_shape(self):
        status = self.client.get("/api/status").json()
        for key in ("gmail_connected", "ai_enabled", "sync", "tokens", "cv"):
            self.assertIn(key, status)
        for key in ("running", "mode", "done", "total", "last_run", "last_error"):
            self.assertIn(key, status["sync"])
        for key in ("month_tokens", "budget", "percent", "model"):
            self.assertIn(key, status["tokens"])

    def test_csrf_required_on_writes(self):
        self.assertEqual(self.client.post("/api/companies", json={"name": "X"}).status_code, 403)

    def test_company_lifecycle(self):
        created = self.client.post("/api/companies", json={"name": "Acme"}, headers=CSRF)
        self.assertEqual(created.status_code, 200)
        cid = created.json()["id"]

        self.assertEqual(
            self.client.post("/api/companies", json={"name": "acme"}, headers=CSRF).status_code, 409)
        self.assertEqual(
            self.client.post("/api/companies", json={"name": "B", "email": "nope"}, headers=CSRF).status_code, 400)

        listing = self.client.get("/api/companies").json()
        self.assertTrue(any(c["id"] == cid for c in listing["prospects"]))

        patched = self.client.patch(f"/api/companies/{cid}", json={"status": "replied"}, headers=CSRF)
        self.assertEqual(patched.json()["status"], "replied")
        self.assertEqual(
            self.client.patch(f"/api/companies/{cid}", json={"status": "zzz"}, headers=CSRF).status_code, 400)

        thread = self.client.get(f"/api/companies/{cid}/thread").json()
        self.assertEqual(thread["company"]["id"], cid)
        self.assertEqual(thread["messages"], [])
        self.assertIsNone(thread["draft"])

        self.assertTrue(self.client.post(f"/api/companies/{cid}/ignore", headers=CSRF).json()["ok"])
        self.assertEqual(self.client.post(f"/api/companies/{cid}/restore", headers=CSRF).status_code, 200)

    def test_company_not_found(self):
        self.assertEqual(self.client.get("/api/companies/999999/thread").status_code, 404)
        self.assertEqual(self.client.patch("/api/companies/999999", json={"name": "z"}, headers=CSRF).status_code, 404)

    def test_notifications_and_followup(self):
        self.assertIn("unread", self.client.get("/api/notifications").json())
        self.assertIn("item", self.client.get("/api/next-followup").json())

    def test_cv_lifecycle(self):
        self.assertFalse(self.client.get("/api/cv").json()["exists"])
        bad = self.client.post("/api/cv", files={"file": ("cv.txt", b"hello", "text/plain")}, headers=CSRF)
        self.assertEqual(bad.status_code, 400)
        pdf = b"%PDF-1.4\n%fake\n%%EOF"
        ok = self.client.post("/api/cv", files={"file": ("cv.pdf", pdf, "application/pdf")}, headers=CSRF)
        self.assertTrue(ok.json()["exists"])
        self.assertEqual(self.client.get("/api/cv/download").status_code, 200)

    def test_draft_unknown_kind(self):
        cid = self.client.post("/api/companies", json={"name": "DraftCo"}, headers=CSRF).json()["id"]
        self.assertEqual(
            self.client.post(f"/api/companies/{cid}/draft", json={"kind": "zzz"}, headers=CSRF).status_code, 400)


if __name__ == "__main__":
    unittest.main()
