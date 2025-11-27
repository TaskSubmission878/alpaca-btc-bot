from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data import CryptoHistoricalDataClient
from alpaca.data.requests import CryptoBarsRequest, CryptoLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
import pandas as pd
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
import pytz
from flask import Flask
import threading
import os
import requests
from dotenv import load_dotenv

load_dotenv()

# ========================== SECRETS ==========================
API_KEY = os.getenv("ALPACA_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET")
SENDER_EMAIL = os.getenv("EMAIL_USER")
SENDER_PASSWORD = os.getenv("EMAIL_PASS")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not all([API_KEY, SECRET_KEY]):
    raise ValueError("Missing Alpaca keys!")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = CryptoHistoricalDataClient(API_KEY, SECRET_KEY)

# ========================== CONFIG ==========================
SYMBOL = "BTC/USD"
LOT_SIZE = 0.01
RISK_REWARD = 2.5
MAX_TRADES_PER_DAY = 10
COOLDOWN_BARS = 10
EMA_FAST = 9
EMA_SLOW = 21
ATR_PERIOD = 14
SL_ATR_BUFFER = 0.1
MIN_BODY_PCT = 0.20
VOL_MULT = 1.05
USE_BODY_FILTER = False
USE_VOLUME_FILTER = False
USE_EMA_FILTER = True
ENTRY_TF = TimeFrame(5, TimeFrameUnit.Minute)
HTF_TF = TimeFrame(30, TimeFrameUnit.Minute)
SERVER_TZ = pytz.timezone("Europe/Moscow")
RECIPIENTS = ["tasksubmission878@gmail.com", "eventshadab@gmail.com"]

# ========================== TELEGRAM ALERT ==========================
def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    try:
        requests.post(url, data=payload, timeout=10)
        print("Telegram alert sent")
    except Exception as e:
        print(f"Telegram send failed: {e}")

# ========================== EMAIL ALERT ==========================
def send_email(subject: str, body: str):
    if not SENDER_EMAIL or not SENDER_PASSWORD:
        return
    try:
        msg = MIMEMultipart()
        msg["From"] = SENDER_EMAIL
        msg["To"] = ", ".join(RECIPIENTS)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECIPIENTS, msg.as_string())
        print(f"Email sent: {subject}")
    except Exception as e:
        print(f"Email failed: {e}")

# ========================== FLASK ==========================
app = Flask(__name__)
@app.route('/')
def home():
    return f"BTC Bot + Telegram Alerts<br>Running 24/7 | {datetime.now(SERVER_TZ).strftime('%Y-%m-%d %H:%M:%S MSK')}"

threading.Thread(target=lambda: app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)), debug=False, use_reloader=False), daemon=True).start()

# ========================== DATA FUNCTIONS ==========================
def get_bars(tf, limit=1000):
    try:
        req = CryptoBarsRequest(symbol_or_symbols=SYMBOL, timeframe=tf, limit=limit)
        bars = data_client.get_crypto_bars(req)
        if bars.df.empty: return None
        df = bars.df.copy()
        if isinstance(df.index, pd.MultiIndex): df = df.droplevel(0)
        df = df.reset_index()
        df['time'] = pd.to_datetime(df['timestamp']).dt.tz_convert(SERVER_TZ)
        df.rename(columns={'volume': 'tick_volume'}, inplace=True)
        return df
    except Exception as e:
        print(f"Bars error: {e}")
        return None

def get_latest_quote():
    try:
        req = CryptoLatestQuoteRequest(symbol_or_symbols=SYMBOL)
        quote = data_client.get_crypto_latest_quote(req)
        q = quote[SYMBOL]
        return {"bid": float(q.bid_price), "ask": float(q.ask_price)}
    except Exception as e:
        return {"bid": 0.0, "ask": 0.0}

def ema(series, period): return series.ewm(span=period, adjust=False).mean()
def atr(df, period=14):
    tr = pd.concat([df['high']-df['low'], (df['high']-df['close'].shift()).abs(), (df['low']-df['close'].shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()
def vwap(df):
    tp = (df['high'] + df['low'] + df['close']) / 3
    df['day'] = df['time'].dt.date
    df['pv'] = tp * df['tick_volume']
    return df.groupby('day')['pv'].cumsum() / df.groupby('day')['tick_volume'].cumsum()

# ========================== ACCOUNT ==========================
def reset_paper_balance():
    try:
        trading_client._client.reset()
        msg = "Paper Balance Reset → $100,000"
        print(msg)
        send_telegram(msg)
        time.sleep(3)
        return True
    except: return False

def get_account_info():
    try:
        acc = trading_client.get_account()
        bp = float(acc.non_marginable_buying_power or acc.buying_power or 0)
        if bp < 500: reset_paper_balance(); acc = trading_client.get_account(); bp = float(acc.non_marginable_buying_power or 0)
        try:
            pos = trading_client.get_position(SYMBOL)
            return {"side": "LONG" if float(pos.qty)>0 else "SHORT", "qty": abs(float(pos.qty)), "entry": float(pos.avg_entry_price), "upl": float(pos.unrealized_pl)}
        except:
            return {"side": "FLAT", "qty": 0, "entry": 0, "upl": 0}
    except: return {"side": "ERR", "qty": 0}

# ========================== ORDER + ALERTS ==========================
def place_order(side_str, sl, tp):
    quote = get_latest_quote()
    if quote["ask"] == 0: return False, None
    price = quote["ask"] if side_str == "BUY" else quote["bid"]
    try:
        trading_client.submit_order(MarketOrderRequest(symbol=SYMBOL, qty=LOT_SIZE, side=OrderSide.BUY if side_str=="BUY" else OrderSide.SELL, time_in_force=TimeInForce.GTC))
        msg = f"TRADE OPENED\n" \
              f"{side_str} BTC/USD\n" \
              f"Entry ≈ ${price:,.2f}\n" \
              f"SL: ${sl:,.2f} | TP: ${tp:,.2f}\n" \
              f"Size: {LOT_SIZE} BTC\n" \
              f"{datetime.now(SERVER_TZ).strftime('%H:%M %Y-%m-%d')} MSK"
        print(msg)
        send_email(f"TRADE – {side_str}", msg.replace('\n', '<br>'))
        send_telegram(msg)
        return True, None
    except Exception as e:
        msg = f"TRADE FAILED\n{side_str} BTC/USD\n{str(e)[:100]}"
        print(msg)
        send_telegram(msg)
        return False, None

# ========================== MAIN LOOP ==========================
def run_strategy():
    trades_today = cooldown = 0
    last_trade_day = last_bar_time = None
    in_trade = last_signal = last_risk = None

    print("BTC/USD BOT + TELEGRAM ALERTS STARTED!")
    get_account_info()

    while True:
        try:
            ltf = get_bars(ENTRY_TF, 200)
            htf = get_bars(HTF_TF, 100)
            if ltf is None or htf is None or len(ltf) < 50 or len(htf) < 10: 
                time.sleep(5); continue
            if ltf['time'].iloc[-1] == last_bar_time: time.sleep(1); continue
            last_bar_time = ltf['time'].iloc[-1]
            if last_trade_day != last_bar_time.date(): trades_today = cooldown = 0; last_trade_day = last_bar_time.date()

            # Indicators
            ltf['ema_fast'] = ema(ltf['close'], EMA_FAST)
            ltf['ema_slow'] = ema(ltf['close'], EMA_SLOW)
            ltf['atr'] = atr(ltf)
            ltf['vwap'] = vwap(ltf)

            close = ltf['close'].iloc[-1]; open_p = ltf['open'].iloc[-1]
            bullish = close > open_p and (abs(close-open_p)/(ltf['high'].iloc[-1]-ltf['low'].iloc[-1]+1e-8) >= MIN_BODY_PCT or not USE_BODY_FILTER)
            bearish = close < open_p and (abs(close-open_p)/(ltf['high'].iloc[-1]-ltf['low'].iloc[-1]+1e-8) >= MIN_BODY_PCT or not USE_BODY_FILTER)
            vol_ok = ltf['tick_volume'].iloc[-1] >= ltf['tick_volume'].iloc[-2] * VOL_MULT or not USE_VOLUME_FILTER
            trend_up = (not USE_EMA_FILTER) or (ltf['ema_fast'].iloc[-1] > ltf['ema_slow'].iloc[-1] and close > ltf['vwap'].iloc[-1])
            trend_down = (not USE_EMA_FILTER) or (ltf['ema_fast'].iloc[-1] < ltf['ema_slow'].iloc[-1] and close < ltf['vwap'].iloc[-1])
            htf_bull = htf['close'].iloc[-1] > htf['open'].iloc[-1]
            htf_bear = htf['close'].iloc[-1] < htf['open'].iloc[-1]

            sl_long = htf['low'].iloc[-2] - ltf['atr'].iloc[-1] * SL_ATR_BUFFER
            tp_long = close + (close - sl_long) * RISK_REWARD
            sl_short = htf['high'].iloc[-2] + ltf['atr'].iloc[-1] * SL_ATR_BUFFER
            tp_short = close - (sl_short - close) * RISK_REWARD

            quote = get_latest_quote()

            if cooldown == 0 and trades_today < MAX_TRADES_PER_DAY and not in_trade:
                if htf_bull and trend_up and bullish and vol_ok:
                    if place_order("BUY", sl_long, tp_long)[0]:
                        trades_today += 1; cooldown = COOLDOWN_BARS; in_trade = True; last_signal = "BUY"; last_risk = close - sl_long
                elif htf_bear and trend_down and bearish and vol_ok:
                    if place_order("SELL", sl_short, tp_short)[0]:
                        trades_today += 1; cooldown = COOLDOWN_BARS; in_trade = True; last_signal = "SELL"; last_risk = sl_short - close

            if in_trade:
                try:
                    pos = trading_client.get_position(SYMBOL)
                    entry = float(pos.avg_entry_price)
                    price = quote["bid"] if last_signal == "BUY" else quote["ask"]
                    if (last_signal == "BUY" and price <= entry - last_risk) or (last_signal == "SELL" and price >= entry + last_risk):
                        trading_client.close_position(SYMBOL)
                        msg = f"STOP LOSS HIT\n{last_signal} closed @ ${price:,.2f}"
                        print(msg); send_telegram(msg); in_trade = False
                    elif (last_signal == "BUY" and price >= entry + last_risk * RISK_REWARD) or (last_signal == "SELL" and price <= entry - last_risk * RISK_REWARD):
                        trading_client.close_position(SYMBOL)
                        msg = f"TAKE PROFIT HIT\n{last_signal} closed @ ${price:,.2f}\n+{RISK_REWARD}:1!"
                        print(msg); send_telegram(msg); in_trade = False
                except: in_trade = False

            if cooldown > 0: cooldown -= 1
        except Exception as e:
            print(f"Error: {e}")
        time.sleep(1)

print("Launching FINAL Bot with Telegram Alerts...")
run_strategy()
