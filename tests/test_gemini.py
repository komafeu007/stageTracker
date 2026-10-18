"""Tests du repli automatique entre modèles Gemini (aucun appel réseau)."""
import dataclasses
import unittest
from unittest import mock

from app import gemini_client as gc


class FakeResponse:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _ok(text):
    return FakeResponse(200, {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"promptTokenCount": 1, "totalTokenCount": 2},
    })


def _err(status, message):
    return FakeResponse(status, {"error": {"message": message}}, message)


class CleanBodyTest(unittest.TestCase):
    def test_removes_a_copied_subject_line(self):
        raw = "Objet : Relance - Candidature Stage en robotique\n\nMadame, Monsieur,\n\nJe me permets…"
        self.assertEqual(gc._clean_body(raw), "Madame, Monsieur,\n\nJe me permets…")

    def test_removes_a_whole_header_block(self):
        raw = "De : Simon\nÀ : rh@exemple.fr\nObjet : Candidature\n\nMadame, Monsieur,"
        self.assertEqual(gc._clean_body(raw), "Madame, Monsieur,")

    def test_leaves_a_normal_body_untouched(self):
        raw = "Madame, Monsieur,\n\nJe me permets de revenir vers vous.\n\nCordialement,"
        self.assertEqual(gc._clean_body(raw), raw)

    def test_leaves_text_containing_the_word_objet(self):
        raw = "Bonjour,\n\nL'objet de ma démarche est simple."
        self.assertEqual(gc._clean_body(raw), raw)

    def test_empty(self):
        self.assertEqual(gc._clean_body("  \n "), "")

    def test_followup_prompt_forbids_and_strips_the_subject_line(self):
        reply = '{"body": "Objet : Relance - Candidature\\n\\nMadame, Monsieur,"}'
        with mock.patch.object(gc, "generate", return_value=reply) as gen, \
             mock.patch.object(gc, "load_prompt_file", return_value=""):
            text = gc.draft_followup({"name": "A"}, {"subject": "s", "body": "b"}, 1, "J+7", 7)
        self.assertEqual(text, "Madame, Monsieur,")
        self.assertIn("objet", gen.call_args.kwargs["system"].lower())

    def test_application_body_is_stripped_of_a_copied_subject(self):
        reply = '{"subject": "Candidature", "body": "Objet : Candidature\\n\\nMadame, Monsieur,"}'
        with mock.patch.object(gc, "generate", return_value=reply), \
             mock.patch.object(gc, "load_prompt_file", return_value=""):
            _, body = gc.draft_application({"name": "A", "sector": None, "website": None}, "")
        self.assertEqual(body, "Madame, Monsieur,")


class CompanyContextTest(unittest.TestCase):
    """Le contexte du site ne doit jamais viser une adresse locale ou interne (SSRF)."""

    def test_rejects_local_and_private_hosts(self):
        for url in ("localhost", "http://localhost:8000", "127.0.0.1", "192.168.1.10",
                    "10.0.0.5/site", "169.254.169.254", "[::1]", "http://serveur.local",
                    "file:///etc/passwd", "ftp://exemple.fr"):
            with self.subTest(url=url):
                self.assertEqual(gc.fetch_company_context(url), "")

    def test_accepts_a_public_host(self):
        page = FakeResponse(200, text="<html><title>ACME</title><h1>Robotique</h1></html>")
        page.headers = {"content-type": "text/html; charset=utf-8"}
        with mock.patch.object(gc.httpx, "get", return_value=page) as get:
            context = gc.fetch_company_context("acme.fr")
        self.assertIn("ACME", context)
        self.assertEqual(get.call_args.args[0], "https://acme.fr")


class FollowupPromptTest(unittest.TestCase):
    def test_dates_from_the_last_sent_mail(self):
        with mock.patch.object(gc, "generate", return_value='{"body": "x"}') as gen, \
             mock.patch.object(gc, "load_prompt_file", return_value=""):
            gc.draft_followup({"name": "A"}, {"subject": "s", "body": "b"}, 1, "J+7", 7)
        prompt = gen.call_args.args[0]
        self.assertIn("7 jours après le dernier e-mail", prompt)
        self.assertNotIn("après la candidature", prompt)


class GeminiFallbackTest(unittest.TestCase):
    def _patched_settings(self, primary, fallbacks):
        return dataclasses.replace(
            gc.settings, gemini_api_key="test-key", gemini_model=primary,
            gemini_fallback_models=list(fallbacks),
        )

    def _run(self, post, *, primary="primary", fallbacks=("secondary",)):
        with mock.patch.object(gc, "settings", self._patched_settings(primary, fallbacks)), \
             mock.patch.object(gc, "_log_usage"), \
             mock.patch.object(gc, "_throttle"), \
             mock.patch.object(gc, "_ROUNDS", 1), \
             mock.patch.object(gc, "httpx") as http:
            http.post.side_effect = post
            return gc.generate("p", system="s", purpose="relance")

    def test_falls_back_when_primary_saturated(self):
        calls = []

        def post(url, **kwargs):
            calls.append(url)
            return _err(503, "high demand") if "primary" in url else _ok("bonjour")

        self.assertEqual(self._run(post), "bonjour")
        self.assertEqual(len(calls), 2)
        self.assertIn("secondary", calls[1])

    def test_falls_back_when_primary_model_missing(self):
        calls = []

        def post(url, **kwargs):
            calls.append(url)
            return _err(404, "not found") if "primary" in url else _ok("ok")

        self.assertEqual(self._run(post), "ok")
        self.assertEqual(len(calls), 2)

    def test_raises_when_all_models_fail(self):
        def post(url, **kwargs):
            return _err(503, "down")

        with self.assertRaises(gc.AIError):
            self._run(post, primary="p", fallbacks=("a", "b"))

    def test_quota_on_all_models_is_quota_error(self):
        def post(url, **kwargs):
            return _err(429, "quota")

        with self.assertRaises(gc.AIQuotaError):
            self._run(post, primary="p", fallbacks=("a",))

    def test_fatal_status_is_not_retried(self):
        calls = []

        def post(url, **kwargs):
            calls.append(url)
            return _err(403, "bad key")

        with self.assertRaises(gc.AIError):
            self._run(post)
        self.assertEqual(len(calls), 1, "une erreur définitive ne doit pas basculer de modèle")

    def test_truncated_response_falls_back_to_another_model(self):
        truncated = FakeResponse(200, {
            "candidates": [{"finishReason": "MAX_TOKENS",
                            "content": {"parts": [{"text": '{"text": "coupé'}]}}],
            "usageMetadata": {},
        })

        def post(url, **kwargs):
            return truncated if "primary" in url else _ok("complet")

        self.assertEqual(self._run(post), "complet")

    def test_truncation_on_all_models_raises_a_clear_error(self):
        truncated = FakeResponse(200, {
            "candidates": [{"finishReason": "MAX_TOKENS",
                            "content": {"parts": [{"text": '{"text": "coupé'}]}}],
            "usageMetadata": {},
        })

        with self.assertRaises(gc.AIError) as ctx:
            self._run(lambda url, **kw: truncated, primary="p", fallbacks=("a",))
        self.assertIn("coupée", str(ctx.exception))

    def test_retries_without_thinking_when_the_model_rejects_it(self):
        bodies = []

        def post(url, **kwargs):
            bodies.append(kwargs["json"]["generationConfig"])
            if "thinkingConfig" in kwargs["json"]["generationConfig"]:
                return _err(400, 'Invalid JSON payload: Unknown name "thinkingConfig"')
            return _ok("ok")

        self.assertEqual(self._run(post, primary="gemini-flash-latest", fallbacks=()), "ok")
        self.assertEqual(len(bodies), 2)
        self.assertIn("thinkingConfig", bodies[0])
        self.assertNotIn("thinkingConfig", bodies[1])


class GeminiBodyTest(unittest.TestCase):
    def test_thinking_disabled_for_25_flash(self):
        body = gc._build_body("gemini-2.5-flash", "sys", "prompt", 0.4, 100, json_mode=True)
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingBudget": 0})
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")

    def test_thinking_disabled_for_latest_aliases_and_25_family(self):
        # Les alias `*-latest` pointent aujourd'hui vers des modèles 2.5 qui « réfléchissent » :
        # sans ça le raisonnement mange le budget de sortie et tronque le JSON.
        for model in ("gemini-flash-latest", "gemini-flash-lite-latest", "gemini-2.5-flash-lite"):
            generation = gc._build_body(model, "s", "p", 0.4, 100, json_mode=False)["generationConfig"]
            self.assertEqual(generation["thinkingConfig"], {"thinkingBudget": 0}, model)

    def test_thinking_left_alone_for_older_models(self):
        body = gc._build_body("gemini-2.0-flash", "sys", "prompt", 0.4, 100, json_mode=False)
        self.assertNotIn("thinkingConfig", body["generationConfig"])

    def test_draft_reply_parses_body(self):
        with mock.patch.object(gc, "generate", return_value='{"body": "Bonjour Madame"}'), \
             mock.patch.object(gc, "load_prompt_file", return_value=""):
            text = gc.draft_reply({"name": "Acme"}, {"subject": "s", "body": "b"})
        self.assertEqual(text, "Bonjour Madame")

    def test_draft_reply_rejects_empty_body(self):
        with mock.patch.object(gc, "generate", return_value='{"body": ""}'), \
             mock.patch.object(gc, "load_prompt_file", return_value=""):
            with self.assertRaises(gc.AIError):
                gc.draft_reply({"name": "Acme"}, {"subject": "s", "body": "b"})

    def test_correct_spelling_parses_body(self):
        with mock.patch.object(gc, "generate", return_value='{"body": "Bonjour Madame,"}') as gen:
            self.assertEqual(gc.correct_spelling("Bonjour Madam,"), "Bonjour Madame,")
        self.assertIn("orthographe", gen.call_args.kwargs["system"].lower())

    def test_correct_spelling_rejects_empty_body(self):
        with mock.patch.object(gc, "generate", return_value='{"body": ""}'):
            with self.assertRaises(gc.AIError):
                gc.correct_spelling("Bonjour")

    def test_application_system_reuses_examples_paragraph(self):
        system = gc._application_system()
        self.assertIn("2e paragraphe", system)
        self.assertIn("exemples validés", system.lower())

    def test_application_system_adapts_project_to_company_domain(self):
        system = gc._application_system()
        self.assertIn("domaine de l'entreprise", system)
        self.assertIn("hors sujet", system)

    def test_application_system_uses_sector_when_present(self):
        self.assertIn("Secteur", gc._application_system())

    def test_draft_application_passes_sector_to_the_model(self):
        with mock.patch.object(gc, "generate", return_value='{"subject": "s", "body": "b"}') as gen, \
             mock.patch.object(gc, "load_prompt_file", return_value=""):
            gc.draft_application({"name": "Acme", "sector": "Vision / Perception", "website": None}, "")
        self.assertIn("Vision / Perception", gen.call_args.args[0])

    def test_application_system_forbids_superlatives_and_complex_words(self):
        system = gc._application_system().lower()
        self.assertIn("superlatif", system)
        self.assertIn("compliqu", system)

    def test_followup_and_reply_forbid_superlatives(self):
        with mock.patch.object(gc, "generate", return_value='{"body": "b"}') as gen, \
             mock.patch.object(gc, "load_prompt_file", return_value=""):
            gc.draft_followup({"name": "A"}, {"subject": "s", "body": "b"}, 1, "J+7", 7)
            self.assertIn("superlatif", gen.call_args.kwargs["system"].lower())
            gc.draft_reply({"name": "A"}, {"subject": "s", "body": "b"})
            self.assertIn("superlatif", gen.call_args.kwargs["system"].lower())

    def test_draft_paragraph_parses_text_and_reuses_examples(self):
        with mock.patch.object(gc, "generate", return_value='{"text": "Nouveau paragraphe."}') as gen, \
             mock.patch.object(gc, "load_prompt_file", return_value=""):
            text = gc.draft_paragraph({"name": "Acme"}, "", ["intro", "parcours"], 1)
        self.assertEqual(text, "Nouveau paragraphe.")
        self.assertIn("exemples validés", gen.call_args.args[0].lower())

    def test_draft_paragraph_rejects_empty_text(self):
        with mock.patch.object(gc, "generate", return_value='{"text": ""}'), \
             mock.patch.object(gc, "load_prompt_file", return_value=""):
            with self.assertRaises(gc.AIError):
                gc.draft_paragraph({"name": "Acme"}, "", ["a"], 0)


if __name__ == "__main__":
    unittest.main()
