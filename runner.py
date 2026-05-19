import os
import re
import json
import math
import time
import requests
import datetime as dt
from zoneinfo import ZoneInfo

# ============================================================
# CONFIG
# ============================================================
TZ = ZoneInfo("Europe/Berlin")

# Yahoo Finance symbols
SIE = "SIE.DE"      # Siemens on Yahoo Finance (Xetra) [3](https://www.wsj.com/market-data/quotes/XE/DB1/historical-prices)
MKT = "^GDAXI"      # DAX index on Yahoo Finance [4](https://eu-prod.asyncgw.teams.microsoft.com/v1/objects/0-weu-d1-bc63e31fe54c2fb80053c46503327fe7/views/original)

# Strategy thresholds
GAP_MIN = 0.003     # +0.3%
GAP_MAX = 0.015     # +1.5%

# Trade sizing / exits
NOTIONAL_EUR = 10000.0
SLIPPAGE_BUFFER = 0.002  # 0.2% buffer to reduce chance of exceeding €10k on market fill
STOP_PCT = 0.006         # -0.6%
TP1_PCT = 0.010          # +1.0%
EXIT_TIME_LOCAL = "14:30"

# Run window for morning signal (keep narrow if you trigger once/day)
RUN_WINDOW_START = dt.time(9, 20)
RUN_WINDOW_END   = dt.time(9, 35)

# Telegram env vars (GitHub secrets)
TG_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHATID = os.environ["TELEGRAM_CHAT_ID"]

# Yahoo chart endpoint is unofficial; UA helps and may rate-limit. [1](https://eu-prod.asyncgw.teams.microsoft.com/v1/objects/0-weu-d15-992e42504d94c5bad6c714fec07bc430/views/original)[2](https://www.siemens.com/en-us/company/investor-relations/)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

STATE_FILE = "state.json"

# ============================================================
# TELEGRAM HELPERS
# ============================================================
def tg_api(method: str, payload=None):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/{method}"
    r = requests.post(url, json=(payload or {}), timeout=25)
    r.raise_for_status()
    data = r.json()
    if not data.get("ok", False):
        raise RuntimeError(f"Telegram API error calling {method}: {data}")
    return data

def send_telegram(text: str, reply_to_message_id: int = None):
    payload = {"chat_id": TG_CHATID, "text": text, "disable_web_page_preview": True}
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id
    return tg_api("sendMessage", payload)["result"]["message_id"]

def get_updates(offset: int = None):
    payload = {}
    if offset is not None:
        payload["offset"] = offset
    return tg_api("getUpdates", payload).get("result", [])

# ============================================================
# STATE (avoid reprocessing updates)
# ============================================================
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_update_id": 0}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ============================================================
# YAHOO FINANCE (UNOFFICIAL) - V8 CHART ENDPOINT
# ============================================================
def yahoo_chart(symbol: str, rng: str = "1d", interval: str = "1m",
                retries: int = 3, backoff: float = 2.0) -> dict:
    """
    Unofficial Yahoo Finance v8 chart endpoint; requires User-Agent and may rate-limit. [1](https://eu-prod.asyncgw.teams.microsoft.com/v1/objects/0-weu-d15-992e42504d94c5bad6c714fec07bc430/views/original)[2](https://www.siemens.com/en-us/company/investor-relations/)
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

def extract_prev_close_open_last(chart_obj: dict):
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
# ORDER / TICKET LOGIC
# ============================================================
def compute_qty(reference_price: float) -> int:
    if reference_price <= 0:
        return 0
    # floor() and buffer to reduce chance of exceeding €10k with market order
    return int(math.floor(NOTIONAL_EUR / (reference_price * (1.0 + SLIPPAGE_BUFFER))))

def build_exit_prices(fill_price: float):
    stop_px = fill_price * (1.0 - STOP_PCT)
    tp1_px  = fill_price * (1.0 + TP1_PCT)
    return stop_px, tp1_px

def fmt2(x: float) -> str:
    return f"{x:.2f}"

# ============================================================
# MESSAGE BUILDERS
# ============================================================
def build_signal_message(now,
                         sie_prev, sie_open, gap, gap_ok,
                         dax_prev, dax_open, dax_last, mkt_chg, mkt_ok,
                         sie_last, confirm_ok, ok):
    status_line = "✅ GREEN LIGHT (BUY)" if ok else "❌ NO TRADE (conditions not met)"

    # 1st block
    block1 = (
        "1) GAP (SIE)\n"
        f"• SIE prev close: {sie_prev:.2f}\n"
        f"• SIE open (1m):  {sie_open:.2f}\n"
        f"• Gap:            {gap:.2%}  (target {GAP_MIN:.2%}–{GAP_MAX:.2%})\n"
        f"• Gap check:      {gap_ok}\n"
    )

    # 2nd block
    block2 = (
        "2) MARKET (DAX)\n"
        f"• DAX prev close: {dax_prev:.2f}\n"
        f"• DAX open (1m):  {dax_open:.2f}\n"
        f"• DAX last:       {dax_last:.2f}\n"
        f"• Market change:  {mkt_chg:.2%}  (need > 0)\n"
        f"• Market check:   {mkt_ok}\n"
    )

    # 3rd block
    block3 = (
        "3) CONFIRM (SIE)\n"
        f"• SIE last:       {sie_last:.2f}\n"
        f"• SIE open (1m):  {sie_open:.2f}\n"
        f"• Confirm check:  {confirm_ok}\n"
    )

    summary = (
        "\nSummary\n"
        f"• Signal: {'BUY' if ok else 'NO TRADE'}\n"
        f"• Time:   {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"• Symbols: {SIE} / {MKT}\n"
    )

    return f"{status_line} — {SIE}\n\n{block1}\n{block2}\n{block3}{summary}"

def build_buy_ticket(now, sie_last, qty):
    return (
        "🟢 BUY TICKET (Swissquote – manual entry)\n"
        f"Time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n"
        f"Instrument: {SIE}\n"
        "Order type: BUY MARKET\n"
        f"Notional cap: €{NOTIONAL_EUR:,.0f}\n"
        f"Reference price (SIE last): {sie_last:.2f}\n"
        f"Slippage buffer: {SLIPPAGE_BUFFER:.2%}\n"
        f"• Qty: {qty} shares  (intended to keep cost ≤ €{NOTIONAL_EUR:,.0f})\n\n"
        "Next step:\n"
        "➡️ After you get filled, REPLY to THIS message with:\n"
        "fill 260.35\n"
    )

def build_exits_message(fill_price, qty):
    stop_px, tp1_px = build_exit_prices(fill_price)
    qty_tp1 = qty // 2
    qty_remain = qty - qty_tp1
    cash_used = qty * fill_price

    # Swissquote stop: triggers and becomes market order (slippage possible). [6](https://www.chartmill.com/stock/quote/DB1.DE/profile)
    return (
        "🧾 EXIT ORDERS (based on your fill)\n\n"
        f"Fill price: {fill_price:.2f}\n"
        f"Qty: {qty} shares  |  Est. cash used: €{cash_used:,.2f}\n\n"
        "1) STOP (SELL STOP / stop-market)\n"
        f"• Qty: {qty} shares\n"
        f"• Trigger: {fmt2(stop_px)}  (≈ -{STOP_PCT:.2%})\n\n"
        "2) TAKE PROFIT 1 (SELL LIMIT)\n"
        f"• Qty: {qty_tp1} shares (50%)\n"
        f"• Limit: {fmt2(tp1_px)}  (≈ +{TP1_PCT:.2%})\n\n"
        "3) TIME EXIT\n"
        f"• Qty: {qty_remain} shares (remainder)\n"
        f"• Action: SELL MARKET at {EXIT_TIME_LOCAL} (manual)\n"
    )

# ============================================================
# FILL REPLY HANDLING (Message #3)
# ============================================================
FILL_RE = re.compile(r"^\s*fill\s+([0-9]+(?:\.[0-9]+)?)\s*$", re.IGNORECASE)

def parse_qty_from_ticket(ticket_text: str):
    m = re.search(r"Qty:\s*([0-9]+)\s*shares", ticket_text)
    return int(m.group(1)) if m else None

def process_fill_replies():
    state = load_state()
    offset = state.get("last_update_id", 0) + 1
    updates = get_updates(offset=offset)

    if not updates:
        return

    max_update_id = state.get("last_update_id", 0)

    for upd in updates:
        max_update_id = max(max_update_id, upd.get("update_id", 0))
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            continue

        text = (msg.get("text") or "").strip()
        m = FILL_RE.match(text)
        if not m:
            continue

        reply = msg.get("reply_to_message")
        if not reply:
            continue

        reply_text = reply.get("text") or ""
        if "BUY TICKET" not in reply_text:
            continue

        qty = parse_qty_from_ticket(reply_text)
        if not qty:
            continue

        fill_price = float(m.group(1))
        exits_msg = build_exits_message(fill_price, qty)

        send_telegram(exits_msg, reply_to_message_id=msg["message_id"])

    state["last_update_id"] = max_update_id
    save_state(state)

# ============================================================
# SIGNAL + BUY TICKET (Messages #1 and #2)
# ============================================================
def within_window(now: dt.datetime) -> bool:
    t = now.time()
    return RUN_WINDOW_START <= t <= RUN_WINDOW_END

def run_signal():
    now = dt.datetime.now(TZ)

    # Weekdays only
    if now.weekday() >= 5:
        return

    # Only run signal in morning window
    if not within_window(now):
        return

    sie_chart = yahoo_chart(SIE, rng="1d", interval="1m")
    dax_chart = yahoo_chart(MKT, rng="1d", interval="1m")

    sie_prev, sie_open, sie_last = extract_prev_close_open_last(sie_chart)
    dax_prev, dax_open, dax_last = extract_prev_close_open_last(dax_chart)

    gap = (sie_open / sie_prev) - 1.0
    mkt_chg = (dax_last / dax_prev) - 1.0

    gap_ok = (gap >= GAP_MIN) and (gap <= GAP_MAX)
    mkt_ok = (mkt_chg > 0.0)
    confirm_ok = (sie_last >= sie_open)

    ok = gap_ok and mkt_ok and confirm_ok

    # Message #1 always
    msg1 = build_signal_message(
        now, sie_prev, sie_open, gap, gap_ok,
        dax_prev, dax_open, dax_last, mkt_chg, mkt_ok,
        sie_last, confirm_ok, ok
    )
    send_telegram(msg1)

    # Message #2 only if BUY
    if ok:
        qty = compute_qty(sie_last)
        if qty <= 0:
            send_telegram("⚠️ BUY signal but qty computed as 0 (price too high vs €10k cap).")
            return
        msg2 = build_buy_ticket(now, sie_last, qty)
        send_telegram(msg2)

# ============================================================
# MAIN
# ============================================================
def main():
    # 1) Process fill replies first (Message #3)
    process_fill_replies()

    # 2) Run morning signal (Message #1 and possibly #2)
    run_signal()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # If anything fails (Yahoo rate limit / data missing / etc.), notify you
        now = dt.datetime.now(TZ)
        send_telegram(
            "⚠️ alpha22x_bot ERROR\n"
            f"Time: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            f"{type(e).__name__}: {e}\n\n"
            "Note: Yahoo Finance data is informational and may be delayed/not intended for trading. [5](https://www.wsj.com/market-data/quotes/XE/SIE)"
        )
        raise
