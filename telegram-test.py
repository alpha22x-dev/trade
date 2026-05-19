import os
import requests

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]  # e.g. "123456789"

def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": True
    }
    r = requests.post(url, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()

if __name__ == "__main__":
    send_telegram("✅ Test message from alpha22x_bot — you are connected!")
