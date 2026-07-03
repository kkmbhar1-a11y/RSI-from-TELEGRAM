# Power BI Dashboard Setup

## Data source
Connect Power BI Desktop to your SharePoint list:
`Get Data → SharePoint Online List → [your site URL] → StockAlerts`

Load the table, then in Power Query:
- Set `AlertDate` to Date type, `Count` to Whole Number
- Add a calculated column `AlertDateTime` by combining `AlertDate` and
  `AlertTime` for time-of-day analysis
- Add a calculated column `Hour` extracting the hour from `AlertTime`, useful
  for spotting which part of the trading day generates the most alerts

## Recommended visuals

### 1. Top Repeated Stocks
- Visual: Horizontal bar chart
- Axis: StockSymbol
- Value: Sum of Count (or COUNT of records if you prefer raw alert volume
  over deduplicated count)
- Sort descending, show top 10–15

### 2. Alert Frequency Over Time
- Visual: Line chart
- Axis: AlertDate (or AlertDateTime for intraday granularity)
- Value: Count of records
- Legend: Category, to see which alert type drives volume on which day

### 3. Breakout Count
- Visual: Card or KPI
- Value: COUNT of records where Category = "Breakout"
- Add a KPI visual comparing this week's breakout count to last week's for
  trend direction

### 4. Volume Spikes (Pocket Pivot Volume)
- Visual: Card + supporting bar chart broken down by StockSymbol
- Filter: Category = "Volume Spike"

### 5. Pocket Pivots
- If you want Pocket Pivot tracked separately from general Volume Spikes,
  refine the `categorize()` function in `telegram_listener.py` to assign it
  its own category value (currently grouped under "Volume Spike" since the
  source message uses "Pocket Pivot Volume"). Then add a dedicated card and
  trend visual for it, mirroring the Breakout Count visual.

### 6. Fresh IV Count
- Visual: Card + trend line
- Filter: Category = "Fresh IV"

## Suggested page layout
- **Page 1 — Overview:** KPI cards for each category count (Breakout, Volume
  Spike, Fresh IV, RVOL, Other) across the top, with the alert frequency line
  chart and top repeated stocks bar chart below.
- **Page 2 — Stock Drilldown:** A table or matrix with StockSymbol as rows,
  Category as columns, Count as values, with a slicer for date range so you
  can isolate any trading session.

## Refresh setup
Publish the report to the Power BI Service, then either:
- Use the dataset's scheduled refresh settings (up to 8 times/day on Pro), or
- Rely on the `StockAlertBIRefresh` flow described in
  `POWER_AUTOMATE_SETUP.md` for more frequent, on-demand refreshes during
  market hours.

If you need true near-real-time visuals (sub-minute), look into Power BI
**Streaming datasets / DirectQuery** instead of scheduled refresh — that's a
larger architectural change and worth a separate conversation if low latency
becomes a hard requirement.
