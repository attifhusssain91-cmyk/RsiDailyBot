import ccxt
import pandas as pd
import requests
import time
from datetime import datetime
import pytz

# --- ⚙️ CONFIGURATION ---
# ✅ UPDATED TELEGRAM BOT CREDENTIALS
TELEGRAM_TOKEN = '8650963510:AAEuyDpP7TTURk0gfygY4uxXHQqCoqF179U'
TELEGRAM_CHAT_ID = '6088825847'

# ✅ BINANCE SETUP WITH MAXIMUM RESILIENCE
future_ex = ccxt.binance({
    'options': {'defaultType': 'future'}, 
    'enableRateLimit': True,
    'urls': {'api': {'public': 'https://api1.binance.com/api/v3'}}
})

pkt_timezone = pytz.timezone('Asia/Karachi')
# Persistent tracker: { 'BTC/USDT': {'div_bear_1d': timestamp, 'div_bull_1d': timestamp, 'ema_touch_1d': timestamp} }
signal_tracker = {}

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: 
        pass

def get_pkt_now():
    return datetime.now(pkt_timezone).strftime('%d-%m-%Y | %I:%M:%S %p')

def calculate_rsi_wilders(series, period=14):
    """ Wilder's Smoothing RSI - Exactly matches TradingView """
    if len(series) < period:
        return pd.Series([50] * len(series))
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def get_all_futures_symbols():
    """ Fetches all active USDT futures markets cleanly, filtering out delisted ones """
    try:
        markets = future_ex.load_markets()
        symbols = []
        for symbol, market in markets.items():
            if market['linear'] and market['settle'] == 'USDT' and market['active']:
                symbols.append(symbol)
        return symbols
    except Exception as e:
        print(f"Error fetching symbols: {e}")
        return []

def analyze_coin(symbol):
    global signal_tracker
    try:
        # Fetch 350 daily candles to guarantee enough warm-up history for a perfect EMA 190
        bars_1d = future_ex.fetch_ohlcv(symbol, timeframe='1d', limit=350)
        
        # FAIL-SAFE: Completely skip new or low-data coins without breaking the bot loop
        if not bars_1d or len(bars_1d) < 240:
            return

        df = pd.DataFrame(bars_1d, columns=['ts', 'o', 'h', 'l', 'close', 'v'])
        
        # Technical Indicator Calculations
        df['rsi'] = calculate_rsi_wilders(df['close'], 14)
        df['ema190'] = df['close'].ewm(span=190, adjust=False).mean()
        
        # Verify calculation didn't leave NaN values
        if df['ema190'].isna().iloc[-1] or df['rsi'].isna().iloc[-1]:
            return

        clean_name = symbol.replace(':USDT', '')
        if symbol not in signal_tracker:
            signal_tracker[symbol] = {}

        # Last completed closed day index (prevent mid-day signal shifts)
        last_closed_idx = len(df) - 2
        p_closed = df.iloc[last_closed_idx]
        
        # ---------------------------------------------------------------------
        # 🎯 STRATEGY 1: HISTORICAL RSI 14 DIVERGENCE (LOOKING BACK 50 CANDLES)
        # ---------------------------------------------------------------------
        # Identifies structural pivot points within the last 50 closed daily candles
        window = 3
        lookback_limit = max(window, last_closed_idx - 50)
        high_swings = []
        low_swings = []
        
        for i in range(lookback_limit, last_closed_idx + 1):
            if i + window >= len(df): 
                continue
            # Scan structural Peak Highs
            if df['h'].iloc[i] == df['h'].iloc[i-window:i+window+1].max():
                high_swings.append(i)
            # Scan structural Trough Lows
            if df['l'].iloc[i] == df['l'].iloc[i-window:i+window+1].min():
                low_swings.append(i)

        # 🐻 Bearish Divergence Scan (Runs even through extended choppy consolidation)
        if len(high_swings) >= 2 and high_swings[-1] == last_closed_idx:
            curr_peak = df.iloc[high_swings[-1]]
            # Look at previous peaks found within your 50 candle range
            for prev_idx in reversed(high_swings[:-1]):
                prev_peak = df.iloc[prev_idx]
                
                # Condition: Current price high is equal or higher, but RSI peak has clearly weakened
                if curr_peak['h'] >= prev_peak['h'] and curr_peak['rsi'] < prev_peak['rsi']:
                    if signal_tracker[symbol].get('div_bear_1d') != p_closed['ts']:
                        msg = (f"🔴 *RSI BEARISH DIVERGENCE (1D)* 🔴\n"
                               f"🪙 *Coin:* {clean_name}\n"
                               f"📉 *Action:* SELL / SHORT\n"
                               f"📊 *Price Highs:* {prev_peak['h']} ➡️ {curr_peak['h']} (Higher/Equal High)\n"
                               f"📉 *RSI Highs:* {round(prev_peak['rsi'], 2)} ➡️ {round(curr_peak['rsi'], 2)} (Lower High)\n"
                               f"⏱️ *Peak Gap:* {last_closed_idx - prev_idx} days ago\n"
                               f"🕒 {get_pkt_now()}")
                        send_telegram(msg)
                        signal_tracker[symbol]['div_bear_1d'] = p_closed['ts']
                        time.sleep(240)  # Strategy 1 execution sleep: 4 minutes
                        break

        # 🐂 Bullish Divergence Scan
        if len(low_swings) >= 2 and low_swings[-1] == last_closed_idx:
            curr_trough = df.iloc[low_swings[-1]]
            for prev_idx in reversed(low_swings[:-1]):
                prev_trough = df.iloc[prev_idx]
                
                # Condition: Current price low is lower, but RSI has bottomed into a higher structural low
                if curr_trough['l'] < prev_trough['l'] and curr_trough['rsi'] > prev_trough['rsi']:
                    if signal_tracker[symbol].get('div_bull_1d') != p_closed['ts']:
                        msg = (f"🟢 *RSI BULLISH DIVERGENCE (1D)* 🟢\n"
                               f"🪙 *Coin:* {clean_name}\n"
                               f"📈 *Action:* BUY / LONG\n"
                               f"📊 *Price Lows:* {prev_trough['l']} ➡️ {curr_trough['l']} (Lower Low)\n"
                               f"📈 *RSI Lows:* {round(prev_trough['rsi'], 2)} ➡️ {round(curr_trough['rsi'], 2)} (Higher Low)\n"
                               f"⏱️ *Trough Gap:* {last_closed_idx - prev_idx} days ago\n"
                               f"🕒 {get_pkt_now()}")
                        send_telegram(msg)
                        signal_tracker[symbol]['div_bull_1d'] = p_closed['ts']
                        time.sleep(240)  # Strategy 1 execution sleep: 4 minutes
                        break

        # ---------------------------------------------------------------------
        # 🎯 STRATEGY 2: REAL-TIME DAILY EMA 190 TOUCH (LIVE CANDLE)
        # ---------------------------------------------------------------------
        live_candle = df.iloc[-1]
        prev_day = df.iloc[-2]
        
        was_above_ema = prev_day['low'] > prev_day['ema190']
        live_touch = live_candle['l'] <= live_candle['ema190']

        if was_above_ema and live_touch:
            if signal_tracker[symbol].get('ema_touch_1d') != live_candle['ts']:
                msg = (f"⚡ *EMA 190 DAILY TOUCH (REAL-TIME)* ⚡\n"
                       f"🪙 *Coin:* {clean_name}\n"
                       f"📈 *Action:* BUY ALERT\n"
                       f"📉 *Context:* Price dropped from above and just hit the Daily EMA 190 line!\n"
                       f"📊 *Daily EMA 190 Level:* {round(live_candle['ema190'], 4)}\n"
                       f"💲 *Live Low:* {live_candle['l']} | *Live Close:* {live_candle['close']}\n"
                       f"🕒 {get_pkt_now()}")
                send_telegram(msg)
                signal_tracker[symbol]['ema_touch_1d'] = live_candle['ts']
                time.sleep(300)  # Strategy 2 execution sleep: 5 minutes

    except Exception as e:
        # Absolute Fail-safe protection: Delisted coins or API timeouts prints an error internally but NEVER crashes execution loop.
        print(f"Skipping error or delisted token {symbol}: {e}")
        pass

if __name__ == "__main__":
    welcome = f"🚀 *Binance Smart Batch-Scanning Trading Bot*\n📍 *Strategies:* 50-Bar RSI Divergence + Live EMA 190 Touch\n🕒 {get_pkt_now()}"
    send_telegram(welcome)
    
    while True:
        try:
            # Dynamically fetch up-to-date market tickers to safely account for newly added or removed listings
            all_coins = get_all_futures_symbols()
            
            if all_coins:
                batch_size = 100
                # Split total symbols array into sequential 100-coin sub-arrays
                batches = [all_coins[i:i + batch_size] for i in range(0, len(all_coins), batch_size)]
                
                for idx, batch in enumerate(batches):
                    print(f"Scanning batch {idx + 1}/{len(batches)} ({len(batch)} coins) at {get_pkt_now()}...")
                    
                    for symbol in batch:
                        analyze_coin(symbol)
                        time.sleep(0.15) # Safe rate-limit buffer spacing
                    
                    # Do not sleep after completing the very last batch, loop back up to start immediately
                    if idx < len(batches) - 1:
                        print(f"Batch {idx + 1} finished. Entering mandated 5-minute cooldown...")
                        time.sleep(300)
                        
            print("Completed scanning all available Binance pairs. Restarting loop back to Batch 1 instantly...\n")
            
        except Exception as e:
            print(f"Core main processing loop recovery exception: {e}")
            time.sleep(60)