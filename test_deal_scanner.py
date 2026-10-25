"""Checks the scanner's filters and messages offline: no network, nothing sent or saved.

Run: python -m unittest -v test_deal_scanner
GitHub runs this before every scan, so a broken config.json or filter stops the run
instead of silently letting everything (or nothing) through. Prices are set relative to
the config's thresholds, so tuning the numbers doesn't break the checks.
"""
import html
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import deal_scanner as ds

CFG = json.loads(ds.CONFIG.read_text(encoding="utf-8"))
ASIN = "B0TEST1234"


def amazon_deal(title, ref=300.0, drop=None, temperature=None, **extra):
    """A voted mydealz deal on Amazon.de, `drop` percent (default: 5 over the minimum) below `ref`."""
    drop = CFG["min_drop_percent"] + 5 if drop is None else drop
    deal = {"key": "mydealz:1", "flag": "🇩🇪", "where": "Amazon", "source": "mydealz", "title": title,
            "price": round(ref * (1 - drop / 100), 2), "ref": ref, "cur": "€", "price_error": False,
            "url": f"https://www.amazon.de/dp/{ASIN}", "shop_host": "amazon.de", "category": "",
            "temperature": CFG["min_temperature"] + 10 if temperature is None else temperature,
            "comments": 3}
    deal.update(extra)
    return deal


def greek_offer(title):
    """An insomnia.gr member's offer: free text, no usual price."""
    return {"key": "insomnia:1", "flag": "🇬🇷", "where": "insomnia.gr", "source": "προσφορές μελών",
            "title": title, "price": None, "ref": None, "cur": "€", "price_error": False,
            "url": "https://www.insomnia.gr/forums/topic/1-x/", "local": True, "curated": True}


HEADPHONES = "Sony WH-1000XM5 Kopfhörer"


class ConfigTest(unittest.TestCase):
    def test_has_every_setting(self):
        # the Apps Script reads topics, brands, used_words, like_new_words and exclude_words too
        for key in ["min_drop_percent", "min_saving_eur", "min_usual_price_eur", "min_temperature",
                    "hot_tier", "shops", "forwarding", "topics", "brands", "used_words", "like_new_words",
                    "exclude_words", "sources", "pepper_pages", "insomnia_days", "daily_status_utc_hour",
                    "skroutz_categories"]:
            self.assertIn(key, CFG)
        self.assertIn(CFG["daily_status_utc_hour"], range(24))

    def test_word_lists(self):
        for key in ["brands", "used_words", "like_new_words", "exclude_words"]:
            self.assertTrue(CFG[key], key)
            for word in CFG[key]:
                self.assertTrue(isinstance(word, str) and word.strip(), f"{key}: {word!r}")

    def test_topics(self):
        for name, topic in CFG["topics"].items():
            self.assertTrue(topic["words"], name)
            self.assertIsInstance(topic["min_usual_price_eur"], (int, float), name)
            for word in topic["words"]:
                # a topic word that contains an excluded word could never pass
                self.assertFalse(ds.mentions(word, CFG["exclude_words"]), f"{name}: {word}")
        forwarding = CFG["forwarding"]
        for topic, size in forwarding["size"].items():
            self.assertIn(topic, CFG["topics"])
            self.assertIn(size, forwarding["cost_eur"])
        self.assertIn("small", forwarding["cost_eur"])  # the default size

    def test_sources_exist(self):
        for name in CFG["sources"]:
            self.assertTrue(name in ds.PEPPER_SITES or name in ds.CAMEL_SITES or name == "insomnia", name)


class FilterTest(unittest.TestCase):
    def test_voted_amazon_deal_passes(self):
        deal = amazon_deal(HEADPHONES)
        self.assertTrue(ds.is_big(deal, CFG))
        self.assertEqual(deal["delivery"], 0)  # over Amazon.de's free-delivery minimum
        self.assertTrue(deal["topic"])

    def test_items_other_tools_track_are_excluded(self):
        for title in ["Samsung Odyssey G7 Gaming Monitor", "Lenovo Legion Laptop", "LG OLED TV 55 Zoll",
                      "Samsung Soundbar HW-Q990D", "Ubiquiti SFP+ Transceiver", "Τηλεόραση LG 55"]:
            self.assertTrue(ds.mentions(title, CFG["exclude_words"]), title)
            self.assertFalse(ds.is_big(amazon_deal(title), CFG), title)

    def test_known_brands_only(self):
        self.assertFalse(ds.is_big(amazon_deal("Noname Bluetooth Kopfhörer"), CFG))
        self.assertTrue(ds.has_known_brand("SONY Kopfhörer", CFG))
        self.assertFalse(ds.has_known_brand("Sonyx Kopfhörer", CFG))  # whole words only

    def test_used_only_when_like_new(self):
        self.assertFalse(ds.is_big(amazon_deal(HEADPHONES + " refurbished"), CFG))
        self.assertTrue(ds.is_big(amazon_deal(HEADPHONES + " refurbished, wie neu"), CFG))

    def test_chosen_topics_only(self):
        self.assertFalse(ds.is_big(amazon_deal("Samsung Galaxy S25 Smartphone"), CFG))

    def test_community_votes(self):
        self.assertFalse(ds.is_big(amazon_deal(HEADPHONES, temperature=CFG["min_temperature"] - 1), CFG))
        hot = CFG["hot_tier"]
        smaller_drop = hot["min_drop_percent"] + 1
        self.assertLess(smaller_drop, CFG["min_drop_percent"])
        self.assertTrue(ds.is_big(amazon_deal(HEADPHONES, drop=smaller_drop, temperature=hot["min_temperature"]), CFG))
        self.assertFalse(ds.is_big(amazon_deal(HEADPHONES, drop=smaller_drop), CFG))

    def test_price_errors_need_no_votes(self):
        self.assertTrue(ds.is_big(amazon_deal(HEADPHONES, temperature=0, price_error=True), CFG))

    def test_cheap_items_skipped(self):
        topic = ds.matching_topic(amazon_deal(HEADPHONES), CFG)
        too_cheap = CFG["topics"][topic]["min_usual_price_eur"] - 1
        self.assertFalse(ds.is_big(amazon_deal(HEADPHONES, ref=too_cheap, drop=60), CFG))

    def test_delivery_eats_into_the_drop(self):
        free_from = CFG["shops"]["amazon.de"]["free_from"]
        drop = CFG["min_drop_percent"] + 5
        ref = free_from * 0.9 / (1 - drop / 100)  # the price lands just under free delivery
        deal = amazon_deal(HEADPHONES, ref=ref, drop=drop)
        self.assertFalse(ds.is_big(deal, CFG))
        self.assertEqual(deal["delivery"], CFG["shops"]["amazon.de"]["delivery_eur"])

    def test_shops_that_cant_reach_greece(self):
        self.assertFalse(ds.is_big(amazon_deal(HEADPHONES, shop_host="bestbuy.com"), CFG))

    def test_german_shops_through_forwarding(self):
        forwarding = CFG["forwarding"]
        low = forwarding["min_usual_price_eur"] - 1
        self.assertFalse(ds.is_big(amazon_deal(HEADPHONES, ref=low, drop=60, shop_host="www.mediamarkt.de"), CFG))
        deal = amazon_deal(HEADPHONES, ref=600, drop=60, shop_host="www.mediamarkt.de")
        self.assertTrue(ds.is_big(deal, CFG))
        self.assertTrue(deal["forwarded"])
        size = forwarding["size"].get(deal["topic"], "small")
        self.assertEqual(deal["delivery"], forwarding["cost_eur"][size])
        # forwarded parcels arrive too late for a price error to wait for no votes
        self.assertFalse(ds.is_big(amazon_deal(HEADPHONES, ref=600, temperature=0, price_error=True,
                                               shop_host="mediamarkt.de"), CFG))

    def test_accents_and_case_are_ignored(self):
        self.assertEqual(ds.plain("Ακουστικά"), ds.plain("ΑΚΟΥΣΤΙΚΑ"))
        self.assertTrue(ds.mentions("ΗΧΕΙΟ JBL", ["ηχείο"]))

    def test_greek_member_offers(self):
        self.assertTrue(ds.is_big(greek_offer("Φριτέζα αέρος Philips 7.2lt 129€ @ Kotsovolos"), CFG))
        self.assertFalse(ds.is_big(greek_offer("Ζυμαρικά De Cecco -50%"), CFG))


class LinksAndMessagesTest(unittest.TestCase):
    def test_amazon_short_links_are_resolved(self):
        deal = amazon_deal(HEADPHONES, shop_host="amzn.eu", url="https://www.mydealz.de/deals/x-1",
                           visit_url="https://www.mydealz.de/visit/threadmain/1")
        targets = lambda session, url: "https://amzn.eu/d/abc" if "visit" in url else f"https://www.amazon.de/dp/{ASIN}"
        with mock.patch.object(ds, "redirect_target", side_effect=targets), mock.patch.object(ds.time, "sleep"):
            ds.resolve_short_shops(None, [deal])
        self.assertEqual(deal["shop_host"], "www.amazon.de")
        self.assertEqual(deal["url"], f"https://www.amazon.de/dp/{ASIN}")
        self.assertTrue(ds.is_big(deal, CFG))

    def test_one_message_per_product(self):
        less = amazon_deal(HEADPHONES, temperature=300)
        more = amazon_deal(HEADPHONES, temperature=500, source="preisjaeger")
        other = amazon_deal("Ninja Creami", url="https://www.amazon.de/dp/B0OTHER123")
        kept = ds.unique_products([less, more, other])
        self.assertEqual(len(kept), 2)
        self.assertIs(kept[0], more)

    def test_message(self):
        deal = amazon_deal(HEADPHONES + " <neu>")
        self.assertTrue(ds.is_big(deal, CFG))
        text = ds.format_deal(deal)
        self.assertIn(f"−{CFG['min_drop_percent'] + 5}%", text)
        self.assertIn("&lt;neu&gt;", text)
        self.assertIn(f"keepa.com/#!product/3-{ASIN}", text)
        self.assertIn("skroutz.gr/search?keyphrase=", text)
        self.assertIn("👍", text)
        self.assertLess(len(text), 4000)

    def test_message_with_delivery_and_forwarding(self):
        deal = amazon_deal(HEADPHONES, ref=80, drop=40, temperature=CFG["hot_tier"]["min_temperature"])
        ds.is_big(deal, CFG)
        self.assertIn("μεταφορικά · τελικά ≈ −", ds.format_deal(deal))
        deal = amazon_deal(HEADPHONES, ref=600, drop=60, shop_host="mediamarkt.de")
        ds.is_big(deal, CFG)
        self.assertIn("μόνο μέσω προώθησης", ds.format_deal(deal))

    def test_greek_offer_message(self):
        deal = greek_offer("Φριτέζα αέρος Philips 7.2lt @ Kotsovolos")
        ds.is_big(deal, CFG)
        text = ds.format_deal(deal)
        self.assertIn("προσφορά", text)
        self.assertNotIn("τιμή στην Ελλάδα", text)  # already a Greek shop


def vue_blob(thread):
    return "data-vue3='" + html.escape(json.dumps({"props": {"thread": thread}}), quote=True) + "'"


class ParsingTest(unittest.TestCase):
    def test_pepper_page(self):
        base = {"type": "Deal", "isExpired": False, "isLocal": False, "title": HEADPHONES, "price": 195,
                "nextBestPrice": 300, "percentage": 0, "temperature": 420.5, "commentCount": 12,
                "linkHost": "amazon.de", "merchant": {"merchantName": "Amazon"},
                "mainGroup": {"threadGroupUrlName": "audio"}, "shareableLink": "https://www.mydealz.de/share-deal/1"}
        threads = [
            dict(base, threadId=1),
            dict(base, threadId=2, nextBestPrice=0, percentage=50, price=100),  # usual price from the stated discount
            dict(base, threadId=3, title="iPhone 16 mit Vertrag 29,99€ mtl."),  # monthly price
            dict(base, threadId=4, isExpired=True),
            dict(base, threadId=5, nextBestPrice=150),  # dearer than elsewhere
        ]
        page = "<html>" + "".join(vue_blob(t) for t in threads) + "</html>"
        with mock.patch.object(ds, "fetch", return_value=page):
            deals = ds.pepper_deals(None, "mydealz", dict(CFG, pepper_pages=1))
        self.assertEqual([d["key"] for d in deals], ["mydealz:1", "mydealz:2"])
        first = deals[0]
        self.assertEqual((first["price"], first["ref"], first["temperature"], first["comments"]), (195, 300, 420.5, 12))
        self.assertEqual(first["visit_url"], "https://www.mydealz.de/visit/threadmain/1")
        self.assertEqual(first["shop_host"], "amazon.de")
        self.assertAlmostEqual(deals[1]["ref"], 200)

    def test_camel_feed(self):
        feed = ("<rss><channel><item><title>Sony WH-1000XM5 - down 35.00% (€105.00) to €1.195,00 from €1.300,00</title>"
                f"<link>https://de.camelcamelcamel.com/product/{ASIN}</link></item>"
                "<item><title>not a price drop</title><link>x</link></item></channel></rss>")
        with mock.patch.object(ds, "fetch", return_value=feed):
            deals = ds.camel_deals(None, "camel_de", CFG)
        self.assertEqual(len(deals), len(ds.CAMEL_LISTS))
        self.assertEqual((deals[0]["price"], deals[0]["ref"]), (1195.0, 1300.0))
        self.assertEqual(deals[0]["url"], f"https://www.amazon.de/dp/{ASIN}")


class DailyStatusTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.object(ds, "STATUS", Path(folder.name) / "status.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_once_a_day_after_the_hour(self):
        hour = CFG["daily_status_utc_hour"]
        day = datetime(2026, 10, 25, tzinfo=timezone.utc)
        if hour:
            self.assertIsNone(ds.daily_status_due(CFG, day.replace(hour=hour - 1)))
        self.assertEqual(ds.daily_status_due(CFG, day.replace(hour=hour)), "2026-10-25")
        ds.STATUS.write_text(json.dumps({"last_status": "2026-10-25"}), encoding="utf-8")
        self.assertIsNone(ds.daily_status_due(CFG, day.replace(hour=23)))

    def test_message(self):
        ok = ds.status_message({"mydealz": 80, "camel_de": 40}, {}, 30, 0, datetime(2026, 10, 24, 11, 13))
        self.assertTrue(ok.startswith("✔️"))
        self.assertIn("30 έλεγχοι, 0 προσφορές πέρασαν τα φίλτρα (η τελευταία στις 24/10)", ok)
        broken = ds.status_message({"mydealz": 0}, {"preisjaeger": "σφάλμα 403"}, None, 1, None)
        self.assertTrue(broken.startswith("⚠️"))
        self.assertIn("preisjaeger: σφάλμα 403", broken)
        self.assertIn("mydealz: δεν βρήκε καμία προσφορά", broken)


if __name__ == "__main__":
    unittest.main()
