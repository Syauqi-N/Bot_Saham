import unittest
from unittest.mock import patch

import ai_router


class BackendSaviorFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        ai_router._backend_savior_history.clear()

    def tearDown(self) -> None:
        ai_router._backend_savior_history.clear()

    def test_timeout_uses_groq_fallback(self) -> None:
        with patch.object(ai_router, "BACKEND_SAVIOR_FALLBACK_TO_GROQ", True), patch.object(
            ai_router,
            "BACKEND_SAVIOR_FALLBACK_MAX_TOKENS",
            321,
        ), patch.object(
            ai_router,
            "backend_savior_chat",
            return_value=(None, "Gagal memanggil Backend Savior: timeout (connect=10s, read=45s)"),
        ), patch.object(
            ai_router,
            "groq_chat",
            return_value=("Gunakan queue + retry idempotent.", None),
        ) as mock_groq:
            reply, error = ai_router.get_backend_savior_reply("chat-1", "cara handle retry?")

        self.assertIsNone(error)
        self.assertEqual(reply, "Gunakan queue + retry idempotent.")
        self.assertEqual(mock_groq.call_count, 1)
        self.assertEqual(mock_groq.call_args.kwargs.get("max_tokens"), 321)

        history = ai_router._backend_savior_history.get("chat-1", [])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].get("role"), "user")
        self.assertEqual(history[1].get("role"), "assistant")

    def test_non_retryable_error_skips_fallback(self) -> None:
        with patch.object(ai_router, "BACKEND_SAVIOR_FALLBACK_TO_GROQ", True), patch.object(
            ai_router,
            "backend_savior_chat",
            return_value=(None, "Backend Savior error 400: invalid request"),
        ), patch.object(ai_router, "groq_chat") as mock_groq:
            reply, error = ai_router.get_backend_savior_reply("chat-2", "jelasin")

        self.assertIsNone(reply)
        self.assertEqual(error, "Backend Savior error 400: invalid request")
        self.assertEqual(mock_groq.call_count, 0)

    def test_timeout_without_fallback_returns_original_error(self) -> None:
        with patch.object(ai_router, "BACKEND_SAVIOR_FALLBACK_TO_GROQ", False), patch.object(
            ai_router,
            "backend_savior_chat",
            return_value=(None, "Gagal memanggil Backend Savior: timeout (connect=10s, read=45s)"),
        ), patch.object(ai_router, "groq_chat") as mock_groq:
            reply, error = ai_router.get_backend_savior_reply("chat-3", "jelasin")

        self.assertIsNone(reply)
        self.assertIn("timeout", error or "")
        self.assertEqual(mock_groq.call_count, 0)


if __name__ == "__main__":
    unittest.main()
