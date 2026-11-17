"""
Hollister restock watcher.

Checks Hollister product pages on a timer and sends a WhatsApp (CallMeBot)
or SMS (Twilio) message to every recipient when a product / size goes from
"sold out" to "in stock".

Usage:
    python hollister_restock.py            # run forever
    python hollister_restock.py --once     # check once and exit
    python hollister_restock.py --test     # send a test message to everyone
"""

import json
import re
import sys
import time
import random
import argparse
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests

BASE = Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"
STATE_FILE = BASE / "state.json"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# --------------------------------------------------------------------------
# Fetching the page
# --------------------------------------------------------------------------

def fetch_html(url):
    """Plain HTTP first. If Hollister's bot protection blocks it, fall back to
    a real headless browser (Playwright) when it is installed."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200 and len(r.text) > 5000 and "Access Denied" not in r.text:
            return r.text
        log(f"  plain request got HTTP {r.status_code} / blocked, trying browser...")
    except requests.RequestException as e:
        log(f"  plain request failed ({e}), trying browser...")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Hollister blocked the plain request. Install the browser fallback:\n"
            "    pip install playwright\n    python -m playwright install chromium")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=HEADERS["User-Agent"], locale="de-DE")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)  # let the size picker render
        html = page.content()
        browser.close()
        return html


# --------------------------------------------------------------------------
# Working out what is in stock
# --------------------------------------------------------------------------

def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def parse_stock(html):
    """Returns (overall_in_stock: bool|None, sizes: {size_label: bool})."""
    sizes = {}
    any_in = any_out = False

    # 1) schema.org JSON-LD offers (most reliable when present)
    for block in re.findall(
            r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
            html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for node in _walk(data):
            avail = node.get("availability")
            if not isinstance(avail, str):
                continue
            in_stock = avail.lower().endswith(("instock", "limitedavailability"))
            any_in |= in_stock
            any_out |= not in_stock
            label = node.get("size") or node.get("name") or node.get("sku")
            if label:
                sizes[str(label)] = sizes.get(str(label), False) or in_stock

    # 2) size buttons in the HTML (disabled / "sold out" = not available)
    for m in re.finditer(r'<(?:button|input|li)[^>]*(?:data-size|aria-label|data-value)="([^"]{1,12})"[^>]*>',
                         html, re.I):
        tag, label = m.group(0), m.group(1).strip()
        if not re.fullmatch(r"(XXS|XS|S|M|L|XL|XXL|\d{2}(?:/\d{2})?|\d{1,2}[WL]?(?:\s?x\s?\d{2})?)", label, re.I):
            continue
        out = re.search(r'disabled|sold.?out|out.?of.?stock|unavailable|ausverkauft', tag, re.I)
        sizes.setdefault(label, not out)

    if any_in or any_out:
        overall = any_in
    else:
        low = html.lower()
        if re.search(r"add to bag|in den warenkorb|zur tasche hinzufügen", low) and \
                not re.search(r'"availability"\s*:\s*"[^"]*outofstock', low):
            overall = True
        elif re.search(r"sold out|out of stock|ausverkauft|nicht vorrätig", low):
            overall = False
        else:
            overall = None
    if sizes and overall is None:
        overall = any(sizes.values())
    return overall, sizes


def size_matches(label, wanted):
    a = re.sub(r"\s+", "", label).upper()
    b = re.sub(r"\s+", "", wanted).upper()
    return a == b or re.search(rf"(^|[^A-Z0-9]){re.escape(b)}($|[^A-Z0-9])", a) is not None


def is_available(product, overall, sizes):
    wanted = product.get("sizes") or []
    if not wanted:
        return overall
    if not sizes:
        return None  # can't tell sizes apart on this page
    hits = [ok for label, ok in sizes.items() if any(size_matches(label, w) for w in wanted)]
    return any(hits) if hits else False


# --------------------------------------------------------------------------
# Sending messages
# --------------------------------------------------------------------------

def send_whatsapp_callmebot(cfg, number, text):
    key = cfg["whatsapp_callmebot"]["api_keys"].get(number)
    if not key or key.startswith("PUT_"):
        raise RuntimeError(f"No CallMeBot API key set for {number} in config.json")
    url = (f"https://api.callmebot.com/whatsapp.php?phone={quote(number)}"
           f"&text={quote(text)}&apikey={quote(key)}")
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"CallMeBot HTTP {r.status_code}: {r.text[:200]}")


def send_twilio_sms(cfg, number, text):
    t = cfg["twilio_sms"]
    r = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{t['account_sid']}/Messages.json",
        data={"From": t["from_number"], "To": number, "Body": text},
        auth=(t["account_sid"], t["auth_token"]), timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Twilio HTTP {r.status_code}: {r.text[:200]}")


def notify_all(cfg, text):
    sender = {"whatsapp_callmebot": send_whatsapp_callmebot,
              "twilio_sms": send_twilio_sms}[cfg["notifier"]]
    for number in cfg["recipients"]:
        try:
            sender(cfg, number, text)
            log(f"  message sent to {number}")
        except Exception as e:
            log(f"  !! could not message {number}: {e}")
        time.sleep(2)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def check_all(cfg, state):
    for product in cfg["products"]:
        url, name = product["url"], product.get("name", product["url"])
        try:
            overall, sizes = parse_stock(fetch_html(url))
        except Exception as e:
            log(f"{name}: error - {e}")
            continue

        available = is_available(product, overall, sizes)
        size_info = ", ".join(f"{k}:{'yes' if v else 'no'}" for k, v in sizes.items()) or "n/a"
        log(f"{name}: {'IN STOCK' if available else 'unknown' if available is None else 'sold out'}"
            f"  (sizes {size_info})")

        prev = state.get(url)
        if available and prev is False:
            wanted = product.get("sizes")
            text = (f"Hollister restock! {name}"
                    f"{' (size ' + '/'.join(wanted) + ')' if wanted else ''} is back in stock: {url}")
            log("  RESTOCK -> sending messages")
            notify_all(cfg, text)
        if available is not None:
            state[url] = available
        STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
        time.sleep(random.uniform(3, 8))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="check once and exit")
    ap.add_argument("--test", action="store_true", help="send a test message and exit")
    args = ap.parse_args()

    cfg = load_json(CONFIG_FILE, None)
    if cfg is None:
        sys.exit(f"Could not read {CONFIG_FILE}")

    # In the cloud (GitHub Actions) numbers and keys come from an encrypted
    # secret instead of config.json, so they never appear in the repository.
    import os
    secret = os.environ.get("NOTIFY_SECRETS")
    if secret:
        s = json.loads(secret)
        cfg["recipients"] = s["recipients"]
        cfg.setdefault("whatsapp_callmebot", {})["api_keys"] = s["api_keys"]

    if args.test:
        notify_all(cfg, "Test from your Hollister restock watcher - alerts are working.")
        return

    state = load_json(STATE_FILE, {})
    # Changes once a day, so the cloud version commits regularly and GitHub
    # doesn't pause the schedule for inactivity.
    state["_last_run_day"] = datetime.now().strftime("%Y-%m-%d")
    interval = max(1, cfg.get("check_interval_minutes", 5)) * 60
    log(f"Watching {len(cfg['products'])} product(s), every {interval // 60} min. Ctrl+C to stop.")
    while True:
        check_all(cfg, state)
        if args.once:
            break
        time.sleep(interval + random.uniform(0, 30))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped.")
