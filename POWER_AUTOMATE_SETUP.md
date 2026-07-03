# Power Automate Flow + SharePoint List Setup

## 1. SharePoint List: "StockAlerts"

Create a SharePoint list with these columns. Set the column types exactly as
listed so the Power Automate actions below work without conversion errors.

| Column Name | Type | Notes |
|---|---|---|
| Title | Single line of text | Default column; use it for "Symbol \| AlertType" for quick scanning |
| AlertType | Single line of text | e.g. "Breakout Alert", "Pocket Pivot Volume" |
| StockSymbol | Single line of text | e.g. "ZENSARTECH" — indexed for fast duplicate lookups |
| AlertDate | Date Only | Date portion of the alert timestamp |
| AlertTime | Single line of text | Stored as text (HH:MM:SS) since SharePoint's Date/Time column doesn't separate time-only well |
| Status | Choice | Values: New, Updated, Reviewed |
| Category | Choice | Values: Volume Spike, Fresh IV, RVOL, Breakout, Other |
| RSI | Number | Latest RSI value from Alpha Vantage (can be blank when unavailable) |
| Action | Choice | Values: Hold, Buy, Sell, No Data, Error |
| Count | Number | Number of times this symbol+category combo has fired within the dedup window |
| Notes | Multiple lines of text | Free text, e.g. "Duplicate suppressed at 14:32" |

**Indexing:** In List Settings → Indexed Columns, add an index on
`StockSymbol` and on `AlertDate`. Without this, the duplicate-check lookup
in the flow will be slow and may hit SharePoint's list-view threshold as
the list grows.

## 2. Power Automate Flow: "StockAlertIngestion"

### Trigger: "When an HTTP request is received"

Use this Request Body JSON Schema:

```json
{
    "type": "object",
    "properties": {
        "AlertType": { "type": "string" },
        "StockSymbol": { "type": "string" },
        "Category": { "type": "string" },
        "RSI": { "type": ["number", "null"] },
        "Action": { "type": "string" },
        "Date": { "type": "string" },
        "Time": { "type": "string" },
        "TimestampUtc": { "type": "string" },
        "Status": { "type": "string" },
        "Count": { "type": "integer" },
        "Notes": { "type": "string" },
        "IsDailyRefresh": { "type": "boolean" },
        "Timezone": { "type": "string" }
    }
}
```

    RSI decision mapping used by the listener:
    - RSI < 30 -> Hold
    - RSI 30-70 -> Buy
    - RSI > 70 -> Sell

Copy the generated HTTP POST URL into `POWER_AUTOMATE_URL` in your `.env`.

### Step 1: Validate shared secret (optional but recommended)
Add a Condition checking that the incoming header `X-Webhook-Secret` matches
the value you configured. If it doesn't match, respond with HTTP 401 and
terminate the run. This stops anyone who discovers the URL from injecting
fake records.

To read the header, use the expression:
`triggerOutputs()?['headers']?['X-Webhook-Secret']`

### Step 2: Get duplicate candidates from SharePoint
Action: **Get items** (SharePoint)
- Site: your SharePoint site
- List: StockAlerts
- Filter Query (OData):
  `StockSymbol eq '@{triggerBody()?['StockSymbol']}' and AlertDate eq '@{triggerBody()?['Date']}'`
- Order By: `Created desc`
- Top Count: 5

### Daily midnight branch (recommended)
Because the listener now posts a daily refresh signal at IST 12:00 AM,
insert a Condition immediately after the trigger:

- Expression: `@equals(triggerBody()?['IsDailyRefresh'], true)`

If **true**:
- Trigger your Power BI dataset refresh action
- Return HTTP 200 response
- Skip duplicate-check and SharePoint create/update steps

If **false**:
- Continue with the normal stock-alert ingestion flow below

### Step 3: Determine if any match is within the last 15 minutes
Use an **Apply to each** over the items from Step 2, with a **Condition**
inside comparing timestamps:

`@greater(triggerBody()?['TimestampUtc'], addMinutes(item()?['Created'], -15))`

Combined with a string match on Category, this identifies whether an
existing record for the same symbol+category was created within the last
15 minutes.

A cleaner approach that avoids nested loops: filter Step 2's results down to
matching Category using a **Filter array** action first (filter on
`Category eq '@{triggerBody()?['Category']}'`), then check if the filtered
array's length is greater than 0 and its single most recent item falls
inside the 15-minute window. This keeps the flow flat and easier to debug.

### Step 4: Branch — Update vs Create

**If a recent duplicate exists:**
Action: **Update item** (SharePoint)
- Id: the matched item's ID
- Count: `@{add(item()?['Count'], 1)}`
- Status: "Updated"
- AlertTime: the new alert's time (so the record reflects the latest hit)
- RSI: `@{triggerBody()?['RSI']}`
- Action: `@{coalesce(triggerBody()?['Action'], 'No Data')}`
- Notes: `@{concat(item()?['Notes'], '; repeated at ', triggerBody()?['Time'])}`

**If no recent duplicate:**
Action: **Create item** (SharePoint)
- Map every field directly from the trigger body
- RSI: `@{triggerBody()?['RSI']}`
- Action: `@{coalesce(triggerBody()?['Action'], 'No Data')}`
- Count: 1
- Status: "New"

### Step 5: Trigger Power BI dataset refresh
Action: **HTTP** (Power BI REST API) — or use the dedicated **"Refresh a
dataset"** Power BI connector action if available in your tenant.

If using the HTTP action directly against the Power BI REST API:
- Method: POST
- URI: `https://api.powerbi.com/v1.0/myorg/groups/{groupId}/datasets/{datasetId}/refreshes`
- Authentication: Active Directory OAuth, using a service principal or your
  own credentials with a Power BI Pro/PPU license
- This step should run after Step 4 completes, not on every single alert if
  alerts arrive in bursts — consider adding a **Delay** of 1–2 minutes before
  refresh, or better, scheduling a separate "refresh every 5 minutes" flow
  instead of refreshing on every webhook call. Triggering a full dataset
  refresh per alert can hit Power BI's refresh-frequency limits quickly if
  your group is active.

### Step 6: Respond to the caller
Action: **Response**
- Status Code: 200
- Body: `{"result": "ok"}`

This lets `telegram_listener.py` confirm successful delivery and avoid
unnecessary retries.

## 3. Recommended refinement: batch instead of per-alert refresh

Given that stock alert groups can be bursty (many alerts within seconds
during market open), consider splitting this into two flows:
1. **StockAlertIngestion** — exactly as above, but without Step 5.
2. **StockAlertBIRefresh** — a separate Scheduled flow running every 5–10
   minutes that triggers the Power BI refresh once, regardless of how many
   alerts arrived in that window.

This avoids refresh-quota issues and keeps the ingestion flow fast.
