import base64
import unittest
from unittest.mock import patch

import bot_saham


def image_media(data: bytes, filename: str = "chart.jpg", mimetype: str = "image/jpeg") -> dict:
    return {
        "data": base64.b64encode(data).decode("ascii"),
        "filename": filename,
        "mimetype": mimetype,
        "messageId": "message-1",
    }


class PostFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.allowed_chat_id = "123456789"
        self.other_chat_id = "987654321"
        self.sent_messages = []

        bot_saham.post_drafts.clear()
        bot_saham.logbook_sessions.clear()
        bot_saham.rate_limit.clear()

        self.send_text_patch = patch.object(
            bot_saham,
            "send_text",
            side_effect=lambda chat_id, text: self.sent_messages.append((chat_id, text)),
        )
        self.publish_patch = patch.object(
            bot_saham,
            "create_linkedin_image_post",
            return_value=("urn:li:share:123", None),
        )
        self.config_patch = patch.multiple(
            bot_saham,
            LINKEDIN_ALLOWED_CHAT_IDS={self.allowed_chat_id},
            LINKEDIN_MAX_IMAGES=3,
            RATE_LIMIT_SECONDS=0,
        )

        self.send_text_patch.start()
        self.mock_publish = self.publish_patch.start()
        self.config_patch.start()

    def tearDown(self) -> None:
        self.send_text_patch.stop()
        self.publish_patch.stop()
        self.config_patch.stop()
        bot_saham.post_drafts.clear()
        bot_saham.logbook_sessions.clear()
        bot_saham.rate_limit.clear()

    def test_post_access_control_rejects_unauthorized_chat(self) -> None:
        status = bot_saham.handle_post_command(self.other_chat_id, "post")
        self.assertEqual(status, "ok")
        self.assertIsNone(bot_saham.get_post_draft(self.other_chat_id))
        joined = "\n".join(message for _, message in self.sent_messages).lower()
        self.assertIn("tidak diizinkan", joined)

    def test_post_whitelist_empty_keeps_feature_available(self) -> None:
        with patch.object(bot_saham, "LINKEDIN_ALLOWED_CHAT_IDS", set()):
            status = bot_saham.handle_post_command(self.other_chat_id, "post")
        self.assertEqual(status, "ok")
        self.assertIsNotNone(bot_saham.get_post_draft(self.other_chat_id))

    def test_end_to_end_post_flow_uses_base64_media(self) -> None:
        start_status = bot_saham.process_incoming_message("!post", self.allowed_chat_id, False, None, "private")
        self.assertEqual(start_status, "ok")

        update_status = bot_saham.process_incoming_message(
            "Caption test",
            self.allowed_chat_id,
            False,
            image_media(b"fake-image"),
            "private",
        )
        self.assertEqual(update_status, "ok")

        review_status = bot_saham.process_incoming_message("!review", self.allowed_chat_id, False, None, "private")
        self.assertEqual(review_status, "ok")

        publish_status = bot_saham.process_incoming_message("!postok", self.allowed_chat_id, False, None, "private")
        self.assertEqual(publish_status, "ok")

        self.assertEqual(self.mock_publish.call_count, 1)
        kwargs = self.mock_publish.call_args.kwargs
        self.assertEqual(kwargs.get("caption"), "Caption test")
        media_items = kwargs.get("media_items")
        self.assertEqual(len(media_items), 1)
        self.assertTrue(media_items[0].get("data"))
        self.assertIsNone(bot_saham.get_post_draft(self.allowed_chat_id))


if __name__ == "__main__":
    unittest.main()
