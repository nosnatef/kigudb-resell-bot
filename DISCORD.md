# Discord posting every 10 minutes

Cloudflare dispatches the GitHub Actions workflow every 10 minutes. Each run sends at most one listing to each enabled, enrolled Discord server. GitHub runner queues can delay execution. The bot uses REST and needs no always-on Gateway process.

Each server independently receives the oldest never-reserved `FOR_SALE` listing created at least 24 hours ago and at most one calendar month ago (UTC), ordered by `createdAt` and ID. Manual and imported listings are included. Empty queues send nothing and never recycle listings. Prices use current CNY to USD/JPY rates from Frankfurter.

## Database rollout

The website Prisma schema (`../kigudb-info/prisma/schema.prisma`) defines:

- `ResellPostTranslation`: one shared English title per listing, with model and input hash.
- `DiscordServer`: guild ID, destination channel, enabled flag, latest confirmed post/message/time.
- `DiscordDelivery`: permanent server/listing reservations and confirmed message IDs/times.

Before running the updated worker, apply `sql/discord-state.sql` to the existing PostgreSQL database. This additive, transactional SQL was generated from the Prisma schema diff. It creates only the three new tables, their indexes, and foreign keys; it does not alter listing data. Use your database SQL console or, with a standard PostgreSQL URL in the PowerShell environment:

```powershell
psql "$env:DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/discord-state.sql
```

Apply it once. If these tables already exist, inspect their schema before applying; the script intentionally fails instead of silently accepting an incompatible table. This repository does not apply schema changes automatically. Regenerate the website Prisma client after updating its schema using `npx prisma generate` in the website project.

The worker's `DATABASE_URL` must have read access to listings and write access to the three new tables. It locks listing rows while generating translations; it never changes listing content. Local and Actions runs now share history through PostgreSQL. Use a database endpoint that supports transactions and row locks; transaction pooling is supported.

## Enroll servers

Install the Discord bot into each server with View Channel, Send Messages, Embed Links, and Attach Files permissions for its destination channel. Use server text or announcement channels. Privileged intents and Administrator are unnecessary.

Set `DATABASE_URL` and `DISCORD_BOT_TOKEN` in local `.env`, then enroll each server by its destination channel ID:

```powershell
.\.venv\Scripts\python.exe discord_bot.py --env-file .env --enroll 111111111111111111
```

Enrollment reads the channel's guild ID from Discord and creates or updates its `DiscordServer` row. One destination channel is supported per server. Explicit re-enrollment updates the channel and enables the server, preserving its delivery history. New servers start at the oldest eligible listing.

Set `DiscordServer.enabled = false` in the database to pause a server. Update `channelId` or use `--enroll` to change its destination. Changing the destination preserves server-level deduplication. New rows can also be managed through Prisma; guild and channel snowflakes are strings.

Existing `DISCORD_CHANNEL_IDS` (comma or space separated) and `DISCORD_CHANNEL_ID` remain bootstrap options for `--send` and `--setup`. Unregistered channels are resolved using Discord and enrolled only if their server is new. Bootstrap never re-enables or overwrites existing servers. Remove these variables after moving enrollment management to the database. Runs load every enabled server from the database, including servers absent from the environment.

## Preserve existing delivery history

Before enabling the updated workflow, retain the latest `.cache/discord-deliveries.sqlite3` file. Enroll every channel present in that history, then run:

```powershell
.\.venv\Scripts\python.exe discord_bot.py --env-file .env --setup
```

`--setup` enrolls environment channels and imports legacy history without sending. `--cache-file PATH` chooses another legacy file. Existing `--send` runs also import the file when present. Imports are transactional and repeatable: confirmed messages and uncertain reservations are preserved, missing/deleted listings are skipped, and newer confirmed cache deliveries upgrade older or unconfirmed database rows; older or unconfirmed cache entries never replace confirmed database deliveries. The latest confirmed delivery advances each server's latest-post fields only when its send time is newer, so a restored GitHub Actions cache ahead of PostgreSQL brings the database up to its progress without moving it backward. Unknown legacy channels stop the import before sending; enroll them first. Import every retained channel history before changing its destination.

The Actions workflow keeps the existing cache **restore** step for this transition and imports the restored history before sending. It no longer saves SQLite snapshots. After successful import, PostgreSQL is authoritative; the legacy file/cache can be retired along with the restore step. If the old cache has already been lost, recover it or seed delivery history before enabling posting to avoid reposts.

## Preview and send

Add `GROQ_API_KEY` to `.env` and GitHub Actions secrets. Optional `GROQ_MODEL` defaults to `openai/gpt-oss-20b`. Cache hits do not need a Groq key. Install `requirements-discord.txt`, then:

```powershell
.\.venv\Scripts\python.exe discord_bot.py --env-file .env --check
.\.venv\Scripts\python.exe discord_bot.py --env-file .env
.\.venv\Scripts\python.exe discord_bot.py --env-file .env --send
```

Preview sends nothing and creates no delivery reservations, but populates the shared translation cache. `--check` checks authentication and enabled channel access without translation or sending. `--setup` does not create database tables.

The full first line of the note is translated with available maker, character, media, and sub-media reference names. The cache hash includes source title, reference names, prompt, and model, so changing them refreshes the translation. Empty titles are cached without calling Groq. Failed translations are neither cached nor reserved. Concurrent requests for the same listing serialize around the cache check to avoid duplicate translation calls.

Embeds show the English title, price, maker, character, cover, and Goofish link when available. Titles longer than 256 characters are included in full in the description; translations beyond embed limits are attached as `translation.txt`. Existing Discord messages are never edited.

## Delivery tracking and recovery

A database row lock serializes runs for each server. Busy or disabled servers are skipped. Before contacting Discord, the worker permanently records a `DiscordDelivery` reservation in a separate committed transaction. Successful sends atomically confirm the delivery and update the server's latest post/message/time. A failure in one server does not stop attempts for other enrolled servers; the run reports failure afterward.

An unconfirmed delivery has `sentAt` and `messageId` null. Timeouts, process crashes, and rejected HTTP requests retain that reservation for review, preventing automatic duplicate sends. Later runs can continue with other unreserved posts. Check Discord before deleting an unconfirmed reservation to allow a retry. `latestPostId` tracks confirmed delivery, while the full delivery table supplies deduplication even for uncertain sends and channel changes. Price edits do not trigger reposts.

## GitHub Actions and Cloudflare

Keep `DATABASE_URL`, `DISCORD_BOT_TOKEN`, and `GROQ_API_KEY` in repository Actions secrets. Optional `GROQ_MODEL` is an Actions variable. Set `DISCORD_POSTING_ENABLED=true` to enable runs. No destination secret is needed after enrollment. Under **Discord resell post every 10 minutes**, a manual run previews unless **send** is checked.

Cloudflare dispatches real sends at :00, :10, :20, :30, :40, and :50 each UTC hour. See [Cloudflare setup](cloudflare-cron/README.md). API-triggered runs appear as manual runs in Actions. Workflow concurrency serializes Actions runs; database locks additionally protect against overlapping local/other runners. Each run has a 30-minute timeout for multiple servers; inspect run durations as enrollment grows.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

PostgreSQL integration tests require `TEST_DISCORD_DATABASE_URL` pointing to a disposable database with permission to create/drop schemas. They create an isolated schema per test and mock Discord/Groq, covering caching, invalidation, enrollment, migration, latest-post updates, failure recovery, and concurrent runs. Without this variable they are skipped.
