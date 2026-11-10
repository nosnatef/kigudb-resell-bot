"""Run against a disposable PostgreSQL DB via TEST_DISCORD_DATABASE_URL.

Each test gets a separate schema; no Discord/Groq requests are made.
"""
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from discord_bot import enroll_channel, import_history, next_post, prepare_translation, process_channel, main


@unittest.skipUnless(os.environ.get("TEST_DISCORD_DATABASE_URL"), "Set TEST_DISCORD_DATABASE_URL to a disposable PostgreSQL database")
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.schema = "discord_test_" + uuid4().hex
        self.admin = self.stack.enter_context(psycopg.connect(os.environ["TEST_DISCORD_DATABASE_URL"], autocommit=True))
        self.admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(self.schema)))
        self.addCleanup(lambda: self.admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(self.schema))))
        self.conn = self.connect()
        self.locks = self.connect()
        self.conn.execute('''
            CREATE TABLE "ResellPost" (id TEXT PRIMARY KEY, "priceRMB" NUMERIC DEFAULT 100,
                note TEXT, "coverImageUrl" TEXT, makerid INT, characterid TEXT,
                status TEXT DEFAULT 'FOR_SALE', "createdAt" TIMESTAMP DEFAULT CURRENT_TIMESTAMP - INTERVAL '2 days');
            CREATE TABLE "Maker" (id INT PRIMARY KEY, name TEXT, name_zh TEXT, name_jp TEXT);
            CREATE TABLE "Character" (id TEXT PRIMARY KEY, name TEXT, name_zh TEXT, name_jp TEXT, "originId" INT, "subOriginId" INT);
            CREATE TABLE "Origin" (id INT PRIMARY KEY, name TEXT, name_zh TEXT, name_jp TEXT);
            CREATE TABLE "SubOrigin" (id INT PRIMARY KEY, name TEXT, name_zh TEXT, name_jp TEXT);
            CREATE TABLE "ResellPostLink" (id INT, "resellpostId" TEXT, "linkType" TEXT, link TEXT);
        ''')
        self.conn.execute((Path(__file__).resolve().parents[1] / "sql/discord-state.sql").read_text(encoding="utf-8"))
        self.conn.execute('''INSERT INTO "ResellPost" (id, note) VALUES ('p1', 'title'), ('p2', 'title 2');
            INSERT INTO "DiscordServer" ("guildId", "channelId", "updatedAt")
            VALUES ('1', '123', CURRENT_TIMESTAMP), ('2', '456', CURRENT_TIMESTAMP)''')

    def connect(self):
        return self.stack.enter_context(psycopg.connect(os.environ["TEST_DISCORD_DATABASE_URL"],
            autocommit=True, row_factory=dict_row,
            options=f"-c search_path={self.schema} -c timezone=UTC -c statement_timeout=5000"))

    def post(self):
        return next_post(self.conn, "1")

    @patch("discord_bot.translate_title", return_value="English title")
    def test_translation_reused_and_invalidated(self, translate):
        post = self.post()
        prepare_translation(post, self.conn)
        prepare_translation(self.post(), self.locks)
        self.assertEqual(translate.call_count, 1)
        for field, value in [("note", "changed"), ("maker_en", "New maker")]:
            post[field] = value
            prepare_translation(post, self.conn)
        with patch.dict(os.environ, {"GROQ_MODEL": "another-model"}):
            prepare_translation(post, self.conn)
        with patch("discord_bot.TRANSLATION_PROMPT", "changed prompt"):
            prepare_translation(post, self.conn)
        self.assertEqual(translate.call_count, 5)
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM "ResellPostTranslation"').fetchone()["n"], 1)

    @patch("discord_bot.translate_title", return_value="English title")
    @patch("discord_bot.send_message", return_value="987")
    @patch("discord_bot.payload", return_value={})
    def test_independent_servers_latest_and_shared_cache(self, payload, send, translate):
        for server in ["1", "2", "1"]:
            process_channel(self.conn, self.locks, server, "token", {}, True)
        self.assertEqual(send.call_count, 3)
        self.assertEqual(translate.call_count, 2)
        rows = self.conn.execute('SELECT "guildId", "latestPostId", "latestMessageId", "latestSentAt" FROM "DiscordServer" ORDER BY "guildId"').fetchall()
        self.assertEqual([row["latestPostId"] for row in rows], ["p2", "p1"])
        self.assertTrue(all(row["latestMessageId"] == "987" and row["latestSentAt"] for row in rows))
        self.assertIsNone(next_post(self.conn, "1"))

    @patch("discord_bot.translate_title", return_value="English title")
    @patch("discord_bot.send_message", side_effect=TimeoutError)
    @patch("discord_bot.payload", return_value={})
    def test_uncertain_send_keeps_reservation_without_advancing_latest(self, payload, send, translate):
        with self.assertRaises(TimeoutError):
            process_channel(self.conn, self.locks, "1", "token", {}, True)
        self.assertEqual(next_post(self.conn, "1")["id"], "p2")
        self.assertIsNone(self.conn.execute('SELECT "sentAt" FROM "DiscordDelivery"').fetchone()["sentAt"])
        self.assertIsNone(self.conn.execute('SELECT "latestPostId" FROM "DiscordServer" WHERE "guildId" = \'1\'').fetchone()["latestPostId"])

    @patch("discord_bot.translate_title", side_effect=RuntimeError("translation failed"))
    def test_failed_translation_is_not_cached_or_reserved(self, translate):
        with self.assertRaises(RuntimeError):
            process_channel(self.conn, self.locks, "1", "token", {}, True)
        for table in ['DiscordDelivery', 'ResellPostTranslation']:
            self.assertEqual(self.conn.execute(sql.SQL('SELECT count(*) AS n FROM {}').format(sql.Identifier(table))).fetchone()["n"], 0)

    @patch("discord_bot.translate_title", return_value="English title")
    @patch("discord_bot.send_message")
    @patch("discord_bot.payload", return_value={})
    def test_preview_caches_without_sending_or_advancing(self, payload, send, translate):
        process_channel(self.conn, self.locks, "1", "", {}, False)
        send.assert_not_called()
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM "ResellPostTranslation"').fetchone()["n"], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM "DiscordDelivery"').fetchone()["n"], 0)

    @patch("discord_bot.translate_title", return_value="English title")
    @patch("discord_bot.payload", return_value={})
    def test_overlapping_run_skips_locked_server(self, payload, translate):
        sending, release = Event(), Event()
        def send(*args):
            sending.set()
            self.assertTrue(release.wait(4))
            return "987"
        other, other_locks = self.connect(), self.connect()
        with patch("discord_bot.send_message", side_effect=send) as sender, ThreadPoolExecutor(1) as pool:
            future = pool.submit(process_channel, self.conn, self.locks, "1", "token", {}, True)
            try:
                self.assertTrue(sending.wait(4))
                process_channel(other, other_locks, "1", "token", {}, True)
            finally:
                release.set()
            future.result()
            self.assertEqual(sender.call_count, 1)

    def test_import_preserves_uncertain_and_confirmed_history_idempotently(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with closing(sqlite3.connect(path)) as cache:
                with cache:
                    cache.execute('CREATE TABLE deliveries (channel_id TEXT, post_id TEXT, reserved_at REAL, message_id TEXT, sent_at REAL)')
                    cache.executemany('INSERT INTO deliveries VALUES (?, ?, ?, ?, ?)',
                        [('123', 'p1', 1000, '987', 1100), ('123', 'p2', 1200, None, None)])
            import_history(self.conn, path)
            self.conn.execute('UPDATE "DiscordServer" SET "channelId" = \'789\' WHERE "guildId" = \'1\'')
            import_history(self.conn, path)
        self.assertEqual(self.conn.execute('SELECT count(*) AS n FROM "DiscordDelivery"').fetchone()["n"], 2)
        self.assertIsNone(next_post(self.conn, "1"))
        self.assertEqual(self.conn.execute('SELECT "latestPostId" FROM "DiscordServer" WHERE "guildId" = \'1\'').fetchone()["latestPostId"], "p1")

    @patch("discord_bot.requests.get")
    def test_enrollment_and_channel_change_keep_server_history(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = {"guild_id": "1", "type": 0}
        self.conn.execute('INSERT INTO "DiscordDelivery" ("serverId", "postId", "channelId") VALUES (\'1\', \'p1\', \'123\')')
        enroll_channel(self.conn, "token", "789")
        self.assertEqual(next_post(self.conn, "1")["id"], "p2")
        self.conn.execute('UPDATE "DiscordServer" SET enabled = false WHERE "guildId" = \'1\'')
        enroll_channel(self.conn, "token", "123", replace=False)
        row = self.conn.execute('SELECT * FROM "DiscordServer" WHERE "guildId" = \'1\'').fetchone()
        self.assertEqual(row["channelId"], "789")
        self.assertFalse(row["enabled"])

    def test_newer_cache_advances_database_and_older_cache_cannot_rewind_it(self):
        self.conn.execute('''INSERT INTO "DiscordDelivery"
            ("serverId", "postId", "channelId", "reservedAt", "messageId", "sentAt")
            VALUES ('1', 'p1', '123', to_timestamp(900), '900', to_timestamp(1000)),
                   ('1', 'p2', '123', to_timestamp(1100), NULL, NULL);
            UPDATE "DiscordServer" SET "latestPostId" = 'p1', "latestMessageId" = '900',
                "latestSentAt" = to_timestamp(1000) WHERE "guildId" = '1' ''')
        with TemporaryDirectory() as directory:
            path = Path(directory) / "history.sqlite3"
            with closing(sqlite3.connect(path)) as cache:
                with cache:
                    cache.execute('CREATE TABLE deliveries (channel_id TEXT, post_id TEXT, reserved_at REAL, message_id TEXT, sent_at REAL)')
                    cache.executemany('INSERT INTO deliveries VALUES (?, ?, ?, ?, ?)',
                        [('123', 'p1', 900, '950', 1200), ('123', 'p2', 1100, '987', 1300)])
                import_history(self.conn, path)
                latest = self.conn.execute('SELECT * FROM "DiscordServer" WHERE "guildId" = \'1\'').fetchone()
                self.assertEqual((latest["latestPostId"], latest["latestMessageId"]), ("p2", "987"))
                self.assertEqual(latest["latestSentAt"].isoformat(), '1970-01-01T00:21:40')
                rows = self.conn.execute('SELECT * FROM "DiscordDelivery" ORDER BY "postId"').fetchall()
                self.assertEqual([row["messageId"] for row in rows], ["950", "987"])
                with cache:
                    cache.execute("UPDATE deliveries SET message_id = '800', sent_at = 800 WHERE post_id = 'p1'")
                    cache.execute("UPDATE deliveries SET message_id = NULL, sent_at = NULL WHERE post_id = 'p2'")
                import_history(self.conn, path)
                self.assertEqual(self.conn.execute('SELECT * FROM "DiscordServer" WHERE "guildId" = \'1\'').fetchone(), latest)
                self.assertEqual(self.conn.execute('SELECT * FROM "DiscordDelivery" ORDER BY "postId"').fetchall(), rows)
        self.assertIsNone(next_post(self.conn, "1"))

    def test_selection_age_status_and_reservations(self):
        self.conn.execute('''UPDATE "ResellPost" SET status = 'SOLD' WHERE id = 'p1';
            UPDATE "ResellPost" SET "createdAt" = CURRENT_TIMESTAMP WHERE id = 'p2' ''')
        self.assertIsNone(next_post(self.conn, "1"))

    def test_selection_excludes_posts_older_than_one_month(self):
        self.conn.execute('''UPDATE "ResellPost" SET "createdAt" =
            (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '1 month' - INTERVAL '1 second'
            WHERE id = 'p1' ''')
        self.assertEqual(next_post(self.conn, "1")["id"], "p2")
        # Hold the transaction time fixed to exercise the inclusive boundary.
        with self.conn.transaction():
            self.conn.execute('''UPDATE "ResellPost" SET "createdAt" =
                (CURRENT_TIMESTAMP AT TIME ZONE 'UTC') - INTERVAL '1 month' WHERE id = 'p1' ''')
            self.assertEqual(next_post(self.conn, "1")["id"], "p1")

    @patch("discord_bot.payload", return_value={})
    @patch("discord_bot.send_message", return_value="987")
    @patch("discord_bot.translate_title", return_value="English title")
    def test_confirmation_failure_rolls_back_latest_and_delivery_together(self, translate, send, payload):
        self.conn.execute('''CREATE FUNCTION reject_confirmation() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'simulated confirmation failure'; END; $$;
            CREATE TRIGGER reject_confirmation BEFORE UPDATE ON "DiscordServer"
            FOR EACH ROW EXECUTE FUNCTION reject_confirmation()''')
        with self.assertRaises(psycopg.errors.RaiseException):
            process_channel(self.conn, self.locks, "1", "token", {}, True)
        self.assertIsNone(self.conn.execute('SELECT "sentAt" FROM "DiscordDelivery"').fetchone()["sentAt"])
        self.assertIsNone(self.conn.execute('SELECT "latestPostId" FROM "DiscordServer" WHERE "guildId" = \'1\'').fetchone()["latestPostId"])
        self.assertEqual(next_post(self.conn, "1")["id"], "p2")

    def test_concurrent_translation_uses_one_provider_call(self):
        translating, release = Event(), Event()
        def translate(*args):
            translating.set()
            self.assertTrue(release.wait(4))
            return "English title"
        first, second = self.post(), self.post()
        with patch("discord_bot.translate_title", side_effect=translate) as provider, ThreadPoolExecutor(2) as pool:
            one = pool.submit(prepare_translation, first, self.conn)
            try:
                self.assertTrue(translating.wait(4))
                two = pool.submit(prepare_translation, second, self.locks)
                with self.assertRaises(TimeoutError):
                    two.result(timeout=0.1)
            finally:
                release.set()
            one.result()
            two.result()
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(first["english_title"], second["english_title"])

    @patch("discord_bot.process_channel", side_effect=[RuntimeError("private details"), None])
    @patch("discord_bot.exchange_rates", return_value={})
    def test_failure_does_not_stop_other_servers(self, rates, process):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://unused/db", "DISCORD_BOT_TOKEN": "token",
                                    "DISCORD_CHANNEL_ID": "", "DISCORD_CHANNEL_IDS": ""}), \
             patch("discord_bot.psycopg.connect", side_effect=[self.conn, self.locks]), \
             patch("sys.argv", ["discord_bot.py", "--send", "--cache-file", str(Path(__file__).parent / "absent.sqlite3")]):
            with self.assertRaisesRegex(RuntimeError, "1 server"):
                main()
        self.assertEqual([call.args[2] for call in process.call_args_list], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
