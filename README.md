# KiguDB daily resell importer

For the automated hourly Discord poster and free hosting instructions, see [DISCORD.md](DISCORD.md).

Reads the public [Feishu Goofish radar](https://scn984wlthg8.feishu.cn/share/base/webpage/shrcn19ZKt2MvLp9NSMGrMyPYuh) and inserts new listings into the existing PostgreSQL `ResellPost` / `ResellPostLink` tables. Python 3.12+. No always-on server, Feishu app credentials, or browser required.

## Behavior

- Scans newest publication times first. Uses the latest parseable `Published:` source timestamp in existing worker-imported posts as a cutoff, skipping strictly older listings before matching or uploading images. Stops pagination at a fully dated page older than that cutoff. Equal timestamps and unknown dates encountered during the scan remain eligible. With no usable cutoff, scans all pages. Fetch errors abort before writes; the report includes `publicationCutoff`.
- Uses the numeric RMB price column. Missing/invalid prices and recognizable rental/wanted/sold posts are skipped.
- Matches maker `name`, `name_zh`, `name_jp` and `MakerNickname` aliases, plus character English/Chinese/Japanese names, against the listing title. Apply the maker name columns before running. Maker and character relations are optional. Missing or ambiguous matches import as null, never invented relations; reports include the assigned IDs or null. This is conservative title matching, not semantic classification; review dry-run results.
- Uses a deterministic `goofish_<hash of product ID>` primary key. Database conflict handling prevents repeated daily runs from duplicating a listing. Canonical links also detect existing manual imports. Concurrent manual imports using different IDs are outside this protection.
- New posts are explicitly `FOR_SALE`. Existing `SOLD` and `HIDDEN` posts remain unchanged on repeat imports. Source disappearance does not prove a sale; the source has no explicit availability field. Set sold/hidden states through your database/admin process. Apply the latest frontend schema including `ResellPostStatus` before running this worker.
- Inserts a post and its Goofish link in one database transaction. Existing imported posts are **never updated or deleted**, even if their source price changes or they disappear. All currently visible listings are eligible on the first run; later runs insert only previously unimported products. Previously imported records remain unchanged even if new mappings become available; enrich their relations through your database/admin process. No local checkpoint or GitHub cache is needed.
- Stores source title, seller, region, publication/first-seen text and source URL in `note`.
- Adapts the download/upload pipeline from `C:\Users\Shizi\PycharmProjects\pythonProject\file_utils.py`, using the existing `kigu-db` bucket and `https://d1kkex4vltbo88.cloudfront.net` distribution. For new listings in write mode, authorizes the cover through the public share, decrypts AES-GCM attachments (or downloads plain images), verifies image bytes, converts to WebP, and uploads under `resell-posts/covers/<productId>/<contentHash>.webp`. Only the resulting CloudFront URL is stored in `coverImageUrl`. No user credentials, cookies, or config files from the old project are copied.
- Dry runs and already imported listings never upload images. A missing source cover imports as null. Failed image authorization, decoding, or S3 uploads skip that listing, record `image_failed`, and fail the job after saving the report; the next run retries it. Existing posts, including their cover URLs and statuses, remain unchanged. Unsupported attachment ciphers such as SM4 fail explicitly. A DB failure after upload may leave a reusable content-addressed object; retries do not create random duplicate keys.
- Uses an existing `User.id` for both audit fields. Apply the latest frontend schema with optional maker/character relations and `coverImageUrl` before importing.
- Produces `reports/latest.json` with product IDs, decisions, and skipped reasons. An empty result is a successful no-op.

The public page uses an **undocumented Feishu shared-page API**, verified against this page. Field IDs/table ID are in `worker.py`. Anonymous session cookies are obtained automatically each run. If the owner changes the table or Feishu changes its API, the fetcher may need maintenance. It does not bypass passwords, login requirements, or sharing restrictions.

## Local setup (PowerShell)

```powershell
cd C:\Users\Shizi\Documents\t3kigudb\kigudb-info-worker
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe worker.py --fetch-only

$env:DATABASE_URL = 'postgresql://USER:PASSWORD@HOST:5432/DATABASE?sslmode=require'
$env:IMPORT_USER_ID = 'EXISTING_USER_ID'
$env:AWS_REGION = 'YOUR_EXISTING_BUCKET_REGION'
$env:AWS_ACCESS_KEY_ID = 'YOUR_ACCESS_KEY'
$env:AWS_SECRET_ACCESS_KEY = 'YOUR_SECRET_KEY'
.\.venv\Scripts\python.exe worker.py          # dry run, database reads only
.\.venv\Scripts\python.exe worker.py --write  # inserts new listings
```

`.env.example` documents configuration; the worker reads environment variables. Use `--env-file PATH` to explicitly load an environment file; already-set environment variables take precedence. Use the same database as the frontend. Prisma-specific `schema=public`, `pgbouncer`, `connection_limit`, `pool_timeout`, and `socket_timeout` options are stripped for libpq; non-public schemas are rejected. Use a normal PostgreSQL connection URL, not a Prisma Accelerate URL.

Use `--full-scan` to bypass the publication cutoff for historical backfills, late arrivals, or retries of failed older listings. A newer successful import can put a failed listing behind the next run's cutoff. This also matters when using `--limit`: later runs normally process only listings at or after the newest imported publication time; use `--full-scan` to continue importing the older backlog. Full scans still use duplicate checks and never update existing posts. Manual GitHub Actions runs expose the same option as **full_scan**. `--fetch-only` and `--check-image` always scan the full source.

Before importing, apply the frontend schema from `kigudb-info` using your usual database deployment process (`npx.cmd prisma db push` if that is how you manage this database). The tables and `GOOFISH` enum value must exist. Find an existing audit user with `SELECT id, name FROM "User";` in your database console. The importer never creates users, makers, or characters.

### Local five-post demo

After applying the latest Prisma schema, use the frontend's existing database/AWS configuration without copying credentials:

```powershell
cd C:\Users\Shizi\Documents\t3kigudb\kigudb-info-worker
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# Replace EXISTING_USER_ID with a User.id from your database.
.\.venv\Scripts\python.exe worker.py --env-file ..\kigudb-info\.env --import-user-id EXISTING_USER_ID --limit 5 --write
```

This imports **at most five new posts**, uploads their available covers to S3, and stores the CloudFront URLs. Existing, invalid, manually excluded, or image-failed listings do not count toward five. The importer scans onward until five successful inserts or the source is exhausted. Remove `--write` to preview up to five posts without database/S3 writes. Check the JSON summary and `reports/latest.json`; fewer than five may be inserted if the source has too few eligible new listings. Repeating the command adds up to five **additional** new posts, without duplicating earlier ones.

The selected environment file determines the actual database and S3 destination. `--import-user-id` overrides `IMPORT_USER_ID`; the ID must already exist. No users or placeholder/demo database records are created. The daily workflow omits `--limit` and remains unlimited.

### Resolve unmatched listings

Edit `mappings.json` using actual database IDs:

```json
{
  "makerAliases": {"a seller's spelling of a studio": 123},
  "characterAliases": {"an alternate character name": "existing-character-id"},
  "products": {
    "1095153548801": {"makerid": 123, "characterid": "existing-character-id"},
    "1093006394017": {"skip": true}
  }
}
```

These IDs are illustrative. Keep mappings empty until you know the correct IDs. Per-product overrides resolve ambiguity; aliases supplement automatic matching. Add missing makers/characters through your existing database/admin process, then rerun. Unknown or ambiguous relations are stored as null. Mapping changes affect new imports only; existing posts remain unchanged.

## Free deployment: GitHub Actions

1. Create a GitHub repository for **this worker folder** and push its contents, including `.github/workflows/daily-import.yml`, to the default branch. Do not upload `.env`, virtual environments, or reports.
2. In **Settings → Secrets and variables → Actions**, add repository secrets `DATABASE_URL`, `IMPORT_USER_ID`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, and `AWS_SECRET_ACCESS_KEY`. Add `AWS_SESSION_TOKEN` only for temporary credentials. The IAM identity needs `s3:PutObject` on `arn:aws:s3:::kigu-db/resell-posts/covers/*`; the existing CloudFront origin must be able to read that prefix. The database must accept PostgreSQL connections from GitHub-hosted runners. No Vercel deployment or new database is needed.
3. Open **Actions → Daily resell import → Run workflow**. Leave **write** off for a dry run. Download the `import-report` artifact, review matches, and commit any needed changes to `mappings.json`.
4. Run it manually with **write** checked to verify inserts. The scheduled workflow runs with writes enabled every day at **02:17 UTC** (10:17 China time; 19:17 previous day in Los Angeles during daylight saving, 18:17 in standard time). To postpone automatic writes while configuring it, disable the workflow in Actions and re-enable when ready.
5. Check the next scheduled run and its report. Each job has a 30-minute cap, read-only repository permissions, and concurrency protection. Database and AWS credentials are passed only to the import step. Reports are retained seven days.

[Standard hosted runners are free for public repositories](https://docs.github.com/en/actions/concepts/billing-and-usage). Private repositories on [GitHub Free include 2,000 minutes/month](https://docs.github.com/en/billing/concepts/product-billing/github-actions): this job's 30-minute cap at one run per day is at most 930 scheduled minutes in a 31-day month, plus manual runs and any other Actions usage on your account. Existing database, S3 storage/requests, and CloudFront traffic charges remain separate.

[Scheduled workflows run from the default branch, can be delayed, and public repositories can have schedules disabled after 60 days without activity](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule). This is daily best-effort scheduling, not an exact-time service. Keep Actions notifications enabled to see failures.

## Verify images independently

With AWS credentials supplied through environment variables, an existing AWS profile, or an IAM role:

```powershell
.\.venv\Scripts\python.exe worker.py --check-image
.\.venv\Scripts\python.exe worker.py --check-image --upload-image
```

The first command authorizes, downloads, decrypts, validates and converts one live cover without S3 or database writes. The second uploads that cover to S3 and prints its public CloudFront URL, also without database writes. Normal `--write` imports do this automatically for every new listing that has a cover. `S3_BUCKET` and `S3_PUBLIC_BASE_URL` can override the defaults. S3 objects have `image/webp` content type and immutable cache headers, and no public ACL is requested. This relies on your existing private-bucket/CloudFront access configuration. The image module uses bounded downloads (10 MB), at most 25 million decoded pixels, and thumbnails up to 1600 pixels; it strips original metadata during re-encoding.

## Validation

Verified 21 tests plus a live Feishu cover authorization/download/AES-GCM decode/WebP conversion, S3 upload, and CloudFront HTTP 200 response (`image/webp`). The live image smoke test makes no database changes.

`python -m unittest discover -s tests -v` checks source pagination, duplicate runs, no updates on price changes, dry runs, transaction rollback behavior with an in-memory database fake, name ambiguity, optional relations, product overrides, cover URL handling, invalid prices, and Prisma URL compatibility. It does not replace a real database integration test. `--fetch-only` verifies the real source without database access; a manual dry run then validates your database configuration and matching coverage. Image tests cover authenticated decryption, upload parameters, upload failures, image validation, and suppression of uploads for dry runs or duplicate listings.
