import unittest
from unittest.mock import patch

import bot_saham


class PortfolioFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chat_id = "8273793694"
        bot_saham.portfolio_sessions.clear()
        bot_saham.rate_limit.clear()
        self.send_text_patch = patch.object(bot_saham, "send_text")
        self.api_patch = patch.object(bot_saham, "portfolio_api_request")
        self.config_patch = patch.multiple(
            bot_saham,
            PORTFOLIO_API_BASE_URL="https://docs.soqisoqi.my.id",
            PORTFOLIO_API_SECRET="secret",
            PORTFOLIO_ALLOWED_CHAT_IDS={self.chat_id},
            RATE_LIMIT_SECONDS=0,
        )
        self.mock_send_text = self.send_text_patch.start()
        self.mock_api = self.api_patch.start()
        self.config_patch.start()

    def tearDown(self) -> None:
        self.send_text_patch.stop()
        self.api_patch.stop()
        self.config_patch.stop()
        bot_saham.portfolio_sessions.clear()
        bot_saham.rate_limit.clear()

    def portfolio_snapshot(self) -> dict:
        return {
            "profile": {
                "name": "Syauqi Naufal",
                "role": "Backend Engineer",
                "headline": "Automation builder",
                "bio": "Builds systems.",
                "location": "Indonesia",
                "email": "syauqi@example.com",
            },
            "socialLinks": {
                "githubUrl": "https://github.com/Syauqi-N",
                "linkedinUrl": "https://www.linkedin.com/in/syauqi-naufal/",
                "telegramUrl": "https://wa.me/6285782797494",
                "portfolioUrl": "https://docs.soqisoqi.my.id",
            },
            "projects": [
                {
                    "slug": "bot-saham-telegram",
                    "title": "Bot Saham Telegram",
                    "summary": "Market bot",
                    "description": "Automation bot for market data.",
                    "status": "live",
                    "featured": True,
                    "techStack": ["Python", "Telegram Bot API"],
                    "repoUrl": "",
                    "demoUrl": "",
                }
            ],
        }

    def process_message(self, text: str) -> str:
        return bot_saham.process_incoming_message(text, self.chat_id, False, None, "private")

    def sent_messages(self) -> str:
        return "\n".join(call.args[1] for call in self.mock_send_text.call_args_list)

    def test_portfolio_session_menu_and_list(self) -> None:
        self.mock_api.return_value = (self.portfolio_snapshot(), None)

        start_status = self.process_message("!porto")
        list_status = self.process_message("1")

        self.assertEqual(start_status, "ok")
        self.assertEqual(list_status, "ok")
        self.assertIn(self.chat_id, bot_saham.portfolio_sessions)
        self.mock_api.assert_called_once_with("GET", "/api/admin/portfolio")
        joined = self.sent_messages()
        self.assertIn("Mode !porto aktif.", joined)
        self.assertIn("Menu utama:", joined)
        self.assertIn("1. Bot Saham Telegram [bot-saham-telegram] (live featured)", joined)

    def test_portfolio_add_project_wizard(self) -> None:
        self.mock_api.return_value = ({"project": {"slug": "new-project"}}, None)

        self.process_message("!porto")
        self.process_message("2")
        self.process_message("New Project")
        self.process_message("new-project")
        self.process_message("Short summary")
        self.process_message("2")
        self.process_message("yes")
        self.process_message("Python, API")
        self.process_message("Project description")
        status = self.process_message("save")

        self.assertEqual(status, "ok")
        method, path, payload = self.mock_api.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/admin/projects")
        self.assertEqual(payload["slug"], "new-project")
        self.assertEqual(payload["status"], "wip")
        self.assertEqual(payload["techStack"], ["Python", "API"])
        self.assertTrue(payload["featured"])
        self.assertEqual(bot_saham.portfolio_sessions[self.chat_id]["flow"], "menu")

    def test_portfolio_edit_project_wizard(self) -> None:
        self.mock_api.side_effect = [
            (self.portfolio_snapshot(), None),
            ({"project": {"slug": "bot-saham-telegram"}}, None),
        ]

        self.process_message("!porto")
        self.process_message("3")
        self.process_message("1")
        self.process_message("2")
        self.process_message("Updated summary")
        status = self.process_message("save")

        self.assertEqual(status, "ok")
        method, path, payload = self.mock_api.call_args.args
        self.assertEqual(method, "PUT")
        self.assertEqual(path, "/api/admin/projects/bot-saham-telegram")
        self.assertEqual(payload["summary"], "Updated summary")
        self.assertEqual(payload["title"], "Bot Saham Telegram")

    def test_portfolio_delete_project_wizard(self) -> None:
        self.mock_api.side_effect = [
            (self.portfolio_snapshot(), None),
            ({"ok": True}, None),
        ]

        self.process_message("!porto")
        self.process_message("4")
        self.process_message("1")
        status = self.process_message("yes")

        self.assertEqual(status, "ok")
        self.assertEqual(self.mock_api.call_args.args[0], "DELETE")
        self.assertEqual(self.mock_api.call_args.args[1], "/api/admin/projects/bot-saham-telegram")

    def test_portfolio_edit_profile_wizard(self) -> None:
        self.mock_api.side_effect = [
            (self.portfolio_snapshot(), None),
            ({"profile": {"role": "Backend & AI Engineering"}}, None),
        ]

        self.process_message("!porto")
        self.process_message("5")
        self.process_message("2")
        self.process_message("Backend & AI Engineering")
        status = self.process_message("save")

        self.assertEqual(status, "ok")
        method, path, payload = self.mock_api.call_args.args
        self.assertEqual(method, "PUT")
        self.assertEqual(path, "/api/admin/profile")
        self.assertEqual(payload["role"], "Backend & AI Engineering")

    def test_portfolio_edit_social_wizard(self) -> None:
        self.mock_api.side_effect = [
            (self.portfolio_snapshot(), None),
            ({"socialLinks": {"telegramUrl": "https://wa.me/620000000000"}}, None),
        ]

        self.process_message("!porto")
        self.process_message("6")
        self.process_message("3")
        self.process_message("https://wa.me/620000000000")
        status = self.process_message("save")

        self.assertEqual(status, "ok")
        method, path, payload = self.mock_api.call_args.args
        self.assertEqual(method, "PUT")
        self.assertEqual(path, "/api/admin/social-links")
        self.assertEqual(payload["telegramUrl"], "https://wa.me/620000000000")


if __name__ == "__main__":
    unittest.main()
