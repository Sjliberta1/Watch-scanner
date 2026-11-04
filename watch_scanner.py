#!/usr/bin/env python3
"""eBay watch deal scanner (v2: filters for brand, price range, movement).
Usage:  export EBAY_APP_ID=... EBAY_CERT_ID=...
        python watch_scanner.py          # one scan
        python watch_scanner.py --loop   # scan every hour
Outputs report.html (interactive filters) + watches.db."""
import os, re, sys, json, time, html, base64, sqlite3, statistics
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
MIN_PRICE = 150            # scan floor in USD; narrow further on the page itself
MIN_FEEDBACK = 98.0        # seller feedback %
PAGES = 2                  # 200 listings per page, per category, per type
AUCTION_WINDOW_H = 72      # only include auctions ending within this many hours
AUCTION_TARGET = 0.85      # auctions typically close ~15% under asking
KEEP = 250                 # listings kept per category per type for the page
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


def movement(title):
    t = title.lower()
    if re.search(r"automatic|self[- ]?winding", t):
        return "Automatic"
    if re.search(r"manual|hand[- ]?wound|hand[- ]?winding|mechanical", t):
        return "Manual / mechanical"
    if re.search(r"eco[- ]?drive|solar|kinetic|spring drive|accutron|tuning fork", t):
        return "Solar / kinetic / other"
    if re.search(r"quartz|battery", t):
        return "Quartz"
    return "Unknown"


def parse(it, kind):
    title = it.get("title", "")
    if EXCLUDE.search(title):
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
    return dict(id=it["itemId"], title=title, brand=brand, move=movement(title),
                total=float(p["value"]) + ship, bids=it.get("bidCount", 0), end=end,
                url=it.get("itemWebUrl"), img=(it.get("image") or {}).get("imageUrl"),
                cond=it.get("condition", ""))


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
                           key=lambda x: -x["score"])[:KEEP]

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
        auc = sorted(auc, key=lambda x: -x["score"])[:KEEP]
        result[cat] = (bin_items, auc)
    db.close()
    return result


TEMPLATE = r'''<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Watch deals</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif}
body{max-width:1100px;margin:auto;padding:10px}
.bar{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.bar label{font-size:12px;opacity:.75;display:block}
.bar select,.bar input{width:100%;box-sizing:border-box;padding:8px;font-size:15px}
details{margin:10px 0;border:1px solid #8884;border-radius:8px;padding:8px 10px}
summary{font-weight:600}
.br{display:grid;grid-template-columns:1fr 1fr;gap:4px 8px;margin-top:8px;font-size:14px}
button{padding:6px 12px;font-size:14px}
.g{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:8px;margin-top:8px}
.c{display:flex;gap:8px;padding:8px;border:1px solid #8884;border-radius:8px;color:inherit;text-decoration:none}
.c img{width:80px;height:80px;object-fit:cover;border-radius:6px}
small{opacity:.75}
</style>
<h1>Watch deals</h1>
<p>Updated __STAMP__. Percent = how far below typical asking price (or auction target). Movement is read from listing titles.</p>
<div class=bar>
<div><label>Category</label><select id=cat></select></div>
<div><label>Listing type</label><select id=kind><option value=b>Buy It Now</option><option value=a>Auctions</option></select></div>
<div><label>Movement</label><select id=mov></select></div>
<div><label>Sort by</label><select id=sort><option value=s>Best value</option><option value=price>Lowest price</option><option value=end>Ending soonest</option></select></div>
<div><label>Min price $</label><input id=lo type=number inputmode=numeric></div>
<div><label>Max price $</label><input id=hi type=number inputmode=numeric></div>
</div>
<details><summary id=bs>Brands</summary>
<p><button id=all>Select all</button> <button id=none>Clear all</button></p>
<div class=br id=brs></div></details>
<p id=cnt></p><div class=g id=out></div>
<script>
var D=__DATA__;
function $(i){return document.getElementById(i)}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]})}
var cats=[],brands=[],cnt={};
var movs=['Automatic','Manual / mechanical','Quartz','Solar / kinetic / other','Unknown'];
D.forEach(function(x){if(cats.indexOf(x.c)<0)cats.push(x.c);if(brands.indexOf(x.b)<0)brands.push(x.b);cnt[x.b]=(cnt[x.b]||0)+1});
brands.sort();
var S={},off={};
try{S=JSON.parse(localStorage.getItem('wf')||'{}')}catch(e){}
(S.off||[]).forEach(function(b){off[b]=1});
$('cat').innerHTML='<option value="">All categories</option>'+cats.map(function(c){return '<option>'+esc(c)+'</option>'}).join('');
$('mov').innerHTML='<option value="">Any movement</option>'+movs.map(function(c){return '<option>'+esc(c)+'</option>'}).join('');
['cat','kind','mov','lo','hi','sort'].forEach(function(id){if(S[id]!=null)$(id).value=S[id];$(id).onchange=render;$(id).oninput=render});
function drawBrands(){$('brs').innerHTML=brands.map(function(b){return '<label><input type=checkbox data-b="'+esc(b)+'"'+(off[b]?'':' checked')+'> '+esc(b)+' ('+cnt[b]+')</label>'}).join('')}
$('brs').onchange=function(e){var b=e.target.getAttribute('data-b');if(e.target.checked)delete off[b];else off[b]=1;render()};
$('all').onclick=function(){off={};drawBrands();render()};
$('none').onclick=function(){brands.forEach(function(b){off[b]=1});drawBrands();render()};
function card(x){
var line=x.k=='a'?'Bid $'+x.p+' · '+x.n+' bids · '+x.h+'h left<br>Target $'+x.tg+' · headroom $'+(x.tg-x.p):'$'+x.p+' vs typical $'+x.base;
return '<a class=c href="'+esc(x.u)+'" target=_blank>'+(x.i?'<img src="'+esc(x.i)+'" loading=lazy>':'')+'<div><b>'+(x.s>0?'+':'')+x.s+'%</b> '+esc(x.t)+'<br><small>'+esc(x.b)+' · '+esc(x.m)+' · '+esc(x.cd)+'<br>'+line+'</small></div></a>'}
function render(){
var c=$('cat').value,k=$('kind').value,m=$('mov').value,s=$('sort').value;
var lo=parseFloat($('lo').value)||0,hi=parseFloat($('hi').value)||1e9;
var r=D.filter(function(x){return x.k==k&&(!c||x.c==c)&&(!m||x.m==m)&&!off[x.b]&&x.p>=lo&&x.p<=hi});
r.sort(s=='price'?function(a,b){return a.p-b.p}:s=='end'?function(a,b){return a.h-b.h}:function(a,b){return b.s-a.s});
var n=brands.filter(function(b){return !off[b]}).length;
$('bs').textContent='Brands ('+n+' of '+brands.length+' selected)';
$('cnt').textContent='Showing '+Math.min(r.length,60)+' of '+r.length+' listings';
$('out').innerHTML=r.slice(0,60).map(card).join('')||'<p>No listings match these filters.</p>';
try{localStorage.setItem('wf',JSON.stringify({cat:c,kind:k,mov:m,sort:s,lo:$('lo').value,hi:$('hi').value,off:Object.keys(off)}))}catch(e){}}
drawBrands();render();
</script>'''


def write_report(res):
    rows = []
    for cat, (b, a) in res.items():
        for kind, items in (("b", b), ("a", a)):
            for x in items:
                rows.append({"c": cat, "k": kind, "t": x["title"][:90], "b": x["brand"],
                             "m": x["move"], "p": round(x["total"]), "s": round(x["score"] * 100),
                             "u": x["url"], "i": x["img"], "h": round(x.get("hrs", 0), 1),
                             "n": x["bids"], "base": round(x["base"]),
                             "tg": round(x.get("target", 0)), "cd": x["cond"]})
    data = json.dumps(rows, separators=(",", ":")).replace("</", "<\\/")
    page = TEMPLATE.replace("__DATA__", data).replace("__STAMP__", datetime.now().strftime("%Y-%m-%d %H:%M"))
    open("report.html", "w", encoding="utf-8").write(page)


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
