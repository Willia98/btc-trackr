import os
import time
import threading
import requests
import pandas as pd
from flask import Flask, jsonify

app = Flask(__name__)

SYMBOL = "BTCUSDT"
BASE_URL = "https://api.bybit.com/v5/market/kline"
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
RISK_BRL = 1.08
MAX_MINUTES = 5
last_signal = None

def candles(interval, limit=250):
    interval_map = {"1m": "1", "5m": "5", "15m": "15"}
    bybit_interval = interval_map[interval]
    r = requests.get(
        BASE_URL,
        params={
            "category": "linear",
            "symbol": SYMBOL,
            "interval": bybit_interval,
            "limit": limit,
        },
        timeout=10,
    )
    r.raise_for_status()
    payload = r.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error: {payload.get('retMsg', 'unknown error')}")
    rows = payload["result"]["list"]
    df = pd.DataFrame(
        rows,
        columns=["open_time", "open", "high", "low", "close", "volume", "turnover"],
    )
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c])
    df["open_time"] = pd.to_numeric(df["open_time"])
    # Bybit returns newest first; indicators need oldest -> newest.
    df = df.sort_values("open_time").reset_index(drop=True)
    return df

def indicators(df):
    close = df["close"]
    df["ema20"] = close.ewm(span=20, adjust=False).mean()
    df["ema50"] = close.ewm(span=50, adjust=False).mean()
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-12)
    df["rsi"] = 100 - (100 / (1 + rs))
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["vol_avg"] = df["volume"].rolling(20).mean()
    return df

def analyze():
    d15 = indicators(candles("15m"))
    d5 = indicators(candles("5m"))
    d1 = indicators(candles("1m"))
    a, b, c = d15.iloc[-2], d5.iloc[-2], d1.iloc[-2]

    long15 = a.ema20 > a.ema50 and a.close > a.ema20
    short15 = a.ema20 < a.ema50 and a.close < a.ema20
    long5 = b.macd > b.macd_signal and 50 <= b.rsi <= 70
    short5 = b.macd < b.macd_signal and 30 <= b.rsi <= 50
    volume_ok = c.volume >= c.vol_avg if pd.notna(c.vol_avg) else False
    long1 = c.close > c.ema20 and c.close > c.open
    short1 = c.close < c.ema20 and c.close < c.open

    signal = "NÃO ENTRAR"
    if long15 and long5 and long1 and volume_ok:
        signal = "LONG"
    elif short15 and short5 and short1 and volume_ok:
        signal = "SHORT"

    price = float(c.close)
    recent_low = float(d1["low"].iloc[-7:-1].min())
    recent_high = float(d1["high"].iloc[-7:-1].max())
    if signal == "LONG":
        stop = recent_low
        risk_dist = max(price - stop, 0)
        target = price + 1.5 * risk_dist
    elif signal == "SHORT":
        stop = recent_high
        risk_dist = max(stop - price, 0)
        target = price - 1.5 * risk_dist
    else:
        stop = target = None

    return {
        "signal": signal, "price": round(price, 2),
        "stop": round(stop, 2) if stop else None,
        "target": round(target, 2) if target else None,
        "risk_brl": RISK_BRL, "max_minutes": MAX_MINUTES
    }

def send_telegram(data):
    if not BOT_TOKEN or not CHAT_ID or data["signal"] == "NÃO ENTRAR":
        return
    msg = (
        f"BTC — SINAL {data['signal']}\n"
        f"Entrada de referência: {data['price']}\n"
        f"Stop técnico: {data['stop']}\n"
        f"Alvo técnico: {data['target']}\n"
        f"Risco planejado: até R$ {data['risk_brl']:.2f}\n"
        f"Tempo máximo: {data['max_minutes']} min\n"
        "Teste primeiro em conta demo."
    )
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": msg}, timeout=10
    ).raise_for_status()

def loop():
    global last_signal
    while True:
        try:
            data = analyze()
            key = (data["signal"], data["price"])
            if data["signal"] != "NÃO ENTRAR" and key != last_signal:
                send_telegram(data)
                last_signal = key
        except Exception as e:
            print("analysis error:", repr(e), flush=True)
        time.sleep(60)

@app.get("/")
def home():
    return jsonify({"status":"online","project":"btc-tracker","mode":"signals-only"})

@app.get("/signal")
def signal():
    try:
        return jsonify(analyze())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
