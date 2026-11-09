"""Grenada fare scanner.

Checks Google Flights (through SerpApi) for the trip in config.json, saves the
result to docs/data/history.json for the price page, and sends a phone push
through ntfy when the fare drops.

Environment variables:
  SERPAPI_KEY   your SerpApi key (required)
  NTFY_TOPIC    your private ntfy topic name (optional; no alerts without it)
  SCANNER_FIXTURE  path to a saved SerpApi JSON response, for offline testing
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HISTORY_PATH = ROOT / "docs" / "data" / "history.json"
MAX_HISTORY = 2000


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def fetch_flights(cfg):
    fixture = os.environ.get("SCANNER_FIXTURE")
    if fixture:
        return json.loads(Path(fixture).read_text(encoding="utf-8"))

    key = os.environ.get("SERPAPI_KEY")
    if not key:
        sys.exit("SERPAPI_KEY is not set. Add it as a GitHub secret (see README).")

    params = {
        "engine": "google_flights",
        "departure_id": cfg["origin"],
        "arrival_id": cfg["destination"],
        "outbound_date": cfg["outbound_date"],
        "currency": cfg.get("currency", "CAD"),
        "adults": cfg.get("adults", 1),
        "hl": "en",
        "api_key": key,
    }
    if cfg.get("return_date"):
        params["return_date"] = cfg["return_date"]
        params["type"] = 1  # round trip
    else:
        params["type"] = 2  # one way
    if cfg.get("max_stops") is not None:
        # SerpApi: 1 = nonstop only, 2 = 1 stop or fewer, 3 = 2 stops or fewer
        params["stops"] = int(cfg["max_stops"]) + 1

    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data:
        sys.exit(f"SerpApi error: {data['error']}")
    return data


def summarize_option(opt):
    legs = opt.get("flights", [])
    if not legs:
        return None
    airlines = []
    for leg in legs:
        name = leg.get("airline")
        if name and name not in airlines:
            airlines.append(name)
    return {
        "price": opt.get("price"),
        "airlines": airlines,
        "stops": max(len(legs) - 1, 0),
        "duration_min": opt.get("total_duration"),
        "depart": legs[0].get("departure_airport", {}).get("time"),
        "arrive": legs[-1].get("arrival_airport", {}).get("time"),
        "route": [legs[0].get("departure_airport", {}).get("id")]
        + [leg.get("arrival_airport", {}).get("id") for leg in legs],
    }


def cheapest(data):
    options = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    summaries = [s for s in (summarize_option(o) for o in options) if s and s["price"]]
    summaries.sort(key=lambda s: s["price"])
    return summaries


def notify(topic, title, message, click_url, high=False):
    req = urllib.request.Request(
        f"https://ntfy.sh/{urllib.parse.quote(topic)}",
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Tags": "airplane,chart_with_downwards_trend",
            "Priority": "high" if high else "default",
            "Click": click_url,
        },
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def decide_alert(cfg, price, history):
    """Return a reason string if this price deserves an alert, else None."""
    prior = [h["price"] for h in history if h.get("price")]
    if not prior:
        return None  # first check, nothing to compare against
    last = prior[-1]
    low = min(prior)
    target = cfg.get("target_price")
    min_drop = cfg.get("min_drop_amount", 0)

    if target and price <= target and last > target:
        return f"Below your target of ${target}"
    if price < low:
        return f"New lowest price (was ${low})"
    if cfg.get("alert_on_any_drop", True) and last - price >= max(min_drop, 1):
        return f"Down ${last - price} since last check"
    return None


def main():
    cfg = load_json(CONFIG_PATH, None)
    if not cfg:
        sys.exit("config.json is missing or invalid.")

    data = fetch_flights(cfg)
    options = cheapest(data)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    store = load_json(HISTORY_PATH, {"history": []})
    history = store.get("history", [])

    if not options:
        print("No flights returned for this search.")
        store["last_checked"] = now
        store["last_error"] = "No flights found for these dates"
    else:
        best = options[0]
        price = best["price"]
        reason = decide_alert(cfg, price, history)

        insights = data.get("price_insights") or {}
        history.append({"time": now, "price": price})
        store.update(
            {
                "trip": {
                    k: cfg.get(k)
                    for k in ("origin", "destination", "outbound_date", "return_date", "adults", "currency", "target_price")
                },
                "last_checked": now,
                "last_error": None,
                "current": best,
                "top_options": options[:5],
                "price_level": insights.get("price_level"),
                "typical_range": insights.get("typical_price_range"),
                "google_flights_url": (data.get("search_metadata") or {}).get("google_flights_url"),
                "history": history[-MAX_HISTORY:],
            }
        )
        print(f"Cheapest {cfg['origin']}->{cfg['destination']}: ${price} {cfg.get('currency', '')} ({', '.join(best['airlines'])})")

        topic = os.environ.get("NTFY_TOPIC")
        if reason and topic:
            msg = (
                f"${price} {cfg.get('currency', '')} {cfg['origin']}->{cfg['destination']} "
                f"{cfg['outbound_date']}"
                + (f" to {cfg['return_date']}" if cfg.get("return_date") else "")
                + f"\n{', '.join(best['airlines'])}, {best['stops']} stop(s)\n{reason}"
            )
            page = os.environ.get("PAGE_URL") or store.get("google_flights_url") or "https://www.google.com/travel/flights"
            notify(topic, f"Grenada fare drop: ${price}", msg, page, high=bool(cfg.get("target_price") and price <= cfg["target_price"]))
            print(f"Alert sent: {reason}")
        elif reason:
            print(f"Would alert ({reason}) but NTFY_TOPIC is not set.")

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
