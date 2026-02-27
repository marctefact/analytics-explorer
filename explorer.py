"""
EXPLORER — Analytics Tag Auditor
==================================
Launches a headless browser, navigates user journeys defined in a JSON config,
and captures two output files per run:

    datalayer_snapshots_<run_id>.json   — datalayer object captured after each step
    network_requests_<run_id>.json      — filtered network beacon requests per step

Usage:
    python explorer.py
    python explorer.py --config path/to/site_inventory.json
    python explorer.py --config path/to/site_inventory.json --headed   (show browser)

Requirements:
    pip install playwright requests
    playwright install chromium
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import requests

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
except ImportError:
    print("ERROR: Playwright is not installed.")
    print("  pip install playwright")
    print("  playwright install chromium")
    sys.exit(1)


# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------
SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CFG = os.path.join(SCRIPT_DIR, "site_inventory.json")
OUTPUT_DIR  = os.path.join(SCRIPT_DIR, "output")


# ---------------------------------------------------------------------------
# CONFIG LOADING
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    """Load and do basic validation of the site inventory config."""
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    required = ["client", "base_url", "datalayer", "beacon_patterns", "journeys"]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise ValueError(f"site_inventory.json is missing required keys: {missing}")

    return cfg


# ---------------------------------------------------------------------------
# DATALAYER CAPTURE
# ---------------------------------------------------------------------------

def capture_datalayer(page, datalayer_cfg: dict) -> dict:
    """
    Read the datalayer object from the page.

    datalayer_cfg supports:
        {
            "variable": "utag_data",            // top-level JS variable name
            "fallback_variables": ["digitalData", "dataLayer"],  // optional fallbacks
            "extract_path": null                // optional dot-path into the object, e.g. "page.data"
        }
    """
    variable  = datalayer_cfg.get("variable", "utag_data")
    fallbacks = datalayer_cfg.get("fallback_variables", [])
    extract   = datalayer_cfg.get("extract_path")          # e.g. "transaction.products"

    js = _build_capture_js(variable, fallbacks, extract)

    try:
        result = page.evaluate(js)
        return result if result is not None else {"_status": "not_found"}
    except Exception as e:
        return {"_status": "capture_error", "_error": str(e)}


def _build_capture_js(variable: str, fallbacks: list, extract_path: str | None) -> str:
    """Build the JS snippet that reads the datalayer safely."""
    candidates = [variable] + fallbacks
    checks = []
    for var in candidates:
        path = f"{var}.{extract_path}" if extract_path else var
        checks.append(
            f"if (typeof {var} !== 'undefined') {{ "
            f"try {{ return JSON.parse(JSON.stringify({path})); }} catch(e) {{}} }}"
        )
    body = "\n".join(checks) + "\nreturn null;"
    return f"() => {{\n{body}\n}}"


# ---------------------------------------------------------------------------
# NETWORK BEACON CAPTURE
# ---------------------------------------------------------------------------

def compile_beacon_patterns(patterns: list) -> list:
    """Pre-compile beacon URL regex patterns from config."""
    return [re.compile(p) for p in patterns]


def is_beacon(url: str, compiled_patterns: list) -> bool:
    return any(p.search(url) for p in compiled_patterns)


def parse_beacon(url: str) -> dict:
    """Parse a beacon URL into a structured dict."""
    parsed = urlparse(url)
    raw_params = parse_qs(parsed.query)
    # Flatten single-value lists
    params = {k: (v[0] if len(v) == 1 else v) for k, v in raw_params.items()}
    return {
        "url":    url,
        "domain": parsed.netloc,
        "path":   parsed.path,
        "params": params,
    }


# ---------------------------------------------------------------------------
# PRE-FLIGHT (consent banners, MFA, etc.)
# ---------------------------------------------------------------------------

def run_pre_flight(page, pre_flight_cfg: dict, base_url: str):
    """
    Execute pre-flight steps once before any journeys run.
    Supports the same action types as journey steps plus an explicit 'mfa_wait' action.
    """
    steps = pre_flight_cfg.get("steps", [])
    if not steps:
        return

    print("  🛫  Running pre-flight steps...")
    for i, step in enumerate(steps):
        action = step.get("action")
        label  = step.get("label", f"pre_flight_step_{i}")
        print(f"       [{i+1}/{len(steps)}] {label} ({action})")

        try:
            _execute_action(page, step, base_url)
        except Exception as e:
            print(f"       ⚠️  Pre-flight step '{label}' failed: {e}")

    print("  ✅  Pre-flight complete.")


# ---------------------------------------------------------------------------
# ACTION EXECUTOR (shared by pre-flight + journeys)
# ---------------------------------------------------------------------------

def _execute_action(page, step: dict, base_url: str):
    """
    Execute a single step action. Supported actions:

        navigate        — go to base_url + step["url"]
        click           — click step["selector"]
        fill            — fill step["selector"] with step["value"]
        fill_form       — fill multiple fields: step["fields"] = {selector: value}
        select          — select option in a <select>: step["selector"], step["value"]
        wait_for        — wait for selector: step["selector"]
        wait_ms         — explicit wait: step["ms"]
        mfa_wait        — pause and prompt the operator to complete MFA in the browser
        press_key       — press a key: step["selector"] (optional), step["key"]
        scroll_to       — scroll element into view: step["selector"]
        hover           — hover over element: step["selector"]
    """
    action  = step.get("action")
    timeout = step.get("timeout_ms", 10000)

    if action == "navigate":
        url = step["url"]
        full_url = url if url.startswith("http") else base_url.rstrip("/") + url
        page.goto(full_url, wait_until=step.get("wait_until", "networkidle"), timeout=30000)

    elif action == "click":
        page.click(step["selector"], timeout=timeout)

    elif action == "fill":
        page.fill(step["selector"], step["value"], timeout=timeout)

    elif action == "fill_form":
        for selector, value in step.get("fields", {}).items():
            page.fill(selector, str(value), timeout=timeout)
        if "then_click" in step:
            page.click(step["then_click"], timeout=timeout)

    elif action == "select":
        page.select_option(step["selector"], step["value"], timeout=timeout)

    elif action == "wait_for":
        page.wait_for_selector(step["selector"], timeout=timeout)

    elif action == "wait_ms":
        page.wait_for_timeout(step.get("ms", 1000))

    elif action == "mfa_wait":
        # Pause execution so a human can complete MFA in a headed browser
        msg = step.get("message", "Complete MFA in the browser, then press ENTER to continue...")
        print(f"\n  🔐  {msg}")
        input("  ⏎  Press ENTER when ready: ")

    elif action == "press_key":
        if "selector" in step:
            page.press(step["selector"], step["key"], timeout=timeout)
        else:
            page.keyboard.press(step["key"])

    elif action == "scroll_to":
        page.locator(step["selector"]).scroll_into_view_if_needed(timeout=timeout)

    elif action == "hover":
        page.hover(step["selector"], timeout=timeout)

    else:
        print(f"       ⚠️  Unknown action '{action}' — skipping.")

    # Post-action wait to let the page/SPA settle
    post_wait = step.get("wait_ms", 0)
    if post_wait:
        page.wait_for_timeout(post_wait)


# ---------------------------------------------------------------------------
# JOURNEY RUNNER
# ---------------------------------------------------------------------------

def run_journey(page, journey: dict, base_url: str, beacon_patterns: list,
                datalayer_cfg: dict) -> tuple[list, list]:
    """
    Run all steps in a journey.
    Returns (datalayer_snapshots, network_request_records).
    """
    journey_name = journey.get("name", "unnamed")
    journey_type = journey.get("type", "standard")
    dl_snapshots = []
    net_records  = []

    for i, step in enumerate(journey.get("steps", [])):
        label = step.get("snapshot_label", f"step_{i}")
        step_beacons: list = []

        def on_request(request, _b=step_beacons):
            if is_beacon(request.url, beacon_patterns):
                _b.append(parse_beacon(request.url))

        page.on("request", on_request)

        error = None
        try:
            _execute_action(page, step, base_url)
        except (PwTimeout, Exception) as e:
            error = str(e)
            print(f"       ⚠️  Step '{label}' error: {error}")
        finally:
            page.remove_listener("request", on_request)

        # --- Datalayer snapshot ---
        dl_snapshot = {
            "journey":      journey_name,
            "journey_type": journey_type,
            "step_index":   i,
            "step_label":   label,
            "action":       step.get("action"),
            "url":          page.url,
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "datalayer":    capture_datalayer(page, datalayer_cfg),
        }
        if error:
            dl_snapshot["_error"] = error
        dl_snapshots.append(dl_snapshot)

        # --- Network beacon records ---
        for beacon in step_beacons:
            net_records.append({
                "journey":      journey_name,
                "journey_type": journey_type,
                "step_index":   i,
                "step_label":   label,
                "action":       step.get("action"),
                "timestamp":    datetime.now(timezone.utc).isoformat(),
                **beacon,
            })

        print(f"       ✓  {label}  —  {len(step_beacons)} beacon(s) captured")

    return dl_snapshots, net_records


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------

def save_outputs(run_id: str, client: str, dl_snapshots: list, net_records: list,
                 run_meta: dict):
    """Write the two output JSON files."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    slug = client.lower().replace(" ", "_")

    dl_path  = os.path.join(OUTPUT_DIR, f"datalayer_snapshots_{slug}_{run_id}.json")
    net_path = os.path.join(OUTPUT_DIR, f"network_requests_{slug}_{run_id}.json")

    dl_output = {
        "meta":      run_meta,
        "snapshots": dl_snapshots,
    }
    net_output = {
        "meta":     run_meta,
        "requests": net_records,
    }

    with open(dl_path, "w", encoding="utf-8") as f:
        json.dump(dl_output, f, indent=2, ensure_ascii=False)

    with open(net_path, "w", encoding="utf-8") as f:
        json.dump(net_output, f, indent=2, ensure_ascii=False)

    return dl_path, net_path


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Analytics Explorer")
    parser.add_argument("--config",  default=DEFAULT_CFG, help="Path to site_inventory.json")
    parser.add_argument("--headed",  action="store_true",  help="Run browser in headed mode (required for MFA)")
    args = parser.parse_args()

    cfg          = load_config(args.config)
    client       = cfg["client"]
    base_url     = cfg["base_url"].rstrip("/")
    datalayer    = cfg["datalayer"]
    journeys     = cfg["journeys"]
    pre_flight   = cfg.get("pre_flight", {})
    beacon_pats  = compile_beacon_patterns(cfg["beacon_patterns"])

    run_id   = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_meta = {
        "run_id":     run_id,
        "client":     client,
        "base_url":   base_url,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }

    all_dl_snapshots: list = []
    all_net_records:  list = []

    headless = not args.headed
    if not headless:
        print("🖥️  Running in HEADED mode (browser window will open)")

    print(f"\n🚀 Explorer starting")
    print(f"   Client  : {client}")
    print(f"   Base URL: {base_url}")
    print(f"   Run ID  : {run_id}")
    print(f"   Journeys: {len(journeys)}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=cfg.get(
                "user_agent",
                "AnalyticsQA-Explorer/2.0 (Playwright; Headless; Internal QA)"
            ),
        )
        page = context.new_page()

        # --- Pre-flight (consent banners, login, MFA) ---
        if pre_flight.get("steps"):
            run_pre_flight(page, pre_flight, base_url)
            print()

        # --- Run journeys ---
        for journey in journeys:
            print(f"🧭  Journey: {journey['name']}  ({len(journey.get('steps', []))} steps)")
            dl_snaps, net_recs = run_journey(
                page, journey, base_url, beacon_pats, datalayer
            )
            all_dl_snapshots.extend(dl_snaps)
            all_net_records.extend(net_recs)
            print()

        browser.close()

    run_meta["finished_at"]         = datetime.now(timezone.utc).isoformat()
    run_meta["total_dl_snapshots"]  = len(all_dl_snapshots)
    run_meta["total_net_records"]   = len(all_net_records)

    dl_path, net_path = save_outputs(
        run_id, client, all_dl_snapshots, all_net_records, run_meta
    )

    print("✅  Explorer complete.")
    print(f"   Datalayer snapshots : {dl_path}")
    print(f"   Network requests    : {net_path}")
    print()


if __name__ == "__main__":
    main()
