# Analytics Explorer

Headless browser tool that walks configured user journeys on a website, captures datalayer snapshots and filtered network beacon requests at each step, and writes two timestamped JSON files for downstream auditing.

---

## Output files

| File | Contents |
|---|---|
| `datalayer_snapshots_<client>_<run_id>.json` | One entry per journey step — the full datalayer object as it existed after the action fired |
| `network_requests_<client>_<run_id>.json` | One entry per matched beacon request — URL, domain, path, and parsed query params |

Both files share the same `meta` block (run ID, client, timestamps) so they can be correlated.

---

## Setup

```bash
pip install playwright requests
playwright install chromium
```

---

## Running

```bash
# Default config (site_inventory.json next to explorer.py)
python explorer.py

# Custom config path
python explorer.py --config clients/acme/site_inventory.json

# Headed mode — required when the journey includes an mfa_wait step
python explorer.py --headed
```

---

## site_inventory.json — field reference

### Top level

| Key | Type | Description |
|---|---|---|
| `client` | string | Client name — used in output filenames |
| `base_url` | string | Root URL of the acceptance environment |
| `user_agent` | string | Optional. Override the browser user-agent string |
| `datalayer` | object | See below |
| `beacon_patterns` | array | Regex strings to match trackable network requests |
| `pre_flight` | object | Steps that run once before any journey (consent, login, MFA) |
| `journeys` | array | Named sequences of steps to execute |

---

### `datalayer`

```json
{
  "variable": "utag_data",
  "fallback_variables": ["digitalData", "dataLayer[0]"],
  "extract_path": null
}
```

| Key | Description |
|---|---|
| `variable` | Primary JS variable name to read |
| `fallback_variables` | Tried in order if the primary variable is `undefined` |
| `extract_path` | Dot-path into the object, e.g. `"page.analytics"`. Leave `null` to capture the whole object |

---

### `beacon_patterns`

A list of regex strings. Any network request whose URL matches at least one pattern is captured.

```json
"beacon_patterns": [
  ".*\\.omtrdc\\.net.*",
  ".*\\.google-analytics\\.com.*",
  ".*metrics\\.yourclient\\.com.*"
]
```

---

### `pre_flight`

Runs before all journeys — and is automatically re-run after each `reset_before_run` context reset. Typical use: accept cookie consent, log in, complete MFA.

```json
"pre_flight": {
  "steps": [
    { "action": "navigate", "url": "/" },
    { "action": "click", "selector": "#accept-cookies", "wait_ms": 500 },
    { "action": "fill_form", "fields": { "#user": "qa@test.com", "#pass": "secret" }, "then_click": "#login" },
    { "action": "mfa_wait", "message": "Complete MFA, then press ENTER." }
  ]
}
```

> `mfa_wait` pauses the script and prompts the operator. Requires `--headed` so the browser window is visible.

---

### Journey-level keys

| Key | Type | Default | Description |
|---|---|---|---|
| `name` | string | required | Display name — supports `{{variable}}` placeholders when using `foreach` |
| `type` | string | `"standard"` | `"standard"` or `"spa"` — informational, stored in output |
| `reset_before_run` | boolean | `false` | Opens a fresh browser context (clean cookies, cart, session) before this journey runs. Pre-flight re-runs automatically in the new context |
| `foreach` | object | — | Repeat the journey for each value in a list. See below |

---

### `foreach` — looping journeys

Use `foreach` to run the same journey template multiple times, once per value. Values can be plain strings or named-variable dicts. Any `{{key}}` placeholder in step fields is substituted per iteration.

```json
"foreach": {
  "variable": "plp",
  "values": [
    { "label": "Women", "url": "/shop/women", "category": "women" },
    { "label": "Men",   "url": "/shop/men",   "category": "men" }
  ]
}
```

The journey name and all step fields (`url`, `snapshot_label`, `selector`, `match_text`, `value`, field values) support `{{variable}}` substitution. For example, `"snapshot_label": "plp_{{label}}"` becomes `"plp_Women"` and `"plp_Men"` in the output.

Each foreach iteration produces its own set of snapshots, tagged with a `foreach_context` field in the output JSON so iterations are easy to distinguish.

---

### Step actions

Every step in `pre_flight.steps` and `journeys[*].steps` supports these actions:

| `action` | Required keys | Optional keys | Description |
|---|---|---|---|
| `navigate` | `url` | `wait_until`, `wait_ms` | Navigate to URL (absolute or relative to `base_url`) |
| `click` | `selector` | `timeout_ms`, `wait_ms` | Click a specific element |
| `click_random` | `selector` | `timeout_ms`, `wait_ms` | Find all matching elements and click one at random — useful for PLPs |
| `click_match` | `selector`, `match_text` | `timeout_ms`, `wait_ms` | Click the element whose visible text contains `match_text` — useful for payment method selection |
| `fill` | `selector`, `value` | `timeout_ms` | Fill a single input |
| `fill_form` | `fields` | `then_click`, `timeout_ms`, `wait_ms` | Fill multiple inputs, optionally submit |
| `select` | `selector`, `value` | `timeout_ms` | Choose a `<select>` option |
| `wait_for` | `selector` | `timeout_ms` | Wait for an element to appear in the DOM |
| `wait_ms` | `ms` | — | Explicit pause in milliseconds |
| `mfa_wait` | — | `message` | Pause and prompt operator to complete MFA (headed mode only) |
| `press_key` | `key` | `selector` | Press a keyboard key, optionally scoped to an element |
| `scroll_to` | `selector` | — | Scroll element into view |
| `hover` | `selector` | — | Hover over element |

---

### Journey step extra keys

| Key | Description |
|---|---|
| `snapshot_label` | Label used in output JSON to identify this step. Supports `{{variable}}` placeholders. Defaults to `step_<index>` |
| `wait_ms` | Extra wait (ms) after the action fires, before the snapshot is taken |

---

## Configured journeys

The template `site_inventory.json` includes three journeys as a starting point:

**PLP → PDP → Checkout** loops over all product listing pages using `foreach`. For each PLP it clicks a random product (`click_random`), adds it to cart, proceeds to checkout, selects Credit Card via `click_match`, fills in card details, and waits for the order confirmation screen. `reset_before_run: true` ensures each PLP iteration starts with a clean browser context and empty cart.

**Appointment Booking Funnel** runs once, navigates to the booking page, selects a service and a date, fills in patient details, submits, and waits for the confirmation screen.

**Homepage** is a simple single-page check — navigates to `/` and captures the datalayer state on load.

---

## Output schema

### `datalayer_snapshots_*.json`

```json
{
  "meta": {
    "run_id": "20240315_143022",
    "client": "ACME Corp",
    "base_url": "https://acceptance.acmecorp.com",
    "started_at": "2024-03-15T14:30:22Z",
    "finished_at": "2024-03-15T14:38:51Z",
    "total_dl_snapshots": 38,
    "total_net_records": 72
  },
  "snapshots": [
    {
      "journey": "PLP → PDP → Checkout [Women]",
      "journey_type": "spa",
      "foreach_context": { "label": "Women", "url": "/shop/women", "category": "women" },
      "step_index": 0,
      "step_label": "plp_Women",
      "action": "navigate",
      "url": "https://acceptance.acmecorp.com/shop/women",
      "timestamp": "2024-03-15T14:30:28Z",
      "datalayer": {
        "page_name": "shop:women",
        "page_type": "category",
        "user_id": "qa-tester-001"
      }
    }
  ]
}
```

### `network_requests_*.json`

```json
{
  "meta": { "...": "same meta block" },
  "requests": [
    {
      "journey": "PLP → PDP → Checkout [Women]",
      "journey_type": "spa",
      "foreach_context": { "label": "Women", "url": "/shop/women", "category": "women" },
      "step_index": 0,
      "step_label": "plp_Women",
      "action": "navigate",
      "timestamp": "2024-03-15T14:30:29Z",
      "url": "https://metrics.acmecorp.com/b/ss/acmeprod/1/...",
      "domain": "metrics.acmecorp.com",
      "path": "/b/ss/acmeprod/1/",
      "params": {
        "pageName": "shop:women",
        "events": "event1",
        "v10": "category"
      }
    }
  ]
}
```

---

## Adding a new client

1. Copy `site_inventory.json` → `clients/<client_name>/site_inventory.json`
2. Update `client`, `base_url`, `datalayer.variable`, `beacon_patterns`
3. Replace `pre_flight` steps with the client's actual consent/login flow
4. Define the journeys you want to audit — use `foreach` for any funnel that needs to run across multiple entry points
5. Run: `python explorer.py --config clients/<client_name>/site_inventory.json`
