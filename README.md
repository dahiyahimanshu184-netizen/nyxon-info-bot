# NYXON INFO BOT — Fresh Railway package

## Setup
1. Open `config.py`.
2. Set `BOT_TOKEN` to the token from @BotFather.
3. Replace `ADMIN_IDS` with your numeric Telegram ID (use `/id` after token setup if needed).
4. Paste your Numverify and Abstract API keys in `NUMVERIFY_KEY` and `ABSTRACT_KEY`.
5. Keep `CUSTOM_API_ENABLED = False` until your custom API is deployed or refactored into a local module.
6. Push these files to a private GitHub repository and deploy the repository on Railway.
7. Start command: `python bot.py`.

## Railway
This package intentionally uses `config.py` rather than a `.env` file. The bot token and keys are secrets: keep the repository private, never share screenshots showing them, and rotate any credentials that have been exposed. A secure alternative is Railway Variables.

## Commands
- `/start` — start bot
- `/help` — usage
- `/id` — show your Telegram numeric ID
- `/status` — admin-only configuration status

Send an international phone number such as `+14155552671` for basic validation, country, carrier, and line-type metadata where returned by your providers.

## Custom API note
The uploaded custom `main.py` is a Flask app that sends a request to a third-party website and scrapes its response. It is not a local Python module and has no URL until deployed. This bot only allows basic general metadata from a configured endpoint; it does not relay personal identity, address, IP, IMEI, tracking, or tower-location fields.
