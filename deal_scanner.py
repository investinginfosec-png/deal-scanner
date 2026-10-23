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
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import requests

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
SECRETS = HERE / "secrets.json"  # local only, listed in .gitignore
STATE = HERE / "seen.json"
FOUND_LOG = HERE / "found_deals.csv"
RUN_LOG = HERE / "scanner.log"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Deal communities (Pepper network). Each deal carries its price and the
# cheapest price elsewhere ("nextBestPrice"), which we use as the usual price.
PEPPER_SITES = {
    "mydealz":     ("🇩🇪", "https://www.mydealz.de/new", "€"),
    "preisjaeger": ("🇦🇹", "https://www.preisjaeger.at/neu", "€"),
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

# Skroutz (Greek shops): its deals pages list products whose price dropped, with the
# previous price struck through. The categories to check come from config.json.
# Skroutz blocks IPs that send many requests, so each run fetches one page per category,
# alternating hourly between the newest drops and the biggest drops.
SKROUTZ_ORDERS = ["newest", "discount_percentage"]

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
    # be polite between requests; camelcamelcamel and Skroutz block bursts of requests
    time.sleep(15 if "camelcamelcamel" in url else 8 if "skroutz" in url else 3)
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
            })
    return deals


CAMEL_TITLE = re.compile(r"^(.*) - down ([\d.]+)% \(.*?\) to (.+?) from (.+)$")


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


def skroutz_deals(session, name, cfg):
    deals = []
    order = SKROUTZ_ORDERS[datetime.now().hour % len(SKROUTZ_ORDERS)]
    for path in cfg["skroutz_categories"]:
        page_html = fetch(session, f"https://www.skroutz.gr/deals/c/{path}?order_by={order}&order_dir=desc")
        for card in page_html.split('data-testid="sku-cardd"')[1:]:
            badge = re.search(r'<span>-\d+%</span></span><span class="secondary-text">([^<]+)<', card)
            if not badge or badge.group(1) != "πτώση τιμής":  # skip multi-buy, Plus-only, expiring stock
                continue
            title = re.search(r'title="([^"]+)" data-testid="sku-title-link"', card)
            link = re.search(r'href="(/s/\d+/[^"?]+)', card)
            before = re.search(r"<strike>([\d.,]+)\s*€</strike>", card)
            now = re.search(r'sku-price-link"[^>]*>(?:<span>[^<]*</span>)?([\d.,]+)\s*€', card)
            if not (title and link and before and now):
                continue
            price, ref = parse_money(now.group(1)), parse_money(before.group(1))
            if not price or not ref or ref <= price:
                continue
            category = re.search(r'class="category-name"[^>]*>([^<]+)<', card)
            shops = re.search(r"σε (\d+) καταστήματα", card)
            deals.append({
                "key": f"skroutz:{link.group(1).split('/')[2]}:{price}",
                "flag": "🇬🇷",
                "where": html.unescape(category.group(1)) if category else "Skroutz",
                "source": "skroutz" + (f" · σε {shops.group(1)} καταστήματα" if shops else ""),
                "title": html.unescape(title.group(1)),
                "price": price,
                "ref": ref,
                "cur": "€",
                "price_error": False,
                "url": "https://www.skroutz.gr" + link.group(1),
                "shop_host": "skroutz.gr",
                "local": True,
            })
    return deals


def add_shop_links(session, deals):
    """Point community deals straight at the shop, read from the site's redirect (not followed)."""
    for d in deals:
        if "visit_url" not in d:
            continue
        try:
            target = session.get(d["visit_url"], allow_redirects=False, timeout=30).headers.get("Location", "")
        except requests.RequestException:
            continue
        shop = parse_qs(urlsplit(target).query).get("url", [target])[0]
        if shop.startswith("http") and urlsplit(shop).netloc != urlsplit(d["url"]).netloc:
            d["extra_url"], d["extra_label"] = d["url"], "💬 σελίδα προσφοράς"
            d["url"] = shop
        time.sleep(1)


def free_delivery_from(deal, cfg):
    """Price from which the deal's shop delivers free to Greece; None if the shop isn't allowed."""
    if deal.get("local"):
        return 0
    host = deal.get("shop_host", "").lower().removeprefix("www.")
    for domain, min_price in cfg["shops"].items():
        if host == domain or host.endswith("." + domain):
            return min_price
    return None


def is_big(deal, cfg):
    text = f"{deal['title']} {deal['where']}".lower()
    if any(w.lower() in text for w in cfg.get("exclude_words", [])):
        return False
    free_from = free_delivery_from(deal, cfg)
    if free_from is None:
        return False
    deal["free_from"] = free_from  # shown in the message
    if "category" in deal and deal["category"] not in cfg["pepper_categories"]:
        return False
    if deal["price_error"]:
        return True  # worth it even with a delivery fee
    if not deal["ref"] or deal["price"] < free_from:
        return False
    if deal["ref"] * TO_EUR.get(deal["cur"], 1.0) < cfg["min_usual_price_eur"]:
        return False
    drop = 1 - deal["price"] / deal["ref"]
    saving_eur = (deal["ref"] - deal["price"]) * TO_EUR.get(deal["cur"], 1.0)
    return drop * 100 >= cfg["min_drop_percent"] and saving_eur >= cfg["min_saving_eur"]


def fmt_price(value, cur):
    return f"{value:,.2f}".replace(",", " ").replace(".", ",") + cur


def format_deal(d):
    if d["ref"]:
        drop = round((1 - d["price"] / d["ref"]) * 100)
        head = f"🔥 <b>−{drop}%</b>  {fmt_price(d['price'], d['cur'])} (αντί {fmt_price(d['ref'], d['cur'])})"
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
    delivery = f"\n🚚 δωρεάν αποστολή στην Ελλάδα (πάνω από {d['free_from']}€)" if d.get("free_from") else ""
    return (f"{head}\n{html.escape(d['title'][:160], quote=False)}\n"
            f"{d['flag']} {html.escape(d['where'], quote=False)} · {d['source']}{delivery}\n{links}")


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

    found, counts = [], []
    for name, enabled in cfg["sources"].items():
        if not enabled:
            continue
        if name == "skroutz":
            scan = skroutz_deals
        else:
            scan = pepper_deals if name in PEPPER_SITES else camel_deals
        try:
            deals = scan(session, name, cfg)
        except Exception as e:  # one broken source must not stop the others
            log(f"{name}: σφάλμα {e.__class__.__name__}: {e}")
            continue
        big = [d for d in deals if is_big(d, cfg)]
        counts.append(f"{name} {len(big)}/{len(deals)}")
        found.extend(big)

    new, keys = [], set()
    for d in found:
        if d["key"] not in seen and d["key"] not in keys:
            keys.add(d["key"])
            new.append(d)
    new.sort(key=lambda d: (not d["price_error"], d["price"] / d["ref"] if d["ref"] else 1))
    log(f"έλεγχος: {', '.join(counts)} -> {len(new)} νέες")
    add_shop_links(session, new)

    if args.dry_run:
        for d in new:
            text = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2 \1", format_deal(d))
            print("\n" + re.sub(r"</?b>", "", text))
        return 0
    if not new:
        return 0

    if telegram_ready(cfg):
        send_deals(cfg, new)
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
        for d in new:
            drop = round((1 - d["price"] / d["ref"]) * 100) if d["ref"] else ""
            w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M"), drop, d["price"],
                        d["ref"] or "", d["cur"], d["title"], d["where"], d["url"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
