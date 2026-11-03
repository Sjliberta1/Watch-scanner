#!/usr/bin/env python3
"""eBay watch deal scanner.
Usage:  export EBAY_APP_ID=... EBAY_CERT_ID=...
        python watch_scanner.py          # one scan
        python watch_scanner.py --loop   # scan every hour
Outputs report.html + watches.db in the current folder."""
import os, re, time, html, base64, sqlite3, statistics, sys
from datetime import datetime, timezone
import requests

# ---------- CONFIG: edit freely ----------
CATEGORIES = {
    "Diver": "diver automatic watch",
    "Chronograph": "chronograph automatic watch",
    "Dress": "dress watch",
    "Super Compressor": "super compressor watch",
    "Pilot": "pilot watch",
    "Perpetual Calendar": "perpetual calendar watch",
}
BRANDS = ["rolex", "omega", "tudor", "seiko", "grand seiko", "citizen", "tissot", "longines",
          "hamilton", "oris", "breitling", "iwc", "jaeger", "cartier", "zenith", "heuer",
          "tag heuer", "universal geneve", "enicar", "squale", "doxa", "blancpain",
          "patek", "vacheron", "audemars", "baume", "bulova", "benrus", "wittnauer",
          "eterna", "girard", "nivada", "glycine", "orient", "bell & ross", "panerai"]
MIN_PRICE = 150            # USD, ignores junk below this
MIN_FEEDBACK = 98.0        # seller feedback %
PAGES = 2                  # 200 listings per page, per category, per type
AUCTION_WINDOW_H = 72      # only rank auctions ending within this many hours
AUCTION_TARGET = 0.85      # auctions typically close ~15% under asking
TOP_N = 10
EXCLUDE = re.compile(r"\b(parts|repair|not working|broken|band|strap|bracelet only|case only|"
                     r"dial only|movement only|box only|empty box|crystal|lot of|for parts)\b", re.I)
# ------------------------------------------

API = "https://api.ebay.com/buy/browse/v1/item_summary/search"
APP_ID, CERT_ID = os.getenv("EBAY_APP_ID"), os.getenv("EBAY_CERT_ID")


def token():
    auth = base64.b64encode(f"{APP_ID}:{CERT_ID}".encode()).decode()
    r = requests.post("https://api.ebay.com/identity/v1/oauth2/token",
                      headers={"Authorization": f"Basic {auth}",
                               "Content-Type": "application/x-www-form-urlencoded"},
                      data={"grant_type": "client_credentials",
                            "scope": "https://api.ebay.com/oauth/api_scope"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def search(tok, q, kind):
    out = []
    for p in range(PAGES):
        params = {"q": q, "category_ids": "31387", "limit": 200, "offset": p * 200,
                  "filter": f"buyingOptions:{{{kind}}},price:[{MIN_PRICE}..],priceCurrency:USD"}
        if kind == "AUCTION":
            params["sort"] = "endingSoonest"
        r = requests.get(API, params=params, timeout=30,
                         headers={"Authorization": f"Bearer {tok}",
                                  "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
        r.raise_for_status()
        items = r.json().get("itemSummaries", [])
        out += items
        if len(items) < 200:
            break
    return out


def parse(it, kind):
    title = it.get("title", "")
    if EXCL_HIT(title):
        return None
    p = it.get("currentBidPrice") if kind == "AUCTION" else it.get("price")
    if not p:
        return None
    fb = float((it.get("seller") or {}).get("feedbackPercentage") or 0)
    if fb < MIN_FEEDBACK:
        return None
    ship = 0.0
    so = it.get("shippingOptions") or []
    if so and so[0].get("shippingCost"):
        ship = float(so[0]["shippingCost"]["value"])
    low = title.lower()
    brand = next((b.title() for b in sorted(BRANDS, key=len, reverse=True) if b in low), "Other")
    end = None
    if it.get("itemEndDate"):
        end = datetime.fromisoformat(it["itemEndDate"].replace("Z", "+00:00"))
    return dict(id=it["itemId"], title=title, brand=brand, total=float(p["value"]) + ship,
                bids=it.get("bidCount", 0), end=end, url=it.get("itemWebUrl"),
                img=(it.get("image") or {}).get("imageUrl"),
                cond=it.get("condition", ""))


def EXCL_HIT(t):
    return bool(EXCLUDE.search(t))


def baseline(db, cat, brand):
    since = time.time() - 14 * 86400
    rows = [r[0] for r in db.execute(
        "select price from fixed where cat=? and brand=? and ts>?", (cat, brand, since))]
    if len(rows) < 8:
        rows = [r[0] for r in db.execute(
            "select price from fixed where cat=? and ts>?", (cat, since))]
    return statistics.median(rows) if rows else None


def scan():
    db = sqlite3.connect("watches.db")
    db.execute("create table if not exists fixed(id text primary key, cat text, brand text, price real, ts real)")
    tok = token()
    now = datetime.now(timezone.utc)
    result = {}
    for cat, q in CATEGORIES.items():
        bin_items = [x for x in (parse(i, "FIXED_PRICE") for i in search(tok, q, "FIXED_PRICE")) if x]
        for x in bin_items:
            db.execute("insert or ignore into fixed values(?,?,?,?,?)",
                       (x["id"], cat, x["brand"], x["total"], time.time()))
        db.commit()
        for x in bin_items:
            b = baseline(db, cat, x["brand"])
            x["base"] = b
            x["score"] = (1 - x["total"] / b) if b else None
        bin_items = sorted([x for x in bin_items if x["score"] is not None],
                           key=lambda x: -x["score"])[:TOP_N]

        auc = []
        for it in search(tok, q, "AUCTION"):
            x = parse(it, "AUCTION")
            if not x or not x["end"]:
                continue
            hrs = (x["end"] - now).total_seconds() / 3600
            if not 0 < hrs <= AUCTION_WINDOW_H:
                continue
            b = baseline(db, cat, x["brand"])
            if not b:
                continue
            x["base"], x["hrs"] = b, hrs
            x["target"] = b * AUCTION_TARGET
            x["score"] = 1 - x["total"] / x["target"]
            auc.append(x)
        auc = sorted(auc, key=lambda x: -x["score"])[:TOP_N]
        result[cat] = (bin_items, auc)
    db.close()
    return result


def card(x, auction):
    meta = f'{html.escape(x["brand"])} · {html.escape(x["cond"])}'
    if auction:
        line = (f'Bid ${x["total"]:,.0f} · {x["bids"]} bids · {x["hrs"]:.1f}h left<br>'
                f'Target ${x["target"]:,.0f} · headroom ${x["target"]-x["total"]:,.0f}')
    else:
        line = f'${x["total"]:,.0f} vs median ${x["base"]:,.0f}'
    img = f'<img src="{x["img"]}" loading="lazy">' if x["img"] else ""
    return (f'<a class="c" href="{x["url"]}" target="_blank">{img}<div><b>{x["score"]*100:+.0f}%</b> '
            f'{html.escape(x["title"][:80])}<br><small>{meta}<br>{line}</small></div></a>')


def write_report(res):
    parts = []
    for cat, (b, a) in res.items():
        parts.append(f"<h2>{cat}</h2><h3>Buy It Now — best vs. median</h3><div class=g>"
                     + "".join(card(x, False) for x in b) + "</div>"
                     f"<h3>Auctions ending ≤{AUCTION_WINDOW_H}h — best vs. target</h3><div class=g>"
                     + ("".join(card(x, True) for x in a) or "<p>None</p>") + "</div>")
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    open("report.html", "w", encoding="utf-8").write(f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Watch deals</title>
<style>:root{{color-scheme:light dark;font-family:system-ui,sans-serif}}body{{max-width:1100px;margin:auto;padding:12px}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px}}
.c{{display:flex;gap:8px;padding:8px;border:1px solid #8884;border-radius:8px;color:inherit;text-decoration:none}}
.c img{{width:80px;height:80px;object-fit:cover;border-radius:6px}}small{{opacity:.75}}</style>
<h1>Watch deals</h1><p>Updated {stamp}. Percent = discount vs. median asking (BIN) or vs. auction target.</p>
{''.join(parts)}""")


def main():
    if not APP_ID or not CERT_ID:
        sys.exit("Set EBAY_APP_ID and EBAY_CERT_ID environment variables.")
    while True:
        try:
            write_report(scan())
            print(datetime.now().strftime("%H:%M"), "report.html updated")
        except Exception as e:
            print("scan failed:", e)
        if "--loop" not in sys.argv:
            break
        time.sleep(3600)


if __name__ == "__main__":
    main()
