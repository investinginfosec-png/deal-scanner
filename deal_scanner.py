"""Deal scanner: finds deals far below their usual price and sends them to Telegram.

Usage:
    python deal_scanner.py            scan, send new deals, remember them
    python deal_scanner.py --dry-run  scan and print only (sends nothing, remembers nothing)
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
from datetime import datetime, timedelta, timezone
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
RUN_LOG = HERE / "scanner.log"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Deal communities (Pepper network). Each deal carries its price and the
# cheapest price elsewhere ("nextBestPrice"), which we use as the usual price.
# mydealz and preisjaeger are read from their "hot" lists (deals the community voted
# over 100°), since only those can reach the config's min_temperature.
PEPPER_SITES = {
    "mydealz":     ("🇩🇪", "https://www.mydealz.de/hot", "€"),
    "preisjaeger": ("🇦🇹", "https://www.preisjaeger.at/", "€"),
    "dealabs":     ("🇫🇷", "https://www.dealabs.com/nouveaux", "€"),
    "chollometro": ("🇪🇸", "https://www.chollometro.com/nuevos", "€"),
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
RECORD_LOW = re.compile(r"bestpreis|tiefstpreis|allzeittief|all[- ]time[- ]low|niedrigster preis", re.IGNORECASE)

AMAZON_ASIN = re.compile(r"amazon\.[a-z.]+/(?:[^/?]+/)?(?:dp|gp/product)/([A-Z0-9]{10})")
AMAZON_SHORT_HOSTS = {"amzn.eu", "amzn.to"}


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
    s = re.sub(r"[^\d,.]", "", text)
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
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
            if ref <= price and 0 < pct < 100 and price > 0:
                ref = price / (1 - pct / 100)  # discount stated by the poster
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
PRICE_IN_TEXT = re.compile(r"(\d{1,5}(?:[.,]\d{1,2})?)\s*€|€\s*(\d{1,5}(?:[.,]\d{1,2})?)")


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
                "cur": "€",
                "price_error": False,
                # camelcamelcamel pages often block phone browsers, so link the product itself
                "url": f"https://www.{store.lower()}/dp/{asin}",
                "extra_url": link,
                "extra_label": "📈 ιστορικό τιμής",
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
            d["extra_url"], d["extra_label"] = d["url"], "💬 σελίδα προσφοράς"
            d["url"] = shop
        time.sleep(1)


def resolve_short_shops(session, deals):
    """Amazon short links hide which Amazon sells the deal, so look it up before filtering."""
    short = [d for d in deals if d.get("shop_host") in AMAZON_SHORT_HOSTS]
    add_shop_links(session, short)
    for d in short:
        d["shop_host"] = urlsplit(d["url"]).netloc


def unique_products(deals):
    """One message per product when several communities post it; the most voted post is kept."""
    kept = {}
    for d in sorted(deals, key=lambda d: -d.get("temperature", 0)):
        asin = AMAZON_ASIN.search(d["url"])
        kept.setdefault(asin.group(1) if asin else " ".join(d["title"].lower().split()[:3]), d)
    return [d for d in deals if any(d is k for k in kept.values())]


def shop_terms(deal, cfg):
    """How the deal's shop reaches Greece: (order value for free delivery, delivery fee below it, forwarded).

    German shops that don't ship to Greece go through a parcel forwarder, so their delivery
    is never free and its fee depends on the parcel's size. None if the shop can't reach Greece.
    """
    if deal.get("local"):
        return 0, 0, False
    host = deal.get("shop_host", "").lower().removeprefix("www.")
    is_host = lambda domain: host == domain or host.endswith("." + domain)
    for domain, terms in cfg["shops"].items():
        if is_host(domain):
            return terms["free_from"], terms["delivery_eur"], False
    if any(is_host(domain) for domain in cfg["forwarding"]["shops"]):
        return float("inf"), None, True
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


def is_big(deal, cfg):
    if mentions(f"{deal['title']} {deal['where']}", cfg.get("exclude_words", [])):
        return False
    if not has_known_brand(deal["title"], cfg) or not good_condition(deal["title"], cfg):
        return False
    terms = shop_terms(deal, cfg)
    if terms is None:
        return False
    deal["free_from"], delivery, deal["forwarded"] = terms  # all shown in the message
    deal["topic"] = matching_topic(deal, cfg)
    if not deal["topic"]:
        return False
    if deal["forwarded"]:
        forwarding = cfg["forwarding"]
        delivery = forwarding["cost_eur"][forwarding["size"].get(deal["topic"], "small")]
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


def format_deal(d):
    if d["ref"]:
        drop = round((1 - d["price"] / d["ref"]) * 100)
        head = f"🔥 <b>−{drop}%</b>  {fmt_price(d['price'], d['cur'])} (αντί {fmt_price(d['ref'], d['cur'])})"
    elif d["price"] is None:
        head = "🔥 <b>προσφορά</b>"
    else:
        head = f"🔥 <b>{fmt_price(d['price'], d['cur'])}</b>"
    if d["price"] == 0:
        head += "  <b>ΔΩΡΕΑΝ</b>"
    if d["price_error"]:
        head += "  ⚠️ <b>λάθος τιμής</b>"
    host = urlsplit(d["url"]).netloc.removeprefix("www.")
    links = f'🛒 <a href="{html.escape(d["url"])}">{html.escape(host)}</a>'
    if d.get("extra_url"):
        links += f'\n<a href="{html.escape(d["extra_url"])}">{d["extra_label"]}</a>'
    asin = AMAZON_ASIN.search(d["url"])
    if asin and "camelcamelcamel" not in d.get("extra_url", ""):
        links += f'\n<a href="https://keepa.com/#!product/3-{asin.group(1)}">📈 ιστορικό τιμής</a>'
    if not d.get("local"):  # what it costs in Greece, to judge the deal and the resale price
        name = " ".join(re.sub(r"[\[\(].*?[\]\)]", " ", d["title"]).split()[:6])
        links += f'\n<a href="https://www.skroutz.gr/search?keyphrase={quote_plus(name)}">🇬🇷 τιμή στην Ελλάδα</a>'
    evidence = ""
    if d.get("temperature"):
        evidence += f"\n👍 {round(d['temperature'])}° στο {d['source']}"
        evidence += f" · {d['comments']} σχόλια" if d.get("comments") else ""
    if RECORD_LOW.search(d["title"]):
        evidence += "\n📉 χαμηλότερη τιμή μέχρι σήμερα, κατά τον αναρτήσαντα"
    # the headline drop leaves out delivery, so state what remains after it
    final = (f" · τελικά ≈ −{round((1 - (d['price'] + d['delivery']) / d['ref']) * 100)}%"
             if d.get("delivery") and d["ref"] else "")
    if d.get("forwarded"):
        delivery = (f"\n📦 μόνο μέσω προώθησης από Γερμανία: +≈{fmt_price(d['delivery'], '€')}{final}, "
                    f"1–2 εβδομάδες (Forward2me / ColisExpat)")
    elif d.get("delivery"):
        delivery = (f"\n🚚 +≈{fmt_price(d['delivery'], '€')} μεταφορικά{final} "
                    f"(δωρεάν αν η παραγγελία φτάσει {d['free_from']}€)")
    elif d.get("free_from"):
        delivery = f"\n🚚 δωρεάν αποστολή στην Ελλάδα (πάνω από {d['free_from']}€)"
    else:
        delivery = ""
    topic = f"\n🏷️ {html.escape(d['topic'], quote=False)}" if d.get("topic") else ""
    return (f"{head}\n{html.escape(d['title'][:160], quote=False)}\n"
            f"{d['flag']} {html.escape(d['where'], quote=False)} · {d['source']}{evidence}{delivery}{topic}\n{links}")


def telegram_call(token, method, http_timeout=30, **payload):
    """Call the Bot API without ever letting the URL (it contains the token) reach an error or log."""
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=http_timeout)
    except requests.RequestException as e:
        raise RuntimeError(f"Telegram {method}: {e.__class__.__name__}") from None
    if not r.ok:
        raise RuntimeError(f"Telegram {method}: {r.status_code} {r.text[:200]}")
    return r.json()


def send_telegram(cfg, text):
    telegram_call(cfg["telegram_bot_token"], "sendMessage", chat_id=cfg["telegram_chat_id"],
                  text=text, parse_mode="HTML", disable_web_page_preview=True)


def send_deals(cfg, deals):
    header = f"<b>{len(deals)} νέες προσφορές</b> (πτώση ≥{cfg['min_drop_percent']}%)"
    chunks, current = [], header
    for d in deals:
        block = format_deal(d)
        if len(current) + len(block) + 2 > 4000:  # Telegram limit is 4096
            chunks.append(current)
            current = block
        else:
            current += "\n\n" + block
    chunks.append(current)
    for chunk in chunks:
        send_telegram(cfg, chunk)


def runs_last_day():
    """How many scheduled scans GitHub started in the last 24 hours; None outside GitHub Actions."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and token):
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/actions/workflows/scan.yml/runs",
                         params={"event": "schedule", "created": f">={since}", "per_page": 1},
                         headers={"Authorization": f"Bearer {token}"}, timeout=30)
        return r.json()["total_count"] if r.ok else None
    except (requests.RequestException, ValueError, KeyError):
        return None


def sent_last_day():
    """How many deals were sent in the last 24 hours, and when the last one was (from found_deals.csv)."""
    if not FOUND_LOG.exists():
        return 0, None
    with FOUND_LOG.open(newline="", encoding="utf-8-sig") as f:
        times = [datetime.strptime(row["time"], "%Y-%m-%d %H:%M") for row in csv.DictReader(f)]
    since = datetime.now() - timedelta(days=1)  # the log is written in the runner's local time
    return sum(t >= since for t in times), max(times, default=None)


def daily_status_due(cfg, now):
    """Today's date if the daily status message is due (once a day, after the configured UTC hour)."""
    last = json.loads(STATUS.read_text(encoding="utf-8")).get("last_status") if STATUS.exists() else None
    today = now.date().isoformat()
    return today if now.hour >= cfg["daily_status_utc_hour"] and last != today else None


def status_message(read, failed, runs, sent, last_sent):
    """The daily message that tells a quiet day apart from a broken scanner.

    read: deals each source returned in this run; failed: the error of each source that broke.
    """
    problems = [f"• {name}: {why}" for name, why in failed.items()]
    problems += [f"• {name}: δεν βρήκε καμία προσφορά στη σελίδα (μήπως άλλαξε;)"
                 for name, count in read.items() if count == 0]
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
        big = [d for d in deals if is_big(d, cfg)]
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
    to_send = unique_products(new)  # the duplicates are still remembered as seen below
    log(f"έλεγχος: {', '.join(counts)} -> {len(to_send)} νέες")

    if args.dry_run:
        for d in to_send:
            text = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2 \1", format_deal(d))
            print("\n" + re.sub(r"</?b>", "", text))
        print("\n" + re.sub(r"</?b>", "", status_message(read, failed, runs_last_day(), *sent_last_day())))
        return 0

    if new:
        if telegram_ready(cfg):
            send_deals(cfg, to_send)
        else:
            log("Το Telegram δεν έχει ρυθμιστεί ακόμα· οι προσφορές γράφτηκαν μόνο στο found_deals.csv")

        now = int(time.time())
        seen.update({d["key"]: now for d in new})
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
    if today and telegram_ready(cfg):
        send_telegram(cfg, status_message(read, failed, runs_last_day(), *sent_last_day()))
        STATUS.write_text(json.dumps({"last_status": today}), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
