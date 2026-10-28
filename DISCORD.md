# Discord resell bot every 30 minutes

A scheduled Python process sends one rich embed to a normal Discord text channel, then exits. It uses a bot account through Discord's REST API; no always-on server, Gateway connection, privileged intents, or slash commands are required. It may appear offline between runs.

It selects the oldest never-posted `FOR_SALE` database listing, including manually created posts. It does not check Feishu availability. Embeds show the source title, converted USD / JPY price, maker, character, cover, Goofish listing links when available. Both the embed title and ?View original listing? point to the Goofish listing; both links are omitted when no Goofish link is available. Maker names prefer Chinese, then Japanese, then the default name. Characters display English / Japanese, showing the available name if one is missing. Prices use the latest CNY reference rates from [Frankfurter](https://frankfurter.dev/v1/), rounded to two decimals for USD and whole yen for JPY. Rate lookup failures stop the run before any reservation. When the queue is empty, it sends nothing; it does not recycle listings.

## Discord setup

1. Open <https://discord.com/developers/applications>, create **KiguDB Resell**, and open **Bot**. Generate/reset its token. Keep the token in local `.env` and GitHub secrets; do not paste it into chat or commit it.
2. Under **Installation**, enable **Guild Install**, add the `bot` scope, and select **View Channels**, **Send Messages**, and **Embed Links**. Install the app into your server using the provided install link. Administrator and privileged intents are unnecessary. Use a normal text channel, and make sure its permission overrides allow the bot these permissions.
3. In Discord settings, enable **Advanced → Developer Mode**. Right-click the destination channel and **Copy Channel ID**.
4. Add `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` to local `.env`. The existing `DATABASE_URL` must point to the same database as the website.

## Initialize and preview

From this project in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-discord.txt
.\.venv\Scripts\python.exe discord_bot.py --env-file .env --setup
.\.venv\Scripts\python.exe discord_bot.py --env-file .env
```

`--setup` optionally initializes `.cache/discord-deliveries.sqlite3`; sending and preview also initialize it automatically. The listings database is always read-only. Preview sends nothing. Use `--cache-file PATH` to choose a different cache file. To send the first real post:

```powershell
.\.venv\Scripts\python.exe discord_bot.py --env-file .env --send
```

## Free hosting with GitHub Actions

1. Push this project, including `.github/workflows/discord-hourly.yml`, to the default branch of a **public** GitHub repository. `.env` is ignored; do not commit credentials. Existing public repository setup for the daily importer can be reused.
2. In **Settings → Secrets and variables → Actions → Secrets**, add `DATABASE_URL`, `DISCORD_BOT_TOKEN`, and `DISCORD_CHANNEL_ID`. Reuse the existing database secret if configured. The runner needs network access to the database and Discord.
3. Delivery history is restored and saved automatically using GitHub Actions cache. No database setup is needed.
4. Under Actions **Variables**, set `DISCORD_POSTING_ENABLED` to `true`. This explicitly enables both scheduled and manual runs. Set it to `false` to stop posting.
5. Under **Actions → Discord resell post every 30 minutes → Run workflow**, leave **send** unchecked to preview. Check **send** for a real message. Automatic runs are triggered by the Cloudflare Worker at minutes **07 and 37** of every UTC hour. Follow [Cloudflare setup](cloudflare-cron/README.md) to deploy it. GitHub?s native schedule is disabled; keep the workflow enabled for API dispatch and manual runs.

Standard GitHub-hosted runners are free for public repositories: <https://docs.github.com/en/actions/concepts/billing-and-usage>. Private repositories consume your plan's minutes, shared with the importer; do not assume they will remain free. No additional hosting service is needed; your existing database costs remain separate.

Cloudflare controls the posting schedule, and GitHub Actions executes each dispatched run. GitHub runner queues can still delay execution. The bot has no internal cooldown: each send run can post one unposted listing. Cloudflare dispatches appear as manual/API-triggered workflow runs in GitHub.

## Delivery tracking and recovery

Delivery history lives in `.cache/discord-deliveries.sqlite3` inside this project, ignored by Git. It records each channel/listing pair, reservation time, Discord message ID, and sent time. SQLite locking serializes processes sharing the same file. Each attempt reserves its listing before contacting Discord to prevent duplicate delivery. Price changes do not cause reposts.

GitHub Actions restores the latest cache and saves a new snapshot even after a failed send. Workflow concurrency prevents overlapping Actions runs. Caches can be evicted or deleted; losing the cache loses deduplication history. Local runs and Actions have separate histories unless you copy the cache between them. Use a single runner/history for a channel to avoid duplicates.

Failed or uncertain sends remain reserved with `sent_at` null. Inspect the `deliveries` table with a SQLite viewer and check the Discord channel before removing a reservation to retry. The bot does not automatically retry HTTP errors. Back up the file if delivery history must be retained permanently.

When switching from the previous database delivery log, existing history is not automatically imported. Populate the SQLite cache from the old log before enabling posting if you need to avoid reposting previously shared listings. The old database table is no longer used or modified.

Discord references: <https://discord.com/developers/docs/quick-start/getting-started>, <https://discord.com/developers/docs/resources/message#create-message>.

## English title translation with Groq

Add `GROQ_API_KEY` to local `.env` and to GitHub repository Actions secrets. The workflow passes it to the bot. Optionally set `GROQ_MODEL` in `.env` or as an Actions variable; the default is `openai/gpt-oss-20b`.

Only the first line of the listing note (the original listing title) is sent to Groq for English translation and used as the clickable embed title, limited to 256 characters. The translated note block and source metadata are omitted from the embed; its description contains only the Goofish link. The prompt requires ?\u5934\u58f3 = mask?. Preview also calls Groq. Empty titles need no translation. Failed translations stop before a reservation is saved. Existing Discord posts are not edited.

Translation prompts include available English, Chinese, and Japanese maker, character, media (Origin), and sub-media (SubOrigin) names from the listing?s relations. These references guide translation of names already mentioned in the title, preferring stored English names. Price, maker, and character fields keep their existing formatting.

If the English title exceeds 256 characters, its full translation also appears in the embed body above the Goofish link. If the translation exceeds Discord embed limits, it is attached in full as `translation.txt` instead (the bot then needs Attach Files permission).

The entire listing title is passed to Groq before applying Discord title limits. Chinese names may remain in the result; their presence does not trigger rejection or a retry.
