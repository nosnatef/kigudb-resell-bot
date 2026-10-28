import copy
from contextlib import contextmanager
from decimal import Decimal
import json
import unittest
from unittest.mock import Mock

from worker import (FIELDS, FeishuFetcher, SourceError, SkipRecord, database_url,
                    import_rows, insert_listing, match_entity, parse_listing,
                    publication_time, latest_publication_time)


def source_row(product_id="1095153548801", title="Studio Luna Alice mask", price=7100):
    values = {"product_id": product_id, "title": title, "price": price,
              "seller": "Seller", "region": "Beijing"}
    return {FIELDS[key]: {"value": value, "originValue": value} for key, value in values.items()}


class Result:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeDatabase:
    def __init__(self):
        self.posts, self.links = {}, []
        self.fail_link = False

    @contextmanager
    def transaction(self):
        old = copy.deepcopy((self.posts, self.links))
        try:
            yield
        except Exception:
            self.posts, self.links = old
            raise

    def execute(self, query, params=()):
        if 'INSERT INTO "ResellPostLink"' in query:
            if self.fail_link:
                raise RuntimeError("link insert failed")
            self.links.append({"link": params[1], "resellpostId": params[0]})
        elif 'INSERT INTO "ResellPost"' in query:
            assert "'FOR_SALE'" in query, "New imports must explicitly be FOR_SALE"
            if params[0] in self.posts:
                return Result([])
            self.posts[params[0]] = params
            return Result([{"id": params[0]}])
        elif 'FROM "MakerNickname"' in query:
            return Result([])
        elif 'FROM "Maker"' in query:
            return Result([{"id": 1, "name": "Studio Luna", "name_zh": "月亮工房", "name_jp": "ルナ工房"}])
        elif 'FROM "Character"' in query:
            return Result([{"id": "alice", "name": "Alice", "name_zh": None, "name_jp": None}])
        elif 'FROM "User"' in query:
            return Result([{"id": "worker"}])
        elif 'FROM "ResellPostLink"' in query:
            return Result(self.links)
        elif 'FROM "ResellPost"' in query:
            return Result([{"id": key} for key in self.posts])
        else:
            raise AssertionError(query)
        return Result([])


class ImportTests(unittest.TestCase):
    def test_localized_maker_names(self):
        for title in ("月亮工房 mask", "ルナ工房 mask", "Studio Luna 月亮工房 mask"):
            _, report = import_rows(FakeDatabase(), [source_row(title=title)], "worker", {}, "https://source")
            self.assertEqual(report[0]["makerid"], 1)

    def test_cutoff_uses_latest_source_publication(self):
        conn = Mock()
        conn.execute.return_value = Result([
            {"note": "Title\nPublished: 2026-10-20 12:00:00\nSource: test"},
            {"note": "Title\nPublished: 2026-10-21 12:00:00\nSource: test"},
            {"note": "Published: unknown"},
        ])
        self.assertEqual(latest_publication_time(conn), publication_time("2026-10-21T04:00:00Z"))
        conn.execute.return_value = Result([])
        self.assertIsNone(latest_publication_time(conn))

    def test_publication_times(self):
        expected = publication_time("2026-10-21T04:00:00Z")
        self.assertEqual(publication_time("2026/10/21 12:00:00"), expected)
        self.assertEqual(publication_time(str(int(expected.timestamp() * 1000))), expected)
        for value in ("", "yesterday", "2026-10-21", "2026-99-99 12:00"):
            self.assertIsNone(publication_time(value))

    def test_numeric_price_and_canonical_link(self):
        listing = parse_listing(source_row())
        self.assertEqual(listing.price, Decimal("7100.00"))
        self.assertEqual(listing.url, "https://www.goofish.com/item?id=1095153548801")

    def test_invalid_prices_and_rentals(self):
        for price in (None, "", "NaN", "Infinity", -1, 0, "0.001", "99999999999999"):
            with self.subTest(price=price), self.assertRaises(SkipRecord):
                parse_listing(source_row(price=price))
        with self.assertRaises(SkipRecord):
            parse_listing(source_row(title="Alice rental mask"))

    def test_names_are_unambiguous(self):
        self.assertEqual(match_entity("ALICE mask", [("Alice", "a")], None, {"a"}, "character"), "a")
        for title, aliases in [("Spring", [("Rin", "a")]), ("Alice Bob", [("Alice", "a"), ("Bob", "b")])]:
            self.assertIsNone(match_entity(title, aliases, None, {"a", "b"}, "character"))
        self.assertEqual(match_entity("Unknown", [], "a", {"a"}, "character"), "a")
        with self.assertRaises(SkipRecord):
            match_entity("Unknown", [], "missing", {"a"}, "character")

    def test_repeated_import_and_price_edits_do_not_duplicate_or_update(self):
        conn = FakeDatabase()
        rows = [source_row(), source_row()]
        first, _ = import_rows(conn, rows, "worker", {}, "https://source", True)
        self.assertEqual(first, {"inserted": 1, "existing": 1})
        second, _ = import_rows(conn, [source_row(price=9999)], "worker", {}, "https://source", True)
        self.assertEqual(second, {"existing": 1})
        self.assertEqual(len(conn.posts), 1)
        self.assertEqual(len(conn.links), 1)
        self.assertEqual(next(iter(conn.posts.values()))[1], Decimal("7100.00"))

    def test_dry_run_writes_nothing(self):
        conn = FakeDatabase()
        summary, _ = import_rows(conn, [source_row()], "worker", {}, "https://source")
        self.assertEqual(summary, {"would_insert": 1})
        self.assertFalse(conn.posts)
        self.assertFalse(conn.links)

    def test_link_failure_rolls_back_post(self):
        conn = FakeDatabase()
        conn.fail_link = True
        with self.assertRaises(RuntimeError):
            insert_listing(conn, parse_listing(source_row()), 1, "alice", "worker")
        self.assertFalse(conn.posts)
        self.assertFalse(conn.links)

    def test_missing_matches_import_with_null_relations(self):
        conn = FakeDatabase()
        summary, report = import_rows(conn, [source_row(title="Unknown mask")], "worker", {}, "https://source", True)
        self.assertEqual(summary, {"inserted": 1})
        self.assertIsNone(report[0]["makerid"])
        self.assertIsNone(report[0]["characterid"])
        params = next(iter(conn.posts.values()))
        self.assertEqual(params[5:8], (None, None, None))

    def test_product_overrides_still_resolve_relations(self):
        conn = FakeDatabase()
        mappings = {"products": {"1095153548801": {"makerid": 1, "characterid": "alice"}}}
        summary, report = import_rows(conn, [source_row(title="Unknown mask")], "worker", mappings, "https://source", True)
        self.assertEqual(summary, {"inserted": 1})
        self.assertEqual((report[0]["makerid"], report[0]["characterid"]), (1, "alice"))

    def test_cover_urls_and_attachment_tokens(self):
        row = source_row()
        row[FIELDS["cover"]] = {"value": ["https://images.example.com/cover.webp"]}
        listing = parse_listing(row)
        conn = FakeDatabase()
        insert_listing(conn, listing, None, None, "worker")
        self.assertEqual(next(iter(conn.posts.values()))[7], "https://images.example.com/cover.webp")
        for value in (["filename.webp"], ["javascript:alert(1)"], None):
            row[FIELDS["cover"]] = {"value": value, "originValue": [{"attachmentToken": "token"}]}
            self.assertIsNone(parse_listing(row).cover_image_url)

    def test_prisma_connection_options_are_removed(self):
        self.assertEqual(database_url("postgresql://user:pass@host/db?schema=public&sslmode=require&pgbouncer=true"),
                         "postgresql://user:pass@host/db?sslmode=require")
        with self.assertRaises(ValueError):
            database_url("postgresql://host/db?schema=private")

    def test_five_post_limit_ignores_existing_and_invalid_records(self):
        conn = FakeDatabase()
        insert_listing(conn, parse_listing(source_row()), 1, "alice", "worker")
        rows = [source_row(), source_row("1095153548802", price=None)]
        rows += [source_row(str(1095153548810 + index)) for index in range(8)]
        importer = Mock(return_value="https://cdn.example.com/cover.webp")
        summary, _ = import_rows(conn, rows, "worker", {}, "https://source", True, importer, limit=5)
        self.assertEqual(summary["inserted"], 5)
        self.assertEqual(summary["existing"], 1)
        self.assertEqual(summary["missing_or_invalid_price"], 1)
        self.assertEqual(len(conn.posts), 6)
        self.assertEqual(importer.call_count, 5)

    def test_limit_counts_successful_imports_after_image_failure(self):
        conn = FakeDatabase()
        rows = [source_row(str(1095153548810 + index)) for index in range(8)]
        importer = Mock(side_effect=[RuntimeError("image failed")] + ["https://cdn.example.com/cover.webp"] * 7)
        summary, _ = import_rows(conn, rows, "worker", {}, "https://source", True, importer, limit=5)
        self.assertEqual(summary["inserted"], 5)
        self.assertEqual(summary["image_failed"], 1)
        self.assertEqual(len(conn.posts), 5)

    def test_limited_dry_run_never_uploads(self):
        conn = FakeDatabase()
        rows = [source_row(str(1095153548810 + index)) for index in range(8)]
        importer = Mock()
        summary, _ = import_rows(conn, rows, "worker", {}, "https://source", False, importer, limit=5)
        self.assertEqual(summary, {"would_insert": 5})
        self.assertFalse(conn.posts)
        importer.assert_not_called()

    def test_invalid_limit_is_rejected_before_database_access(self):
        for limit in (0, -1, True):
            with self.assertRaises(ValueError):
                import_rows(Mock(), [], "worker", {}, "https://source", limit=limit)


def response(data):
    result = Mock()
    result.json.return_value = {"code": 0, "data": data}
    return result


class FetchTests(unittest.TestCase):
    def test_cutoff_retains_equal_and_unknown_dates_and_stops_before_older_pages(self):
        def dated(product_id, date):
            row = source_row(product_id)
            row[FIELDS["published"]] = {"value": date}
            return row
        recent = dated("10001", "2026-10-22 12:00:00")
        equal = dated("10002", "2026-10-21 12:00:00")
        unknown = dated("10003", "unknown")
        old = dated("10004", "2026-10-20 12:00:00")
        fetcher = self.fetcher([([recent, equal, unknown], True), ([old], True)])
        self.assertEqual(fetcher.fetch(publication_time("2026-10-21 12:00:00")), [recent, equal, unknown])
        self.assertEqual(fetcher.session.post.call_count, 2)
        dsl = json.loads(fetcher.session.post.call_args.kwargs["json"]["dsl"])
        self.assertEqual(dsl["sort"][0], {"fieldId": FIELDS["published"], "order": "desc"})

    def fetcher(self, pages):
        fetcher = FeishuFetcher(page_size=1)
        session = Mock()
        session.cookies = []
        session.get.side_effect = [response({}), response({"snapshot": json.dumps({"vibeViewEntity": {
            "baseToken": "base", "blockID": "block"}})})]
        session.post.side_effect = [response({"data": json.dumps(rows), "more": more}) for rows, more in pages]
        fetcher.session = session
        return fetcher

    def test_all_pages_are_fetched(self):
        fetcher = self.fetcher([([source_row()], True), ([source_row("1095153548802")], False)])
        self.assertEqual(len(fetcher.fetch()), 2)
        second_request = fetcher.session.post.call_args_list[1].kwargs["json"]
        self.assertEqual(json.loads(second_request["dsl"])["pagination"]["offset"], 1)

    def test_repeated_page_fails(self):
        fetcher = self.fetcher([([source_row()], True), ([source_row()], True)])
        with self.assertRaises(SourceError):
            fetcher.fetch()

    def test_empty_source(self):
        self.assertEqual(self.fetcher([([], False)]).fetch(), [])


if __name__ == "__main__":
    unittest.main()
