# Deal scanner

Checks European deal communities (mydealz, preisjaeger, dealabs, chollometro,
pepper.nl, pepper.pl) and camelcamelcamel's Amazon price drops (DE, IT, ES, FR)
every hour, keeps only deals far below their usual price, and sends them to Telegram.

- Settings: `config.json` (minimum drop, minimum saving, shops, topics, excluded words, sources).
  A deal is kept only if it fits one of the `topics`: by the community's category,
  or by one of the topic's words in its title (camelcamelcamel has no categories).
  It must also name a known brand, be new or like new, and on mydealz/preisjaeger
  have at least `min_temperature` community votes (price errors don't wait for votes);
  posts in the `hot_tier` of votes need a smaller drop. Each topic can set its own
  `min_usual_price_eur`. Below a shop's free-delivery minimum the delivery fee is added
  to the price before the drop is checked.
- `forwarding`: German shops that don't ship to Greece (MediaMarkt, Otto, …) are reachable
  through a parcel forwarder (Forward2me, ColisExpat). Their deals count only for items
  whose usual price is at least `min_usual_price_eur`, and the estimated forwarding cost
  for the topic's parcel size is added before the drop is checked.
- Each message shows the evidence: community votes and comments, record-low claims,
  a price-history link (Keepa) and a link to the product's price in Greece (Skroutz).
- Telegram credentials are **not** in this repository. On GitHub they come from the
  `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` repository secrets; locally from
  `secrets.json` (ignored by git).
- `seen.json` remembers which deals were already sent; `found_deals.csv` is the history.

Run locally: `python deal_scanner.py --dry-run` (prints only) or `python deal_scanner.py`.
