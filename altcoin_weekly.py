"""
altcoin_weekly.py — Altcoin Weekly Watch Bot
Runs every Monday at 08:00 UTC.
Fetches top altcoins by weekly movement, generates Claude analysis,
sends to Telegram formatted for Patreon.
"""

import os
import time
import schedule
import requests
import anthropic
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────

COINGECKO_URL    = "https://api.coingecko.com/api/v3"
TELEGRAM_API     = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE      = 4096

# Altcoins to always exclude (stables, wrapped, LSDs)
EXCLUDE_SYMBOLS  = {
    "usdt", "usdc", "busd", "dai", "tusd", "usdp", "frax", "lusd",
    "wbtc", "weth", "wbnb", "steth", "reth", "cbeth",
    "btc", "eth",  # BTC/ETH used only as macro reference
}

# How many altcoins to feature (dynamic: 3-7)
MIN_ALTS = 3
MAX_ALTS = 7

# ── Telegram ──────────────────────────────────────────────────────────────────

def _tg_url(method):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    return TELEGRAM_API.format(token=token, method=method)

def send_telegram(text):
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    chunks  = [text[i:i+MAX_MESSAGE] for i in range(0, len(text), MAX_MESSAGE)]
    for chunk in chunks:
        try:
            resp = requests.post(
                _tg_url("sendMessage"),
                json={
                    "chat_id":                  chat_id,
                    "text":                     chunk,
                    "parse_mode":               "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=15,
            )
            resp.raise_for_status()
            time.sleep(0.5)
        except Exception as e:
            print(f"  [!] Telegram error: {e}")

# ── Data Fetch ────────────────────────────────────────────────────────────────

def fetch_macro_reference():
    """Fetch BTC and ETH 7-day data for macro context."""
    try:
        resp = requests.get(
            f"{COINGECKO_URL}/coins/markets",
            params={
                "vs_currency":         "usd",
                "ids":                 "bitcoin,ethereum",
                "price_change_percentage": "7d",
                "sparkline":           False,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        result = {}
        for coin in data:
            result[coin["symbol"].upper()] = {
                "name":       coin["name"],
                "price":      coin["current_price"],
                "change_7d":  round(coin.get("price_change_percentage_7d_in_currency") or 0, 2),
                "volume_24h": coin.get("total_volume", 0),
                "market_cap": coin.get("market_cap", 0),
            }
        return result
    except Exception as e:
        print(f"  [!] Macro fetch error: {e}")
        return {}

def fetch_top_altcoins(limit=150):
    """Fetch top coins by market cap, filter to altcoins only."""
    coins = []
    for page in range(1, 4):  # up to 3 pages of 50
        try:
            resp = requests.get(
                f"{COINGECKO_URL}/coins/markets",
                params={
                    "vs_currency":             "usd",
                    "order":                   "market_cap_desc",
                    "per_page":                50,
                    "page":                    page,
                    "price_change_percentage": "7d",
                    "sparkline":               False,
                },
                timeout=15,
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            coins.extend(batch)
            if len(coins) >= limit:
                break
            time.sleep(1.5)  # CoinGecko rate limit
        except Exception as e:
            print(f"  [!] CoinGecko page {page} error: {e}")
            break

    # Filter out stables, wrapped, BTC, ETH
    altcoins = []
    for c in coins:
        symbol = (c.get("symbol") or "").lower()
        if symbol in EXCLUDE_SYMBOLS:
            continue
        change_7d = c.get("price_change_percentage_7d_in_currency")
        if change_7d is None:
            continue
        altcoins.append({
            "id":         c["id"],
            "name":       c["name"],
            "symbol":     c["symbol"].upper(),
            "price":      c["current_price"],
            "change_7d":  round(float(change_7d), 2),
            "change_24h": round(float(c.get("price_change_percentage_24h") or 0), 2),
            "volume_24h": c.get("total_volume", 0),
            "market_cap": c.get("market_cap", 0),
            "rank":       c.get("market_cap_rank", 999),
        })

    return altcoins

def select_featured_alts(altcoins):
    """
    Select 3-7 most interesting altcoins dynamically:
    - Top 2-3 gainers
    - Top 2-3 losers
    - 1-2 high volume movers (not already in list)
    """
    if not altcoins:
        return []

    sorted_by_change = sorted(altcoins, key=lambda x: x["change_7d"], reverse=True)
    gainers = sorted_by_change[:3]
    losers  = sorted_by_change[-3:]

    # High volume movers not already selected
    selected_ids = {c["id"] for c in gainers + losers}
    vol_movers   = sorted(
        [c for c in altcoins if c["id"] not in selected_ids],
        key=lambda x: abs(x["change_7d"]) * x["volume_24h"],
        reverse=True
    )[:2]

    featured = gainers + losers + vol_movers

    # Deduplicate preserving order
    seen = set()
    unique = []
    for c in featured:
        if c["id"] not in seen:
            seen.add(c["id"])
            unique.append(c)

    # Clamp to MIN_ALTS - MAX_ALTS
    unique = unique[:MAX_ALTS]
    if len(unique) < MIN_ALTS:
        # fill with next coins by absolute change
        extras = [c for c in sorted_by_change if c["id"] not in seen]
        unique += extras[:MIN_ALTS - len(unique)]

    return unique

# ── Claude Analysis ───────────────────────────────────────────────────────────

def generate_analysis(featured_alts, macro):
    """Call Claude API to generate Patreon-ready weekly analysis."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # Build data summary for the prompt
    macro_str = ""
    for sym, d in macro.items():
        macro_str += f"- {sym}: ${d['price']:,.2f} | 7d: {d['change_7d']:+.2f}% | Vol 24h: ${d['volume_24h']:,.0f}\n"

    alts_str = ""
    for c in featured_alts:
        alts_str += (
            f"- {c['name']} ({c['symbol']}) | Rank #{c['rank']}\n"
            f"  Price: ${c['price']:,.4f} | 7d: {c['change_7d']:+.2f}% | 24h: {c['change_24h']:+.2f}%\n"
            f"  Volume 24h: ${c['volume_24h']:,.0f} | Market Cap: ${c['market_cap']:,.0f}\n"
        )

    week_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    prompt = f"""You are a professional crypto analyst writing a weekly Patreon post for BitMagnet Analytics (@at_cripto).

Today is {week_str}.

## Macro Reference (BTC & ETH this week):
{macro_str}

## Featured Altcoins this week:
{alts_str}

Write a weekly altcoin watch post for Patreon subscribers. The tone should be professional, analytical, and direct — no hype, no financial advice disclaimers mid-text (add one at the very end only).

Structure:
1. **Weekly Macro Context** (2-3 sentences — how BTC/ETH set the tone this week)
2. **This Week's Altcoin Watch** — for each featured altcoin:
   - Bold the name and ticker
   - 3-5 sentences: what happened, why it matters, key level to watch
   - Note if it's a gainer, loser, or high-volume mover
3. **Key Themes This Week** (2-3 bullet points — patterns or narratives across the alts)
4. **What to Watch Next Week** (1-2 sentences)
5. One-line disclaimer at the end.

Use plain English. Avoid generic phrases like "in the volatile world of crypto". Be specific about prices and percentages. Format with clear headers using markdown bold (**text**).

Write the full post now:"""

    print("  -> Calling Claude API...")
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text

# ── Formatter ─────────────────────────────────────────────────────────────────

def format_telegram(analysis, featured_alts, macro):
    """Wrap analysis in Telegram-friendly format with header and footer."""
    week_str = datetime.now(timezone.utc).strftime("%B %d, %Y")

    # Header
    header = (
        f"📊 <b>ALTCOIN WEEKLY WATCH</b>\n"
        f"Week of {week_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )

    # Quick stats table
    stats = "<b>📈 Quick Stats</b>\n"
    for sym, d in macro.items():
        emoji = "🟢" if d["change_7d"] >= 0 else "🔴"
        stats += f"{emoji} <b>{sym}</b>: ${d['price']:,.2f} ({d['change_7d']:+.2f}% 7d)\n"
    stats += "\n"

    for c in featured_alts:
        emoji = "🟢" if c["change_7d"] >= 0 else "🔴"
        stats += f"{emoji} <b>{c['symbol']}</b>: ${c['price']:,.4f} ({c['change_7d']:+.2f}% 7d)\n"
    stats += "\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    # Convert markdown bold to HTML bold for Telegram
    body = analysis.replace("**", "<b>", 1)
    i    = 0
    result_body = ""
    open_tag    = True
    for char in analysis:
        if analysis[i:i+2] == "**":
            result_body += "<b>" if open_tag else "</b>"
            open_tag = not open_tag
            i += 2
            continue
        result_body += analysis[i]
        i += 1
        if i >= len(analysis):
            break

    # Simpler bold conversion
    parts = analysis.split("**")
    converted = ""
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            converted += f"<b>{part}</b>"
        else:
            converted += part

    # Footer
    footer = (
        f"\n\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧲 <b>BitMagnet Analytics</b> | @at_cripto\n"
        f"📌 Full analysis on Patreon\n"
        f"⚠️ <i>Not financial advice. DYOR.</i>"
    )

    return header + stats + converted + footer

# ── Main Job ──────────────────────────────────────────────────────────────────

def run_weekly_watch():
    print("\n" + "=" * 55)
    print(f"  Altcoin Weekly Watch — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("=" * 55)

    # 1. Macro reference
    print("\n  -> Fetching BTC/ETH macro data...")
    macro = fetch_macro_reference()
    if not macro:
        print("  [!] Failed to fetch macro data. Aborting.")
        return

    # 2. Top altcoins
    print("  -> Fetching top altcoins from CoinGecko...")
    altcoins = fetch_top_altcoins(limit=150)
    print(f"  -> {len(altcoins)} altcoins fetched after filtering")

    if not altcoins:
        print("  [!] No altcoins fetched. Aborting.")
        return

    # 3. Select featured
    featured = select_featured_alts(altcoins)
    print(f"  -> {len(featured)} altcoins selected for analysis:")
    for c in featured:
        print(f"     {c['symbol']}: {c['change_7d']:+.2f}% 7d")

    # 4. Generate analysis
    try:
        analysis = generate_analysis(featured, macro)
        print("  -> Analysis generated successfully")
    except Exception as e:
        print(f"  [!] Claude API error: {e}")
        return

    # 5. Format and send
    message = format_telegram(analysis, featured, macro)
    print("  -> Sending to Telegram...")
    send_telegram(message)
    print("  [✓] Weekly Watch sent!")

# ── Scheduler ─────────────────────────────────────────────────────────────────

def main():
    print("=" * 55)
    print("  Altcoin Weekly Watch Bot — BitMagnet Analytics")
    print("  Runs every Monday at 08:00 UTC")
    print("=" * 55)

    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    api_key = os.getenv("ANTHROPIC_API_KEY")

    if not token or not chat_id:
        print("[!] TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. Exiting.")
        return
    if not api_key:
        print("[!] ANTHROPIC_API_KEY not set. Exiting.")
        return

    # Startup confirmation
    send_telegram(
        "🧲 <b>Altcoin Weekly Watch Bot started</b>\n"
        "Runs every <b>Monday at 08:00 UTC</b>\n"
        "Dynamic selection: top gainers, losers & volume movers\n"
        "Analysis powered by Claude API"
    )

    # Schedule every Monday at 08:00 UTC
    schedule.every().monday.at("08:00").do(run_weekly_watch)

    print("\n  Scheduler active. Waiting for Monday 08:00 UTC...")
    print("  (Set RUN_NOW=1 env var to trigger immediately for testing)\n")

    # Allow immediate test run via env var
    if os.getenv("RUN_NOW") == "1":
        print("  RUN_NOW=1 detected — running immediately...\n")
        run_weekly_watch()

    while True:
        schedule.run_pending()
        time.sleep(60)

if __name__ == "__main__":
    main()
