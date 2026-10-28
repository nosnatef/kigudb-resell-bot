"""Daily, insert-only importer for the public Feishu Goofish radar."""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_SHARE_URL = "https://scn984wlthg8.feishu.cn/share/base/webpage/shrcn19ZKt2MvLp9NSMGrMyPYuh"
TABLE_ID = "tblSrlleO3WNkGfo"
FIELDS = {
    "product_id": "fld8sgLNVP", "title": "fldIjVb8QG",
    "price": "fld6n9Qp1V", "price_display": "fldvMA18Iw",
    "region": "fldDgSwrAj", "seller": "fld3vRKWgz",
    "published": "fld4AbMfeb", "first_seen": "fldDClkh5Z",
    "last_seen": "fld2pmHqk0", "keywords": "fldISx2QdD",
    "url": "fldlJSfaDA", "cover": "fldohsSnWP",
}


class SourceError(RuntimeError):
    pass


class SkipRecord(ValueError):
    pass


def unwrap(response):
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("code") != 0:
        code = body.get("code") if isinstance(body, dict) else "invalid response"
        raise SourceError(f"Feishu API rejected the request (code {code}); check public sharing")
    return body["data"]


class FeishuFetcher:
    def __init__(self, share_url=DEFAULT_SHARE_URL, page_size=500, max_pages=200):
        parsed = urlsplit(share_url)
        match = re.fullmatch(r"/share/base/webpage/([A-Za-z0-9]+)", parsed.path.rstrip("/"))
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".feishu.cn") or not match:
            raise ValueError("FEISHU_SHARE_URL must be an HTTPS Feishu shared webpage")
        self.origin = f"https://{parsed.hostname}"
        self.share_url, self.share_token = share_url, match[1]
        self.page_size, self.max_pages = page_size, max_pages
        self.session = requests.Session()
        retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET", "POST"])
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    def fetch(self, cutoff=None):
        # The shared API requires cookies from an anonymous page visit, not a user login.
        self.session.get(self.share_url, timeout=30).raise_for_status()
        data = unwrap(self.session.get(
            self.origin + "/space/api/bitable/external/share/content_meta",
            params={"shareToken": self.share_token}, timeout=30))
        snapshot = data["snapshot"]
        snapshot = json.loads(snapshot) if isinstance(snapshot, str) else snapshot
        view = snapshot["vibeViewEntity"]
        base_token, block_id = view["baseToken"], view["blockID"]
        self.base_token = base_token
        rows, seen_pages = [], set()
        for page in range(self.max_pages):
            dsl = {
                "datasource": {"table": {"tableId": TABLE_ID}},
                "columns": [{"fieldId": field} for field in [*FIELDS.values(), "rec_id"]],
                "sort": [{"fieldId": FIELDS["published"], "order": "desc"},
                         {"fieldId": FIELDS["product_id"], "order": "asc"}],
                "pagination": {"limit": self.page_size, "offset": page * self.page_size},
            }
            csrf = next((cookie.value for cookie in self.session.cookies if cookie.name == "_csrf_token"), "")
            data = unwrap(self.session.post(
                f"{self.origin}/space/api/v1/bitable/{base_token}/query/json-dsl",
                json={"dsl": json.dumps(dsl), "vibeBlockID": block_id, "shareToken": self.share_token},
                headers={"X-CSRFToken": csrf, "Referer": self.share_url}, timeout=45))
            batch = data["data"]
            batch = json.loads(batch) if isinstance(batch, str) else batch
            if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                raise SourceError("Feishu record shape changed; import aborted")
            if not isinstance(data.get("more"), bool):
                raise SourceError("Feishu pagination shape changed; import aborted")
            fingerprint = hashlib.sha256(json.dumps(batch, sort_keys=True).encode()).hexdigest()
            if batch and fingerprint in seen_pages:
                raise SourceError("Feishu repeated a page; import aborted")
            seen_pages.add(fingerprint)
            if cutoff is not None:
                dates = [publication_time(cell(row, "published")) for row in batch]
                rows.extend(row for row, date in zip(batch, dates) if date is None or date >= cutoff)
                # Only stop at a wholly dated, old page; unknown dates never prove age.
                if dates and all(date is not None and date < cutoff for date in dates):
                    return rows
            else:
                rows.extend(batch)
            if not data["more"]:
                return rows
            if not batch:
                raise SourceError("Feishu returned an empty page with more=true")
        raise SourceError("Pagination safety limit reached; no partial import performed")


    def get_cover_source(self, row):
        direct = cover_image_url(row)
        if direct:
            return {"url": direct, "cipherType": "0"}
        image_cell = row.get(FIELDS["cover"], {})
        attachments = image_cell.get("originValue") if isinstance(image_cell, dict) else None
        if not attachments:
            if isinstance(image_cell, dict) and image_cell.get("value"):
                raise SourceError("Cover is present but attachment metadata is missing")
            return None
        if not isinstance(attachments, list) or not isinstance(attachments[0], dict):
            raise SourceError("Cover attachment shape changed")
        token = attachments[0].get("attachmentToken") or attachments[0].get("fileToken")
        record_id = row.get("rec_id", {}).get("value")
        if not token or not record_id:
            raise SourceError("Missing cover attachment token or record ID")
        csrf = next((cookie.value for cookie in self.session.cookies if cookie.name == "_csrf_token"), "")
        data = unwrap(self.session.post(
            self.origin + "/space/api/bitable/vibe/share/auth_attach",
            json={"fieldIDs": [FIELDS["cover"]], "recordIDs": [record_id],
                  "tableID": TABLE_ID, "token": self.base_token, "shareToken": self.share_token},
            headers={"X-CSRFToken": csrf, "Referer": self.share_url}, timeout=30))
        source = data.get(token)
        if not isinstance(source, dict) or not source.get("url"):
            raise SourceError("Cover download authorization unavailable")
        return source


def cell(row, field, original=False):
    value = row.get(FIELDS[field], {})
    if not isinstance(value, dict):
        raise SkipRecord("invalid_source_cell")
    value = value.get("originValue" if original else "value")
    return "" if value is None else str(value).strip()


def normalize(value):
    return unicodedata.normalize("NFKC", value).casefold().strip()


def publication_time(value):
    """Parse source timestamps; Feishu's zone-less display times are China time."""
    if not value:
        return None
    try:
        if re.fullmatch(r"\d{10}|\d{13}", value):
            return datetime.fromtimestamp(int(value) / (1000 if len(value) == 13 else 1), timezone.utc)
        # Date-only/relative text is insufficient to establish that a listing is older.
        if not re.match(r"^\d{4}[-/]\d{2}[-/]\d{2}[ T]\d{2}:\d{2}", value):
            return None
        parsed = datetime.fromisoformat(value.replace("/", "-").replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone(timedelta(hours=8)))).astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def latest_publication_time(conn):
    # Source times, not createdAt: an import can contain months of historical posts.
    posts = conn.execute('''SELECT note FROM "ResellPost"
                            WHERE left(id, 8) = 'goofish_' AND note IS NOT NULL''').fetchall()
    dates = []
    for post in posts:
        match = re.search(r"^Published: (.+)$", post["note"], re.MULTILINE)
        date = publication_time(match[1].strip()) if match else None
        if date is not None:
            dates.append(date)
    return max(dates, default=None)


def contains_name(text, name):
    name = normalize(name)
    if len(name) < 2:
        return False
    # Avoid matching Latin names inside unrelated words (e.g. Rin inside Spring).
    left = r"(?<![a-z0-9])" if name[0].isascii() and name[0].isalnum() else ""
    right = r"(?![a-z0-9])" if name[-1].isascii() and name[-1].isalnum() else ""
    return re.search(left + re.escape(name) + right, normalize(text)) is not None


def match_entity(text, aliases, explicit, valid_ids, kind):
    if explicit is not None:
        if explicit not in valid_ids:
            raise SkipRecord(f"invalid_{kind}_override")
        return explicit
    matches = {entity_id for alias, entity_id in aliases if contains_name(text, alias)}
    if len(matches) != 1:
        return None
    return matches.pop()


@dataclass(frozen=True)
class Listing:
    product_id: str
    title: str
    price: Decimal
    note: str
    url: str
    cover_image_url: str | None = None

    @property
    def post_id(self):
        # Same Goofish product always gets the same primary key, including across share pages.
        return "goofish_" + hashlib.sha256(self.product_id.encode()).hexdigest()[:32]


def cover_image_url(row):
    # Feishu attachment filenames/tokens and encrypted CDN links are not browser images.
    value = row.get(FIELDS["cover"], {})
    if not isinstance(value, dict):
        return None
    candidates = value.get("value")
    candidates = candidates if isinstance(candidates, list) else [candidates]
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        parsed = urlsplit(candidate)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return candidate
    return None


def parse_listing(row, share_url=DEFAULT_SHARE_URL):
    product_id, title = cell(row, "product_id"), cell(row, "title")
    if not re.fullmatch(r"[0-9]{5,30}", product_id) or not title:
        raise SkipRecord("missing_product_id_or_title")
    if re.search(r"出租|求租|求购|收购|已售|已出|rental|wanted", title[:80], re.IGNORECASE):
        raise SkipRecord("not_a_resale")
    try:
        # Use the numeric source column; never guess a sale price from title or rental text.
        price = Decimal(cell(row, "price", original=True))
        if not price.is_finite() or price <= 0 or price > Decimal("9999999999.99"):
            raise InvalidOperation
        price = price.quantize(Decimal("0.01"))
        if price <= 0:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise SkipRecord("missing_or_invalid_price") from None
    note = "\n".join([
        title, f"Seller: {cell(row, 'seller')}", f"Region: {cell(row, 'region')}",
        f"Published: {cell(row, 'published')}", f"Source first seen: {cell(row, 'first_seen')}",
        f"Source: {share_url}", f"Goofish product ID: {product_id}",
    ])
    return Listing(product_id, title, price, note, f"https://www.goofish.com/item?id={product_id}", cover_image_url(row))


def database_url(value):
    # Prisma-specific URL options are not PostgreSQL/libpq connection parameters.
    parsed = urlsplit(value)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key == "schema" and val != "public" for key, val in pairs):
        raise ValueError("This worker currently supports the public PostgreSQL schema only")
    prisma_options = {"schema", "pgbouncer", "connection_limit", "pool_timeout", "socket_timeout"}
    return urlunsplit(parsed._replace(query=urlencode([(k, v) for k, v in pairs if k not in prisma_options])))


def load_catalog(conn, mappings):
    makers = conn.execute('SELECT id, name, name_zh, name_jp FROM "Maker"').fetchall()
    maker_aliases = [(row[key], row["id"]) for row in makers
                     for key in ("name", "name_zh", "name_jp") if row[key]]
    maker_aliases += [(row["name"], row["makerId"]) for row in
                      conn.execute('SELECT name, "makerId" FROM "MakerNickname"').fetchall()]
    characters = conn.execute('SELECT id, name, name_zh, name_jp FROM "Character"').fetchall()
    character_aliases = [(row[key], row["id"]) for row in characters
                         for key in ("name", "name_zh", "name_jp") if row[key]]
    maker_ids, character_ids = {row["id"] for row in makers}, {row["id"] for row in characters}
    for name, entity_id in mappings.get("makerAliases", {}).items():
        if entity_id not in maker_ids:
            raise ValueError("makerAliases references a missing maker ID")
        maker_aliases.append((name, entity_id))
    for name, entity_id in mappings.get("characterAliases", {}).items():
        if entity_id not in character_ids:
            raise ValueError("characterAliases references a missing character ID")
        character_aliases.append((name, entity_id))
    return maker_aliases, character_aliases, maker_ids, character_ids


def insert_listing(conn, listing, maker_id, character_id, user_id):
    # Atomically insert the post and its link. A duplicate never edits existing data.
    with conn.transaction():
        inserted = conn.execute('''
            INSERT INTO "ResellPost"
              (id, "priceRMB", "createdBy", "updatedBy", note, makerid, characterid, "coverImageUrl", status, "createdAt", "updatedAt")
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'FOR_SALE', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT (id) DO NOTHING RETURNING id
        ''', (listing.post_id, listing.price, user_id, user_id, listing.note, maker_id, character_id, listing.cover_image_url)).fetchone()
        if not inserted:
            return False
        conn.execute('''INSERT INTO "ResellPostLink" ("resellpostId", "linkType", link)
                        VALUES (%s, 'GOOFISH', %s)''', (listing.post_id, listing.url))
        return True


def import_rows(conn, rows, user_id, mappings, share_url, write=False, cover_importer=None, limit=None):
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("limit must be a positive integer")
    if not conn.execute('SELECT id FROM "User" WHERE id = %s', (user_id,)).fetchone():
        raise ValueError("IMPORT_USER_ID must identify an existing User")
    catalog = load_catalog(conn, mappings)
    maker_aliases, character_aliases, maker_ids, character_ids = catalog
    existing_ids = {row["id"] for row in conn.execute('SELECT id FROM "ResellPost"').fetchall()}
    existing_links = {row["link"] for row in conn.execute('SELECT link FROM "ResellPostLink"').fetchall()}
    summary, report, seen = Counter(), [], set()
    for row in rows:
        if limit is not None and summary["inserted" if write else "would_insert"] >= limit:
            break
        product_id = cell(row, "product_id")
        try:
            listing = parse_listing(row, share_url)
            if listing.product_id in seen or listing.post_id in existing_ids or listing.url in existing_links:
                summary["existing"] += 1
                continue
            seen.add(listing.product_id)
            override = mappings.get("products", {}).get(listing.product_id, {})
            if override.get("skip"):
                raise SkipRecord("manually_excluded")
            maker_id = match_entity(listing.title, maker_aliases, override.get("makerid"), maker_ids, "maker")
            character_id = match_entity(listing.title, character_aliases, override.get("characterid"), character_ids, "character")
            if write and cover_importer is not None:
                try:
                    listing = replace(listing, cover_image_url=cover_importer(listing, row))
                except Exception as error:
                    # Do not publish a post with a failed cover; retry the listing next run.
                    summary["image_failed"] += 1
                    report.append({"productId": product_id, "status": "skipped", "reason": "image_failed",
                                   "errorType": type(error).__name__})
                    continue
            if write:
                status = "inserted" if insert_listing(conn, listing, maker_id, character_id, user_id) else "existing"
            else:
                status = "would_insert"
            summary[status] += 1
            report.append({"productId": product_id, "status": status, "makerid": maker_id, "characterid": character_id})
        except SkipRecord as error:
            summary[str(error)] += 1
            report.append({"productId": product_id, "status": "skipped", "reason": str(error)})
    return dict(summary), report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Insert new records; default is a read-only dry run")
    parser.add_argument("--full-scan", action="store_true", help="Include older listings for backfills and retries")
    parser.add_argument("--limit", type=int, help="Maximum new posts to insert (or preview); skipped/existing posts do not count")
    parser.add_argument("--env-file", type=Path, help="Load local environment configuration; existing environment variables take precedence")
    parser.add_argument("--import-user-id", help="Existing User.id for audit fields; overrides IMPORT_USER_ID")
    parser.add_argument("--fetch-only", action="store_true", help="Verify source without database access")
    parser.add_argument("--check-image", action="store_true", help="Download/decrypt one cover without database access")
    parser.add_argument("--upload-image", action="store_true", help="With --check-image, also upload that cover to S3")
    parser.add_argument("--mappings", type=Path, default=Path(__file__).with_name("mappings.json"))
    parser.add_argument("--report", type=Path, default=Path("reports/latest.json"))
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be a positive integer")
    if args.limit is not None and (args.fetch_only or args.check_image):
        parser.error("--limit applies only to imports or dry runs")
    if args.write and args.fetch_only:
        parser.error("--write and --fetch-only cannot be combined")
    if args.upload_image and not args.check_image:
        parser.error("--upload-image requires --check-image")
    if args.check_image and (args.write or args.fetch_only):
        parser.error("--check-image cannot be combined with --write or --fetch-only")
    try:
        if args.env_file is not None:
            if not args.env_file.is_file():
                raise ValueError("The specified --env-file does not exist")
            from dotenv import load_dotenv
            load_dotenv(args.env_file, override=False)
        share_url = os.environ.get("FEISHU_SHARE_URL", DEFAULT_SHARE_URL)
        fetcher = FeishuFetcher(share_url)
        rows = fetcher.fetch() if args.check_image or args.fetch_only else None
        if args.check_image:
            from file_utils import CoverImageStore
            store = CoverImageStore()
            for row in rows:
                source = fetcher.get_cover_source(row)
                if source is None:
                    continue
                image = store.prepare(source)
                result = {"productId": cell(row, "product_id"), "decodedBytes": len(image), "uploaded": False}
                if args.upload_image:
                    result["coverImageUrl"] = store.upload(result["productId"], image)
                    result["uploaded"] = True
                print(json.dumps(result))
                return 0
            raise SourceError("No listing has a cover image")
        if args.fetch_only:
            print(json.dumps({"fetched": len(rows), "mode": "fetch-only"}))
            return 0
        import psycopg
        from psycopg.rows import dict_row
        user_id = args.import_user_id or os.environ.get("IMPORT_USER_ID", "")
        if not os.environ.get("DATABASE_URL") or not user_id:
            raise ValueError("Set DATABASE_URL and IMPORT_USER_ID; see .env.example")
        mappings = json.loads(args.mappings.read_text(encoding="utf-8"))
        with psycopg.connect(database_url(os.environ["DATABASE_URL"]), row_factory=dict_row,
                             autocommit=True, connect_timeout=15) as conn:
            cutoff = None if args.full_scan else latest_publication_time(conn)
            rows = fetcher.fetch(cutoff=cutoff)
            cover_importer = None
            if args.write:
                from file_utils import CoverImageStore
                store = CoverImageStore()
                def cover_importer(listing, row):
                    source = fetcher.get_cover_source(row)
                    return store.import_cover(listing.product_id, source) if source else None
            summary, report = import_rows(conn, rows, user_id, mappings, share_url, args.write, cover_importer, args.limit)
        result = {"mode": "write" if args.write else "dry-run", "fetched": len(rows),
                  "publicationCutoff": cutoff.isoformat() if cutoff else None,
                  "limit": args.limit, "summary": summary, "records": report}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in result.items() if key != "records"}))
        return 1 if summary.get("image_failed", 0) else 0
    except Exception as error:
        # Exception messages from database libraries can contain connection credentials.
        if isinstance(error, (SourceError, SkipRecord, ValueError)):
            print(f"Import failed: {error}", file=sys.stderr)
        else:
            print(f"Import failed ({type(error).__name__}); check source/database connectivity and schema", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
