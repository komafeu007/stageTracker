"""Traduction des erreurs techniques en messages compréhensibles.

L'interface (bandeau de synchro, toast) et les réponses JSON ne doivent jamais montrer
de trace technique : on garde une phrase en français qui dit quoi faire.
"""
import sqlite3
import unittest

import httplib2
import requests
from fastapi.testclient import TestClient
from googleapiclient.errors import HttpError

from app import errors, gemini_client, sync
from app.main import app


def _dns_failure():
    """Reproduit l'erreur réelle d'un conteneur dont le DNS ne répond plus."""
    return requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='oauth2.googleapis.com', port=443): Max retries exceeded with "
        "url: /token (Caused by NameResolutionError(\"Failed to resolve 'oauth2.googleapis.com' "
        "([Errno -3] Temporary failure in name resolution)\"))"
    )


def _http_error(status, message):
    return HttpError(httplib2.Response({"status": status}), message.encode(), uri="/gmail/v1")


class ExplainTest(unittest.TestCase):
    def test_dns_failure_is_explained_simply(self):
        message = errors.explain(_dns_failure())
        self.assertIn("Internet", message)
        self.assertNotIn("oauth2.googleapis.com", message)
        self.assertNotIn("name resolution", message.lower())
        self.assertNotIn("Errno", message)

    def test_google_quota_is_explained_simply(self):
        message = errors.explain(_http_error(429, "rateLimitExceeded"))
        self.assertIn("Trop de requêtes", message)
        self.assertNotIn("rateLimitExceeded", message)

    def test_google_auth_error_asks_to_reconnect(self):
        self.assertIn("reconnecte Gmail", errors.explain(_http_error(401, "Invalid Credentials")))

    def test_locked_database_is_explained(self):
        self.assertIn("occupée", errors.explain(sqlite3.OperationalError("database is locked")))

    def test_unknown_error_hides_technical_details(self):
        message = errors.explain(RuntimeError("KeyError: 'secret_interne'"))
        self.assertNotIn("secret_interne", message)
        self.assertIn("logs", message)

    def test_already_readable_messages_are_kept(self):
        exc = gemini_client.AIQuotaError("Quota Gemini atteint sur tous les modèles essayés.")
        self.assertEqual(errors.explain(exc), str(exc))


class SyncErrorMessageTest(unittest.TestCase):
    def setUp(self):
        self._get_service = sync.gm.get_service
        sync.state["last_error"] = None

    def tearDown(self):
        sync.gm.get_service = self._get_service
        sync.state["last_error"] = None

    def test_failed_sync_shows_a_readable_message(self):
        def _unreachable():
            raise _dns_failure()

        sync.gm.get_service = _unreachable
        self.assertTrue(sync.run_sync())
        self.assertIn("Internet", sync.state["last_error"])
        self.assertNotIn("oauth2.googleapis.com", sync.state["last_error"])


class UnexpectedApiErrorTest(unittest.TestCase):
    """Le filet `errors.install` renvoie un JSON français, jamais une trace."""

    @classmethod
    def setUpClass(cls):
        app.add_api_route("/__boom", cls._boom)
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)

    @staticmethod
    def _boom():
        raise RuntimeError("détail technique interne")

    def test_unexpected_error_returns_a_readable_json(self):
        response = self.client.get("/__boom")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("détail technique interne", response.text)
        self.assertIn("logs", response.json()["detail"])

    def test_invalid_payload_returns_a_french_message(self):
        response = self.client.post(
            "/api/companies", json={}, headers={"X-Requested-With": "stagetracker"}
        )
        self.assertEqual(response.status_code, 422)
        self.assertIn("Données invalides", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
