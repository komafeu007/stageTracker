"""Tests de la classification locale (Ollama simulé) : issue et secteur."""
import unittest
from unittest import mock

from app import local_ai
from app.constants import SECTORS


def _messages():
    return [{"direction": "in", "sent_at": "2026-01-10T10:00:00+00:00", "body": "Bonjour"}]


class LocalAISectorTest(unittest.TestCase):
    def _classify(self, payload):
        with mock.patch.object(local_ai, "_generate", return_value=payload):
            return local_ai.classify_thread("Objet", _messages())

    def test_sector_is_normalised_to_a_known_label(self):
        data = self._classify('{"relevant": true, "company_name": "X", "outcome": "none", "sector": "robotique"}')
        self.assertEqual(data["sector"], "Robotique")

    def test_unknown_sector_is_kept_as_free_text(self):
        data = self._classify('{"relevant": true, "sector": "Photonique quantique"}')
        self.assertEqual(data["sector"], "Photonique quantique")

    def test_missing_sector_is_empty(self):
        self.assertEqual(self._classify('{"relevant": true}')["sector"], "")

    def test_prompt_lists_the_sectors(self):
        self.assertIn(SECTORS[0], local_ai.SYSTEM_PROMPT)

    def test_empty_thread_has_a_sector_key(self):
        self.assertEqual(local_ai.classify_thread("Objet", [])["sector"], "")


if __name__ == "__main__":
    unittest.main()
