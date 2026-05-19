import os
import json
import requests
import datetime as dt
from zoneinfo import ZoneInfo

# ---------------------------
# Config
# ---------------------------
TZ = ZoneInfo("Europe/Berlin")

SIE_SYMBOL = "SIE.DE"
MKT_SYMBOL = "EXS1.DE"  # DAX ETF proxy (verify symbol in your provider)

GAP_MIN = 0.003  # +0.3%
GAP_MAX = 0.015  # +1.5% optional filter

TD_APIKEY = os.environ["TWELVEDATA_APIKEY"]
TG_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHATID = os.environ["TELEGRAM_CHAT_ID"]

BASE = "https://api.twelvedata.com"

# ---------------------------
# Helpers: Twelve Data
# ---------------------------
def td_get(path, params):
    params = dict(params)
    params["apikey"] = TD_APIKEY
    r = requests.get(f"{BASE}/{path}", params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    # Twelve Data returns status != ok on errors for many endpoints [2](https://publicapi.dev/twelve-data-api)[1](https://twelvedata.com/docs)
    if isinstance(data, dict) and data.get("status") not in (None, "ok"):
        raise RuntimeError(f"TwelveData error: {data}")
    return data

def td_time_series(symbol, interval, **kwargs):
    # Uses /time_series endpoint [2](https://publicapi.dev/twelve-data-api)[5](https://support.twelvedata.com/en/articles/5214728-getting-historical-data)
    params = {"symbol": symbol, "interval": interval}
    params.update(kwargs)
    return td_get("time_series", params)

def td_price(symbol):
    # Uses /price endpoint [1](https://twelvedata.com/docs)
    return td_get("price", {"symbol": symbol})

# ---------------------------
# Helpers: Telegram
# ---------------------------
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHATID, "text": text, "disable_web_page_preview": True}
    r = requests.post(url, json=payload, timeout=20)
    r.raise_for_status()

# ---------------------------
# Market snapshot logic
# ---------------------------
def prev_close_1day(symbol: str) -> float:
    # Request last 2 daily bars; newest first is typical
    ts = td_time_series(symbol, "1day", outputsize=2, timezone="Exchange")  # timezone param supported [3](https://pypi.org/project/twelvedata/)
    values = ts["values"]
    # values[0] is latest day, values[1] is previous day
    return float(values[1]["close"])

def today_open_from_1min(symbol: str, today_local: dt.date) -> float:
    # Derive open as the first 1-min bar from 09:00–09:10 (Exchange timezone)
    # start_date/end_date are supported and described by Twelve Data support docs [5](https://support.twelvedata.com/en/articles/5214728-getting-historical-data)
    start = dt.datetime.combine(today_local, dt.time(9, 0), TZ).strftime("%Y-%m-%d %H:%M:%S")
    end   = dt.datetime.combine(today_local, dt.time(9, 10), TZ).strftime("%Y-%m-%d %H:%M:%S")
    ts = td_time_series(
        symbol,
        "1min",
        start_date=start,
        end_date=end,
        timezone="Exchange",
        order="ASC"  # request chronological bars
    )
    values = ts["values"]
    # first bar open
    return float(values[0]["open"])

def current_price(symbol: str) -> float:
    p = td_price(symbol)  # latest price [1](https://twelvedata.com/docs)
    return float(p["price"])

def compute_signal(now_local: dt.datetime):
    today = now_local.date()

    sie_prev = prev_close_1day(SIE_SYMBOL)
    sie_open = today_open_from_1min(SIE_SYMBOL, today)
    sie_last = current_price(SIE_SYMBOL)

    mkt_prev = prev_close_1day(MKT_SYMBOL)
    mkt_last = current_price(MKT_SYMBOL)

    gap = (sie_open / sie_prev) - 1.0
    mkt_chg = (mkt_last / mkt_prev) - 1.0

    cond_gap = (gap >= GAP_MIN) and (gap <= GAP_MAX)
    cond_mkt = (mkt_chg > 0.0)
    cond_confirm = (sie_last >= sie_open)

    ok = cond_gap and cond_mkt and cond_confirm

    snapshot = {
        "ts": now_local.isoformat(),
        "sie": {"prev_close": sie_prev, "open": sie_open, "last": sie_last, "gap": gap},
        "mkt": {"symbol": MKT_SYMBOL, "prev_close": mkt_prev, "last": mkt_last, "chg": mkt_chg},
        "conditions": {"gap": cond_gap, "market": cond_mkt, "confirm": cond_confirm},
        "signal": "BUY" if ok else "NO"
    }
    return ok, snapshot

def main():
    now_local = dt.datetime.now(TZ)

    # Safety: run only on weekdays (optional)
    if now_local.weekday() >= 5:
        return

    ok, snap = compute_signal(now_local)

    # Always write a log artifact (useful for debugging)
    with open("signal_log.json", "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)

    if ok:
        msg = (
            f"✅ GREEN LIGHT – {SIE_SYMBOL}\n\n"
            f"Gap: {snap['sie']['gap']:.2%}\n"
            f"Market ({snap['mkt']['symbol']}): {snap['mkt']['chg']:.2%}\n"
            f"Open: {snap['sie']['open']:.2f} | Last: {snap['sie']['last']:.2f}\n\n"
            "Reminder:\n"
            "• Stop: -0.6%\n"
            "• TP1: +1.0% (sell 50%)\n"
            "• Exit all by 14:30\n"
        )
        send_telegram(msg)

if __name__ == "__main__":
    main()
