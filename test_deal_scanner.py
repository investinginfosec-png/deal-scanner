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
                    "hot_tier", "shops", "parcel_kg", "forwarding", "topics", "brands", "used_words", "like_new_words",
                    "barebone_words",
                    "exclude_words", "sources", "pepper_pages", "insomnia_days", "daily_status_utc_hour",
                    "expected_runs_per_day", "skroutz_categories"]:
            self.assertIn(key, CFG)
        self.assertIn(CFG["daily_status_utc_hour"], range(24))

    def test_word_lists(self):
        for key in ["brands", "used_words", "like_new_words", "exclude_words", "barebone_words"]:
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
        self.assertEqual(set(CFG["parcel_kg"]), set(forwarding["cost_eur"]))

    def test_shops(self):
        for domain, terms in CFG["shops"].items():
            self.assertTrue(terms["free_from"] is None or terms["free_from"] > 0, domain)  # None: never free
            self.assertGreater(terms["per_parcel_eur"], 0, domain)
            self.assertGreaterEqual(terms["per_kg_eur"], 0, domain)
            self.assertTrue(1 <= terms.get("vat_factor", 1.0) < 1.2, domain)

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
                      "Samsung Soundbar HW-Q990D", "Ubiquiti SFP+ Transceiver", "Τηλεόραση LG 55",
                      "PC Portable 14\" Asus Vivobook 14 - Snapdragon X, SSD 512 Go", "Ordenador portátil Lenovo IdeaPad",
                      "PC portatile HP 15", "Téléviseur LG OLED 55", "Televisor Samsung 55", "Televisore Sony Bravia",
                      "Barre de son Sonos Ray", "Barra de sonido Samsung", "Écran PC Samsung Odyssey G5"]:
            self.assertTrue(ds.mentions(title, CFG["exclude_words"]), title)
            self.assertFalse(ds.is_big(amazon_deal(title), CFG), title)

    def test_known_brands_only(self):
        self.assertFalse(ds.is_big(amazon_deal("Noname Bluetooth Kopfhörer"), CFG))
        self.assertTrue(ds.has_known_brand("SONY Kopfhörer", CFG))
        self.assertFalse(ds.has_known_brand("Sonyx Kopfhörer", CFG))  # whole words only

    def test_used_only_when_like_new(self):
        self.assertFalse(ds.is_big(amazon_deal(HEADPHONES + " refurbished"), CFG))
        self.assertTrue(ds.is_big(amazon_deal(HEADPHONES + " refurbished, wie neu"), CFG))
        for used, like_new in [("reconditionné", "comme neuf"), ("reacondicionado", "como nuevo"),
                               ("ricondizionato", "come nuovo")]:
            self.assertFalse(ds.is_big(amazon_deal(f"{HEADPHONES} {used}"), CFG), used)
            self.assertTrue(ds.is_big(amazon_deal(f"{HEADPHONES} {used} - {like_new}"), CFG), like_new)

    def test_chosen_topics_only(self):
        self.assertFalse(ds.is_big(amazon_deal("Samsung Galaxy S25 Smartphone"), CFG))

    def test_french_spanish_and_italian_titles(self):
        for title, topic in [("Friteuse sans huile Ninja AF300EU", "σπίτι & κουζίνα"),
                             ("Robot aspirador Roborock Q10 S5", "σπίτι & κουζίνα"),
                             ("Friggitrice ad aria Philips Serie 3000", "σπίτι & κουζίνα"),
                             ("Écouteurs Sony WF-1000XM5", "ακουστικά & ηχεία"),
                             ("Auriculares Bose QuietComfort Ultra", "ακουστικά & ηχεία"),
                             ("Cuffie JBL Tune 770NC", "ακουστικά & ηχεία"),
                             ("Carte graphique MSI GeForce RTX 5070", "υπολογιστές (κομμάτια & περιφερειακά)"),
                             ("Teclado Logitech MX Keys S", "υπολογιστές (κομμάτια & περιφερειακά)"),
                             ("Sedia da ufficio Sihoo M57", "εργονομία γραφείου"),
                             ("Batterie externe Anker 737", "χρήσιμα καθημερινά")]:
            self.assertEqual(ds.matching_topic(amazon_deal(title), CFG), topic, title)

    def test_community_votes(self):
        self.assertFalse(ds.is_big(amazon_deal(HEADPHONES, temperature=CFG["min_temperature"] - 1), CFG))
        hot = CFG["hot_tier"]
        if hot["min_drop_percent"] < CFG["min_drop_percent"]:  # posts with many votes may drop less
            smaller_drop = hot["min_drop_percent"] + 1
            self.assertTrue(ds.is_big(amazon_deal(HEADPHONES, drop=smaller_drop, temperature=hot["min_temperature"]), CFG))
            self.assertFalse(ds.is_big(amazon_deal(HEADPHONES, drop=smaller_drop), CFG))
        self.assertFalse(ds.is_big(amazon_deal(HEADPHONES, drop=min(CFG["min_drop_percent"], hot["min_drop_percent"]) - 2,
                                               temperature=hot["min_temperature"] + 100), CFG))

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
        shop = CFG["shops"]["amazon.de"]
        self.assertEqual(deal["delivery"], shop["per_parcel_eur"] + shop["per_kg_eur"] * CFG["parcel_kg"]["small"])

    def test_other_amazons_never_deliver_free(self):
        for host in ["amazon.it", "amazon.fr", "amazon.es"]:
            shop = CFG["shops"][host]
            for title, size in [(HEADPHONES, "small"), ("Ninja Heißluftfritteuse AF300EU", "medium")]:
                deal = amazon_deal(title, ref=600, drop=60, shop_host=host, url=f"https://www.{host}/dp/{ASIN}")
                self.assertTrue(ds.is_big(deal, CFG), host)
                self.assertAlmostEqual(deal["delivery"], shop["per_parcel_eur"] + shop["per_kg_eur"] * CFG["parcel_kg"][size])

    def test_skipped_when_amazon_de_is_cheaper(self):
        def italian():
            deal = amazon_deal("ASUS NUC 15 Pro+ Mini PC Barebone", ref=861, drop=30, shop_host="amazon.it",
                               url=f"https://www.amazon.it/dp/{ASIN}")
            self.assertTrue(ds.is_big(deal, CFG))
            return deal
        prices = {}
        ds.add_amazon_de_prices([amazon_deal(HEADPHONES, ref=700, drop=27)], prices)  # Amazon.de, same product
        self.assertEqual(prices, {ASIN: 511})
        self.assertTrue(ds.cheaper_on_amazon_de(italian(), prices, CFG))
        self.assertFalse(ds.cheaper_on_amazon_de(italian(), {}, CFG))  # Amazon.de not read in this run
        dearer = italian()
        self.assertFalse(ds.cheaper_on_amazon_de(dearer, {ASIN: 700}, CFG))
        self.assertIn(f"στο Amazon.de ≈ {ds.fmt_price(700 * CFG['shops']['amazon.de']['vat_factor'], '€')}",
                      ds.format_deal(dearer, CFG))
        self.assertFalse(ds.cheaper_on_amazon_de(amazon_deal(HEADPHONES), prices, CFG))  # already Amazon.de

    def test_whole_shop_sales_are_excluded(self):
        for title in ["tink Licht & Sicherheit Sale / z.B. Bosch Smart Home Starter-Set", "Sammeldeal Sony Kopfhörer",
                      "Sélection de casques Sony", "Recopilación de sartenes y utensilios de cocina de WMF"]:
            self.assertFalse(ds.is_big(amazon_deal(title), CFG), title)

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
        text = ds.format_deal(deal, CFG)
        head, title = text.split("\n")[:2]
        self.assertTrue(head.startswith(f"🟢 <b>−{CFG['min_drop_percent'] + 5}%</b>"), head)
        self.assertIn("&lt;neu&gt;", title)
        self.assertIn(f"keepa.com/#!product/3-{ASIN}", text)
        self.assertIn(f"amazon.de/product-reviews/{ASIN}", text)
        self.assertIn("skroutz.gr/search?keyphrase=Sony+WH-1000XM5", text)
        self.assertIn("👍", text)
        self.assertIn("δωρεάν μεταφορικά για Ελλάδα · τελικό με ελληνικό ΦΠΑ ≈", text)
        self.assertLess(len(text), 1000)

    def test_message_with_delivery_and_forwarding(self):
        free_from = CFG["shops"]["amazon.de"]["free_from"]
        deal = amazon_deal(HEADPHONES, ref=free_from * 1.6, drop=50)  # price under the free-delivery minimum
        self.assertTrue(ds.is_big(deal, CFG))
        text = ds.format_deal(deal, CFG)
        final = round((1 - (deal["price"] + deal["delivery"]) / deal["ref"]) * 100)
        self.assertTrue(text.startswith(f"🟢 <b>−{final}%</b>"), text)  # the headline counts the delivery
        self.assertIn("μεταφορικά αντί", text)
        deal = amazon_deal(HEADPHONES, ref=600, drop=60, shop_host="mediamarkt.de", url="https://www.mediamarkt.de/x")
        self.assertTrue(ds.is_big(deal, CFG))
        text = ds.format_deal(deal, CFG)
        self.assertTrue(text.startswith("🟡"), text)
        self.assertIn("μόνο μέσω προώθησης", text)

    def test_message_from_another_amazon(self):
        deal = amazon_deal(HEADPHONES, ref=600, drop=60, shop_host="www.amazon.it", url=f"https://www.amazon.it/dp/{ASIN}")
        self.assertTrue(ds.is_big(deal, CFG))
        text = ds.format_deal(deal, CFG)
        self.assertIn("amazon.it: χωρίς δωρεάν μεταφορικά για Ελλάδα (6,49€ + 5,99€/κιλό, υπολογισμένα για ~1 κιλό)", text)
        self.assertIn("τελικό με ελληνικό ΦΠΑ ≈", text)
        self.assertIn(f"amazon.it/product-reviews/{ASIN}", text)
        self.assertIn(f"keepa.com/#!product/8-{ASIN}", text)  # Keepa's number for Amazon.it
        self.assertIn(f"amazon.de/dp/{ASIN}", text)  # to compare with Amazon.de

    def test_barebone_warning(self):
        deal = amazon_deal("ASUS NUC 15 Pro+ Mini PC Intel Core Ultra 7 255H, Barebone, NO RAM/SSD")
        self.assertTrue(ds.is_big(deal, CFG))
        text = ds.format_deal(deal, CFG)
        self.assertIn("barebone</b>: χωρίς RAM και δίσκο", text)
        self.assertNotIn("ίδιο στο Amazon.de", text)  # already Amazon.de
        deal = amazon_deal(HEADPHONES)
        ds.is_big(deal, CFG)
        self.assertNotIn("barebone", ds.format_deal(deal, CFG))

    def test_bulky_items_warn_about_delivery(self):
        deal = amazon_deal("Ninja Heißluftfritteuse AF300EU")
        self.assertTrue(ds.is_big(deal, CFG))
        self.assertIn("ογκώδες", ds.format_deal(deal, CFG))
        deal = amazon_deal(HEADPHONES)
        ds.is_big(deal, CFG)
        self.assertNotIn("ογκώδες", ds.format_deal(deal, CFG))

    def test_greek_offer_message(self):
        deal = greek_offer("Φριτέζα αέρος Philips 7.2lt @ Kotsovolos")
        ds.is_big(deal, CFG)
        text = ds.format_deal(deal, CFG)
        self.assertIn("προσφορά", text)
        self.assertNotIn("Skroutz", text)  # already a Greek shop

    def test_forwarded_deals_arrive_silently(self):
        sent = []
        loud = amazon_deal(HEADPHONES)
        quiet = amazon_deal(HEADPHONES, ref=600, drop=60, shop_host="mediamarkt.de", url="https://www.mediamarkt.de/x")
        for deal in (loud, quiet):
            ds.is_big(deal, CFG)
        with mock.patch.object(ds, "send_telegram", side_effect=lambda cfg, text, silent: sent.append(silent)), \
                mock.patch.object(ds.time, "sleep"):
            ds.send_deals(CFG, [loud, quiet])
        self.assertEqual(sent, [False, True])

    def test_search_names(self):
        for title, name in [
            ("Ninja CRISPi tragbare Heißluftfritteuse FN101EUSTGD (3,8 L, 1700 W)", "Ninja CRISPi FN101"),
            ("Logitech G PRO X2 SUPERSTRIKE LIGHTSPEED kabellose Gaming-Maus (Weiß)", "Logitech G PRO X2"),
            ("Gastroback 42709 Design Espresso Pro Espressomaschine", "Gastroback 42709"),
            ("ZWILLING Greenwich Besteckset 68-tlg. (12 Personen) für 100,84 € bei Amazon", "ZWILLING Greenwich Besteckset"),
            ("Samsung 990 PRO 2TB NVMe SSD", "Samsung 990 PRO 2TB"),
            ("[Amazon] Apple AirPods Pro 2 mit USB-C", "Apple AirPods Pro 2"),
            ("Bosch Smart Home Starter-Set Heizung II mit 8 Thermostaten", "Bosch Smart Home"),
            ("Robot aspirateur Roborock Q10 S5 avec station de vidage", "Roborock Q10 S5"),
            ("Auriculares Sony WH-1000XM5 con cancelación de ruido", "Sony WH-1000XM5"),
            ("Cuffie Bose QuietComfort Ultra per musica", "Bose QuietComfort"),
            ("Thermaltake TH420 V2 Ultra ARG... Sync All-In-One Liquid Cooler", "Thermaltake TH420 V2"),
        ]:
            self.assertEqual(ds.search_name(title, CFG), name)


class PricesAndDuplicatesTest(unittest.TestCase):
    def test_money(self):
        for text, value in [("1.049,90 €", 1049.90), ("1,049.90", 1049.90), ("1.299", 1299), ("79,99", 79.99),
                            ("79.99", 79.99), ("129", 129), ("0,99", 0.99), ("€1.195,00", 1195)]:
            self.assertEqual(ds.parse_money(text), value, text)

    def test_price_in_a_title(self):
        for title, value in [("Roborock Saros 10 1.049,90 € @ Public", 1049.90), ("TV 1.299€", 1299),
                             ("Φριτέζα αέρος Philips 7.2lt 129€", 129), ("Ninja €79,99", 79.99)]:
            m = ds.PRICE_IN_TEXT.search(title)
            self.assertEqual(ds.parse_money(m.group(1) or m.group(2)), value, title)

    def test_different_models_stay_apart(self):
        a = amazon_deal("Logitech G Pro X Superlight 2 Gaming-Maus", url="https://www.mediamarkt.de/a")
        b = amazon_deal("Logitech G Pro X TKL Tastatur", url="https://www.mediamarkt.de/b")
        self.assertNotEqual(ds.product_key(a), ds.product_key(b))
        self.assertEqual(len(ds.unique_products([a, b])), 2)

    def test_same_product_from_two_communities(self):
        # the same Amazon product posted on mydealz and preisjaeger has one key, whatever the title
        a = amazon_deal("ZWILLING Greenwich Besteckset 68-teilig für 12 Personen", key="mydealz:1")
        b = amazon_deal("ZWILLING Greenwich Besteckset 68-tlg. (12 Personen)", key="preisjaeger:2")
        self.assertEqual(ds.product_key(a), ds.product_key(b))


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

    def test_problems(self):
        expected = CFG["expected_runs_per_day"]
        self.assertEqual(ds.status_problems({"mydealz": 80, "camel_de": 40}, {}, expected, expected), [])
        self.assertEqual(ds.status_problems({"mydealz": 80}, {}, None, expected), [])  # run count unknown
        problems = ds.status_problems({"mydealz": 0}, {"preisjaeger": "σφάλμα 403"}, expected // 3, expected)
        self.assertEqual(len(problems), 3)
        self.assertIn("preisjaeger: σφάλμα 403", problems[0])
        self.assertIn("mydealz: δεν βρήκε καμία προσφορά", problems[1])
        self.assertIn(f"μόνο {expected // 3} από", problems[2])

    def test_message(self):
        ok = ds.status_message([], {"mydealz": 80, "camel_de": 40}, 30, 0, datetime(2026, 10, 24, 11, 13))
        self.assertTrue(ok.startswith("✔️"))
        self.assertIn("30 έλεγχοι, 0 προσφορές πέρασαν τα φίλτρα (η τελευταία στις 24/10)", ok)
        broken = ds.status_message(["• preisjaeger: σφάλμα 403"], {"mydealz": 3}, None, 1, None)
        self.assertTrue(broken.startswith("⚠️"))
        self.assertTrue(broken.endswith("• preisjaeger: σφάλμα 403"))


if __name__ == "__main__":
    unittest.main()
