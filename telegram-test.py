import os
import time
import requests
import datetime as dt
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")

SIE = "SIE.DE"     # Siemens (Xetra) [1](https://finance.yahoo.com/quote/SIE.DE/)
MKT = "^GDAXI"     # DAX index [2](https://uk.finance.yahoo.com/quote/%5EGDAXI/)

GAP_MIN = 0.003    # +0.3%
GAP_MAX = 0.015    # +1.5%

TG_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHATID = os.environ["TELEGRAM_CHAT_ID"]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(url, json={"chat_id": TG_CHATID, "text": text}, timeout=20)
    r.raise_for_status()

def yahoo_chart(symbol: str, rng="1d", interval="1m", retries=3, backoff=2.0):
    """
    Unofficial Yahoo Finance endpoint. Requires a browser-like User-Agent. [7](https://dev.to/avabuildsdata/how-to-get-historical-stock-data-from-yahoo-finance-without-paying-for-an-api-key-5ein)
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    params = {"range": rng, "interval": interval}
    headers = {"User-Agent": UA, "Accept": "application/json"}

    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=20)
            if r.status_code == 429:
                # rate limited; wait and retry
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
    meta = chart_obj["meta"]

    # Previous close fields are typically present in meta
    prev_close = meta.get("chartPreviousClose") or meta.get("previousClose")
    if prev_close is None:
        raise RuntimeError("prev_close not found in meta")

    # Latest "regular market" price in meta
    last_price = meta.get("regularMarketPrice")
    if last_price is None:
        # fallback: last close from the series
        closes = chart_obj["indicators"]["quote"][0]["close"]
        last_price = next(x for x in reversed(closes) if x is not None)

    # Derive today's open from first non-null 1m bar open
    opens = chart_obj["indicators"]["quote"][0]["open"]
    first_open = next(x for x in opens if x is not None)

    return float(prev_close), float(first_open), float(last_price)

def main():
    now = dt.datetime.now(TZ)

    # Optional: only weekdays
    if now.weekday() >= 5:
        return

    # Pull data
    sie_chart = yahoo_chart(SIE, rng="1d", interval="1m")
    dax_chart = yahoo_chart(MKT, rng="1d", interval="1m")

    sie_prev, sie_open, sie_last = extract_prev_close_open_last(sie_chart)
    dax_prev, dax_open, dax_last = extract_prev_close_open_last(dax_chart)

    gap = (sie_open / sie_prev) - 1.0
    mkt_chg = (dax_last / dax_prev) - 1.0

    cond_gap = (gap >= GAP_MIN) and (gap <= GAP_MAX)
    cond_mkt = (mkt_chg > 0.0)
    cond_confirm = (sie_last >= sie_open)

    if cond_gap and cond_mkt and cond_confirm:
        msg = (
            f"✅ GREEN LIGHT – {SIE}\n\n"
            f"Gap (Open vs PrevClose): {gap:.2%}\n"
            f"Market ({MKT}) vs PrevClose: {mkt_chg:.2%}\n"
            f"SIE Open: {sie_open:.2f} | SIE Last: {sie_last:.2f}\n\n"
            "Reminder:\n"
            "• Stop: -0.6%\n"
            "• TP1: +1.0% (sell 50%)\n"
            "• Exit all by 14:30\n"
        )
        send_telegram(msg)

if __name__ == "__main__":
    main()
