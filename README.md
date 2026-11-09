
Grenada Fare Watch ✈️
Checks Google Flights for Toronto (YYZ) → Grenada (GND) every 6 hours, sends a push notification to your phone when the price drops, and shows the current price and history on a web page you can open anywhere.

Prices: Google Flights, through SerpApi (free plan covers this)
Alerts: ntfy phone push (free, no account)
Runs on: GitHub Actions (free), page hosted on GitHub Pages (free)
One-time setup (about 15 minutes)
1. Set your trip
Edit config.json:

Setting	What it does
outbound_date / return_date	Your dates, YYYY-MM-DD. Set return_date to null for one way.
adults	Number of travellers
target_price	Get a high-priority alert when the fare goes at or below this (CAD)
min_drop_amount	Ignore drops smaller than this many dollars
max_stops	null = any, 0 = nonstop only, 1 = up to 1 stop
2. Get a SerpApi key
Sign up free at https://serpapi.com/users/sign_up.
Copy your key from https://serpapi.com/manage-api-key.
Checking 4 times a day uses about 120 searches a month. If you're on a plan with fewer, change the cron line in .github/workflows/scan.yml, e.g. "17 */12 * * *" for twice a day.

3. Set up phone alerts
Install ntfy on your phone (Android / iPhone).
Tap +, subscribe to a topic with a long random name nobody would guess, e.g. grenada-fares-k7q2m9xv4p. Anyone who knows the name can read it, so keep it private.
4. Put it on GitHub
Create a free account at https://github.com and make a new public repository (e.g. grenada-fares). Public is needed for free GitHub Pages.
Upload all of these files (keep the folder structure, including .github/workflows/scan.yml).
In the repo: Settings → Secrets and variables → Actions → New repository secret, add:
SERPAPI_KEY = your SerpApi key
NTFY_TOPIC = your ntfy topic name
Settings → Pages: Source Deploy from a branch, Branch main, folder /docs, Save.
Actions tab → enable workflows if asked → Scan Grenada fares → Run workflow.
After a minute or two your page is live at https://<your-github-username>.github.io/grenada-fares/. Add it to your phone's home screen and it works like an app.

How alerts work
You get a push when any of these happen (compared with earlier checks):

The fare drops to or below your target_price (high priority)
It's the lowest price seen so far
It dropped by at least min_drop_amount since the last check
The first check never alerts; it just sets the starting price. Tapping the notification opens your price page.

Test it on your computer (optional)
set SERPAPI_KEY=your_key
set NTFY_TOPIC=your_topic
python scanner.py
Then serve the page: python -m http.server 8000 --directory docs and open http://localhost:8000.

Notes
GitHub may pause scheduled workflows on repos with no activity for 60 days; the scanner's own commits count as activity, so it normally keeps going.
Prices are what Google Flights shows at check time, and can change before you book.
