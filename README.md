# ARQX File Store — Vercel Webhook + Supabase PostgreSQL

This package keeps the existing English bot commands and Telegram-group file storage, but replaces local SQLite and long-running polling with a Vercel HTTP webhook and Supabase PostgreSQL.

## Important limitations
- This is a serverless webhook adapter, not a continuously running worker. Each Telegram update is processed by a Vercel function.
- The `pending_uploads` table makes `/addfile` state survive between separate serverless invocations.
- Vercel and Telegram delivery can retry requests; do not assume a webhook request is processed exactly once.
- The old `store.db` is not imported automatically. Existing catalog metadata must be migrated separately if you need to preserve it. Telegram files in the storage group remain there.

## 1. Rotate the bot token first
The previously uploaded `config.py` contained a bot token. Revoke it through BotFather and create a new token. Do not commit tokens or database passwords.

## 2. Create the Supabase database
1. Create/open a Supabase project.
2. Open **SQL Editor**, paste `schema.sql`, and run it.
3. Open **Connect** and copy the PostgreSQL connection string. For Vercel/serverless, prefer the **Transaction pooler** (port 6543) and use the exact host/username Supabase provides. Set SSL to require.
4. Keep the connection string private. This app uses direct PostgreSQL access with a server-side secret, not the Supabase public Data API.
5. To preserve the existing catalog, put your original `store.db` beside `migrate_sqlite_to_postgres.py` on your own computer, install `psycopg[binary]`, set `DATABASE_URL`, and run `python migrate_sqlite_to_postgres.py`. This copies existing file/share/channel/admin/settings metadata; it does not upload the media again because those files remain in your Telegram storage group. Do not commit `store.db` or the connection string.

## 3. Push this folder to GitHub
Upload `app.py`, `bot.py`, `requirements.txt`, `vercel.json`, `schema.sql`, and `migrate_sqlite_to_postgres.py` to the repository root. Do not upload `store.db`, `config.py`, `.env`, or any token.

## 4. Import into Vercel
1. In Vercel, **Add New → Project**, import the repository.
2. Add these Environment Variables for Production (and Preview only if needed):
   - `BOT_TOKEN` — the newly rotated BotFather token
   - `OWNER_IDS` — comma-separated numeric Telegram user IDs, e.g. `123456789`
   - `ADMIN_IDS` — optional comma-separated initial admin IDs
   - `STORAGE_CHAT_ID` — private storage supergroup ID, usually starts with `-100`
   - `DATABASE_URL` — Supabase PostgreSQL connection string from Connect; use transaction pooler for serverless
   - `WEBHOOK_SECRET` — a random 32-character+ URL-safe secret containing only letters, digits, `_` or `-`
3. Deploy. The health endpoint should respond at `/` with JSON service status.

## 5. Register Telegram webhook
After deployment, replace the placeholders and open this URL once in a browser or use curl. Use the deployed URL without a trailing slash:

```bash
curl -sS -X POST "https://api.telegram.org/bot<NEW_BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://YOUR-PROJECT.vercel.app/api/webhook","secret_token":"YOUR_WEBHOOK_SECRET","allowed_updates":["message","callback_query"]}'
```

Telegram requires `secret_token` to contain 1–256 characters from `A-Z`, `a-z`, `0-9`, `_`, and `-`. The value must exactly match Vercel's `WEBHOOK_SECRET`. Check status with:

```text
https://api.telegram.org/bot<NEW_BOT_TOKEN>/getWebhookInfo
```

Do not share that URL: it contains the bot token.

## 6. Telegram permissions
- Add the bot to the private storage group and grant permissions to post and delete messages.
- Add the bot as an administrator in each Force Join channel/group so membership checks can work.
- The configured owner must open the bot and send `/start` once; this refreshes their private admin command menu.

## 7. Existing commands
Users: `/start`, `/help`
Admins: `/addfile`, `/cancel`, `/listfiles`, `/share FILE_CODE`, `/remove FILE_NAME_OR_CODE`, `/rename OLD_NAME | NEW_NAME`, `/addchannel CHAT_ID INVITE_LINK`, `/removechannel CHAT_ID`, `/listchannel`, `/forcejoin on|off`
Owners: `/addadmin USER_ID`, `/removeadmin USER_ID`, `/listadmins`

## Troubleshooting
- `500`: inspect Vercel Function Runtime Logs; verify all environment variables and Supabase connection string.
- Database connection errors: use the exact transaction pooler host and username copied from Supabase Connect; do not guess the pooler hostname.
- File copy fails: check storage chat ID and bot permissions.
- Force Join fails: ensure the bot is an administrator in each configured channel/group.
- Admin command menu not visible: owner/admin should start the bot privately and send `/start`.
