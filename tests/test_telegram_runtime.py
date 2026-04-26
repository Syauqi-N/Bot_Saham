import base64
import unittest
from unittest.mock import Mock, patch

import bot_saham


def telegram_update(
    *,
    chat_id: int = 123456789,
    chat_type: str = "private",
    text: str | None = None,
    caption: str | None = None,
    is_bot: bool = False,
    photo: list[dict] | None = None,
    document: dict | None = None,
    message_id: int = 100,
    update_id: int = 1,
) -> dict:
    message: dict = {
        "message_id": message_id,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": 99, "is_bot": is_bot},
    }
    if text is not None:
        message["text"] = text
    if caption is not None:
        message["caption"] = caption
    if photo is not None:
        message["photo"] = photo
    if document is not None:
        message["document"] = document
    return {"update_id": update_id, "message": message}


class TelegramRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        bot_saham.rate_limit.clear()

    def tearDown(self) -> None:
        bot_saham.rate_limit.clear()

    def test_extract_telegram_text_message(self) -> None:
        text, chat_id, from_me, media, chat_type = bot_saham.extract_telegram_message(
            telegram_update(text="!help")
        )
        self.assertEqual(text, "!help")
        self.assertEqual(chat_id, "123456789")
        self.assertFalse(from_me)
        self.assertIsNone(media)
        self.assertEqual(chat_type, "private")

    def test_extract_telegram_photo_downloads_largest_variant(self) -> None:
        update = telegram_update(
            caption="Caption gambar",
            photo=[
                {"file_id": "small-photo", "width": 50, "height": 50},
                {"file_id": "large-photo", "width": 200, "height": 200},
            ],
        )
        response = Mock(status_code=200, content=b"image-bytes")
        with patch.object(
            bot_saham,
            "telegram_api_request",
            return_value=({"file_path": "photos/file_1.jpg"}, None),
        ) as mock_api, patch.object(bot_saham.http_session, "get", return_value=response) as mock_get:
            text, _, _, media, _ = bot_saham.extract_telegram_message(update)

        self.assertEqual(text, "Caption gambar")
        self.assertEqual(mock_api.call_args.args[0], "getFile")
        self.assertEqual(mock_api.call_args.args[1]["file_id"], "large-photo")
        self.assertEqual(mock_get.call_count, 1)
        self.assertIsNotNone(media)
        assert media is not None
        self.assertEqual(media.get("filename"), "photo_100.jpg")
        self.assertEqual(media.get("mimetype"), "image/jpeg")
        self.assertEqual(base64.b64decode(media.get("data") or ""), b"image-bytes")

    def test_extract_telegram_document_pdf(self) -> None:
        update = telegram_update(
            document={
                "file_id": "doc-1",
                "file_name": "laporan.pdf",
                "mime_type": "application/pdf",
            }
        )
        response = Mock(status_code=200, content=b"%PDF-1.7")
        with patch.object(
            bot_saham,
            "telegram_api_request",
            return_value=({"file_path": "documents/laporan.pdf"}, None),
        ), patch.object(bot_saham.http_session, "get", return_value=response):
            _, _, _, media, _ = bot_saham.extract_telegram_message(update)

        self.assertIsNotNone(media)
        assert media is not None
        self.assertEqual(media.get("filename"), "laporan.pdf")
        self.assertEqual(media.get("mimetype"), "application/pdf")
        self.assertEqual(base64.b64decode(media.get("data") or ""), b"%PDF-1.7")

    def test_process_incoming_message_ignores_group_and_bot(self) -> None:
        with patch.object(bot_saham, "send_text") as mock_send:
            group_status = bot_saham.process_telegram_update(telegram_update(text="!help", chat_type="group"))
            bot_status = bot_saham.process_telegram_update(telegram_update(text="!help", is_bot=True, update_id=2))

        self.assertEqual(group_status, "ignored")
        self.assertEqual(bot_status, "ignored")
        self.assertEqual(mock_send.call_count, 0)

    def test_send_text_calls_send_message_with_footer(self) -> None:
        with patch.object(bot_saham, "telegram_api_request", return_value=({"message_id": 1}, None)) as mock_api:
            bot_saham.send_text("123456789", "Halo")

        self.assertEqual(mock_api.call_args.args[0], "sendMessage")
        payload = mock_api.call_args.args[1]
        self.assertEqual(payload["chat_id"], "123456789")
        self.assertIn("Halo", payload["text"])
        self.assertIn("© Haris Stockbit", payload["text"])

    def test_poll_updates_once_advances_offset_even_on_processing_error(self) -> None:
        updates = [
            telegram_update(text="!help", update_id=10),
            telegram_update(text="$BBCA", update_id=11),
        ]
        with patch.object(bot_saham, "telegram_api_request", return_value=(updates, None)) as mock_api, patch.object(
            bot_saham,
            "process_telegram_update",
            side_effect=["ok", RuntimeError("boom")],
        ):
            next_offset = bot_saham.poll_updates_once(5)

        self.assertEqual(next_offset, 12)
        payload = mock_api.call_args.args[1]
        self.assertEqual(payload["offset"], 5)
        self.assertEqual(payload["allowed_updates"], ["message"])

    def test_prepare_telegram_runtime_calls_delete_webhook(self) -> None:
        with patch.object(bot_saham, "TELEGRAM_BOT_TOKEN", "123:token"), patch.object(
            bot_saham,
            "telegram_api_request",
            side_effect=[(True, None), ({"username": "harisbot"}, None)],
        ) as mock_api:
            bot_saham.prepare_telegram_runtime()

        self.assertEqual(mock_api.call_args_list[0].args[0], "deleteWebhook")
        self.assertEqual(
            mock_api.call_args_list[0].args[1],
            {"drop_pending_updates": bot_saham.TELEGRAM_DROP_PENDING_UPDATES},
        )
        self.assertEqual(mock_api.call_args_list[1].args[0], "getMe")

    def test_private_commands_are_ignored_in_public_bot(self) -> None:
        with patch.object(bot_saham, "send_text") as mock_send:
            post_status = bot_saham.process_incoming_message("!post", "123456789", False, None, "private")
            logbook_status = bot_saham.process_incoming_message("!logbook", "123456789", False, None, "private")
            porto_status = bot_saham.process_incoming_message("!porto", "123456789", False, None, "private")
            explain_status = bot_saham.process_incoming_message(
                "!explain bagaimana retry queue",
                "123456789",
                False,
                None,
                "private",
            )

        self.assertEqual(post_status, "ignored")
        self.assertEqual(logbook_status, "ignored")
        self.assertEqual(porto_status, "ignored")
        self.assertEqual(explain_status, "ignored")
        self.assertEqual(mock_send.call_count, 0)

    def test_help_text_does_not_show_private_workflows(self) -> None:
        text = bot_saham.help_text()

        self.assertIn("!ai", text)
        self.assertIn("!news", text)
        self.assertNotIn("!post", text)
        self.assertNotIn("!logbook", text)
        self.assertNotIn("!porto", text)
        self.assertNotIn("!explain", text)


if __name__ == "__main__":
    unittest.main()
