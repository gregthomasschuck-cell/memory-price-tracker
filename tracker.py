#!/usr/bin/env python3
"""
Memory Price Tracker
--------------------
Daily fixed-SKU price tracker for DRAM and NAND memory using the DigiKey and Mouser APIs.

  python tracker.py pull      # fetch today's prices for every SKU in basket.csv
  python tracker.py build     # recompute the index and write dashboard.html + tracker.xlsx
  python tracker.py run       # pull, then build (what the daily schedule calls)
  python tracker.py sample    # write a dashboard from synthetic data to preview the layout
  python tracker.py build --out docs   # website mode, used by the GitHub Action

Pulls are resumable: a SKU already captured for a distributor today is skipped,
so re-running `pull` after a quota stop or an error only fetches what's missing. Credentials go in config.json (see config.example.json) or environment
variables DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET / MOUSER_API_KEY.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
BASKET = HERE / "basket.csv"
DATA_DIR = HERE / "data"
OBS_FILE = DATA_DIR / "observations.csv"
MISS_FILE = DATA_DIR / "not_found.csv"
TEMPLATE = HERE / "dashboard_template.html"
DASHBOARD = HERE / "dashboard.html"
XLSX = HERE / "tracker.xlsx"

PRICE_QTY = 1000          # price break tracked for the index
REL_MIN, REL_MAX = 0.5, 2.0   # day-over-day relatives outside this band are treated as data errors
MAX_GAP_DAYS = 14         # compare to the last observation if it is at most this many days old
OBS_FIELDS = ["date", "week", "distributor", "mpn", "manufacturer_returned", "dist_pn",
              "price_1", "price_1k", "stock", "lead_weeks", "status"]


# ----------------------------------------------------------------------------- helpers
def week_of(d: date) -> str:
    """Monday of the ISO week, as YYYY-MM-DD."""
    return (d - timedelta(days=d.weekday())).isoformat()


def load_basket() -> list[dict]:
    with open(BASKET, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("mpn", "").strip()]
    for r in rows:
        r["mpn"] = r["mpn"].strip()
    return rows


def load_config() -> dict:
    cfg = {}
    p = HERE / "config.json"
    if p.exists():
        cfg = json.loads(p.read_text())
    cfg.setdefault("digikey_client_id", os.environ.get("DIGIKEY_CLIENT_ID", ""))
    cfg.setdefault("digikey_client_secret", os.environ.get("DIGIKEY_CLIENT_SECRET", ""))
    cfg.setdefault("mouser_api_key", os.environ.get("MOUSER_API_KEY", ""))
    return cfg


def price_at(breaks: list[tuple[float, float]], qty: int) -> float | None:
    """Unit price that applies when buying `qty`: the largest break <= qty."""
    ok = [(q, p) for q, p in breaks if q <= qty and p and p > 0]
    if not ok:
        return None
    return max(ok, key=lambda t: t[0])[1]


def price_at_or_min(breaks: list[tuple[float, float]], qty: int) -> float | None:
    """Price at `qty`; for reel-only parts whose smallest break is above `qty`,
    the price at that smallest break (consistent week to week, so fine for an index)."""
    p = price_at(breaks, qty)
    if p is not None:
        return p
    ok = [(q, pr) for q, pr in breaks if pr and pr > 0]
    return min(ok, key=lambda t: t[0])[1] if ok else None


def parse_money(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"[\d.,]+", str(s))
    if not m:
        return None
    txt = m.group(0)
    # Mouser US uses "1,234.56"; some locales use "1.234,56"
    if "," in txt and "." in txt and txt.rfind(",") > txt.rfind("."):
        txt = txt.replace(".", "").replace(",", ".")
    else:
        txt = txt.replace(",", "")
    try:
        return float(txt)
    except ValueError:
        return None


def parse_int(s) -> int | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    m = re.search(r"\d[\d,]*", str(s))
    return int(m.group(0).replace(",", "")) if m else None


def parse_lead_weeks(s) -> float | None:
    """'52' / '52 Weeks' / '364 Days' -> weeks."""
    if s is None or s == "":
        return None
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"(\d+(?:\.\d+)?)", str(s))
    if not m:
        return None
    v = float(m.group(1))
    return round(v / 7, 1) if "day" in str(s).lower() else v


def norm(mpn: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", mpn.upper())


# Distributors also list third-party clones under the same part number (e.g. UMW
# copies of TI parts). Only accept listings from the basket manufacturer or a
# brand it has acquired.
MFR_ALIASES = {
    "micron": ["micron"],
    "samsung": ["samsung"],
    "sk hynix": ["hynix"],
    "kioxia": ["kioxia", "toshiba"],
    "winbond": ["winbond"],
    "issi": ["issi", "integrated silicon"],
    "alliance memory": ["alliance"],
    "macronix": ["macronix"],
    "texas instruments": ["texas instruments", "national semi", "burr"],
    "analog devices": ["analog devices", "maxim", "linear tech"],
    "microchip": ["microchip", "atmel", "micrel", "microsemi", "smsc"],
    "nxp": ["nxp", "freescale"],
    "onsemi": ["onsemi", "on semi", "fairchild"],
    "stmicroelectronics": ["stmicro"],
    "infineon": ["infineon", "international rectifier", "cypress"],
}


def mfr_ok(expected: str, got: str) -> bool:
    if not expected or not got:
        return True
    e, g = expected.lower(), got.lower()
    for key, names in MFR_ALIASES.items():
        if key in e or e in key:
            return any(n in g for n in names)
    return e.split()[0] in g


# ----------------------------------------------------------------------------- DigiKey
class DigiKey:
    """DigiKey Product Information API v4 (2-legged OAuth, client credentials)."""
    TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
    BASE = "https://api.digikey.com/products/v4/search"

    def __init__(self, client_id: str, client_secret: str, session):
        self.cid, self.secret, self.s = client_id, client_secret, session
        self.token, self.exp = None, 0
        self.exhausted = False

    def _auth(self):
        if self.token and time.time() < self.exp - 60:
            return
        r = self.s.post(self.TOKEN_URL, data={"client_id": self.cid, "client_secret": self.secret,
                                              "grant_type": "client_credentials"}, timeout=30)
        r.raise_for_status()
        j = r.json()
        self.token, self.exp = j["access_token"], time.time() + int(j.get("expires_in", 600))

    def _headers(self):
        self._auth()
        return {"Authorization": f"Bearer {self.token}", "X-DIGIKEY-Client-Id": self.cid,
                "X-DIGIKEY-Locale-Site": "US", "X-DIGIKEY-Locale-Language": "en",
                "X-DIGIKEY-Locale-Currency": "USD", "Accept": "application/json"}

    def _req(self, method, url, **kw):
        for attempt in range(3):
            r = self.s.request(method, url, headers=self._headers(), timeout=30, **kw)
            if r.status_code == 429:
                retry = int(r.headers.get("Retry-After", "0") or 0)
                if retry > 120 or "day" in r.text.lower():   # daily quota gone
                    self.exhausted = True
                    return None
                time.sleep(max(retry, 5))
                continue
            if r.status_code == 401 and attempt == 0:
                self.token = None
                continue
            return r
        return None

    def lookup(self, mpn: str, manufacturer: str = "") -> dict | None:
        def good(p):
            return (p and norm(p.get("ManufacturerProductNumber", "")) == norm(mpn)
                    and mfr_ok(manufacturer, (p.get("Manufacturer") or {}).get("Name", "")))
        r = self._req("GET", f"{self.BASE}/{quote(mpn, safe='')}/productdetails")
        prod = None
        if r is not None and r.status_code == 200:
            prod = r.json().get("Product")
        if not good(prod):
            # Fall back to keyword search: exact MPN from the right manufacturer
            r = self._req("POST", f"{self.BASE}/keyword", json={"Keywords": mpn, "Limit": 20, "Offset": 0})
            if r is None or r.status_code != 200:
                return None
            j = r.json()
            cands = (j.get("ExactMatches") or []) + (j.get("Products") or [])
            prod = next((p for p in cands if good(p)), None)
        return parse_digikey_product(prod) if prod else None


def parse_digikey_product(p: dict) -> dict:
    p1, p1k, dk_pn = [], [], ""
    var_stock = 0
    for v in p.get("ProductVariations") or []:
        breaks = [(float(b.get("BreakQuantity", 0)), float(b.get("UnitPrice", 0) or 0))
                  for b in v.get("StandardPricing") or []]
        var_stock += parse_int(v.get("QuantityAvailableforPackageType")) or 0
        a, b = price_at(breaks, 1), price_at_or_min(breaks, PRICE_QTY)
        if a: p1.append(a)
        if b:
            p1k.append(b)
            if not dk_pn or b <= min(p1k):
                dk_pn = v.get("DigiKeyProductNumber", "")
    if not p1 and p.get("UnitPrice"):
        p1.append(float(p["UnitPrice"]))
    status = (p.get("ProductStatus") or {}).get("Status", "") if isinstance(p.get("ProductStatus"), dict) else str(p.get("ProductStatus", ""))
    return {
        "manufacturer_returned": (p.get("Manufacturer") or {}).get("Name", ""),
        "dist_pn": dk_pn,
        "price_1": min(p1) if p1 else None,
        "price_1k": min(p1k) if p1k else None,
        # DigiKey omits zero-valued fields, so a missing quantity means none in stock
        "stock": parse_int(p.get("QuantityAvailable")) if p.get("QuantityAvailable") is not None else var_stock,
        "lead_weeks": parse_lead_weeks(p.get("ManufacturerLeadWeeks")),
        "status": status,
    }


# ----------------------------------------------------------------------------- Mouser
class Mouser:
    """Mouser Search API v1. Up to 10 part numbers per call, pipe-separated."""
    URL = "https://api.mouser.com/api/v1/search/partnumber"
    BATCH = 10
    MIN_INTERVAL = 2.2   # stay under 30 calls / minute

    def __init__(self, api_key: str, session):
        self.key, self.s, self.last = api_key, session, 0.0
        self.exhausted = False

    def lookup_many(self, mpns: list[str], mfrs: dict | None = None) -> dict[str, dict]:
        wait = self.MIN_INTERVAL - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        body = {"SearchByPartRequest": {"mouserPartNumber": "|".join(mpns), "partSearchOptions": "Exact"}}
        r = self.s.post(f"{self.URL}?apiKey={self.key}", json=body, timeout=45)
        if r.status_code == 429:
            self.exhausted = True
            return {}
        r.raise_for_status()
        j = r.json()
        errs = j.get("Errors") or []
        if errs and any("limit" in str(e).lower() for e in errs):
            self.exhausted = True
            return {}
        parts = ((j.get("SearchResults") or {}).get("Parts")) or []
        return parse_mouser_parts(parts, mpns, mfrs)


def parse_mouser_parts(parts: list[dict], mpns: list[str], mfrs: dict | None = None) -> dict[str, dict]:
    want = {norm(m): m for m in mpns}
    mfrs = mfrs or {}
    out: dict[str, dict] = {}
    for p in parts:
        key = norm(p.get("ManufacturerPartNumber", ""))
        if key not in want or not mfr_ok(mfrs.get(want[key], ""), p.get("Manufacturer", "")):
            continue
        breaks = [(float(parse_int(b.get("Quantity")) or 0), parse_money(b.get("Price")))
                  for b in p.get("PriceBreaks") or []]
        rec = {
            "manufacturer_returned": p.get("Manufacturer", ""),
            "dist_pn": p.get("MouserPartNumber", ""),
            "price_1": price_at(breaks, 1),
            "price_1k": price_at_or_min(breaks, PRICE_QTY),
            "stock": parse_int(p.get("AvailabilityInStock")) if p.get("AvailabilityInStock") not in (None, "") else (parse_int(p.get("Availability")) or 0),
            "lead_weeks": parse_lead_weeks(p.get("LeadTime")),
            "status": p.get("LifecycleStatus") or "",
        }
        mpn = want[key]
        prev = out.get(mpn)
        # Several Mouser listings can share one MPN (reel vs cut tape); keep the priced one with most stock
        if prev is None or (rec["price_1k"] and not prev["price_1k"]) or \
           (bool(rec["price_1k"]) == bool(prev["price_1k"]) and (rec["stock"] or 0) > (prev["stock"] or 0)):
            out[mpn] = rec
    return out


# ----------------------------------------------------------------------------- storage
def read_obs() -> list[dict]:
    if not OBS_FILE.exists():
        return []
    with open(OBS_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def append_obs(rows: list[dict]):
    DATA_DIR.mkdir(exist_ok=True)
    new = not OBS_FILE.exists()
    with open(OBS_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OBS_FIELDS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in OBS_FIELDS})


def record_missing(dist: str, mpns: list[str], today: str):
    """Keep one row per (week, distributor, mpn) so daily runs don't pile up duplicates."""
    if not mpns:
        return
    DATA_DIR.mkdir(exist_ok=True)
    rows = []
    if MISS_FILE.exists():
        with open(MISS_FILE, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if "week" in r]
    wk = week_of(date.fromisoformat(today))
    seen = {(r["week"], r["distributor"], r["mpn"]) for r in rows}
    rows += [{"week": wk, "distributor": dist, "mpn": m} for m in mpns if (wk, dist, m) not in seen]
    with open(MISS_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["week", "distributor", "mpn"])
        w.writeheader()
        w.writerows(rows)


# ----------------------------------------------------------------------------- pull
def cmd_pull(args):
    import requests
    cfg = load_config()
    basket = load_basket()
    today = date.today()
    wk = week_of(today)
    done = {(r["distributor"], r["mpn"]) for r in read_obs() if r["date"] == today.isoformat()} if not args.force else set()
    s = requests.Session()
    mfr_of = {b["mpn"]: b.get("manufacturer", "") for b in basket}
    total_new = 0

    if cfg.get("digikey_client_id") and "digikey" in args.only:
        dk = DigiKey(cfg["digikey_client_id"], cfg["digikey_client_secret"], s)
        todo = [b["mpn"] for b in basket if ("digikey", b["mpn"]) not in done][: args.limit or None]
        print(f"DigiKey: {len(todo)} SKUs to fetch for {today.isoformat()}")
        rows, miss = [], []
        for i, mpn in enumerate(todo, 1):
            if dk.exhausted:
                print("  DigiKey daily quota reached; the remaining SKUs are skipped today.")
                break
            try:
                rec = dk.lookup(mpn, mfr_of.get(mpn, ""))
            except Exception as e:  # keep going on single-part errors
                print(f"  {mpn}: error {e}")
                rec = None
            if rec:
                rows.append({"date": today.isoformat(), "week": wk, "distributor": "digikey", "mpn": mpn, **rec})
            elif not dk.exhausted:
                miss.append(mpn)
            if i % 25 == 0:
                append_obs(rows); total_new += len(rows); rows = []
                print(f"  {i}/{len(todo)}")
            time.sleep(0.55)   # 120 calls/min ceiling
        append_obs(rows); total_new += len(rows)
        record_missing("digikey", miss, today.isoformat())
        if miss:
            print(f"  DigiKey: {len(miss)} part numbers not found (see data/not_found.csv)")
    elif "digikey" in args.only:
        print("DigiKey: no credentials, skipped")

    if cfg.get("mouser_api_key") and "mouser" in args.only:
        mo = Mouser(cfg["mouser_api_key"], s)
        todo = [b["mpn"] for b in basket if ("mouser", b["mpn"]) not in done][: args.limit or None]
        print(f"Mouser: {len(todo)} SKUs to fetch for {today.isoformat()}")
        miss = []
        for i in range(0, len(todo), Mouser.BATCH):
            chunk = todo[i:i + Mouser.BATCH]
            if mo.exhausted:
                print("  Mouser daily quota reached; the remaining SKUs are skipped today.")
                break
            try:
                got = mo.lookup_many(chunk, mfr_of)
            except Exception as e:
                print(f"  batch starting {chunk[0]}: error {e}")
                continue
            rows = [{"date": today.isoformat(), "week": wk, "distributor": "mouser", "mpn": m, **got[m]} for m in chunk if m in got]
            if not mo.exhausted:
                miss += [m for m in chunk if m not in got]
            append_obs(rows); total_new += len(rows)
        record_missing("mouser", miss, today.isoformat())
        if miss:
            print(f"  Mouser: {len(miss)} part numbers not found (see data/not_found.csv)")
    elif "mouser" in args.only:
        print("Mouser: no API key, skipped")

    print(f"Saved {total_new} observations to {OBS_FILE.relative_to(HERE)}")


# ----------------------------------------------------------------------------- index math
def compute(obs: list[dict], basket: list[dict], is_sample=False) -> dict:
    meta = {b["mpn"]: b for b in basket}
    # Keep the latest observation per (date, distributor, mpn), only for basket SKUs
    latest: dict[tuple, dict] = {}
    for r in obs:
        if r["mpn"] not in meta:
            continue
        k = (r["date"], r["distributor"], r["mpn"])
        latest[k] = r   # later rows (e.g. a forced re-fetch) replace earlier ones
    if not latest:
        raise SystemExit("No observations yet. Run `python tracker.py pull` first.")

    def f(x):
        try:
            v = float(x)
            return v if math.isfinite(v) else None
        except (TypeError, ValueError):
            return None

    dates = sorted({k[0] for k in latest})
    wi = {w: i for i, w in enumerate(dates)}
    dnum = [date.fromisoformat(x).toordinal() for x in dates]
    dists = sorted({k[1] for k in latest})
    T = len(dates)

    # series[(dist, mpn)] = list over dates of price_1k
    series: dict[tuple, list] = {}
    stock: dict[tuple, list] = {}
    lead: dict[tuple, list] = {}
    for (w, d, m), r in latest.items():
        key = (d, m)
        series.setdefault(key, [None] * T)[wi[w]] = f(r["price_1k"])
        stock.setdefault(key, [None] * T)[wi[w]] = f(r["stock"])
        lead.setdefault(key, [None] * T)[wi[w]] = f(r["lead_weeks"])

    # Period-over-period relatives per (dist, mpn): compare with the last priced day within MAX_GAP_DAYS
    rel: dict[tuple, list] = {}
    for key, s in series.items():
        out, last_i = [None] * T, None
        for t, p in enumerate(s):
            if p is None:
                continue
            if last_i is not None and dnum[t] - dnum[last_i] <= MAX_GAP_DAYS:
                x = p / s[last_i]
                out[t] = x if REL_MIN <= x <= REL_MAX else None
            last_i = t
        rel[key] = out

    def chain(keys) -> list:
        idx, lvl = [], 100.0
        for t in range(T):
            rs = [rel[k][t] for k in keys if rel[k][t] is not None]
            if t > 0 and rs:
                lvl *= math.exp(sum(math.log(x) for x in rs) / len(rs))
            idx.append(round(lvl, 3))
        return idx

    keys_all = list(series)
    def keys_where(fn):
        return [k for k in keys_all if fn(meta[k[1]])]

    vendor_order = [v for v in ["Micron", "Samsung", "SK hynix", "Kioxia", "Winbond", "ISSI", "Alliance", "Macronix"] if any(b["vendor"] == v for b in basket)]
    vendor_order += sorted({b["vendor"] for b in basket} - set(vendor_order))
    cat_count = {}
    for b in basket:
        cat_count[b["category"]] = cat_count.get(b["category"], 0) + 1
    category_order = sorted(cat_count, key=lambda c: -cat_count[c])

    idx_all = chain(keys_all)
    idx_v = {v: chain(keys_where(lambda b, v=v: b["vendor"] == v)) for v in vendor_order}
    idx_c = {c: chain(keys_where(lambda b, c=c: b["category"] == c)) for c in category_order}

    L = T - 1
    def back(days):
        """Index of the latest pull at least `days` calendar days before the newest one."""
        if days is None:
            return 0 if L > 0 else None
        target = dnum[L] - days
        js = [i for i in range(L) if dnum[i] <= target]
        return js[-1] if js else None

    def chg(arr, days):
        j = back(days)
        if j is None or arr[j] in (None, 0):
            return None
        return arr[L] / arr[j] - 1

    j90 = next((i for i in range(T) if dnum[i] >= dnum[L] - 90), 0)
    def spark(arr):
        return arr[j90:]

    def lastval(arr):
        return arr[L]

    def med(xs):
        xs = sorted(x for x in xs if x is not None)
        if not xs:
            return None
        n = len(xs)
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

    def instock_share(keys, t):
        vals = [stock[k][t] for k in keys if stock[k][t] is not None]
        return (sum(1 for v in vals if v > 0) / len(vals)) if vals else None

    def lead_med(keys, t):
        return med([lead[k][t] for k in keys])

    def up_down(keys, t):
        rs = [rel[k][t] for k in keys if rel[k][t] is not None]
        return sum(1 for x in rs if x > 1.0005), sum(1 for x in rs if x < 0.9995), sum(1 for x in rs if x > 1.10), len(rs)

    def group_row(name, keys, arr):
        n_up, n_down, _, _ = up_down(keys, L)
        return {"name": name, "parts": len({k[1] for k in keys}), "index": arr[L], "d1": chg(arr, 1),
                "w1": chg(arr, 7), "m1": chg(arr, 30), "m3": chg(arr, 90), "since": chg(arr, None),
                "n_up": n_up, "n_down": n_down,
                "instock": instock_share(keys, L) or 0, "lead": lead_med(keys, L), "spark": spark(arr)}

    vendors = [group_row(v, keys_where(lambda b, v=v: b["vendor"] == v), idx_v[v]) for v in vendor_order]
    categories = [group_row(c, keys_where(lambda b, c=c: b["category"] == c), idx_c[c]) for c in category_order]

    # Product-level: chain across that SKU's distributors
    products = []
    for b in basket:
        m = b["mpn"]
        ks = [k for k in keys_all if k[1] == m]
        if not ks:
            continue
        arr = chain(ks)
        def lastp(d):
            s = series.get((d, m))
            if not s:
                return None
            return s[L]
        stk = [stock[k][L] for k in ks if stock[k][L] is not None]
        products.append({
            "mpn": m, "vendor": b["vendor"], "category": b["category"],
            "dk": lastp("digikey"), "mo": lastp("mouser"),
            "d1": chg(arr, 1), "w1": chg(arr, 7), "m1": chg(arr, 30), "since": chg(arr, None),
            "stock": int(sum(stk)) if stk else None, "lead": med([lead[k][L] for k in ks]),
            "spark": spark(arr),
            "_d": (arr[L] / arr[L - 1] - 1) if L > 0 and arr[L - 1] else None,
        })

    priced = [p for p in products if p["dk"] is not None or p["mo"] is not None]
    n_up, n_down, n_up_big, n_priced = up_down(keys_all, L)
    # count parts, not part/distributor pairs, for the headline
    # change vs the previous pull, per part
    part_wow = [p["_d"] for p in products if p.get("_d") is not None]
    n_up = sum(1 for x in part_wow if x > 0.0005)
    n_down = sum(1 for x in part_wow if x < -0.0005)
    n_up_big = sum(1 for x in part_wow if x > 0.10)

    instock_series = [instock_share(keys_all, t) for t in range(T)]
    lead_series = [lead_med(keys_all, t) for t in range(T)]
    j30 = back(30)
    kpis = {
        "index": idx_all[L], "d1": chg(idx_all, 1), "w1": chg(idx_all, 7), "m1": chg(idx_all, 30), "since": chg(idx_all, None),
        "n_up": n_up, "n_down": n_down, "n_up_big": n_up_big, "n_priced": len(part_wow) or len(priced),
        "lead": lead_series[L], "lead_d30": (lead_series[L] - lead_series[j30]) if j30 is not None and lead_series[L] is not None and lead_series[j30] is not None else None,
        "instock": instock_series[L] or 0, "instock_d30": (instock_series[L] - instock_series[j30]) if j30 is not None and instock_series[L] is not None and instock_series[j30] is not None else None,
    }
    for p in products:
        p.pop("_d", None)

    out = {
        # date of the newest price pulled, so the page only changes when the data does
        "generated": max(r["date"] for r in latest.values()) if not is_sample else datetime.now().date().isoformat(), "is_sample": is_sample,
        "latest_date": dates[-1], "dates": dates, "distributors": [{"digikey": "DigiKey", "mouser": "Mouser"}.get(d, d) for d in dists],
        "n_parts": len(products), "vendor_order": vendor_order, "category_order": category_order,
        "index": {"all": idx_all, "vendors": idx_v, "categories": idx_c},
        "vendors": vendors, "categories": categories, "products": products,
        "supply": {"instock": instock_series, "lead": lead_series},
        "kpis": kpis,
    }
    out["trends"] = trends(out)
    return out


def trends(d: dict) -> list[dict]:
    t = []
    k = d["kpis"]
    P = lambda x: "0.0%" if abs(x) < 0.0005 else f"{x * 100:+.1f}%"
    # use the longest window that has data: 1 month, else 1 week, else since the last pull
    win = next(((key, label) for key, label in (("m1", "the past month"), ("w1", "the past week"), ("d1", "the last day"))
                if k.get(key) is not None), None)
    if win:
        key, label = win
        v = k[key]
        move = "flat" if abs(v) < 0.0005 else f"{'up' if v > 0 else 'down'} {abs(v) * 100:.1f}%"
        t.append({"kind": "price", "text": f"Basket {move} over {label}; {k['n_up']} of {k['n_priced']} SKUs rose and {k['n_down']} fell in the latest pull."})
        vs = [x for x in d["vendors"] if x.get(key) is not None]
        if vs:
            hi = max(vs, key=lambda x: x[key]); lo = min(vs, key=lambda x: x[key])
            if hi[key] != lo[key]:
                t.append({"kind": "price", "text": f"{hi['name']} is the strongest vendor over {label} ({P(hi[key])}); {lo['name']} the weakest ({P(lo[key])})."})
        cs = [c for c in d["categories"] if c.get(key) is not None and abs(c[key]) >= 0.0005]
        if cs:
            hi = max(cs, key=lambda c: c[key])
            t.append({"kind": "price", "text": f"{hi['name']} leads categories over {label} at {P(hi[key])}."})
    steps = [v for v in d["vendors"] if v.get("d1") is not None and v["d1"] > 0.02]
    if steps:
        t.append({"kind": "price", "text": "Step-up in the latest pull, consistent with a price-increase notice landing: " + ", ".join(f"{v['name']} {P(v['d1'])}" for v in steps) + "."})
    if k["lead"] is not None:
        txt = f"Median quoted lead time {k['lead']:.0f} weeks"
        if k.get("lead_d30") is not None:
            txt += f", {k['lead_d30']:+.0f} vs a month ago"
        longest = max((v for v in d["vendors"] if v["lead"] is not None), key=lambda v: v["lead"], default=None)
        if longest:
            txt += f"; longest at {longest['name']} ({longest['lead']:.0f} wk)"
        t.append({"kind": "supply", "text": txt + "."})
    oos = [v for v in d["vendors"] if v["instock"] is not None]
    if oos:
        worst = min(oos, key=lambda v: v["instock"])
        txt = f"{k['instock'] * 100:.0f}% of the basket is in stock at distribution"
        if k.get("instock_d30") is not None:
            txt += f" ({k['instock_d30'] * 100:+.0f} pts vs a month ago)"
        t.append({"kind": "supply", "text": txt + f"; lowest at {worst['name']} ({worst['instock'] * 100:.0f}%)."})
    movers = sorted((p for p in d["products"] if p.get("d1") is not None and abs(p["d1"]) > 0.0005), key=lambda p: -abs(p["d1"]))[:3]
    if movers:
        t.append({"kind": "sku", "text": "Biggest SKU moves in the latest pull: " + ", ".join(f"{p['mpn']} ({p['vendor']}) {P(p['d1'])}" for p in movers) + "."})
    return t


# ----------------------------------------------------------------------------- outputs
def write_dashboard(data: dict, path: Path = DASHBOARD, standalone=True):
    tpl = TEMPLATE.read_text(encoding="utf-8")
    payload = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    html = tpl.replace("/*__DATA__*/null", payload)
    if standalone:
        html = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex"></head><body>\n'
                + html + "\n</body></html>\n")
    path.write_text(html, encoding="utf-8")


def write_xlsx(data: dict, obs: list[dict], path: Path = XLSX):
    try:
        import pandas as pd
    except ImportError:
        print("pandas not installed; skipping tracker.xlsx")
        return
    wk = data["dates"]
    idx = pd.DataFrame({"date": wk, "Basket": data["index"]["all"],
                        **{f"V: {k}": v for k, v in data["index"]["vendors"].items()},
                        **{f"C: {k}": v for k, v in data["index"]["categories"].items()}})
    strip = lambda rows: pd.DataFrame([{k: v for k, v in r.items() if k != "spark"} for r in rows])
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        idx.to_excel(xw, sheet_name="Index", index=False)
        strip(data["vendors"]).to_excel(xw, sheet_name="Vendors", index=False)
        strip(data["categories"]).to_excel(xw, sheet_name="Categories", index=False)
        strip(data["products"]).to_excel(xw, sheet_name="Products", index=False)
        pd.DataFrame({"date": wk, "instock_share": data["supply"]["instock"], "median_lead_wk": data["supply"]["lead"]}).to_excel(xw, sheet_name="Supply", index=False)
        pd.DataFrame(obs).to_excel(xw, sheet_name="Raw", index=False)


def cmd_build(args):
    basket, obs = load_basket(), read_obs()
    data = compute(obs, basket)
    out = getattr(args, "out", None)
    if out:   # website mode: <out>/index.html + <out>/tracker.xlsx
        d = (HERE / out) if not Path(out).is_absolute() else Path(out)
        d.mkdir(parents=True, exist_ok=True)
        (d / ".nojekyll").touch()
        data["download"] = "tracker.xlsx"
        write_dashboard(data, d / "index.html")
        write_xlsx(data, obs, d / "tracker.xlsx")
        print(f"Wrote {d / 'index.html'} and tracker.xlsx ({len(data['dates'])} days, {data['n_parts']} SKUs)")
    else:
        write_dashboard(data)
        write_xlsx(data, obs)
        print(f"Wrote {DASHBOARD.name} and {XLSX.name} ({len(data['dates'])} days, {data['n_parts']} SKUs)")


def cmd_run(args):
    cmd_pull(args)
    cmd_build(args)


# ----------------------------------------------------------------------------- sample data
def synthetic_obs(basket: list[dict], days: int = 120, seed: int = 7) -> list[dict]:
    """Made-up history for previewing the dashboard. Not real prices."""
    import random
    rnd = random.Random(seed)
    end = date.today()
    wks = [end - timedelta(days=days - 1 - i) for i in range(days)]
    # Vendor-level step increases on arbitrary days (sample only)
    steps = {"Micron": {15: 0.10, 50: 0.08, 95: 0.06}, "Samsung": {10: 0.12, 55: 0.09}, "SK hynix": {12: 0.11, 60: 0.08},
             "Kioxia": {30: 0.07, 90: 0.06}, "Winbond": {40: 0.05, 100: 0.04}, "ISSI": {70: 0.06},
             "Alliance": {75: 0.07}, "Macronix": {45: 0.04, 105: 0.03}}
    base_price = {"DDR5": 45.0, "DDR4": 40.0, "DDR3/DDR3L": 9.0, "LPDDR5/5X": 60.0, "LPDDR4/4X": 25.0,
                  "SDR & legacy DRAM": 3.5, "Raw MLC/TLC NAND": 40.0, "UFS": 45.0, "eMMC": 18.0, "SLC NAND": 3.5}
    out = []
    for b in basket:
        p0 = base_price.get(b["category"], 1.0) * rnd.uniform(0.4, 2.2)
        lead0 = rnd.choice([12, 16, 20, 26, 30])
        stock0 = rnd.randint(0, 40000)
        for d in ("digikey", "mouser"):
            p = p0 * rnd.uniform(0.97, 1.05)
            st, ld = stock0 * rnd.uniform(0.6, 1.4), lead0
            for i, w in enumerate(wks):
                if i in steps.get(b["vendor"], {}) and rnd.random() < 0.8:
                    p *= 1 + steps[b["vendor"]][i] * rnd.uniform(0.5, 1.8)
                elif rnd.random() < 0.006:
                    p *= rnd.uniform(0.98, 1.03)
                if b["vendor"] in ("Micron", "Samsung", "SK hynix") and i > 60 and rnd.random() < 0.012:
                    ld = min(70, ld + rnd.choice([2, 4, 8]))
                st = max(0, st * rnd.uniform(0.97, 1.025) - (40 if i > 70 else 0))
                if rnd.random() < 0.01:
                    continue  # occasional missing pull
                out.append({"date": w.isoformat(), "week": w.isoformat(), "distributor": d, "mpn": b["mpn"],
                            "manufacturer_returned": b["manufacturer"], "dist_pn": "", "price_1": round(p * 1.9, 4),
                            "price_1k": round(p, 4), "stock": int(st) if rnd.random() > 0.05 else 0,
                            "lead_weeks": ld, "status": "Active"})
    return out


def cmd_sample(args):
    basket = load_basket()
    obs = [{k: str(v) for k, v in r.items()} for r in synthetic_obs(basket)]
    data = compute(obs, basket, is_sample=True)
    path = Path(args.out) if args.out else HERE / "dashboard_sample.html"
    if path.suffix != ".html":   # a folder: write it as the website
        path.mkdir(parents=True, exist_ok=True)
        (path / ".nojekyll").touch()
        path = path / "index.html"
    write_dashboard(data, path, standalone=not args.fragment)
    print(f"Wrote {path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("pull", cmd_pull), ("run", cmd_run)):
        p = sub.add_parser(name)
        p.add_argument("--force", action="store_true", help="re-fetch SKUs already captured today")
        p.add_argument("--limit", type=int, default=0, help="only fetch the first N SKUs (testing)")
        p.add_argument("--only", nargs="+", default=["digikey", "mouser"], choices=["digikey", "mouser"])
        p.add_argument("--out", help="with run: folder for the website")
        p.set_defaults(fn=fn)
    p = sub.add_parser("build"); p.add_argument("--out", help="folder for the website (writes index.html)"); p.set_defaults(fn=cmd_build)
    p = sub.add_parser("sample"); p.add_argument("--out"); p.add_argument("--fragment", action="store_true"); p.set_defaults(fn=cmd_sample)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
