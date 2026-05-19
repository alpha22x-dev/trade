import os
import time
import requests
import datetime as dt
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")

SIE = "SIE.DE"      # Siemens on Yahoo Finance (Xetra) [4](https://eodhd.com/financial-summary/DB1.XETRA)
MKT = "^GDAXI"      # DAX index on Yahoo Finance [5](https://www.chartmill.com/stock/quote/DB1.DE/profile)

# Strategy thresholds
GAP_MIN = 0.003     # +0.3%
GAP_MAX = 0.015     # +1.5% (optional cap)

TG_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHATID = os.environ["TELEGRAM_CHAT_ID"]

# Yahoo chart endpoint is unofficial; a browser-like UA helps avoid rejects/rate limits. [2](https://www.siemens.com/en-us/company/investor-relations/financial-calendar/)[3](https://www.google.com/finance/beta/quote/DB1:FRA)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(
        url,
        json={"chat_id": TG_CHATID, "text": text, "disable_web_page_preview": True},
        timeout=20
    )
    r.raise_for_status()

def yahoo_chart(symbol: str, rng="1d", interval="1m", retries=3, backoff=2.0):
    """
    Unofficial Yahoo Finance endpoint. Requires a browser-like User-Agent in many cases. [2](https://www.siemens.com/en-us/company/investor-relations/financial-calendar/)[3](https://www.google.com/finance/beta/quote/DB1:FRA)
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": rng, "interval": interval}
    headers = {"User-Agent": UA, "Accept": "application/json"}

    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                time.sleep(backoff * (i + 1))
                continue
            r.raise_for_status()
            data = r.json()
            result = data.get("chart", {}).get("result")
            if not result:
                raise RuntimeError(f"No chart result for {symbol}: {data}")
            return result[0]
        except Exception as e:
            last_err = e
            time.sleep(backoff * (i + 1))
    raise RuntimeError(f"Yahoo chart failed for {symbol}: {last_err}")

def extract_prev_close_open_last(chart_obj):
    """
    Returns:
      prev_close: yesterday close (from meta)
      open_price: today's open approximated by first non-null 1m bar open
      last_price: regularMarketPrice (or last non-null close fallback)
    """
    meta = chart_obj["meta"]

    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    if prev_close is None:
        raise RuntimeError("prev_close not found in meta")

    last_price = meta.get("regularMarketPrice")
    if last_price is None:
        closes = chart_obj["indicators"]["quote"][0]["close"]
        last_price = next(x for x in reversed(closes) if x is not None)

    opens = chart_obj["indicators"]["quote"][0]["open"]
    open_price = next(x for x in opens if x is not None)

    return float(prev_close), float(open_price), float(last_price)

def main():
    now = dt.datetime.now(TZ)

    # Optional: weekdays only
    if now.weekday() >= 5:
        return

    # Fetch SIE and DAX data
    sie_chart = yahoo_chart(SIE, rng="1d", interval="1m")
    dax_chart = yahoo_chart(MKT, rng="1d", interval="1m")

    sie_prev, sie_open, sie_last = extract_prev_close_open_last(sie_chart)
    dax_prev, dax_open, dax_last = extract_prev_close_open_last(dax_chart)

    # Compute conditions
    gap = (sie_open / sie_prev) - 1.0
    mkt_chg = (dax_last / dax_prev) - 1.0

    cond_gap = (gap >= GAP_MIN) and (gap <= GAP_MAX)
    cond_mkt = (mkt_chg > 0.0)
    cond_confirm = (sie_last >= sie_open)

    ok = cond_gap and cond_mkt and cond_confirm

    # Build a consistent message for BOTH outcomes
status_line = "✅ GREEN LIGHT (BUY)" if ok else "❌ NO TRADE (conditions not met)"

# --- Block 1: SIE gap block ---
block1 = (
    "1) GAP (SIE)\n"
    f"• SIE prev close: {sie_prev:.2f}\n"
    f"• SIE open (1m):  {sie_open:.2f}\n"
    f"• Gap:            {gap:.2%}  (target {GAP_MIN:.2%}–{GAP_MAX:.2%})\n"
    f"• Gap check:      {cond_gap}\n"
)

# --- Block 2: DAX market block ---
block2 = (
    "2) MARKET (DAX)\n"
    f"• DAX prev close: {dax_prev:.2f}\n"
    f"• DAX open (1m):  {dax_open:.2f}\n"
    f"• DAX last:       {dax_last:.2f}\n"
    f"• Market change:  {mkt_chg:.2%}  (need > 0)\n"
    f"• Market check:   {cond_mkt}\n"
)

# --- Block 3: SIE confirmation block ---
block3 = (
    "3) CONFIRM (SIE)\n"
    f"• SIE last:       {sie_last:.2f}\n"
    f"• SIE open (1m):  {sie_open:.2f}\n"
    f"• Confirm check:  {cond_confirm}\n"
)

# Optional: compact summary at the end (nice for scanning)
summary = (
    "\nSummary\n"
    f"• Signal:         {'BUY' if ok else 'NO TRADE'}\n"
    f"• Time:           {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
)

# Optional: trade plan reminder only when BUY
trade_plan = ""
if ok:
    trade_plan = (
        "\nTrade plan reminder\n"
        "• Stop: -0.6%\n"
        "• TP1: +1.0% (sell 50%)\n"
        "• Exit all by 14:30\n"
    )

msg = (
    f"{status_line} — {SIE}\n\n"
    f"{block1}\n"
    f"{block2}\n"
    f"{block3}"
    f"{summary}"
    f"{trade_plan}"
)

send_telegram(msg)  
