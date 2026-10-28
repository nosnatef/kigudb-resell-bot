from pathlib import Path
from tempfile import TemporaryDirectory
import time
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

from discord_bot import configured_channels, process_channel, main, database_error_hint, character_text, delivery_cache, exchange_rates, next_post, payload, reserve, send_message, prepare_translation, translate_title, translation_references, TRANSLATION_PROMPT


class DiscordTests(unittest.TestCase):
    rates = {"USD": Decimal("0.14"), "JPY": Decimal("21")}

    def post(self):
        return {"id": "post-1", "note": "@everyone " + "x" * 300, "priceRMB": Decimal("7100"),
                "maker": None, "character": "Alice", "character_jp": "???", "link": "https://www.goofish.com/item?id=12345",
                "coverImageUrl": "https://cdn.example.com/cover.webp"}

    def test_channel_configuration(self):
        self.assertEqual(configured_channels({"DISCORD_CHANNEL_ID": "123"}), ["123"])
        self.assertEqual(configured_channels({"DISCORD_CHANNEL_IDS": "123,456,123", "DISCORD_CHANNEL_ID": "789"}), ["123", "456"])
        self.assertEqual(configured_channels({"DISCORD_CHANNEL_IDS": "", "DISCORD_CHANNEL_ID": "123"}), ["123"])
        with self.assertRaises(RuntimeError):
            configured_channels({"DISCORD_CHANNEL_IDS": "123,bad"})

    @patch("discord_bot.send_message", return_value="987")
    @patch("discord_bot.prepare_translation")
    def test_channels_advance_independently_and_history_survives(self, prepare, send):
        first = self.post()
        second = dict(first, id="post-2")
        conn = Mock()
        def execute(query, params):
            excluded = params[0]
            return Mock(fetchone=lambda: next((dict(p) for p in (first, second) if p["id"] not in excluded), None))
        conn.execute.side_effect = execute
        with TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with delivery_cache(path) as cache:
                process_channel(conn, cache, "123", "token", self.rates, True)
            with delivery_cache(path) as cache:
                process_channel(conn, cache, "123", "token", self.rates, True)
                process_channel(conn, cache, "456", "token", self.rates, True)
                self.assertEqual(cache.execute("SELECT channel_id, post_id FROM deliveries ORDER BY channel_id, post_id").fetchall(),
                                 [("123", "post-1"), ("123", "post-2"), ("456", "post-1")])
                process_channel(conn, cache, "123", "token", self.rates, True)
                self.assertEqual(send.call_count, 3)

    @patch.dict("os.environ", {"DISCORD_CHANNEL_IDS": "123,456", "DISCORD_BOT_TOKEN": "token", "DATABASE_URL": "postgresql://example/db"})
    @patch("discord_bot.exchange_rates")
    @patch("discord_bot.psycopg.connect")
    @patch("discord_bot.process_channel")
    def test_failed_channel_does_not_block_next(self, process, connect, rates):
        process.side_effect = [RuntimeError("private connection details"), None]
        with TemporaryDirectory() as directory:
            with patch("sys.argv", ["discord_bot.py", "--send", "--cache-file", str(Path(directory) / "cache.sqlite3")]):
                with self.assertRaisesRegex(RuntimeError, "1 channel"):
                    main()
        self.assertEqual([call.args[2] for call in process.call_args_list], ["123", "456"])

    def test_embed_limits_mentions_and_stable_nonce(self):
        result = payload(self.post(), "123", self.rates)
        self.assertEqual(result["embeds"][0]["url"], self.post()["link"])
        self.assertIn(self.post()["link"], result["embeds"][0]["description"])
        self.assertEqual(len(result["embeds"][0]["title"]), 256)
        self.assertEqual(result["embeds"][0]["fields"][2]["value"], "Alice / ???")
        self.assertEqual(result["allowed_mentions"], {"parse": []})
        self.assertEqual(result["embeds"][0]["fields"][0]["value"], "994.00 USD / 149,100 JPY")
        self.assertEqual(result["nonce"], payload(self.post(), "123", self.rates)["nonce"])
        self.assertNotEqual(result["nonce"], payload(self.post(), "456", self.rates)["nonce"])

    def test_optional_and_unsafe_urls(self):
        post = self.post()
        post.update(note=None, coverImageUrl="javascript:alert(1)", link=None)
        embed = payload(post, "123", self.rates)["embeds"][0]
        self.assertNotIn("image", embed)
        self.assertNotIn("description", embed)
        self.assertNotIn("url", embed)
        self.assertEqual(embed["title"], "Alice")

    def test_long_title_is_fully_in_body(self):
        listing = dict(self.post(), english_title="English " * 50)
        embed = payload(listing, "123", self.rates)["embeds"][0]
        self.assertEqual(len(embed["title"]), 256)
        self.assertIn(listing["english_title"], embed["description"])
        listing["english_title"] = "x" * 256
        self.assertNotIn(listing["english_title"], payload(listing, "123", self.rates)["embeds"][0]["description"])

    @patch("discord_bot.requests.post")
    def test_oversized_translation_is_attached_in_full(self, post):
        listing = dict(self.post(), english_title="x" * 5000)
        message = payload(listing, "123", self.rates)
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"id": "987"}
        send_message("token", "123", message)
        options = post.call_args.kwargs
        self.assertEqual(options["files"]["files[0]"][1], listing["english_title"].encode())
        self.assertNotIn("_translation_file", options["data"]["payload_json"])

    @patch.dict("os.environ", {"GROQ_API_KEY": "private-groq-key"})
    @patch("discord_bot.requests.post")
    def test_translation_prompt_and_embed(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"choices": [{"finish_reason": "stop",
            "message": {"content": "Mask for sale\nGood condition."}}]}
        listing = self.post()
        listing["note"] += "\nSeller: Someone\nRegion: Somewhere\nSource: https://example.com"
        listing.update(maker_en="Studio Luna", maker_zh="\u6708\u4eae\u5de5\u623f",
                       character_zh="\u7231\u4e3d\u4e1d", media_en="Wonderland", media_jp="\u4e0d\u601d\u8b70",
                       sub_media_en="Wonderland II")
        original = dict(listing)
        prepare_translation(listing)
        self.assertEqual({key: listing[key] for key in original}, original)
        self.assertIn("\u5934\u58f3 = mask", TRANSLATION_PROMPT)
        messages = post.call_args.kwargs["json"]["messages"]
        self.assertEqual(messages[1]["content"], listing["note"].split("\n")[0])
        self.assertNotIn("Seller:", messages[1]["content"])
        self.assertIn("Studio Luna", messages[0]["content"])
        self.assertIn("Wonderland", messages[0]["content"])
        self.assertIn("Wonderland II", messages[0]["content"])
        self.assertIn(listing["character_zh"], messages[0]["content"])
        self.assertNotIn(listing["link"], messages[0]["content"])
        embed = payload(listing, "123", self.rates)["embeds"][0]
        original_embed = payload(original, "123", self.rates)["embeds"][0]
        self.assertEqual(embed["title"], "Mask for sale Good condition.")
        self.assertEqual(embed["fields"], original_embed["fields"])
        self.assertEqual(embed["url"], original_embed["url"])
        self.assertNotIn("Good condition.", embed["description"])
        self.assertNotIn("Seller:", embed["description"])
        self.assertIn(listing["link"], embed["description"])
        listing["english_title"] = "x" * 5000
        self.assertEqual(len(payload(listing, "123", self.rates)["embeds"][0]["title"]), 256)

    @patch.dict("os.environ", {"GROQ_API_KEY": "private-groq-key"})
    @patch("discord_bot.requests.post")
    def test_failed_translation_does_not_reserve(self, post):
        post.return_value.status_code = 401
        post.return_value.text = "private-groq-key"
        with TemporaryDirectory() as directory:
            with delivery_cache(Path(directory) / "history.sqlite3") as cache:
                with patch("discord_bot.next_post", return_value=self.post()):
                    with self.assertRaisesRegex(RuntimeError, "HTTP 401") as error:
                        reserve(Mock(), "123", cache, prepare_translation)
                self.assertNotIn("private-groq-key", str(error.exception))
                self.assertEqual(cache.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)

    @patch.dict("os.environ", {"GROQ_API_KEY": "secret", "GROQ_MODEL": " "})
    @patch("discord_bot.requests.post")
    def test_unavailable_model_has_safe_actionable_error(self, post):
        post.return_value.status_code = 404
        post.return_value.json.return_value = {"error": {"code": "model_not_found", "message": "secret"}}
        with self.assertRaisesRegex(RuntimeError, "cannot access") as error:
            translate_title("Listing")
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(post.call_args.kwargs["json"]["model"], "openai/gpt-oss-20b")

    @patch("discord_bot.requests.post")
    def test_empty_note_skips_translation(self, post):
        self.assertEqual(translate_title(None), "")
        post.assert_not_called()

    @patch.dict("os.environ", {"GROQ_API_KEY": "secret"})
    @patch("discord_bot.requests.post")
    def test_full_input_is_sent_and_chinese_output_is_accepted(self, post):
        source = "x" * 400 + " \u5de5\u574a full mask with lock"
        translation = "Workshop \u5de5\u574a full mask with lock"
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": translation}}]}
        self.assertEqual(translate_title(source), translation)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"]["messages"][1]["content"], source)

    def test_translation_references_handle_missing_names(self):
        self.assertEqual(translation_references({}), {})
        self.assertEqual(translation_references({"maker_en": None, "character_jp": "Alice JP"}),
                         {"character": {"ja": "Alice JP"}})

    def test_database_diagnostics_do_not_expose_connection_details(self):
        cases = [("password authentication failed", "authentication failed"),
                 ("could not translate host name", "hostname"),
                 ("Network is unreachable", "network is unreachable"),
                 ("connection timeout expired", "timed out"),
                 ("SSL error", "TLS"), ("unrecognized failure", "operation failed")]
        for detail, expected in cases:
            with self.subTest(detail=detail):
                hint = database_error_hint(Exception(detail + " private-password private-host"))
                self.assertIn(expected, hint)
                self.assertNotIn("private-password", hint)
                self.assertNotIn("private-host", hint)

    def test_character_missing_translations(self):
        self.assertEqual(character_text({"character": "Alice"}), "Alice")
        self.assertEqual(character_text({"character_jp": "???"}), "???")
        self.assertEqual(character_text({}), "Unknown")

    @patch("discord_bot.requests.get")
    def test_exchange_rates_and_invalid_response(self, get):
        get.return_value.json.return_value = {"base": "CNY", "rates": {"USD": 0.14, "JPY": 21}}
        self.assertEqual(exchange_rates(), self.rates)
        get.return_value.json.return_value["rates"]["JPY"] = -1
        with self.assertRaisesRegex(RuntimeError, "no listing reserved"):
            exchange_rates()

    def test_cache_persists_and_allows_immediate_next_reservation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with delivery_cache(path) as cache:
                with patch("discord_bot.next_post", return_value=self.post()):
                    self.assertEqual(reserve(Mock(), "123", cache)["id"], "post-1")
            with delivery_cache(path) as cache:
                second = dict(self.post(), id="post-2")
                with patch("discord_bot.next_post", return_value=second) as select:
                    self.assertEqual(reserve(Mock(), "123", cache)["id"], "post-2")
                    select.assert_called_once()
                self.assertIsNone(cache.execute("SELECT sent_at FROM deliveries").fetchone()[0])
                conn = Mock()
                next_post(conn, "123", cache)
                self.assertEqual(conn.execute.call_args.args[1], (["post-1", "post-2"],))
                next_post(conn, "456", cache)
                self.assertEqual(conn.execute.call_args.args[1], ([],))

    def test_old_reservation_still_excludes_listing(self):
        with TemporaryDirectory() as directory:
            with delivery_cache(Path(directory) / "history.sqlite3") as cache:
                cache.execute("INSERT INTO deliveries VALUES (?, ?, ?, NULL, NULL)",
                              ("123", "post-1", time.time() - 1801))
                cache.commit()
                conn = Mock()
                conn.execute.return_value.fetchone.return_value = None
                self.assertIsNone(reserve(conn, "123", cache))
                self.assertEqual(conn.execute.call_args.args[1], (["post-1"],))

    @patch("discord_bot.requests.post")
    def test_unknown_message_is_not_reported_as_unknown_channel(self, post):
        post.return_value.status_code = 404
        post.return_value.json.return_value = {"code": 10008}
        with self.assertRaisesRegex(RuntimeError, "Unknown message") as error:
            send_message("token", "123", {})
        self.assertNotIn("Channel not found", str(error.exception))
        post.assert_called_once()

    @patch("discord_bot.requests.post")
    def test_discord_failure_is_not_retried_or_exposed(self, post):
        post.return_value.status_code = 429
        post.return_value.text = "secret"
        post.return_value.json.return_value = {"code": 50013, "message": "secret"}
        with self.assertRaises(RuntimeError) as error:
            send_message("private-token", "123", {})
        self.assertIn("HTTP 429", str(error.exception))
        self.assertIn("50013", str(error.exception))
        self.assertNotIn("secret", str(error.exception))
        self.assertNotIn("private-token", str(error.exception))
        post.assert_called_once()

    @patch("discord_bot.requests.post")
    def test_success_requires_message_id(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"id": "987"}
        self.assertEqual(send_message("token", "123", {}), "987")
        post.return_value.json.return_value = {}
        with self.assertRaises(RuntimeError):
            send_message("token", "123", {})


if __name__ == "__main__":
    unittest.main()
