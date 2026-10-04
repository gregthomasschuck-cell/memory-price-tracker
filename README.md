# Memory Price Tracker

Daily DRAM and NAND spot and contract prices. A GitHub Action reads every free price
table on TrendForce's price pages each morning, adds anything new to the history in
`data/prices.csv`, and rebuilds a dashboard that GitHub Pages serves as a website:
DDR4/DDR5 spot vs contract, NAND wafer spot vs contract, a daily price board covering
all ~40 items, the monthly contract history and TrendForce's quarterly outlook.

Website: https://gregthomasschuck-cell.github.io/memory-price-tracker/

No API keys are needed.

## Supply-chain section

Below the memory charts the page tracks four slower Asia tech supply-chain signals:

| Series | File | How it updates |
|---|---|---|
| LCD panel prices (TV, monitor, notebook) | `data/history_panel.csv` + captured `panel` table | TrendForce's free panel table, captured by the daily Action |
| Korea 1st–10th / 1st–20th / full-month exports (total, semis, computers) | `data/korea_exports.csv` | Added by a Claude scheduled task after each customs release (~1st, 11th, 21st) |
| G75 e-glass yarn, 7628 e-glass cloth, CCL / copper-foil / low-CTE glass price moves | `data/pcb_materials.csv` | Added weekly by the Claude scheduled task |
| Gallium, germanium, antimony, rare earths (PrNd, Dy, Tb), tungsten APT, WF6, helium | `data/minerals.csv` | Added weekly by the Claude scheduled task |

These three CSVs can also be edited by hand on GitHub; the site rebuilds on every change.

## Sources

| Data | Where it comes from |
|---|---|
| Daily tables (DRAM chip, module and GDDR spot; NAND chip, wafer and memory-card spot; DRAM and NAND contract; PC OEM SSD contract; SSD street prices) | trendforce.com/price/dram/dram_spot and trendforce.com/price/flash/flash_spot, captured each run |
| Weekly spot history, Jan 2025 – Sep 2026 (DDR4 1Gx8 3200 and 512Gb TLC wafer) | The "this week" averages in TrendForce's weekly *Memory Spot Price Update* articles (`data/history_weekly.csv`, source link per row) |
| Monthly contract history, Jan 2024 – Sep 2026 (PC DRAM DDR4 8Gb, NAND 128Gb MLC) | DRAMeXchange month averages as reported in the press (`data/history_contract.csv`, source link per row). Later months come from the captured contract tables. |
| Quarterly contract outlook | TrendForce press releases, kept by hand in `data/outlook.json` |

Each TrendForce table has its own update schedule (DRAM chip spot daily, wafer and module
spot weekly, contract twice a month, SSD contract quarterly). A run only records a table
when its "Last Update" stamp is new, so nothing is double-counted.

## Schedule

Runs daily at 11:17 UTC (7:17 am New York in summer), the same time as the Analog tracker.
Actions tab → *Price pull* → *Run workflow* runs it on demand. Editing the template, the
code or the history files rebuilds the site automatically.

## Run it locally

```
pip install -r requirements.txt
python tracker.py pull               # capture today's tables
python tracker.py build --out docs   # rebuild the site
```

## Files

| Path | What it is |
|---|---|
| `tracker.py` | Capture and dashboard builder |
| `dashboard_template.html` | Dashboard layout (data is injected at build time) |
| `data/prices.csv` | Every table row captured, append-only |
| `data/history_weekly.csv`, `data/history_contract.csv` | History from before daily capture began |
| `data/outlook.json` | TrendForce quarterly contract guide |
| `docs/` | The website GitHub Pages serves (rebuilt by the Action) |
| `.github/workflows/price-pull.yml` | The daily schedule |
