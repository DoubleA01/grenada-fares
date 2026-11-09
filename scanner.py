"""Grenada fare scanner.

Checks Google Flights (through SerpApi) for the trip in config.json, finds the
price of the fare type you want (e.g. "Standard", not "Basic"), saves it to
docs/data/history.json for the price page, and sends a phone push through ntfy
when that price drops.

How a check works (each step is one SerpApi search):
  1. Search the trip -> cheapest outbound flights (any fare type).
  2. For each of the top N outbound flights: get the cheapest matching return.
  3. For that round trip: get the airline's fare list (Basic, Standard, Flex...)
     and pick the fare named in config "fare_name".

Environment variables:
  SERPAPI_KEY      your SerpApi key (required)
  NTFY_TOPIC       your private ntfy topic name (optional; no alerts without it)
  PAGE_URL         link opened when you tap the alert (set by the workflow)
  SCANNER_FIXTURE  path to a JSON file of saved responses, for offline testing
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
TRAVEL_CLASS = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}

_fixture = None


def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def serp(params):
    """One SerpApi call. In fixture mode, look the response up by token instead."""
    global _fixture
    path = os.environ.get("SCANNER_FIXTURE")
    if path:
        if _fixture is None:
            _fixture = json.loads(Path(path).read_text(encoding="utf-8"))
        if "booking_token" in params:
            return _fixture.get("booking:" + params["booking_token"], {})
        if "departure_token" in params:
            return _fixture.get("return:" + params["departure_token"], {})
        return _fixture["search"]

    key = os.environ.get("SERPAPI_KEY")
    if not key:
        sys.exit("SERPAPI_KEY is not set. Add it as a GitHub secret (see README).")
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode({**params, "api_key": key})
    with urllib.request.urlopen(url, timeout=90) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if "error" in data and "booking_token" not in params and "departure_token" not in params:
        sys.exit(f"SerpApi error: {data['error']}")
    return data


def base_params(cfg):
    params = {
        "engine": "google_flights",
        "departure_id": cfg["origin"],
        "arrival_id": cfg["destination"],
        "outbound_date": cfg["outbound_date"],
        "currency": cfg.get("currency", "CAD"),
        "adults": cfg.get("adults", 1),
        "hl": "en",
        "gl": "ca",
        "travel_class": TRAVEL_CLASS[cfg.get("travel_class", "economy")],
    }
    if cfg.get("return_date"):
        params["return_date"] = cfg["return_date"]
        params["type"] = 1  # round trip
    else:
        params["type"] = 2  # one way
    if cfg.get("max_stops") is not None:
        # SerpApi: 1 = nonstop only, 2 = 1 stop or fewer, 3 = 2 stops or fewer
        params["stops"] = int(cfg["max_stops"]) + 1
    return params


def all_options(data):
    opts = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    return sorted((o for o in opts if o.get("price") and o.get("flights")), key=lambda o: o["price"])


def summarize(opt):
    legs = opt["flights"]
    airlines = []
    for leg in legs:
        if leg.get("airline") and leg["airline"] not in airlines:
            airlines.append(leg["airline"])
    return {
        "price": opt.get("price"),
        "airlines": airlines,
        "flight_numbers": [leg.get("flight_number") for leg in legs if leg.get("flight_number")],
        "stops": max(len(legs) - 1, 0),
        "duration_min": opt.get("total_duration"),
        "depart": legs[0].get("departure_airport", {}).get("time"),
        "arrive": legs[-1].get("arrival_airport", {}).get("time"),
        "route": [legs[0].get("departure_airport", {}).get("id")]
        + [leg.get("arrival_airport", {}).get("id") for leg in legs],
    }


def pick_fare(booking_data, fare_name):
    """From a booking-options response, return the cheapest fare matching fare_name.

    Matches option titles like "Economy Standard" or "Standard". Never picks a
    Basic fare. Returns None if the airline doesn't list that fare.
    """
    wanted = fare_name.lower()
    matches = []
    for item in booking_data.get("booking_options") or []:
        t = item.get("together") or {}
        title = (t.get("option_title") or "").strip()
        price = t.get("price")
        if not title or not price or "basic" in title.lower():
            continue
        if wanted in title.lower():
            matches.append(
                {
                    "fare_name": title,
                    "price": price,
                    "sold_by": t.get("book_with"),
                    "includes": t.get("extensions") or [],
                    "bags": t.get("baggage_prices") or [],
                }
            )
    matches.sort(key=lambda m: m["price"])
    return matches[0] if matches else None


def find_wanted_fare(cfg):
    """Run the search and return (best_fare, outbound_summaries, raw_search)."""
    params = base_params(cfg)
    search = serp(params)
    outbound = all_options(search)
    fare_name = cfg.get("fare_name", "Standard")
    checks = int(cfg.get("flights_to_check", 3))  # max flights to try if one has no such fare
    results = []

    for opt in outbound[:checks]:
        summary = summarize(opt)
        booking_token = opt.get("booking_token")

        if not booking_token and opt.get("departure_token"):
            returns = all_options(serp({**params, "departure_token": opt["departure_token"]}))
            if not returns:
                continue
            ret = returns[0]
            booking_token = ret.get("booking_token")
            summary["return"] = summarize(ret)
            summary["price"] = ret.get("price", summary["price"])

        if not booking_token:
            continue
        fare = pick_fare(serp({**params, "booking_token": booking_token}), fare_name)
        if fare:
            results.append({**summary, **fare, "lowest_any_fare": summary["price"]})
            break  # found it; skip the rest to save searches

    results.sort(key=lambda r: r["price"])
    return (results[0] if results else None), [summarize(o) for o in outbound[:5]], search


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
    fare_name = cfg.get("fare_name", "Standard")

    best, others, search = find_wanted_fare(cfg)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    store = load_json(HISTORY_PATH, {"history": []})
    history = store.get("history", [])
    insights = search.get("price_insights") or {}
    store.update(
        {
            "trip": {
                k: cfg.get(k)
                for k in ("origin", "destination", "outbound_date", "return_date", "adults",
                          "currency", "target_price", "travel_class", "fare_name")
            },
            "last_checked": now,
            "top_options": others,
            "price_level": insights.get("price_level"),
            "google_flights_url": (search.get("search_metadata") or {}).get("google_flights_url"),
        }
    )

    if not best:
        msg = f"No '{fare_name}' fare found in this check"
        print(msg)
        store["last_error"] = msg
    else:
        price = best["price"]
        reason = decide_alert(cfg, price, history)
        history.append({"time": now, "price": price})
        store.update({"last_error": None, "current": best, "history": history[-MAX_HISTORY:]})
        print(
            f"{best['fare_name']} {cfg['origin']}->{cfg['destination']}: ${price} {cfg.get('currency', '')} "
            f"({', '.join(best['airlines'])}; cheapest any fare ${best['lowest_any_fare']})"
        )

        topic = os.environ.get("NTFY_TOPIC")
        if reason and topic:
            msg = (
                f"${price} {cfg.get('currency', '')} {best['fare_name']} "
                f"{cfg['origin']}->{cfg['destination']} {cfg['outbound_date']}"
                + (f" to {cfg['return_date']}" if cfg.get("return_date") else "")
                + f"\n{', '.join(best['airlines'])}, {best['stops']} stop(s)\n{reason}"
            )
            page = os.environ.get("PAGE_URL") or store.get("google_flights_url") or "https://www.google.com/travel/flights"
            high = bool(cfg.get("target_price") and price <= cfg["target_price"])
            notify(topic, f"Grenada {fare_name} fare drop: ${price}", msg, page, high=high)
            print(f"Alert sent: {reason}")
        elif reason:
            print(f"Would alert ({reason}) but NTFY_TOPIC is not set.")

    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(json.dumps(store, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
