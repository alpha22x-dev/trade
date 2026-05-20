
import os
import time
import requests
import datetime as dt
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")

# Yahoo Finance tickers
SIE = "SIE.DE"      # Siemens (Xetra) [1](https://www.marketsmojo.com/news/stocks-in-action/siemens-ltd-opens-strong-with-significant-gap-up-reflecting-positive-market-sentiment-3937992)
SAP = "SAP.DE"      # SAP (Xetra) [2](https://finance.yahoo.com/quote/SAP.DE/)
MKT = "^GDAXI"      # DAX index [3](https://www.marketwatch.com/investing/stock/sie/download-data?countrycode=de&iso=xfra)

# Strategy thresholds (same logic)
GAP_MIN = 0.003     # +0.3%
GAP_MAX = 0.015     # +1.5%

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

# Unofficial Yahoo endpoint often needs a browser-like User-Agent and can be rate-limited. [4](https://stockanalysis.com/quote/vie/SIE/history/)[5](https://www.investing.com/indices/germany-30-historical-data)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok", False):
        raise RuntimeError(f"Telegram error: {data}")

def yahoo_chart(symbol: str, rng="1d", interval="1m", retries=3, backoff=2.0) -> dict:
    """
    Uses Yahoo Finance v8 chart endpoint (unofficial). [4](https://stockanalysis.com/quote/vie/SIE/history/)[5](https://www.investing.com/indices/germany-30-historical-data)
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
                raise RuntimeError(f"No result for {symbol}: {data}")
            return result[0]
        except Exception as e:
            last_err = e
            time.sleep(backoff * (i + 1))
    raise RuntimeError(f"Yahoo chart failed for {symbol}: {last_err}")

def extract_prev_close_open_last(chart_obj: dict):
    """
    prev_close: meta.chartPreviousClose or meta.previousClose
    open: first non-null 1m bar open (approx today's open)
    last: meta.regularMarketPrice (fallback to last non-null close)
    """
    meta = chart_obj["meta"]

    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    if prev_close is None:
        raise RuntimeError("prev_close missing in meta")

    last_price = meta.get("regularMarketPrice")
    if last_price is None:
        closes = chart_obj["indicators"]["quote"][0]["close"]
        last_price = next(x for x in reversed(closes) if x is not None)

    opens = chart_obj["indicators"]["quote"][0]["open"]
    open_price = next(x for x in opens if x is not None)

    return float(prev_close), float(open_price), float(last_price)

def build_message(symbol_label: str, symbol: str,
                  prev_close: float, open_px: float, last_px: float,
                  gap: float, gap_ok: bool,
                  mkt_prev: float, mkt_open: float, mkt_last: float,
                  mkt_chg: float, mkt_ok: bool,
                  confirm_ok: bool, ok: bool,
                  now: dt.datetime) -> str:
    status = "✅ GREEN LIGHT (BUY)" if ok else "❌ NO TRADE"

    msg = (
        f"{status} — {symbol_label}\n"
        f"Time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
        f"1) GAP ({symbol_label})\n"
        f"• Prev close: {prev_close:.2f}\n"
        f"• Open:       {open_px:.2f}\n"
        f"• Gap:        {gap:.2%}  (target {GAP_MIN:.2%}–{GAP_MAX:.2%})\n"
        f"• Gap check:  {gap_ok}\n\n"
        f"2) MARKET (DAX)\n"
        f"• Prev close: {mkt_prev:.2f}\n"
        f"• Open:       {mkt_open:.2f}\n"
        f"• Last:       {mkt_last:.2f}\n"
        f"• Change:     {mkt_chg:.2%}\n"
        f"• Market ok:  {mkt_ok}\n\n"
        f"3) CONFIRM ({symbol_label})\n"
        f"• Last:       {last_px:.2f}\n"
        f"• Open:       {open_px:.2f}\n"
        f"• Confirm ok: {confirm_ok}\n"
    )
    return msg

def compute_signal_for_symbol(symbol_label: str, symbol: str, mkt_obj: dict, now: dt.datetime) -> str:
    # Get symbol data
    obj = yahoo_chart(symbol, rng="1d", interval="1m")
    prev_close, open_px, last_px = extract_prev_close_open_last(obj)

    # Get market data (already fetched once)
    mkt_prev, mkt_open, mkt_last = extract_prev_close_open_last(mkt_obj)

    gap = (open_px / prev_close) - 1.0
    mkt_chg = (mkt_last / mkt_prev) - 1.0

    gap_ok = (GAP_MIN <= gap <= GAP_MAX)
    mkt_ok = (mkt_chg > 0.0)
    confirm_ok = (last_px >= open_px)

    ok = gap_ok and mkt_ok and confirm_ok

    return build_message(
        symbol_label=symbol_label,
        symbol=symbol,
        prev_close=prev_close,
        open_px=open_px,
        last_px=last_px,
        gap=gap,
        gap_ok=gap_ok,
        mkt_prev=mkt_prev,
        mkt_open=mkt_open,
        mkt_last=mkt_last,
        mkt_chg=mkt_chg,
        mkt_ok=mkt_ok,
        confirm_ok=confirm_ok,
        ok=ok,
        now=now
    )

def main():
    now = dt.datetime.now(TZ)

    # Optional: only weekdays
    if now.weekday() >= 5:
        return

    # Fetch market proxy once (reused for both symbols)
    mkt_obj = yahoo_chart(MKT, rng="1d", interval="1m")

    # 1st Telegram message: Siemens
    sie_msg = compute_signal_for_symbol("SIE.DE", SIE, mkt_obj, now)
    send_telegram(sie_msg)

    # 2nd Telegram message: SAP (same logic)
    sap_msg = compute_signal_for_symbol("SAP.DE", SAP, mkt_obj, now)
    send_telegram(sap_msg)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        now = dt.datetime.now(TZ)
        # Yahoo Finance is informational and can be delayed; endpoint is unofficial and may fail. [6](https://www.ifcmarkets.com/en/historical-data/stocks-history/sie)[5](https://www.investing.com/indices/germany-30-historical-data)
        send_telegram(
            "⚠️ alpha22x_bot ERROR\n"
            f"Time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"{type(e).__name__}: {e}"
        )
        raise
import datetime as dt
