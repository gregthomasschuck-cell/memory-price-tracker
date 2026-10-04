"""
Memory Price Tracker

Daily DRAM and NAND spot and contract prices from TrendForce's free price tables
(trendforce.com/price), kept as a growing history and published as a dashboard.

    python tracker.py pull              # capture today's tables into data/prices.csv
    python tracker.py build --out docs  # rebuild the website from all data
    python tracker.py run --out docs    # both
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
PRICES = DATA / "prices.csv"            # every table snapshot captured, append-only
WEEKLY = DATA / "history_weekly.csv"     # weekly spot history before daily capture began
CONTRACT = DATA / "history_contract.csv" # monthly contract history before daily capture began
OUTLOOK = DATA / "outlook.json"          # TrendForce quarterly contract guide (edit by hand)
TEMPLATE = HERE / "dashboard_template.html"

PAGES = ["https://www.trendforce.com/price/dram/dram_spot",
         "https://www.trendforce.com/price/flash/flash_spot"]
FIELDS = ["captured", "table", "title", "updated", "item", "high", "low", "avg", "chg"]

# Contract tables that extend the monthly contract history
DDR4_CONTRACT = ("dram_contract", "DDR4 8Gb 1Gx8")
MLC_CONTRACT = ("flash_contract", "NAND 128Gb 16Gx8 MLC")


def num(s):
    s = str(s or "").replace(",", "").replace("%", "").replace("▲", "").replace("▼", "").replace("—", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


# ----------------------------------------------------------------------------- pull
def parse_page(html: str) -> list[dict]:
    """Every free price table on a TrendForce price page, as flat rows."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for sec in soup.select("div.price-content"):
        table = sec.select_one("table.price-table")
        heads = [th.get_text(" ", strip=True) for th in table.select("thead th")] if table else []
        if not heads:               # member-only tables have no header row
            continue
        upd = sec.select_one(".price-last-update")
        m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", upd.get_text() if upd else "")
        if not m:
            continue
        updated = f"{m.group(1)}T{m.group(2)}+08:00"
        title_el = sec.select_one(".price-title")
        title = title_el.get_text(" ", strip=True) if title_el else sec.get("id", "")

        def col(*names):
            for n in names:
                if n in heads:
                    return heads.index(n)
            return None
        c_high = col("Daily High", "Weekly High", "Session High", "High")
        c_low = col("Daily Low", "Weekly Low", "Session Low", "Low")
        c_avg = col("Session Average", "Average")
        c_chg = col("Session Change", "Average Change", "Change")
        if c_avg is None:
            continue
        for tr in table.select("tbody tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) <= c_avg:
                continue
            if heads[0] == "Brand":  # SSD street price: name = brand + series + capacity
                item = " ".join(x for x in (cells[0], cells[2], cells[3]) if x)
            else:
                item = re.sub(r"\s+", " ", cells[0]).strip()
            g = lambda i: num(cells[i]) if i is not None and i < len(cells) else None
            rows.append({"table": sec.get("id"), "title": title, "updated": updated, "item": item,
                         "high": g(c_high), "low": g(c_low), "avg": g(c_avg), "chg": g(c_chg)})
    return rows


def read_prices() -> list[dict]:
    if not PRICES.exists():
        return []
    with open(PRICES, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def cmd_pull(args):
    import requests
    seen = {(r["table"], r["updated"], r["item"]) for r in read_prices()}
    captured = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new, failed = [], []
    for url in PAGES:
        try:
            r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0 (memory-price-tracker)"})
            r.raise_for_status()
            rows = parse_page(r.text)
            if not rows:
                failed.append(f"{url}: no tables found")
            for row in rows:
                if (row["table"], row["updated"], row["item"]) not in seen:
                    new.append({"captured": captured, **row})
        except Exception as e:  # one page failing must not lose the other
            failed.append(f"{url}: {e}")
    DATA.mkdir(exist_ok=True)
    first = not PRICES.exists()
    with open(PRICES, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if first:
            w.writeheader()
        for r in new:
            w.writerow({k: "" if r.get(k) is None else r[k] for k in FIELDS})
    tables = sorted({r["table"] for r in new})
    print(f"Captured {len(new)} new rows from {len(tables)} updated tables: {', '.join(tables) or 'none'}")
    for m in failed:
        print("FAILED", m)
    if len(failed) == len(PAGES):
        sys.exit(1)


# ----------------------------------------------------------------------------- build
def snapshots(prices: list[dict]) -> list[dict]:
    """Group price rows into one snapshot per table update."""
    snaps = {}
    for r in prices:
        k = (r["table"], r["updated"])
        s = snaps.setdefault(k, {"table": r["table"], "title": r["title"], "updated": r["updated"],
                                 "date": r["updated"][:10], "rows": []})
        s["rows"].append({"item": r["item"], "high": num(r["high"]), "low": num(r["low"]),
                          "avg": num(r["avg"]), "chg": num(r["chg"])})
    return sorted(snaps.values(), key=lambda s: s["updated"])


def compute(prices: list[dict]) -> dict:
    spot = snapshots(prices)
    nz = lambda v: None if v in ("", None) else float(v)
    with open(WEEKLY, newline="", encoding="utf-8") as f:
        weekly = [{"date": r["date"], "ddr4_8gb": nz(r["ddr4_8gb"]), "tlc512_wafer": nz(r["tlc512_wafer"]),
                   "note": r["note"], "url": r["url"]} for r in csv.DictReader(f)]
    with open(CONTRACT, newline="", encoding="utf-8") as f:
        contract = {r["month"]: {"month": r["month"], "ddr4_8gb": nz(r["ddr4_8gb"]),
                                 "nand_128gb_mlc": nz(r["nand_128gb_mlc"]), "url": r["url"]} for r in csv.DictReader(f)}
    # Extend the monthly contract history from captured contract tables: a month's value is its latest print.
    last_hist = max(contract) if contract else ""
    for (table, item), key, page in ((DDR4_CONTRACT, "ddr4_8gb", "dram/dram_contract"),
                                     (MLC_CONTRACT, "nand_128gb_mlc", "flash/flash_contract")):
        for s in spot:
            month = s["updated"][:7]
            if s["table"] != table or month <= last_hist:
                continue
            row = next((x for x in s["rows"] if x["item"] == item), None)
            if row and row["avg"] is not None:
                c = contract.setdefault(month, {"month": month, "ddr4_8gb": None, "nand_128gb_mlc": None,
                                                "url": "https://www.trendforce.com/price/" + page})
                c[key] = row["avg"]
    outlook = json.loads(OUTLOOK.read_text(encoding="utf-8")) if OUTLOOK.exists() else {"rows": []}
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "weekly": weekly,
        "contract": [contract[m] for m in sorted(contract)],
        "spot": spot,
        "outlook": outlook,
    }


def cmd_build(args):
    data = compute(read_prices())
    payload = json.dumps(data, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)
    html = ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex"></head><body>\n'
            + html + "\n</body></html>\n")
    out = Path(args.out) if args.out else HERE / "docs"
    out.mkdir(parents=True, exist_ok=True)
    (out / ".nojekyll").touch()
    (out / "index.html").write_text(html, encoding="utf-8")
    print(f"Wrote {out / 'index.html'}: {len(data['spot'])} table snapshots, "
          f"{len(data['weekly'])} weekly points, {len(data['contract'])} contract months")


def cmd_run(args):
    cmd_pull(args)
    cmd_build(args)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("pull", cmd_pull), ("build", cmd_build), ("run", cmd_run)):
        p = sub.add_parser(name)
        p.add_argument("--out", help="website folder (default: docs)")
        p.set_defaults(fn=fn)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
