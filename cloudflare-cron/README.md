# Cloudflare schedule for the Discord bot

Cloudflare triggers the existing GitHub Actions workflow at :07 and :37 each UTC hour. GitHub still runs Python and persists the SQLite cache. No public HTTP endpoint is enabled. GitHub runner queue delays can still occur.

## Deploy

1. Commit and push the project changes to GitHub first. The updated workflow removes GitHub's cron but retains workflow_dispatch and the manual Run workflow button. Keep the GitHub Actions variable DISCORD_POSTING_ENABLED=true.
2. Create a fine-grained GitHub personal access token: https://github.com/settings/personal-access-tokens/new . Select only nosnatef/kigudb-resell-bot and set repository permission Actions to Read and write. Set an expiration and renew the Worker secret before it expires. This token is distinct from the workflow's built-in GITHUB_TOKEN.
3. From the project directory in PowerShell:

```powershell
Set-Location cloudflare-cron
wrangler.cmd login
wrangler.cmd secret put GITHUB_TOKEN
wrangler.cmd deploy
```

Paste the GitHub token at the secret prompt, never into a tracked file. If Wrangler offers to create the Worker when setting its first secret, accept. If you have several Cloudflare accounts, add the intended account_id to wrangler.toml before deployment.

4. Verify Cloudflare Workers & Pages -> kigudb-discord-cron -> Settings -> Triggers shows 7,37 * * * *. Trigger changes may take up to 15 minutes to propagate.
5. Watch Cloudflare logs with `wrangler.cmd tail`. A successful dispatch logs ?GitHub accepted the Discord workflow dispatch?. Then verify the corresponding GitHub Actions run succeeds and Discord receives the post. Cloudflare-triggered runs appear as workflow_dispatch/manual runs by the GitHub token owner, not schedule events.

The Worker only needs its GITHUB_TOKEN secret; database, Groq, and Discord credentials remain in GitHub Actions. Do not disable the GitHub workflow itself, because that would also block Cloudflare dispatches. To pause posting, set DISCORD_POSTING_ENABLED=false in GitHub. To stop dispatching entirely, remove the Cloudflare cron trigger.

## Tests

```powershell
node --test cloudflare-cron/worker.test.mjs
```

Run this from the project root. Tests mock GitHub and send no real messages.

References: https://developers.cloudflare.com/workers/configuration/cron-triggers/ and https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event
