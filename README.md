# Deal scanner

Checks European deal communities (mydealz, preisjaeger, dealabs, chollometro,
pepper.nl, pepper.pl) and camelcamelcamel's Amazon price drops (DE, IT, ES, FR)
every hour, keeps only deals far below their usual price, and sends them to Telegram.

- Settings: `config.json` (minimum drop, minimum saving, shops, topics, excluded words, sources).
  A deal is kept only if it fits one of the `topics`: by the community's category,
  or by one of the topic's words in its title (camelcamelcamel has no categories).
- Telegram credentials are **not** in this repository. On GitHub they come from the
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` repository secrets; locally from
  `secrets.json` (ignored by git).
- `seen.json` remembers which deals were already sent; `found_deals.csv` is the history.

Run locally: `python deal_scanner.py --dry-run` (prints only) or `python deal_scanner.py`.
