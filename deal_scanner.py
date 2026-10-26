"""Deal scanner: finds deals far below their usual price and sends them to Telegram.

Usage:
    python deal_scanner.py            scan, send new deals, remember them
    python deal_scanner.py --dry-run  scan and print only (sends nothing, remembers nothing, not even prices)
    python deal_scanner.py --setup    find your Telegram chat id and send a test message

Settings live in config.json next to this file. The Telegram token and chat id are
never stored there: they come from the TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
environment variables (GitHub Actions secrets) or from a local secrets.json.
"""
import argparse
import csv
import html
import json
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlsplit

import requests

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
SECRETS = HERE / "secrets.json"  # local only, listed in .gitignore
STATE = HERE / "seen.json"
FOUND_LOG = HERE / "found_deals.csv"
STATUS = HERE / "status.json"  # the day the last daily status message was sent
PRICES = HERE / "prices.json"  # delivered prices seen per Amazon product, by day
RUN_LOG = HERE / "scanner.log"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Deal communities (Pepper network). Each deal carries its price and the
# cheapest price elsewhere ("nextBestPrice"), which we use as the usual price.
# mydealz, preisjaeger, dealabs and chollometro are read from their "hot" lists (deals the
# community voted over 100°), since only those can reach the config's min_temperature.
PEPPER_SITES = {
    "mydealz":     ("🇩🇪", "https://www.mydealz.de/hot", "€"),
    "preisjaeger": ("🇦🇹", "https://www.preisjaeger.at/", "€"),
    "dealabs":     ("🇫🇷", "https://www.dealabs.com/hot", "€"),
    "chollometro": ("🇪🇸", "https://www.chollometro.com/populares", "€"),
    "pepper_nl":   ("🇳🇱", "https://nl.pepper.com/nieuw", "€"),
    "pepper_pl":   ("🇵🇱", "https://www.pepper.pl/nowe", "zł"),
}

# camelcamelcamel Amazon price drops: "recent" is the live stream,
# "t=daily" is the day's top drops (so hourly runs don't miss the big ones).
CAMEL_SITES = {
    "camel_de": ("🇩🇪", "Amazon.de", "https://de.camelcamelcamel.com/top_drops/feed"),
    "camel_it": ("🇮🇹", "Amazon.it", "https://it.camelcamelcamel.com/top_drops/feed"),
    "camel_es": ("🇪🇸", "Amazon.es", "https://es.camelcamelcamel.com/top_drops/feed"),
    "camel_fr": ("🇫🇷", "Amazon.fr", "https://fr.camelcamelcamel.com/top_drops/feed"),
}
CAMEL_LISTS = ["", "?t=daily"]

# Skroutz isn't scanned: it refuses scripts and GitHub's servers (HTTP 403). Its deals are
# checked on request in a browser, from the config's skroutz_categories.

TO_EUR = {"€": 1.0, "zł": 0.23}

# Monthly prices (phone contracts, subscriptions) compared against a device's
# full price would look like 95% drops, so they are skipped.
MONTHLY = re.compile(
    r"mtl\b|monatl|/\s*monat|pro monat|im monat|vertrag|tarif|"
    r"al mese|/\s*mese|par mois|/\s*mois|forfait|al mes\b|/\s*mes\b|"
    r"per maand|/\s*maand|p/m\b|abonnement|miesięcznie|/\s*mies|abonament",
    re.IGNORECASE)

PRICE_ERROR = re.compile(
    r"preisfehler|fehlerpreis|price error|prijsfout|error de precio|"
    r"erreur de prix|errore di prezzo|błąd cenowy",
    re.IGNORECASE)

# Posters mark record lows in the title; shown as evidence, not used as a filter.
RECORD_LOW = re.compile(
    r"bestpreis|tiefstpreis|allzeittief|all[- ]time[- ]low|niedrigster preis|"
    r"prix le plus bas|plus bas historique|mínimo histórico|precio mínimo|minimo storico|prezzo minimo",
    re.IGNORECASE)

AMAZON_ASIN = re.compile(r"amazon\.[a-z.]+/(?:[^/?]+/)?(?:dp|gp/product)/([A-Z0-9]{10})")
KEEPA_DOMAINS = {"amazon.de": 3, "amazon.fr": 4, "amazon.it": 8, "amazon.es": 9}  # Keepa's number for each Amazon
AMAZON_SHORT_HOSTS = {"amzn.eu", "amzn.to"}

# What a deal's usual price is: the communities give the cheapest other shop or the poster's
# stated discount, camelcamelcamel the product's previous price (often a dearer third-party seller's).
REF_KINDS = {"next_shop": "στο επόμενο κατάστημα", "poster": "κατά τον αναρτήσαντα",
             "camel": "πριν (camelcamelcamel)"}

# Keyboards come with the layout of the country they are sold in.
LAYOUTS = {"de": "γερμανική QWERTZ", "at": "γερμανική QWERTZ", "fr": "γαλλική AZERTY", "es": "ισπανική",
           "it": "ιταλική"}
FLAG_COUNTRY = {"🇩🇪": "de", "🇦🇹": "at", "🇫🇷": "fr", "🇪🇸": "es", "🇮🇹": "it"}

HISTORY_DAYS = 90  # how long prices.json keeps a price
LATELY_DAYS = 30  # EU rule: a discount counts against the lowest price of the last 30 days


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M} {msg}"
    print(line)
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def fetch(session, url):
    r = session.get(url, timeout=30)
    if r.status_code == 429:  # rate limited: wait once and retry
        time.sleep(60)
        r = session.get(url, timeout=30)
    r.raise_for_status()
    # be polite between requests; camelcamelcamel blocks bursts of requests
    time.sleep(15 if "camelcamelcamel" in url else 3)
    return r.text


def parse_money(text):
    """"1.299,90", "1,299.90", "1.299", "79,99" or "79.99" → number: a separator followed by
    exactly 3 digits groups thousands, any other is the decimal point."""
    s = re.sub(r"[^\d,.]", "", text)
    last = max(s.rfind(","), s.rfind("."))
    if last >= 0 and len(s) - last - 1 != 3:
        s = re.sub(r"[,.]", "", s[:last]) + "." + s[last + 1:]
    else:
        s = re.sub(r"[,.]", "", s)
    return float(s) if s else None


def pepper_deals(session, name, cfg):
    flag, url, cur = PEPPER_SITES[name]
    deals = []
    for page in range(1, cfg["pepper_pages"] + 1):
        page_html = fetch(session, url + (f"?page={page}" if page > 1 else ""))
        for blob in re.findall(r"data-vue3='([^']*)'", page_html):
            try:
                th = json.loads(html.unescape(blob)).get("props", {}).get("thread")
            except (ValueError, AttributeError):
                continue
            if not th or th.get("type") != "Deal" or th.get("isExpired") or th.get("isLocal"):
                continue
            price = th.get("price") or 0
            ref = th.get("nextBestPrice") or 0
            pct = th.get("percentage") or 0
            ref_kind = "next_shop"
            if ref <= price and 0 < pct < 100 and price > 0:
                ref, ref_kind = price / (1 - pct / 100), "poster"  # discount stated by the poster
            title = html.unescape(th.get("title", ""))
            is_error = bool(PRICE_ERROR.search(title))
            if (ref <= price or MONTHLY.search(title)) and not is_error:
                continue
            deals.append({
                "key": f"{name}:{th['threadId']}",
                "flag": flag,
                "where": (th.get("merchant") or {}).get("merchantName") or th.get("linkHost") or name,
                "source": name,
                "title": title,
                "price": price,
                "ref": ref if ref > price else None,
                "ref_kind": ref_kind,
                "cur": cur,
                "price_error": is_error,
                "url": th.get("shareableLink") or url,
                "visit_url": f"https://{urlsplit(url).netloc}/visit/threadmain/{th['threadId']}",
                "shop_host": th.get("linkHost") or "",
                "category": (th.get("mainGroup") or {}).get("threadGroupUrlName") or "",
                "temperature": th.get("temperature") or 0,
                "comments": th.get("commentCount") or 0,
            })
    return deals


CAMEL_TITLE = re.compile(r"^(.*) - down ([\d.]+)% \(.*?\) to (.+?) from (.+)$")


INSOMNIA_RSS = "https://www.insomnia.gr/rss/16-offers.xml/"
NUMBER = r"\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"  # 1.049,90 · 129 · 79,99
PRICE_IN_TEXT = re.compile(rf"({NUMBER})\s*€|€\s*({NUMBER})")


def insomnia_deals(session, name, cfg):
    """Offers posted by insomnia.gr members, mostly in Greek shops. They are free text with no
    usual price, so only the topic, brand and condition filters apply to them."""
    root = ET.fromstring(fetch(session, INSOMNIA_RSS))
    oldest = datetime.now(timezone.utc) - timedelta(days=cfg["insomnia_days"])
    deals = []
    for item in root.iter("item"):
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", ""))
        except (TypeError, ValueError):
            continue
        if published < oldest:  # the feed reaches back weeks; old offers have usually ended
            continue
        title = html.unescape(item.findtext("title", "")).strip()
        link = item.findtext("link", "")
        price = PRICE_IN_TEXT.search(title)
        deals.append({
            "key": f"insomnia:{link.rsplit('/topic/', 1)[-1].split('-', 1)[0]}",
            "flag": "🇬🇷",
            "where": "insomnia.gr",
            "source": "προσφορές μελών",
            "title": title,
            "price": parse_money(price.group(1) or price.group(2)) if price else None,
            "ref": None,
            "cur": "€",
            "price_error": bool(PRICE_ERROR.search(title)),
            "url": link,
            "local": True,
            "curated": True,
        })
    return deals


def camel_deals(session, name, cfg):
    flag, store, url = CAMEL_SITES[name]
    deals = []
    for suffix in CAMEL_LISTS:
        root = ET.fromstring(fetch(session, url + suffix))
        for item in root.iter("item"):
            m = CAMEL_TITLE.match(item.findtext("title", ""))
            if not m:
                continue
            title, _, now, before = m.groups()
            price, ref = parse_money(now), parse_money(before)
            if price is None or not ref or ref <= price:
                continue
            link = item.findtext("link", "")
            asin = link.rsplit("/", 1)[-1]
            deals.append({
                "key": f"{name}:{asin}:{price}",
                "flag": flag,
                "where": store,
                "source": "camelcamelcamel",
                "title": title,
                "price": price,
                "ref": ref,
                "ref_kind": "camel",
                "cur": "€",
                "price_error": False,
                # camelcamelcamel pages often block phone browsers, so link the product itself
                "url": f"https://www.{store.lower()}/dp/{asin}",
                "extra_url": link,
                "shop_host": store.lower(),
            })
    return deals


def redirect_target(session, url):
    """Where a link redirects to, read from its Location header (not followed)."""
    try:
        return session.get(url, allow_redirects=False, timeout=30).headers.get("Location", "")
    except requests.RequestException:
        return ""


def add_shop_links(session, deals):
    """Point community deals straight at the shop, read from the site's redirect (not followed)."""
    for d in deals:
        if "visit_url" not in d:
            continue
        target = redirect_target(session, d.pop("visit_url"))
        shop = parse_qs(urlsplit(target).query).get("url", [target])[0]
        if urlsplit(shop).netloc in AMAZON_SHORT_HOSTS:  # amzn.eu/… → amazon.de/dp/…
            shop = redirect_target(session, shop) or shop
        if shop.startswith("http") and urlsplit(shop).netloc != urlsplit(d["url"]).netloc:
            d["extra_url"] = d["url"]  # the community's page, linked as the discussion
            d["url"] = shop
        time.sleep(1)


def resolve_short_shops(session, deals):
    """Amazon short links hide which Amazon sells the deal, so look it up before filtering."""
    short = [d for d in deals if d.get("shop_host") in AMAZON_SHORT_HOSTS]
    add_shop_links(session, short)
    for d in short:
        d["shop_host"] = urlsplit(d["url"]).netloc


def product_key(deal):
    """The product a deal is for: its Amazon id, or else the first words of its title."""
    asin = AMAZON_ASIN.search(deal["url"])
    if asin:
        return asin.group(1)
    return " ".join(re.sub(r"[\[\(].*?[\]\)]", " ", plain(deal["title"])).split()[:6])


def unique_products(deals):
    """One message per product when several communities post it; the most voted post is kept."""
    kept = {}
    for d in sorted(deals, key=lambda d: -d.get("temperature", 0)):
        kept.setdefault(product_key(d), d)
    return [d for d in deals if any(d is k for k in kept.values())]


def add_amazon_de_prices(deals, prices):
    """Remember the Amazon.de prices among `deals` (product id → lowest price).

    Amazon's terms forbid robots on its pages and camelcamelcamel's product pages refuse
    scripts, so Amazon.de prices come only from what the sources list in the same run.
    """
    for d in deals:
        asin = AMAZON_ASIN.search(d["url"])
        if asin and d["price"] and urlsplit(d["url"]).netloc.removeprefix("www.") == "amazon.de":
            prices[asin.group(1)] = min(d["price"], prices.get(asin.group(1), d["price"]))


def cheaper_on_amazon_de(deal, de_prices, cfg):
    """Whether Amazon.de sells an Amazon.it/.fr/.es deal's product for less, delivered to Greece.

    The Amazon.de cost is kept on the deal, to show it in the message when it is dearer.
    """
    asin = AMAZON_ASIN.search(deal["url"])
    host = urlsplit(deal["url"]).netloc.removeprefix("www.")
    if not asin or host == "amazon.de" or host not in cfg["shops"] or asin.group(1) not in de_prices:
        return False
    free_from, (per_parcel, per_kg), _, vat = shop_terms({"shop_host": "amazon.de"}, cfg)
    price = de_prices[asin.group(1)]
    deal["amazon_de_cost"] = price * vat + (0 if price >= free_from else per_parcel + per_kg * deal["parcel_kg"])
    return deal["amazon_de_cost"] <= deal["price"] * deal["vat_factor"] + deal["delivery"]


def landed_cost(deal):
    """What a deal costs delivered to Greece, with Greek VAT, in euros (once is_big priced its delivery)."""
    return deal["price"] * deal["vat_factor"] * TO_EUR.get(deal["cur"], 1.0) + deal["delivery"]


def load_prices(today):
    """prices.json without days older than HISTORY_DAYS: product id → {day: lowest delivered price}."""
    history = json.loads(PRICES.read_text(encoding="utf-8")) if PRICES.exists() else {}
    oldest = (today - timedelta(days=HISTORY_DAYS)).isoformat()
    history = {asin: {day: p for day, p in days.items() if day >= oldest} for asin, days in history.items()}
    return {asin: days for asin, days in history.items() if days}


def save_prices(history):
    """One product per line, so a run's commit changes only the lines of the products it saw."""
    lines = [f"{json.dumps(asin)}: {json.dumps(days, sort_keys=True)}" for asin, days in sorted(history.items())]
    PRICES.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8")


def remember_prices(deals, history, today, cfg):
    """Keep the day's lowest delivered price of each Amazon product among the deals is_big priced.

    Price errors are left out (shops often cancel them), and so are used items: a used price
    must not make a new item's deal look like no deal.
    """
    for d in deals:
        asin = AMAZON_ASIN.search(d["url"])
        if (asin and d["price"] and "delivery" in d and not d["price_error"]
                and not mentions(d["title"], cfg["used_words"])):
            cost = round(landed_cost(d), 2)
            days = history.setdefault(asin.group(1), {})
            days[today.isoformat()] = min(cost, days.get(today.isoformat(), cost))


def cheaper_lately(deal, history, today):
    """Whether the deal's product cost less, delivered, in the last LATELY_DAYS days: then it is no
    real drop. The history only holds prices the sources listed, so it can miss lower ones.

    Also keeps on the deal what the message says about the history: a new low since the first
    day seen, or a lower price longer ago. Today's prices may be the deal's own, so a price only
    counts as lower or higher when 2% away.
    """
    asin = AMAZON_ASIN.search(deal["url"])
    days = history.get(asin.group(1), {}) if asin else {}
    if not days or deal["price"] is None:
        return False
    cost = landed_cost(deal)
    lately = (today - timedelta(days=LATELY_DAYS)).isoformat()
    if any(p < cost * 0.98 for day, p in days.items() if day >= lately):
        return True
    before = {day: p for day, p in days.items() if day < today.isoformat()}
    if not before:
        return False
    low_day, first = min(before, key=before.get), date.fromisoformat(min(before))
    if before[low_day] > cost * 1.02 and (today - first).days >= 7:
        deal["history_note"] = f"📉 φθηνότερο από κάθε τιμή που είδαμε από {first:%d/%m}"
    elif before[low_day] < cost * 0.98:
        deal["history_note"] = f"ℹ️ ήταν {fmt_price(before[low_day], '€')} στις {date.fromisoformat(low_day):%d/%m}"
    return False


def shop_terms(deal, cfg):
    """How the deal's shop reaches Greece: (order value for free delivery, delivery rate below it
    as (per parcel, per kg), forwarded, price factor for Greek VAT).

    German shops that don't ship to Greece go through a parcel forwarder, so their delivery
    is never free and its fee depends on the parcel's size. None if the shop can't reach Greece.
    """
    if deal.get("local"):
        return 0, None, False, 1.0
    host = deal.get("shop_host", "").lower().removeprefix("www.")
    is_host = lambda domain: host == domain or host.endswith("." + domain)
    for domain, terms in cfg["shops"].items():
        if is_host(domain):
            free_from = float("inf") if terms["free_from"] is None else terms["free_from"]  # None: never free
            return free_from, (terms["per_parcel_eur"], terms["per_kg_eur"]), False, terms.get("vat_factor", 1.0)
    if any(is_host(domain) for domain in cfg["forwarding"]["shops"]):
        return float("inf"), None, True, 1.0  # bought with German VAT, then forwarded
    return None


def plain(text):
    """Lowercase without accents, so "Ακουστικά", "ΑΚΟΥΣΤΙΚΑ" and "ακουστικα" match alike."""
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if not unicodedata.combining(c))


def mentions(text, words):
    text = plain(text)
    return any(plain(w) in text for w in words)


def matching_topic(deal, cfg):
    """Name of the chosen topic the deal fits, by its community category or else a word in its title."""
    for name, topic in cfg["topics"].items():
        if deal.get("category") in topic.get("categories", []):
            return name
    for name, topic in cfg["topics"].items():
        if mentions(deal["title"], topic.get("words", [])):
            return name
    return None


def has_known_brand(title, cfg):
    """Whether the title names one of the config's brands, as a whole word."""
    return any(re.search(rf"(?<!\w){re.escape(plain(b))}(?!\w)", plain(title)) for b in cfg["brands"])


def good_condition(title, cfg):
    """New items pass; used or refurbished ones only when the title says they are like new."""
    return not mentions(title, cfg["used_words"]) or mentions(title, cfg["like_new_words"])


def keyboard_layout(deal, cfg):
    """The layout a keyboard from a foreign shop most likely has (by the shop's country), or None."""
    if deal.get("local") or not mentions(deal["title"], cfg["keyboard_words"]) \
            or mentions(deal["title"], cfg["keyboard_layout_ok_words"]):
        return None
    host = urlsplit(deal["url"]).netloc or deal.get("shop_host", "")
    return LAYOUTS.get(host.rsplit(".", 1)[-1]) or LAYOUTS.get(FLAG_COUNTRY.get(deal["flag"]))


def is_big(deal, cfg):
    if mentions(f"{deal['title']} {deal['where']}", cfg.get("exclude_words", [])):
        return False
    if not has_known_brand(deal["title"], cfg) or not good_condition(deal["title"], cfg):
        return False
    terms = shop_terms(deal, cfg)
    if terms is None:
        return False
    deal["free_from"], deal["delivery_rate"], deal["forwarded"], deal["vat_factor"] = terms  # all shown in the message
    deal["topic"] = matching_topic(deal, cfg)
    if not deal["topic"]:
        return False
    forwarding = cfg["forwarding"]
    size = forwarding["size"].get(deal["topic"], "small")
    if deal["forwarded"]:
        delivery = forwarding["cost_eur"][size]
    elif deal["delivery_rate"]:  # the shop charges per parcel and per kg; the topic says how heavy it usually is
        deal["parcel_kg"] = cfg["parcel_kg"][size]
        delivery = deal["delivery_rate"][0] + deal["delivery_rate"][1] * deal["parcel_kg"]
    else:
        delivery = 0
    # big parcels may cost extra to send to Greece even above the free-delivery minimum
    deal["bulky"] = size != "small" and not deal.get("local")
    deal["delivery"] = delivery if deal["price"] is not None and deal["price"] < deal["free_from"] else 0
    if deal.get("curated"):
        return True  # Greek community picks state no usual price: topic, brand and condition decide
    if deal["price_error"] and not deal["forwarded"]:  # forwarded orders arrive too late for price errors
        return True  # worth it even with a delivery fee, and too short-lived to wait for votes
    if deal.get("temperature", cfg["min_temperature"]) < cfg["min_temperature"]:
        return False  # not (yet) approved by the community
    rate = TO_EUR.get(deal["cur"], 1.0)
    min_usual = cfg["topics"].get(deal["topic"], {}).get("min_usual_price_eur", cfg["min_usual_price_eur"])
    if deal["forwarded"]:  # forwarding only pays off on expensive items
        min_usual = max(min_usual, cfg["forwarding"]["min_usual_price_eur"])
    if not deal["ref"] or deal["ref"] * rate < min_usual:
        return False
    cost = deal["price"] + deal["delivery"] / rate  # the delivery fee eats into the discount
    drop = 1 - cost / deal["ref"]
    saving_eur = (deal["ref"] - cost) * rate
    # posts hundreds vote for are often record lows even when other shops are only a bit dearer
    tier = cfg["hot_tier"] if deal.get("temperature", 0) >= cfg["hot_tier"]["min_temperature"] else cfg
    return drop * 100 >= tier["min_drop_percent"] and saving_eur >= cfg["min_saving_eur"]


def fmt_price(value, cur):
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + cur


UNIT = re.compile(r"\d+(?:[.,]\d+)?-?(?:tlg|teilig|stk|stück|l|ml|w|kw|wh|kwh|mah|cm|mm|m|kg|g|zoll|v)\.?", re.IGNORECASE)


def search_name(title, cfg):
    """Brand and model from a deal's title ("Ninja CRISPi FN101"), to look the product up in Greek shops."""
    text = re.sub(r"[\[\(].*?[\]\)]", " ", title)
    text = re.sub(r"\S*(?:\.\.\.|…).*", " ", text)  # camelcamelcamel cuts long titles, mid-word: "Ultra ARG...Sync"
    text = re.split(r"\s(?:für|ab|bei|um|nur|statt|mit|inkl\.?|pour|avec|dès|para|con|desde|per|da)\s|\s[|/–-]\s|[:,;]",
                    text, maxsplit=1)[0]
    words = text.split()
    brands = {plain(b) for b in cfg["brands"]}
    words = words[next((i for i, w in enumerate(words) if plain(w) in brands), 0):]
    name = words[:1]
    for before, word in zip(words, words[1:]):
        if len(name) >= 4:
            break
        if not re.search(r"[A-Za-z0-9]", word) or UNIT.fullmatch(word) or re.fullmatch(r"[IVX]+", word):
            continue
        if re.fullmatch(r"\d{1,2}", word):  # a generation number goes with the word before it: Pocket 3
            name += [word] if before in name else [before, word]
        elif re.search(r"\d", word):  # a model code, without colour/region letters: FN101EUSTGD → FN101
            name.append(re.sub(r"^([A-Za-z]{1,4}-?\d{2,5})[A-Za-z]{4,}$", r"\1", word))
        elif word.isupper() or not (word.islower() or word.istitle()):  # product lines: CRISPi, PRO, G
            name.append(word)
    return " ".join(name if len(name) > 1 else words[:3])


def format_deal(d, cfg):
    """One deal as a message: verdict and price, title, evidence, delivery, links.

    🟢 reaches Greece directly, 🟡 only through a parcel forwarder, ⚡ is a price error.
    """
    cur, rate = d["cur"], TO_EUR.get(d["cur"], 1.0)
    delivery = d.get("delivery") or 0
    icon = "⚡" if d["price_error"] else "🟡" if d.get("forwarded") else "🟢"
    if d["ref"] and d["price"] is not None:
        final = round((1 - (d["price"] + delivery / rate) / d["ref"]) * 100)  # after delivery
        fee = f" + {fmt_price(delivery, '€')} {'προώθηση' if d.get('forwarded') else 'μεταφορικά'}" if delivery else ""
        kind = REF_KINDS.get(d.get("ref_kind"), "")
        head = (f"{icon} <b>−{final}%</b> · <b>{fmt_price(d['price'], cur)}</b>{fee} αντί {fmt_price(d['ref'], cur)}"
                + (f" {kind}" if kind else ""))
    elif d["price"] is None:
        head = f"{icon} <b>προσφορά</b>"
    else:
        head = f"{icon} <b>{fmt_price(d['price'], cur)}</b>"
    if d["price"] == 0:
        head += " · <b>ΔΩΡΕΑΝ</b>"
    if d["price_error"]:
        head += " · ⚠️ <b>λάθος τιμής</b>"

    if d.get("temperature"):
        evidence = [f"👍 {round(d['temperature'])}° στο {d['source']}"
                    + (f", {d['comments']} σχόλια" if d.get("comments") else "")]
    elif d.get("local"):
        evidence = [f"{d['flag']} {html.escape(d['where'], quote=False)} · {d['source']}"]
    else:
        evidence = [f"📉 {d['source']}"]
    if RECORD_LOW.search(d["title"]):
        evidence.append("χαμηλότερη τιμή κατά τον αναρτήσαντα")
    if d.get("history_note"):
        evidence.append(d["history_note"])
    if d.get("topic"):
        evidence.append(f"🏷️ {html.escape(d['topic'], quote=False)}")

    shop = (d.get("shop_host") or urlsplit(d["url"]).netloc).removeprefix("www.")
    if d.get("forwarded"):
        delivery_line = f"📦 {shop}: μόνο μέσω προώθησης (Forward2me / ColisExpat), 1–2 εβδομάδες"
    elif d.get("local"):
        delivery_line = ""
    elif d.get("free_from") == float("inf"):
        per_parcel, per_kg = d["delivery_rate"]
        kg = d["parcel_kg"]
        delivery_line = (f"🚚 {shop}: χωρίς δωρεάν μεταφορικά για Ελλάδα ({fmt_price(per_parcel, '€')} + "
                         f"{fmt_price(per_kg, '€')}/κιλό, υπολογισμένα για ~{kg} {'κιλό' if kg == 1 else 'κιλά'})")
    else:
        delivery_line = f"🚚 {shop}: " + (f"δωρεάν μεταφορικά από {d['free_from']}€" if delivery
                                          else "δωρεάν μεταφορικά για Ελλάδα")
    if delivery_line and not d.get("forwarded"):
        if d.get("vat_factor", 1.0) != 1.0 and d["price"]:
            landed = d["price"] * d["vat_factor"] + delivery / rate
            delivery_line += f" · τελικό με ελληνικό ΦΠΑ ≈ {fmt_price(landed, cur)}"
        if d.get("bulky"):
            delivery_line += " · ⚠️ ογκώδες: δες τα μεταφορικά στο καλάθι· και η επιστροφή μπορεί να κοστίσει δεκάδες €"
        if d.get("amazon_de_cost"):
            delivery_line += f" · 🇩🇪 στο Amazon.de ≈ {fmt_price(d['amazon_de_cost'], '€')}"
    if d.get("forwarded"):
        delivery_line += ("\n↩️ επιστροφή στη Γερμανία με δικά σου έξοδα · νόμιμη εγγύηση μέσω του γερμανικού "
                          "καταστήματος · ο γερμανικός ΦΠΑ δεν συμψηφίζεται")
    barebone = ("⚠️ <b>barebone</b>: χωρίς RAM και δίσκο (συνήθως και χωρίς Windows), πρόσθεσε το κόστος τους"
                if mentions(d["title"], cfg["barebone_words"]) else "")
    layout = keyboard_layout(d, cfg)
    keyboard = f"⌨️ διάταξη πληκτρολογίου: μάλλον {layout}, όχι ελληνική" if layout else ""
    unchecked = ["κριτικές (≥30, στο καλύτερο 10%)", "χαμηλό 30 ημερών", "χρώμα/παραλλαγή", "τελικό ποσό στο ταμείο"]
    if not d.get("local"):
        unchecked.append("εγγύηση κατασκευαστή για Ελλάδα")
    unchecked_line = "☑️ δεν ελέγχθηκαν: " + ", ".join(unchecked)

    links = [f'<a href="{html.escape(d["url"])}">🛒 αγορά</a>']
    asin = AMAZON_ASIN.search(d["url"])
    if asin:
        links.append(f'<a href="https://{urlsplit(d["url"]).netloc}/product-reviews/{asin.group(1)}">⭐ κριτικές</a>')
        if urlsplit(d["url"]).netloc.removeprefix("www.") != "amazon.de":  # often cheaper, and free delivery
            links.append(f'<a href="https://www.amazon.de/dp/{asin.group(1)}">🇩🇪 ίδιο στο Amazon.de</a>')
    if not d.get("local"):  # what it costs in Greece, to judge the deal and the resale price
        links.append(f'<a href="https://www.skroutz.gr/search?keyphrase={quote_plus(search_name(d["title"], cfg))}">'
                     f'🇬🇷 Skroutz</a>')
    extra = d.get("extra_url", "")
    if "camelcamelcamel" in extra:
        links.append(f'<a href="{html.escape(extra)}">📈 ιστορικό</a>')
    elif asin and (keepa := KEEPA_DOMAINS.get(urlsplit(d["url"]).netloc.removeprefix("www."))):
        links.append(f'<a href="https://keepa.com/#!product/{keepa}-{asin.group(1)}">📈 ιστορικό</a>')
    if extra and "camelcamelcamel" not in extra:
        links.append(f'<a href="{html.escape(extra)}">💬 συζήτηση</a>')

    lines = [head, html.escape(d["title"][:120], quote=False), barebone, keyboard, " · ".join(evidence),
             delivery_line, unchecked_line, " · ".join(links)]
    return "\n".join(line for line in lines if line)


def telegram_call(token, method, http_timeout=30, **payload):
    """Call the Bot API without ever letting the URL (it contains the token) reach an error or log."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=http_timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"Telegram {method}: {e.__class__.__name__}") from None
    if not r.ok:
        raise RuntimeError(f"Telegram {method}: {r.status_code} {r.text[:200]}")
    return r.json()


def send_telegram(cfg, text, silent=False):
    telegram_call(cfg["telegram_bot_token"], "sendMessage", chat_id=cfg["telegram_chat_id"],
                  text=text, parse_mode="HTML", disable_web_page_preview=True, disable_notification=silent)


def send_deals(cfg, deals):
    """One message per deal; those that need a parcel forwarder arrive without a sound."""
    for d in deals:
        send_telegram(cfg, format_deal(d, cfg), silent=bool(d.get("forwarded")))
        time.sleep(1)  # Telegram allows about one message a second per chat


def runs_last_day():
    """How many scans GitHub ran in the last 24 hours, scheduled or started by the Google script;
    None outside GitHub Actions."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and token):
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/scan.yml/runs",
                         params={"created": f">={since}", "per_page": 1},
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
        return r.json()["total_count"] if r.ok else None
    except (requests.RequestException, ValueError, KeyError):
        return None


def sent_last_day():
    """How many deals were sent in the last 24 hours, and when the last one was (from found_deals.csv)."""
    if not FOUND_LOG.exists():
        return 0, None
    times = []
    with FOUND_LOG.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            try:
                times.append(datetime.strptime(row["time"], "%Y-%m-%d %H:%M"))
            except (KeyError, TypeError, ValueError):  # a damaged line must not stop the run
                continue
    since = datetime.now() - timedelta(days=1)  # the log is written in the runner's local time
    return sum(t >= since for t in times), max(times, default=None)


def daily_status_due(cfg, now):
    """Today's date if the daily status message is due (once a day, after the configured UTC hour)."""
    last = json.loads(STATUS.read_text(encoding="utf-8")).get("last_status") if STATUS.exists() else None
    today = now.date().isoformat()
    return today if now.hour >= cfg["daily_status_utc_hour"] and last != today else None


def status_problems(read, failed, runs, expected_runs):
    """What is wrong today: sources that broke or found nothing, or GitHub skipping too many runs.

    read: deals each source returned in this run; failed: the error of each source that broke.
    """
    problems = [f"• {name}: {why}" for name, why in failed.items()]
    problems += [f"• {name}: δεν βρήκε καμία προσφορά στη σελίδα (μήπως άλλαξε;)"
                 for name, count in read.items() if count == 0]
    if runs is not None and runs < expected_runs / 2:
        problems.append(f"• έγιναν μόνο {runs} από τους ~{expected_runs} ελέγχους του 24ώρου")
    return problems


def status_message(problems, read, runs, sent, last_sent):
    """The daily message, sent only when something is wrong (a quiet day needs no message)."""
    head = "⚠️ <b>Ο ανιχνευτής θέλει έλεγχο</b>" if problems else "✔️ <b>Ο ανιχνευτής δουλεύει</b>"
    day = "" if runs is None else f"{runs} έλεγχο{'ς' if runs == 1 else 'ι'}, "
    day += f"{sent} προσφορ{'ά πέρασε' if sent == 1 else 'ές πέρασαν'} τα φίλτρα"
    if not sent and last_sent:
        day += f" (η τελευταία στις {last_sent:%d/%m})"
    latest = ", ".join(f"{name} {count}" for name, count in read.items()) or "καμία πηγή"
    return "\n".join([head, f"24 ώρες: {day}", f"Τελευταίος έλεγχος, προσφορές που διάβασε: {latest}"] + problems)


TOKEN_SHAPE = re.compile(r"\d+:[A-Za-z0-9_-]{30,}")


def clean_secret(value):
    """Forgive stray spaces, line breaks or quotes pasted around a secret."""
    return str(value).strip().strip("\"'").strip()


def load_secrets():
    local = json.loads(SECRETS.read_text(encoding="utf-8")) if SECRETS.exists() else {}
    return {
        "telegram_bot_token": clean_secret(os.environ.get("TELEGRAM_BOT_TOKEN") or local.get("telegram_bot_token", "")),
        "telegram_chat_id": clean_secret(os.environ.get("TELEGRAM_CHAT_ID") or local.get("telegram_chat_id", "")),
    }


def telegram_ready(cfg):
    token = cfg.get("telegram_bot_token", "")
    return ":" in token and str(cfg.get("telegram_chat_id", "")).strip() != ""


def setup(cfg):
    token = cfg.get("telegram_bot_token", "")
    if ":" not in token:
        print("Βάλε πρώτα το token του bot στο secrets.json (telegram_bot_token).")
        return 1
    print("Περιμένω μήνυμά σου στο bot (έως 5 λεπτά)...", flush=True)
    chats, deadline = [], time.time() + 300
    while not chats and time.time() < deadline:
        updates = telegram_call(token, "getUpdates", http_timeout=60, timeout=50)  # long polling
        chats = [u["message"]["chat"]["id"] for u in updates.get("result", []) if "message" in u]
    if not chats:
        print("Δεν βρέθηκε μήνυμα. Άνοιξε το bot σου στο Telegram, πάτα Start, και ξανατρέξε --setup.")
        return 1
    cfg["telegram_chat_id"] = chats[-1]
    SECRETS.write_text(json.dumps({"telegram_bot_token": token, "telegram_chat_id": chats[-1]},
                                  indent=2), encoding="utf-8")
    send_telegram(cfg, "✅ Ο ανιχνευτής προσφορών είναι συνδεδεμένος. Θα σου στέλνω μόνο νέες μεγάλες πτώσεις.")
    print(f"OK, chat id {chats[-1]} αποθηκεύτηκε και στάλθηκε δοκιμαστικό μήνυμα.")
    return 0


def main():
    if sys.stdout:  # None when started windowless by Task Scheduler (pythonw)
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--setup", action="store_true")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg.update(load_secrets())
    if args.setup:
        return setup(cfg)
    token = cfg["telegram_bot_token"]
    if not args.dry_run and token and not TOKEN_SHAPE.fullmatch(token):
        # stop before scanning, so no deal is marked as sent; never print the token itself
        log("Το token του Telegram δεν έχει τη μορφή token bot (αριθμοί:γράμματα). "
            "Έλεγξε το secret TELEGRAM_BOT_TOKEN.")
        return 1

    seen = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept-Language": "en;q=0.8"})

    found, counts, read, failed = [], [], {}, {}
    de_prices = {}  # Amazon.de prices read in this run, by product id
    today = datetime.now(timezone.utc).date()
    history = load_prices(today)
    for name, enabled in cfg["sources"].items():
        if not enabled:
            continue
        if name == "insomnia":
            scan = insomnia_deals
        else:
            scan = pepper_deals if name in PEPPER_SITES else camel_deals
        try:
            deals = scan(session, name, cfg)
        except Exception as e:  # one broken source must not stop the others
            log(f"{name}: σφάλμα {e.__class__.__name__}: {e}")
            failed[name] = f"σφάλμα {getattr(getattr(e, 'response', None), 'status_code', None) or e.__class__.__name__}"
            continue
        resolve_short_shops(session, deals)
        add_amazon_de_prices(deals, de_prices)
        big = [d for d in deals if is_big(d, cfg)]
        remember_prices(deals, history, today, cfg)
        counts.append(f"{name} {len(big)}/{len(deals)}")
        read[name] = len(deals)
        found.extend(big)

    new, keys = [], set()
    for d in found:
        if d["key"] not in seen and d["key"] not in keys:
            keys.add(d["key"])
            new.append(d)
    new.sort(key=lambda d: (not d["price_error"], d["price"] / d["ref"] if d["ref"] else 1))
    add_shop_links(session, new)
    add_amazon_de_prices(new, de_prices)
    remember_prices(new, history, today, cfg)  # community deals know their Amazon product only now
    # an Amazon.it/.fr/.es deal is no deal when Amazon.de sells the same product for less, nor one
    # whose product cost less in the last 30 days; one message per product, and none for a product
    # another post already brought in the last 30 days; the posts left out are still remembered as seen below
    worth = [d for d in new if not cheaper_on_amazon_de(d, de_prices, cfg)]
    real = [d for d in worth if not cheaper_lately(d, history, today)]
    to_send = [d for d in unique_products(real) if f"product:{product_key(d)}" not in seen]
    left_out = ""
    if len(worth) < len(new):
        left_out += f", {len(new) - len(worth)} φθηνότερες στο Amazon.de"
    if len(real) < len(worth):
        left_out += f", {len(worth) - len(real)} φθηνότερες τις τελευταίες {LATELY_DAYS} μέρες"
    log(f"έλεγχος: {', '.join(counts)} -> {len(to_send)} νέες{left_out}")

    if args.dry_run:
        for d in to_send:
            text = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2 \1", format_deal(d, cfg))
            print("\n" + re.sub(r"</?b>", "", text))
        runs = runs_last_day()
        problems = status_problems(read, failed, runs, cfg["expected_runs_per_day"])
        print("\n" + re.sub(r"</?b>", "", status_message(problems, read, runs, *sent_last_day())))
        return 0

    save_prices(history)
    if new:
        if telegram_ready(cfg):
            send_deals(cfg, to_send)
        else:
            log("Το Telegram δεν έχει ρυθμιστεί ακόμα· οι προσφορές γράφτηκαν μόνο στο found_deals.csv")

        now = int(time.time())
        seen.update({d["key"]: now for d in new})
        seen.update({f"product:{product_key(d)}": now for d in to_send})
        seen = {k: t for k, t in seen.items() if now - t < 30 * 86400}
        STATE.write_text(json.dumps(seen), encoding="utf-8")
        write_header = not FOUND_LOG.exists()
        with FOUND_LOG.open("a", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["time", "drop_%", "price", "usual_price", "currency", "title", "store", "url"])
            for d in to_send:
                drop = round((1 - d["price"] / d["ref"]) * 100) if d["ref"] else ""
                w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), drop, d["price"],
                            d["ref"] or "", d["cur"], d["title"], d["where"], d["url"]])

    today = daily_status_due(cfg, datetime.now(timezone.utc))
    if today:
        try:
            runs = runs_last_day()
            problems = status_problems(read, failed, runs, cfg["expected_runs_per_day"])
            if problems and telegram_ready(cfg):
                send_telegram(cfg, status_message(problems, read, runs, *sent_last_day()))
        except Exception as e:  # the deals are already saved; the status is tried again next run
            log(f"ημερήσια κατάσταση: σφάλμα {e.__class__.__name__}: {e}")
        else:
            # written daily even on quiet days, so the run commits daily and GitHub keeps the schedule
            STATUS.write_text(json.dumps({"last_status": today}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
