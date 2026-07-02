import os
import time
import json
import hmac
import base64
import hashlib
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from uuid import uuid5, NAMESPACE_URL

import requests


API_URL = "https://api.kraken.com"
PAIR = os.getenv("KRAKEN_PAIR", "XBTEUR")

# Par sécurité : true par défaut.
# Mets DRY_RUN=false uniquement quand tu as validé le comportement.
DRY_RUN = os.getenv("DRY_RUN", "true").lower() != "false"

# Prix post-only : on place l'ordre 1 tick sous le meilleur bid.
POST_ONLY_TICKS_BELOW_BID = Decimal(os.getenv("POST_ONLY_TICKS_BELOW_BID", "1"))

# Nombre de tentatives en cas de rejet post-only / erreur temporaire
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

API_KEY = os.environ["KRAKEN_API_KEY"]
API_SECRET = os.environ["KRAKEN_API_SECRET"]

SAFETY_BUFFER_EUR = Decimal(os.getenv("SAFETY_BUFFER_EUR", "2"))


def get_eur_balance() -> Decimal:
    balances = private_post("Balance", {})

    # Kraken utilise souvent ZEUR pour EUR
    eur_balance = Decimal(str(
        balances.get("ZEUR")
        or balances.get("EUR")
        or "0"
    ))

    return eur_balance


def send_email(subject, body):
    gmail_user = os.environ["GMAIL_USERNAME"]
    gmail_password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = gmail_user
    msg["To"] = "pierre.poirier.dbg@gmail.com"

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(gmail_user, gmail_password)
        smtp.send_message(msg)


def public_get(endpoint: str, params: dict | None = None) -> dict:
    response = requests.get(
        f"{API_URL}/0/public/{endpoint}",
        params=params or {},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("error"):
        raise RuntimeError(f"Kraken public error: {data['error']}")

    return data["result"]


def private_post(endpoint: str, data: dict) -> dict:
    path = f"/0/private/{endpoint}"
    nonce = str(int(time.time() * 1000))
    data = {"nonce": nonce, **data}

    post_data = urllib.parse.urlencode(data)
    encoded = (nonce + post_data).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()

    signature = hmac.new(
        base64.b64decode(API_SECRET),
        message,
        hashlib.sha512,
    )
    api_sign = base64.b64encode(signature.digest()).decode()

    headers = {
        "API-Key": API_KEY,
        "API-Sign": api_sign,
        "Content-Type": "application/x-www-form-urlencoded",
    }

    response = requests.post(
        f"{API_URL}{path}",
        headers=headers,
        data=post_data,
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()

    if result.get("error"):
        raise RuntimeError(f"Kraken private error: {result['error']}")

    return result["result"]


def decimal_floor(value: Decimal, decimals: int) -> Decimal:
    step = Decimal("1").scaleb(-decimals)
    return value.quantize(step, rounding=ROUND_DOWN)


def get_pair_metadata() -> dict:
    pairs = public_get("AssetPairs", {"pair": PAIR})
    key = next(iter(pairs.keys()))
    meta = pairs[key]

    return {
        "kraken_key": key,
        "pair_decimals": int(meta["pair_decimals"]),
        "lot_decimals": int(meta["lot_decimals"]),
        "ordermin": Decimal(str(meta.get("ordermin", "0"))),
        "costmin": Decimal(str(meta.get("costmin", "0"))),
        "tick_size": Decimal(str(meta.get("tick_size", "0"))) if meta.get("tick_size") else None,
    }


def get_market_snapshot(pair_key: str) -> dict:
    ticker = public_get("Ticker", {"pair": PAIR})
    t = ticker[pair_key]

    last_price = Decimal(t["c"][0])
    best_bid = Decimal(t["b"][0])
    best_ask = Decimal(t["a"][0])

    return {
        "last_price": last_price,
        "best_bid": best_bid,
        "best_ask": best_ask,
    }


def get_dca_amount_eur(last_price: Decimal) -> Decimal:
    if last_price > Decimal("58000"):
        return Decimal("1000")
    elif Decimal("53000") <= last_price <= Decimal("58000"):
        return Decimal("1100")
    else:
        return Decimal("1200")


def make_post_only_price(best_bid: Decimal, meta: dict) -> Decimal:
    if meta["tick_size"]:
        raw_price = best_bid - POST_ONLY_TICKS_BELOW_BID * meta["tick_size"]
    else:
        tick = Decimal("1").scaleb(-meta["pair_decimals"])
        raw_price = best_bid - POST_ONLY_TICKS_BELOW_BID * tick

    return decimal_floor(raw_price, meta["pair_decimals"])


def build_client_order_id() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    raw = f"eur-btc-dca-{PAIR}-{today}"
    return str(uuid5(NAMESPACE_URL, raw))


def place_dca_order() -> dict:
    meta = get_pair_metadata()

    for attempt in range(1, MAX_RETRIES + 1):
        snapshot = get_market_snapshot(meta["kraken_key"])

        last_price = snapshot["last_price"]
        best_bid = snapshot["best_bid"]
        best_ask = snapshot["best_ask"]

        amount_eur = get_dca_amount_eur(last_price)
        limit_price = make_post_only_price(best_bid, meta)

        eur_balance = get_eur_balance()

        SAFETY_BUFFER_EUR = Decimal(os.getenv("SAFETY_BUFFER_EUR", "2"))
        required_eur = amount_eur + SAFETY_BUFFER_EUR
        
        if eur_balance < required_eur:
            raise RuntimeError(
                f"Solde EUR insuffisant: disponible={eur_balance} EUR, "
                f"requis={required_eur} EUR incluant buffer={SAFETY_BUFFER_EUR} EUR. "
                f"Aucun ordre envoyé."
            )

        volume_btc = amount_eur / limit_price
        volume_btc = decimal_floor(volume_btc, meta["lot_decimals"])

        notional = volume_btc * limit_price

        if volume_btc < meta["ordermin"]:
            raise RuntimeError(
                f"Volume trop faible: {volume_btc} BTC < ordermin {meta['ordermin']}"
            )

        if meta["costmin"] and notional < meta["costmin"]:
            raise RuntimeError(
                f"Notional trop faible: {notional} EUR < costmin {meta['costmin']}"
            )

        cl_ord_id = build_client_order_id()

        order = {
            "pair": PAIR,
            "type": "buy",
            "ordertype": "limit",
            "price": str(limit_price),
            "volume": str(volume_btc),
            "oflags": "post",
            "timeinforce": "GTC",
            "cl_ord_id": cl_ord_id,
            "validate": "true" if DRY_RUN else "false",
        }

        print(json.dumps({
            "attempt": attempt,
            "dry_run": DRY_RUN,
            "last_price": str(last_price),
            "best_bid": str(best_bid),
            "best_ask": str(best_ask),
            "amount_eur": str(amount_eur),
            "limit_price": str(limit_price),
            "volume_btc": str(volume_btc),
            "estimated_notional_eur": str(notional),
            "cl_ord_id": cl_ord_id,
            "order": order,
        }, indent=2))

        try:
            result = private_post("AddOrder", order)
            body = f"""
            Pair       : {PAIR}
            Mode       : {'TEST' if DRY_RUN else 'LIVE'}
            Prix BTC   : {last_price}
            Montant EUR: {amount_eur}
            Prix limite: {limit_price}
            Volume BTC : {volume_btc}
            
            Réponse Kraken:
            {json.dumps(result, indent=2)}
            """
            
            send_email(
                f"[Kraken DCA] {'TEST' if DRY_RUN else 'LIVE'} OK",
                body
            )
            
            print(json.dumps(result, indent=2))
            return result

        except RuntimeError as exc:
            msg = str(exc)
            print(f"Attempt {attempt} failed: {msg}")

            if "post" in msg.lower() or "would execute" in msg.lower():
                time.sleep(2)
                continue

            if "rate limit" in msg.lower() or "busy" in msg.lower() or "temporarily" in msg.lower():
                time.sleep(5 * attempt)
                continue

            raise

    raise RuntimeError(f"Impossible de placer l'ordre après {MAX_RETRIES} tentatives")


if __name__ == "__main__":
    try:
        result = place_dca_order()
        print("DCA completed")

    except Exception as e:
        send_email(
            "[Kraken DCA] ERROR",
            str(e)
        )
        raise
