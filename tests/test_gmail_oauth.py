"""Tests hors-ligne du flux OAuth Gmail (aucun appel réseau).

On vérifie surtout que l'état PKCE survit à un redémarrage du serveur entre
`/auth/login` et `/auth/callback` (cause d'un « Session de connexion introuvable »).
"""
import json
import unittest
from urllib.parse import parse_qs, urlparse

from app import gmail_client as gm
from app.config import settings


def _write_fake_client() -> None:
    settings.client_secret_file.parent.mkdir(parents=True, exist_ok=True)
    settings.client_secret_file.write_text(json.dumps({
        "installed": {
            "client_id": "fake.apps.googleusercontent.com",
            "client_secret": "fakesecret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }))


class OAuthPendingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _write_fake_client()

    def setUp(self):
        gm._pending_flows.clear()
        gm._pending_loaded = False
        gm._pending_file().unlink(missing_ok=True)

    def _login(self) -> str:
        url = gm.start_login("http://localhost:8000/auth/callback")
        return parse_qs(urlparse(url).query)["state"][0]

    def test_state_survives_a_restart(self):
        state = self._login()
        # Simule un redémarrage : tout ce qui est en mémoire est perdu.
        gm._pending_flows.clear()
        gm._pending_loaded = False
        info = gm._pending_state().get(state)
        self.assertIsNotNone(info)
        self.assertEqual(info["redirect_uri"], "http://localhost:8000/auth/callback")
        self.assertTrue(info["code_verifier"])

    def test_state_written_to_disk(self):
        self._login()
        self.assertTrue(gm._pending_file().exists())

    def test_unknown_state_is_rejected(self):
        with self.assertRaises(gm.NotConnectedError):
            gm.finish_login("state-inconnu", "code")


if __name__ == "__main__":
    unittest.main()
