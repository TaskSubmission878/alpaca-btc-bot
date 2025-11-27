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
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# ========================== SECRETS FROM .env ==========================
API_KEY = os.getenv("ALPACA_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET")
SENDER_EMAIL = os.getenv("EMAIL_USER")
SENDER_PASSWORD = os.getenv("EMAIL_PASS")

# Validate secrets
if not all([API_KEY, SECRET_KEY, SENDER_EMAIL, SENDER_PASSWORD]):
    raise ValueError("Missing required environment variables! Check your .env file.")

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

# ========================== EMAIL & FLASK ==========================
def send_email(subject: str, body: str):
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

app = Flask(__name__)
@app.route('/')
def home():
    return f"Alpaca BTC/USD Bot Running 24/7 | {datetime.now(SERVER_TZ).strftime('%Y-%m-%d %H:%M:%S MSK')}"

# Render.com uses PORT env var
threading.Thread(
    target=lambda: app.run(
        host='0.0.0.0',
        port=int(os.environ.get("PORT", 8080)),
        debug=False,
        use_reloader=False
    ),
    daemon=True
).start()

# ========================== DATA & INDICATORS ==========================
def get_bars(tf, limit=1000):
    try:
        req = CryptoBarsRequest(symbol_or_symbols=SYMBOL, timeframe=tf, limit=limit)
        bars = data_client.get_crypto_bars(req)
        if bars.df.empty:
            return None
        df = bars.df.copy()
        if isinstance(df.index, pd.MultiIndex):
            df = df.droplevel(0)
        df = df.reset_index()
        df['time'] = pd.to_datetime(df['timestamp']).dt.tz_convert(SERVER_TZ)
        df.rename(columns={'volume': 'tick_volume'}, inplace=True)
        return df
    except Exception as e:
        print(f"Bars fetch error: {e}")
        return None

def get_latest_quote():
    try:
        req = CryptoLatestQuoteRequest(symbol_or_symbols=SYMBOL)
        quote = data_client.get_crypto_latest_quote(req)
        q = quote[SYMBOL]
        return {"bid": float(q.bid_price), "ask": float(q.ask_price)}
    except Exception as e:
        print(f"Quote error: {e}")
        return {"bid": 0.0, "ask": 0.0}

def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def vwap(df):
    tp = (df['high'] + df['low'] + df['close']) / 3
    df['day'] = df['time'].dt.date
    df['pv'] = tp * df['tick_volume']
    cum_pv = df.groupby('day')['pv'].cumsum()
    cum_vol = df.groupby('day')['tick_volume'].cumsum()
    return cum_pv / cum_vol

# ========================== ACCOUNT & BALANCE ==========================
def reset_paper_balance():
    try:
        trading_client._client.reset()
        print("PAPER BALANCE RESET TO $100,000 USD")
        send_email("Paper Account Reset", "<h2>Balance Reset to $100,000</h2><p>Bot ready!</p>")
        time.sleep(3)
        return True
    except Exception as e:
        print(f"Reset failed: {e}")
        return False

def get_account_info():
    try:
        acc = trading_client.get_account()
        equity = float(acc.equity or 0)
        cash = float(acc.cash or 0)
        buying_power = float(acc.non_marginable_buying_power or acc.buying_power or 0)
        crypto_status = getattr(acc, 'crypto_status', 'UNKNOWN')

        if buying_power < 500:
            print("Low balance → Auto-resetting...")
            reset_paper_balance()
            acc = trading_client.get_account()
            buying_power = float(acc.non_marginable_buying_power or acc.buying_power or 0)

        try:
            pos = trading_client.get_position(SYMBOL)
            qty = float(pos.qty)
            side = "LONG" if qty > 0 else "SHORT"
            entry = float(pos.avg_entry_price)
            upl = float(pos.unrealized_pl)
        except:
            qty = entry = upl = 0.0
            side = "FLAT"

        print(f"Crypto Status: {crypto_status} | Buying Power: ${buying_power:,.0f}")
        return {"equity": equity, "bp": buying_power, "crypto_status": crypto_status, "side": side, "qty": abs(qty), "entry": entry, "upl": upl}
    except Exception as e:
        print(f"Account error: {e}")
        return {"equity": 0, "bp": 0, "crypto_status": "ERR", "side": "ERR", "qty": 0, "entry": 0, "upl": 0}

# ========================== ORDER EXECUTION ==========================
def place_order(side_str, sl, tp):
    quote = get_latest_quote()
    if quote["ask"] == 0:
        return False, None
    price = quote["ask"] if side_str == "BUY" else quote["bid"]
    side = OrderSide.BUY if side_str == "BUY" else OrderSide.SELL
    order = MarketOrderRequest(symbol=SYMBOL, qty=LOT_SIZE, side=side, time_in_force=TimeInForce.GTC)
    try:
        result = trading_client.submit_order(order)
        print(f"{side_str} ORDER EXECUTED @ ~{price:,.2f} | SL {sl:,.2f} | TP {tp:,.2f}")
        send_email(f"TRADE OPENED – {side_str}", f"<h2>New {side_str}</h2><p>Entry ~{price:,.2f}<br>SL {sl:,.2f}<br>TP {tp:,.2f}</p>")
        return True, result.id
    except Exception as e:
        print(f"ORDER FAILED: {e}")
        return False, None

# ========================== MAIN STRATEGY ==========================
def run_strategy():
    trades_today = 0
    last_trade_day = None
    cooldown = 0
    last_bar_time = None
    in_trade = False
    last_signal = None
    last_risk = None

    print("BTC/USD BOT STARTED – 24/7 on Render.com – READY TO TRADE!")
    get_account_info()  # Initialize + reset if needed

    while True:
        try:
            ltf = get_bars(ENTRY_TF, limit=200)
            htf = get_bars(HTF_TF, limit=100)
            if ltf is None or htf is None or len(ltf) < 50 or len(htf) < 10:
                time.sleep(5)
                continue

            bar_time = ltf['time'].iloc[-1]
            if last_bar_time == bar_time:
                time.sleep(1)
                continue
            last_bar_time = bar_time

            if last_trade_day != bar_time.date():
                trades_today = 0
                last_trade_day = bar_time.date()
                cooldown = 0

            ltf['ema_fast'] = ema(ltf['close'], EMA_FAST)
            ltf['ema_slow'] = ema(ltf['close'], EMA_SLOW)
            ltf['atr'] = atr(ltf, ATR_PERIOD)
            ltf['vwap'] = vwap(ltf)

            close = ltf['close'].iloc[-1]
            open_p = ltf['open'].iloc[-1]
            high = ltf['high'].iloc[-1]
            low = ltf['low'].iloc[-1]
            vol = ltf['tick_volume'].iloc[-1]
            vol_prev = ltf['tick_volume'].iloc[-2] if len(ltf) > 1 else 0

            atr_val = ltf['atr'].iloc[-1]
            ema_f = ltf['ema_fast'].iloc[-1]
            ema_s = ltf['ema_slow'].iloc[-1]
            vwap_val = ltf['vwap'].iloc[-1]

            body_pct = abs(close - open_p) / (high - low + 1e-8)
            bullish = close > open_p and (body_pct >= MIN_BODY_PCT or not USE_BODY_FILTER)
            bearish = close < open_p and (body_pct >= MIN_BODY_PCT or not USE_BODY_FILTER)
            vol_ok = vol >= vol_prev * VOL_MULT or not USE_VOLUME_FILTER

            trend_up = (not USE_EMA_FILTER) or (ema_f > ema_s and close > vwap_val)
            trend_down = (not USE_EMA_FILTER) or (ema_f < ema_s and close < vwap_val)

            htf_bull = htf['close'].iloc[-1] > htf['open'].iloc[-1]
            htf_bear = htf['close'].iloc[-1] < htf['open'].iloc[-1]
            htf_high_prev = htf['high'].iloc[-2]
            htf_low_prev = htf['low'].iloc[-2]

            can_enter = cooldown == 0 and trades_today < MAX_TRADES_PER_DAY

            sl_long = htf_low_prev - atr_val * SL_ATR_BUFFER
            tp_long = close + (close - sl_long) * RISK_REWARD
            sl_short = htf_high_prev + atr_val * SL_ATR_BUFFER
            tp_short = close - (sl_short - close) * RISK_REWARD

            acc = get_account_info()
            quote = get_latest_quote()

            print("\n" + "═" * 90)
            print(f" TIME          : {datetime.now(SERVER_TZ):%Y-%m-%d %H:%M:%S} MSK")
            print(f" BAR           : {bar_time:%H:%M} | Close {close:,.2f}")
            print(f" ACCOUNT       : Equity ${acc['equity']:,.0f} | BP ${acc['bp']:,.0f}")
            print(f" POSITION      : {acc['side']} {acc['qty']:.4f} BTC @ ${acc['entry']:,.2f} | P&L ${acc['upl']:+.2f}")
            print(f" BIAS          : HTF {'Bullish' if htf_bull else 'Bearish'} | Trend {'Up' if trend_up else 'Down'}")
            print(f" CAN ENTER     : {'YES' if can_enter else 'NO'} | Trades Today: {trades_today}/{MAX_TRADES_PER_DAY}")
            print("═" * 90)

            if can_enter and htf_bull and trend_up and bullish and vol_ok and not in_trade:
                print(f"LONG SIGNAL → Opening at ~{quote['ask']:,.2f}")
                success, _ = place_order("BUY", sl_long, tp_long)
                if success:
                    trades_today += 1
                    cooldown = COOLDOWN_BARS
                    in_trade = True
                    last_signal = "BUY"
                    last_risk = close - sl_long

            elif can_enter and htf_bear and trend_down and bearish and vol_ok and not in_trade:
                print(f"SHORT SIGNAL → Opening at ~{quote['bid']:,.2f}")
                success, _ = place_order("SELL", sl_short, tp_short)
                if success:
                    trades_today += 1
                    cooldown = COOLDOWN_BARS
                    in_trade = True
                    last_signal = "SELL"
                    last_risk = sl_short - close

            if in_trade:
                try:
                    pos = trading_client.get_position(SYMBOL)
                    entry = float(pos.avg_entry_price)
                    price = quote["bid"] if last_signal == "BUY" else quote["ask"]
                    if (last_signal == "BUY" and price <= entry - last_risk) or \
                       (last_signal == "SELL" and price >= entry + last_risk):
                        trading_client.close_position(SYMBOL)
                        print(f"STOP LOSS HIT @ {price:,.2f}")
                        in_trade = False
                    elif (last_signal == "BUY" and price >= entry + last_risk * RISK_REWARD) or \
                         (last_signal == "SELL" and price <= entry - last_risk * RISK_REWARD):
                        trading_client.close_position(SYMBOL)
                        print(f"TAKE PROFIT HIT @ {price:,.2f}")
                        in_trade = False
                except:
                    in_trade = False

            if cooldown > 0:
                cooldown -= 1

        except Exception as e:
            print(f"Loop error: {e}")
        time.sleep(1)

# ========================== START ==========================
print("Launching FINAL 24/7 Alpaca BTC/USD Bot on Render.com...")
run_strategy()
