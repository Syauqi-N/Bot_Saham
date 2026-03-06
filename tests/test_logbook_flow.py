import unittest
from unittest.mock import patch

import bot_saham


def webhook_payload(text: str, chat_id: str) -> dict:
    return {
        "event": "message",
        "payload": {
            "body": text,
            "chatId": chat_id,
            "fromMe": False,
        },
    }


class LogbookFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chat_id = "628111111111@c.us"
        self.other_chat_id = "628222222222@c.us"
        self.sent_messages = []

        bot_saham.logbook_sessions.clear()
        bot_saham.post_drafts.clear()
        bot_saham.rate_limit.clear()

        self.send_text_patch = patch.object(
            bot_saham,
            "send_text",
            side_effect=lambda chat_id, text: self.sent_messages.append((chat_id, text)),
        )
        self.submit_patch = patch.object(
            bot_saham,
            "submit_logbook_entry",
            return_value=(True, "Logbook berhasil disubmit."),
        )
        self.config_patch = patch.multiple(
            bot_saham,
            LOGBOOK_ENABLED=True,
            LOGBOOK_ALLOWED_CHAT_IDS={self.chat_id},
            LOGBOOK_DEFAULT_START_TIME="08:00",
            LOGBOOK_DEFAULT_END_TIME="17:00",
            LOGBOOK_DEFAULT_RELATED=True,
            LOGBOOK_DEFAULT_COURSE_KEYWORD="RI042106",
            LOGBOOK_DEFAULT_CHECKBOX=True,
            LOGBOOK_MATERIAL_MAX_CHARS=4000,
        )

        self.mock_send_text = self.send_text_patch.start()
        self.mock_submit = self.submit_patch.start()
        self.config_patch.start()

    def tearDown(self) -> None:
        self.send_text_patch.stop()
        self.submit_patch.stop()
        self.config_patch.stop()
        bot_saham.logbook_sessions.clear()
        bot_saham.post_drafts.clear()
        bot_saham.rate_limit.clear()

    def test_state_machine_update_and_cancel(self) -> None:
        status = bot_saham.handle_logbook_command(self.chat_id, "logbook")
        self.assertEqual(status, "ok")
        session = bot_saham.get_logbook_session(self.chat_id)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.get("status"), "awaiting_material")

        handled = bot_saham.handle_logbook_mode_input(self.chat_id, "Implementasi endpoint backend.")
        self.assertTrue(handled)
        session = bot_saham.get_logbook_session(self.chat_id)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.get("status"), "awaiting_confirmation")

        status = bot_saham.handle_logbook_command(self.chat_id, "logbook_update")
        self.assertEqual(status, "ok")
        session = bot_saham.get_logbook_session(self.chat_id)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.get("status"), "awaiting_material")

        status = bot_saham.handle_logbook_command(self.chat_id, "logbook_cancel")
        self.assertEqual(status, "ok")
        self.assertIsNone(bot_saham.get_logbook_session(self.chat_id))

    def test_ok_without_material_rejected(self) -> None:
        bot_saham.handle_logbook_command(self.chat_id, "logbook")
        status = bot_saham.handle_logbook_command(self.chat_id, "logbook_ok")
        self.assertEqual(status, "ok")
        joined = "\n".join(message for _, message in self.sent_messages).lower()
        self.assertIn("belum ada kegiatan", joined)

    def test_access_control_rejects_unauthorized_chat(self) -> None:
        status = bot_saham.handle_logbook_command(self.other_chat_id, "logbook")
        self.assertEqual(status, "ok")
        self.assertIsNone(bot_saham.get_logbook_session(self.other_chat_id))
        joined = "\n".join(message for _, message in self.sent_messages).lower()
        self.assertIn("tidak diizinkan", joined)

    def test_access_control_accepts_s_whatsapp_net_variant(self) -> None:
        variant_chat_id = "628111111111@s.whatsapp.net"
        status = bot_saham.handle_logbook_command(variant_chat_id, "logbook")
        self.assertEqual(status, "ok")
        self.assertIsNotNone(bot_saham.get_logbook_session(variant_chat_id))

    def test_mode_conflict_with_post_mode(self) -> None:
        bot_saham.save_post_draft(
            self.chat_id,
            {
                "caption": "",
                "image_url": None,
                "image_data": None,
                "image_mimetype": None,
            },
        )
        status = bot_saham.handle_logbook_command(self.chat_id, "logbook")
        self.assertEqual(status, "post_mode_waiting")

    def test_ok_submits_and_enters_awaiting_file(self) -> None:
        bot_saham.handle_logbook_command(self.chat_id, "logbook")
        bot_saham.handle_logbook_mode_input(self.chat_id, "Menyusun unit test dan dokumentasi.")
        status = bot_saham.handle_logbook_command(self.chat_id, "logbook_ok")
        self.assertEqual(status, "ok")
        self.assertEqual(self.mock_submit.call_count, 1)
        # Session stays alive in awaiting_file state for optional file upload
        session = bot_saham.get_logbook_session(self.chat_id)
        self.assertIsNotNone(session)
        self.assertEqual(session.get("status"), "awaiting_file")

    def test_skip_after_ok_clears_session(self) -> None:
        bot_saham.handle_logbook_command(self.chat_id, "logbook")
        bot_saham.handle_logbook_mode_input(self.chat_id, "Menyusun unit test dan dokumentasi.")
        bot_saham.handle_logbook_command(self.chat_id, "logbook_ok")
        # !skip should clear the session
        status = bot_saham.handle_logbook_command(self.chat_id, "logbook_skip")
        self.assertEqual(status, "ok")
        self.assertIsNone(bot_saham.get_logbook_session(self.chat_id))



    def test_logbook_mode_blocks_other_command_in_webhook(self) -> None:
        bot_saham.handle_logbook_command(self.chat_id, "logbook")
        client = bot_saham.app.test_client()
        response = client.post("/webhook", json=webhook_payload("!help", self.chat_id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json().get("status"), "logbook_mode_waiting")
        joined = "\n".join(message for _, message in self.sent_messages).lower()
        self.assertIn("mode !logbook", joined)


if __name__ == "__main__":
    unittest.main()
