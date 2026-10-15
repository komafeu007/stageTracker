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


class GeminiBodyTest(unittest.TestCase):
    def test_thinking_disabled_for_25_flash(self):
        body = gc._build_body("gemini-2.5-flash", "sys", "prompt", 0.4, 100, json_mode=True)
        self.assertEqual(body["generationConfig"]["thinkingConfig"], {"thinkingBudget": 0})
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")

    def test_thinking_left_alone_for_other_models(self):
        body = gc._build_body("gemini-flash-latest", "sys", "prompt", 0.4, 100, json_mode=True)
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


if __name__ == "__main__":
    unittest.main()
