# Deal scanner

Checks the voted ("hot") deals on mydealz, preisjaeger, dealabs and chollometro and
camelcamelcamel's price drops on Amazon.de, .it, .fr and .es on GitHub (twice an hour by its
schedule, and every 30 minutes when the Google script in skroutz-alerts starts it, since GitHub
skips many scheduled runs), keeps only deals far below their usual price that can reach Greece,
and sends them to Telegram. Words match without accents or case; titles may be German, French,
Spanish, Italian, English or Greek.
pepper.nl, pepper.pl and insomnia.gr offers can be switched on in `sources`; insomnia.gr refuses
GitHub's servers, so its offers reach Telegram through forum email notifications instead.

- Settings: `config.json` (minimum drop, minimum saving, shops, topics, excluded words, sources).
  A deal is kept only if it fits one of the `topics`: by the community's category,
  or by one of the topic's words in its title (camelcamelcamel has no categories).
  It must also name a known brand, be new or like new, and on mydealz/preisjaeger
  have at least `min_temperature` community votes (price errors don't wait for votes);
  posts in the `hot_tier` of votes need a smaller drop. Each topic can set its own
  `min_usual_price_eur`. Below a shop's free-delivery minimum the delivery fee is added
  to the price before the drop is checked.
- `shops`: how each Amazon delivers to Greece, from its "delivery rates to Europe" help page
  (checked 2026-10-25): a fee per parcel plus one per kg, free from `free_from` (Amazon.de, €59)
  or never (`null`: Amazon.it, .fr, .es). The weight comes from the topic's parcel size
  (`forwarding.size`, default small) through `parcel_kg`. `vat_factor` turns the shop's price
  into one with Greek VAT (24%), which Amazon charges for deliveries to Greece.
- `forwarding`: German shops that don't ship to Greece (MediaMarkt, Otto, …) are reachable
  through a parcel forwarder (Forward2me, ColisExpat). Their deals count only for items
  whose usual price is at least `min_usual_price_eur`, and the estimated forwarding cost
  for the topic's parcel size is added before the drop is checked.
- One message per deal: 🟢 reaches Greece directly, 🟡 only through a forwarder (sent without
  a sound), ⚡ a price error. It shows the drop after delivery, community votes and comments,
  the final price with Greek VAT, a warning for bulky parcels, and links to buy, to the
  Amazon reviews, to Skroutz (searched by brand and model), to the price history (Keepa)
  and to the community's discussion. A product is sent once in 30 days, whichever
  community posts it.
- Once a day, on the first run after `daily_status_utc_hour` (UTC), the scanner checks
  itself and sends a message only if something is wrong: a source failed or found nothing,
  or fewer than half of `expected_runs_per_day` runs happened.
- Skroutz isn't scanned (it refuses scripts and GitHub's servers); `skroutz_categories` lists
  the categories checked on request in a browser.
- Telegram credentials are **not** in this repository. On GitHub they come from the
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` repository secrets; locally from
  `secrets.json` (ignored by git).
- `seen.json` remembers which posts and products were already sent; `found_deals.csv` is the
  history; `status.json` is the day of the last self-check. It changes daily, so the run commits
  daily, which keeps GitHub from pausing the schedule (it does after 60 days without commits).

Run locally: `python deal_scanner.py --dry-run` (prints only) or `python deal_scanner.py`.
Check the filters: `python -m unittest -v test_deal_scanner` (GitHub runs this before every scan).
