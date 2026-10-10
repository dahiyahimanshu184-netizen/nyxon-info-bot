# NYXON INFO BOT — Railway Pro Build

Telegram bot with PostgreSQL persistence, admin-controlled multi-channel force subscription, referrals/credits, API routing, and Telegram Stars payments.

## Features
- PostgreSQL persistence for users, credits, premium expiry, settings, channels, payments, lookups, and audit events.
- Telegram inline UI and admin control center.
- Admin panel can add/remove multiple force-join channels without code edits.
- New users start with 5 credits; each successful unique referral grants +2 to the referrer once.
- Custom API is the default provider for all users.
- Numverify and Abstract are queried only for admins or users with active premium.
- Credit packs and 30-day premium purchase through Telegram Stars (XTR); successful payment is idempotent by Telegram charge ID.
- Admin tools: user stats, API config status, pricing, credit grants, premium grant/revoke, block/unblock, broadcast.
- API response allowlists to avoid returning arbitrary/private fields from a configured provider.

## 1. Create Telegram bot
1. Use `@BotFather` and create the bot.
2. Copy its token into Railway Variables as `BOT_TOKEN`.
3. Obtain your numeric Telegram user ID and set `ADMIN_IDS` (comma-separated IDs).

## 2. Deploy to Railway
1. Create a **private GitHub repository** and upload this folder's contents. Do not commit `.env` or real keys.
2. In Railway, create a project and deploy the repository.
3. Add a PostgreSQL service to the Railway project.
4. In the bot service's Variables, reference the database URL using Railway's variable reference for the PostgreSQL service (commonly `${{Postgres.DATABASE_URL}}`; use the actual service name).
5. Set `BOT_TOKEN` and `ADMIN_IDS`. Add API keys/endpoints as required below.
6. Railway uses `Procfile` / `railway.json` to run `python bot.py` as a long-running worker.
7. Inspect deployment logs and open the bot in Telegram; send `/start`.

## 3. Variables
Required:
- `BOT_TOKEN`: Telegram bot token.
- `ADMIN_IDS`: numeric Telegram IDs, comma-separated.
- `DATABASE_URL`: PostgreSQL connection URL from Railway.

Custom API:
- `CUSTOM_API_URL`: HTTPS endpoint you are authorized to use.
- `CUSTOM_API_KEY`: API key if required.
- `CUSTOM_API_KEY_HEADER`: default `Authorization`.
- `CUSTOM_API_KEY_PREFIX`: default `Bearer `.
- `CUSTOM_PHONE_PARAM`: default `phone`.
- `CUSTOM_API_METHOD`: `GET` or `POST`; default `GET`.
- `CUSTOM_API_TIMEOUT`: default `15` seconds.

Optional premium providers:
- `NUMVERIFY_KEY`, `NUMVERIFY_URL` (default HTTPS endpoint).
- `ABSTRACT_KEY`, `ABSTRACT_URL`.

## 4. Force-subscription setup
1. Add the bot as an **administrator** in every channel you want to gate.
2. Open bot → Admin Control Center → Force-Join Channels → Add channel.
3. Public channel: send `@channelusername`.
4. Private channel: send `-1001234567890 | https://t.me/+yourInviteLink`.
5. Remove channels from the same panel whenever needed. The configuration is stored in PostgreSQL and survives redeploys.

## 5. Payments (Telegram Stars)
Digital services sold inside Telegram use Telegram Stars (`XTR`). This build includes:
- 30-day premium subscription purchase.
- Lookup credit pack purchase.
- Pre-checkout validation of user, product, currency, and amount.
- Duplicate-charge protection before applying credits/premium.

Default prices are 150 Stars for 30-day premium and 50 Stars for a pack of 50 credits. Admin → Payment Pricing can update prices, subscription duration, and pack size. Before launch, set prices you intend to offer and test payments with Telegram's supported test flow. Review Telegram's current Stars terms and refund requirements for your bot.

## 6. Admin panel
- Force-Join Channels: add/remove/list multiple channels.
- Users & Stats: total users, active premium, lookups, recorded Stars.
- Payment Pricing: change subscription price/duration and credit pack price/size.
- API Config: see which providers are configured (never prints keys).
- Manage User: enter a user ID, then grant credits, grant/revoke premium, or block/unblock.
- Broadcast: sends an admin-entered text message to unblocked users. Use responsibly.
- `/stats`: quick stats command.

## 7. Custom API adapter contract
This package assumes a GET or POST HTTPS endpoint. It sends the phone in `CUSTOM_PHONE_PARAM`; optional auth is sent in the configured header. It only displays allowlisted scalar keys such as `valid`, `country`, `country_code`, `carrier`, `line_type`, `type`, and formatted-number fields. Adapt the request/response mapping to the official API docs if your API differs. Do not configure an endpoint that returns leaked/private subscriber data or data you are not authorized to process.

## 8. Test locally
```bash
python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -r requirements-dev.txt
python -m pytest -q
```
Tests cover phone input validation. Full integration tests require a real PostgreSQL database and a Telegram test bot. Do not use real API keys in tests.

## Operational notes
- Do not put secrets in source code or public repositories.
- PostgreSQL persists settings/credits across Railway deploys; no SQLite volume is needed.
- Keep bot token and API keys private; rotate them if exposed.
- This bot returns phone validation and non-sensitive metadata only. It is not a private-person lookup or leaked-record search tool.

Premium subscriptions bypass the free-credit limit while active; the additional providers are available only to admins and active premium users. Credit packs remain useful for non-premium accounts.

Payment support command: `/paysupport`. Before accepting real payments, publish clear terms, a support contact, and a refund process consistent with Telegram's current Stars rules.
