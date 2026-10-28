from pathlib import Path
from tempfile import TemporaryDirectory
import time
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

from discord_bot import database_error_hint, character_text, delivery_cache, exchange_rates, next_post, payload, reserve, send_message


class DiscordTests(unittest.TestCase):
    rates = {"USD": Decimal("0.14"), "JPY": Decimal("21")}

    def post(self):
        return {"id": "post-1", "note": "@everyone " + "x" * 300, "priceRMB": Decimal("7100"),
                "maker": None, "character": "Alice", "character_jp": "???", "link": "https://www.goofish.com/item?id=12345",
                "coverImageUrl": "https://cdn.example.com/cover.webp"}

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
