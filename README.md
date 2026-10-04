# Memory Price Tracker

A daily fixed-SKU price index for DRAM and NAND memory (150 parts from Micron, Samsung, SK hynix, Kioxia, Winbond, ISSI, Alliance Memory and Macronix). A GitHub Action pulls the
1,000-unit price, stock and quoted lead time for every part in `basket.csv` from the
DigiKey and Mouser APIs, saves the history to `data/observations.csv`, and rebuilds a
dashboard that GitHub Pages serves as a website: vendor pricing, category pricing,
product pricing, supply (in-stock share and lead times) and auto-written key trends.

Until the first pull runs, the site shows synthetic sample data, clearly labeled.

## Set it up (about 20 minutes)

1. **Get the API keys** (both free):
   * **DigiKey:** sign in at developer.digikey.com → Organizations → create one →
     Production Apps → *Create Production App* → tick **ProductInformation V4**.
     Copy the Client ID and Client Secret. Quota: 1,000 calls/day.
   * **Mouser:** sign in at mouser.com → My Account → APIs (mouser.com/api-search) →
     request a **Search API** key. Quota: 1,000 calls/day, 10 parts per call.
2. **Create the repo** on GitHub and upload these files (keep the folder structure,
   including the hidden `.github` folder), or push them with git.
3. **Add the keys as secrets:** repo → Settings → Secrets and variables → Actions →
   *New repository secret*, three times:
   `DIGIKEY_CLIENT_ID`, `DIGIKEY_CLIENT_SECRET`, `MOUSER_API_KEY`.
   Never commit keys to the repo.
4. **Turn on the website:** Settings → Pages → Source: *Deploy from a branch* →
   Branch: `main`, folder: `/docs` → Save. The site appears at
   `https://<your-username>.github.io/<repo-name>/` within a minute or two.
5. **Run the first pull:** Actions tab → *Price pull* → *Run workflow*. A full basket
   takes about 5 minutes. When it finishes, the site switches from sample to live data.
6. Open `data/not_found.csv` and fix any part numbers the distributors didn't recognise
   in `basket.csv` (edit it right on GitHub). Corrected parts are picked up next run.

After that it runs on its own every day at 11:17 UTC. The index gets its first
day-over-day reading with the second day of data; 1W, 1M and 3M changes fill in as
history builds. Editing `basket.csv`, `tracker.py` or the dashboard template on GitHub
rebuilds the site automatically.

**Visibility.** On a free GitHub account, Pages only serves public repos, and the site is
visible to anyone with the link (the page asks search engines not to index it). A private
site needs GitHub Enterprise Cloud with private Pages.

## Editing the basket

`basket.csv` has four columns: `mpn, manufacturer, vendor, category`. Add or remove rows
any time. New parts join the index once they have two days of prices, so additions never
cause a jump. Use exact, orderable distributor part numbers including the package suffix
(e.g. `MT40A512M16TB-062E:R`, not `MT40A512M16`). Part numbers containing commas need quotes around them.

**Scaling to thousands of parts.** Mouser covers ~10,000 parts/day (10 per call). DigiKey
uses one or two calls per part, so a daily basket tops out around 500–1,000 parts on the
default quota. DigiKey raises the quota on request through the developer portal.

## How the index works

* **Price:** the unit price at the 1,000-piece break (the largest break ≤ 1,000). At DigiKey
  the cheapest packaging option is used (cut tape vs reel).
* **Chain:** each day, every part/distributor pair priced both today and at its previous
  observation (≤ 14 days back) contributes a price relative. The index is the chained
  geometric mean of those relatives, starting at 100. Vendor and category indices use the
  same method on their subset.
* **Data-error guard:** relatives below 0.5x or above 2.0x are excluded (usually a listing or
  packaging change, not a real price move).
* **Supply:** in-stock share = parts with distributor stock > 0. Lead time = median
  manufacturer lead time quoted by the distributor.

**Caveats.** Distributor list prices lag contract and spot pricing, and the big three DRAM makers
sell little through distribution, so read this as a channel signal and cross-check against
DRAMeXchange spot and contract prices. Out-of-stock parts often have frozen list prices; read price moves alongside
the in-stock chart.

## Run it locally (optional)

```
pip install -r requirements.txt
cp config.example.json config.json      # paste your keys in
python tracker.py pull --limit 5        # quick test on 5 parts
python tracker.py run                   # full pull + dashboard.html + tracker.xlsx
python tracker.py sample                # preview the layout with synthetic data
```

## Files

| Path | What it is |
|---|---|
| `basket.csv` | The parts tracked |
| `tracker.py` | Pull, index, and dashboard/Excel builder |
| `dashboard_template.html` | Dashboard layout (data is injected at build time) |
| `data/observations.csv` | Every price pulled, append-only. This is the history. |
| `data/not_found.csv` | Part numbers a distributor didn't recognise |
| `docs/` | The website GitHub Pages serves (rebuilt by the Action) |
| `.github/workflows/price-pull.yml` | The daily schedule |
