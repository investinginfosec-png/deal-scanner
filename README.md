# Deal scanner

Checks European deal communities (mydealz, preisjaeger, dealabs, chollometro,
pepper.nl, pepper.pl) and camelcamelcamel's Amazon price drops (DE, IT, ES, FR)
every hour, keeps only deals far below their usual price, and sends them to Telegram.

- Settings: `config.json` (minimum drop, minimum saving, excluded words, sources).
- Telegram credentials are **not** in this repository. On GitHub they come from the
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` repository secrets; locally from
  `secrets.json` (ignored by git).
- `seen.json` remembers which deals were already sent; `found_deals.csv` is the history.

Run locally: `python deal_scanner.py --dry-run` (prints only) or `python deal_scanner.py`.
