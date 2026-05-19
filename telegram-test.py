import os
import time
import requests
import datetime as dt
from zoneinfo import ZoneInfo

# ============================================================
# CONFIG
# ============================================================
TZ = ZoneInfo("Europe/Berlin")

# Yahoo Finance tickers:
SIE = "SIE.DE"      # Siemens Aktiengesellschaft on Yahoo Finance [1](https://eodhd.com/financial-summary/DB1.XETRA)
MKT = "^GDAXI"      # DAX index on Yahoo Finance [2](https://www.chartmill.com/stock/quote/DB1.DE/profile)

# Strategy thresholds
GAP_MIN = 0.003     # +0.3%
GAP_MAX = 0.015     # +1.5% (optional cap)

# Optional: restrict execution to a local time window (recommended if your scheduler can run multiple times)
# Set to None to disable.
RUN_WINDOW_START = dt.time(9, 20)   # 09:20 local
RUN_WINDOW_END   = dt.time(9, 35)   # 09:35 local

# Telegram secrets (set in environment variables / GitHub secrets)
TG_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHATID = os.environ["TELEGRAM_CHAT_ID"]

# Yahoo's unofficial chart endpoint is sensitive to User-Agent and rate limits. [3](https://www.siemens.com/en-us/company/investor-relations/financial-calendar/)[4](https://www.google.com/finance/beta/quote/DB1:FRA)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

# ============================================================
# TELEGRAM
# ============================================================
def send_telegram(text: str):
    """Send a message via Telegram bot to your chat."""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHATID, "text": text, "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()


# ============================================================
# YAHOO FINANCE (UNOFFICIAL) - CHART ENDPOINT
# ============================================================
def yahoo_chart(symbol: str, rng: str = "1d", interval: str = "1m",
                retries: int = 3, backoff: float = 2.0) -> dict:
    """
    Fetch chart JSON from Yahoo Finance v8 endpoint (unofficial).
    This endpoint typically requires a browser-like User-Agent and may rate-limit. [3](https://www.siemens.com/en-us/company/investor-relations/financial-calendar/)[4](https://www.google.com/finance/beta/quote/DB1:FRA)
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": rng, "interval": interval}
    headers = {"User-Agent": UA, "Accept": "application/json"}

    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                # Rate limited - wait and retry
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


def extract_prev_close_open_last(chart_obj: dict):
    """
    Extract:
      - prev_close: from meta.chartPreviousClose or meta.previousClose
      - open_price: derived from first non-null intraday 1m bar open
      - last_price: meta.regularMarketPrice (fallback to last non-null close)
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


# ============================================================
# MESSAGE BUILDING (YOUR 3 BLOCKS FIRST)
# ============================================================
def build_message(now: dt.datetime,
                  sie_prev: float, sie_open: float, sie_last: float, gap: float, cond_gap: bool,
                  dax_prev: float, dax_open: float, dax_last: float, mkt_chg: float, cond_mkt: bool,
                  cond_confirm: bool, ok: bool) -> str:
    status_line = "✅ GREEN LIGHT (BUY)" if ok else "❌ NO TRADE (conditions not met)"

    # 1st block: Gap (SIE prev close, SIE open, gap, gap check)
    block1 = (
        "1) GAP (SIE)\n"
        f"• SIE prev close: {sie_prev:.2f}\n"
        f"• SIE open (1m):  {sie_open:.2f}\n"
        f"• Gap:            {gap:.2%}  (target {GAP_MIN:.2%}–{GAP_MAX:.2%})\n"
        f"• Gap check:      {cond_gap}\n"
    )

    # 2nd block: Market (DAX prev close, DAX open, DAX last, market change, market check)
    block2 = (
        "2) MARKET (DAX)\n"
        f"• DAX prev close: {dax_prev:.2f}\n"
        f"• DAX open (1m):  {dax_open:.2f}\n"
        f"• DAX last:       {dax_last:.2f}\n"
        f"• Market change:  {mkt_chg:.2%}  (need > 0)\n"
        f"• Market check:   {cond_mkt}\n"
    )

    # 3rd block: Confirm (SIE last, SIE open, confirm)
    block3 = (
        "3) CONFIRM (SIE)\n"
        f"• SIE last:       {sie_last:.2f}\n"
        f"• SIE open (1m):  {sie_open:.2f}\n"
        f"• Confirm check:  {cond_confirm}\n"
    )

    summary = (
        "\nSummary\n"
        f"• Signal:         {'BUY' if ok else 'NO TRADE'}\n"
        f"• Time:           {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"• Symbols:        {SIE} / {MKT}\n"
    )

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
    return msg


# ============================================================
# MAIN
# ============================================================
def within_window(now: dt.datetime) -> bool:
    if RUN_WINDOW_START is None or RUN_WINDOW_END is None:
        return True
    t = now.time()
    return RUN_WINDOW_START <= t <= RUN_WINDOW_END


def main():
    now = dt.datetime.now(TZ)

    # Optional weekday guard
    if now.weekday() >= 5:
        return

    # Optional time window guard (useful if your external scheduler runs frequently)
    if not within_window(now):
        return

    # Fetch 1-day/1-minute chart data for both SIE and DAX
    sie_chart = yahoo_chart(SIE, rng="1d", interval="1m")
    dax_chart = yahoo_chart(MKT, rng="1d", interval="1m")

    sie_prev, sie_open, sie_last = extract_prev_close_open_last(sie_chart)
    dax_prev, dax_open, dax_last = extract_prev_close_open_last(dax_chart)

    # Compute your conditions
    gap = (sie_open / sie_prev) - 1.0
    mkt_chg = (dax_last / dax_prev) - 1.0

    cond_gap = (gap >= GAP_MIN) and (gap <= GAP_MAX)
    cond_mkt = (mkt_chg > 0.0)
    cond_confirm = (sie_last >= sie_open)

    ok = cond_gap and cond_mkt and cond_confirm

    # Build message with your 3 blocks first, then summary
    msg = build_message(
        now=now,
        sie_prev=sie_prev, sie_open=sie_open, sie_last=sie_last, gap=gap, cond_gap=cond_gap,
        dax_prev=dax_prev, dax_open=dax_open, dax_last=dax_last, mkt_chg=mkt_chg, cond_mkt=cond_mkt,
        cond_confirm=cond_confirm,
        ok=ok
    )

    # Always send (BUY or NO TRADE)
    send_telegram(msg)


if __name__ == "__main__":
    main()
