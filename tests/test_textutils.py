"""Tests de l'heuristique de secteur (repli sans IA) et de sa cohérence avec constants.SECTORS."""
import unittest

from app import textutils as tu
from app.constants import SECTORS

KNOWN = set(SECTORS)


class GuessSectorTest(unittest.TestCase):
    def test_keywords_map_to_known_labels(self):
        self.assertEqual(tu.guess_sector("Nous fabriquons des bras robotiques et des cobots"), "Robotique")
        self.assertEqual(tu.guess_sector("Notre équipe fait de la vision par ordinateur et du LiDAR"),
                         "Vision / Perception")
        self.assertEqual(tu.guess_sector("équipe cybersécurité : pentest et audit"), "Cybersécurité")
        self.assertEqual(tu.guess_sector("déploiement Kubernetes et Docker en CI/CD"), "Cloud / DevOps")

    def test_no_match_returns_none(self):
        self.assertIsNone(tu.guess_sector("Bonjour, comment allez-vous ?"))
        self.assertIsNone(tu.guess_sector(""))

    def test_robotics_wins_over_generic_words(self):
        # « python » (dev) et « capteur » (embarqué) ne doivent pas masquer le domaine robotique.
        self.assertEqual(tu.guess_sector("Stage robotique en python avec des capteurs LiDAR"), "Robotique")

    def test_keyword_labels_are_declared_in_constants(self):
        for sector, _words in tu._SECTOR_KEYWORDS:
            self.assertIn(sector, KNOWN)


if __name__ == "__main__":
    unittest.main()
