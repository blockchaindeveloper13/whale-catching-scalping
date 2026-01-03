import ccxt
import time
import telebot
import os
import pandas as pd
import numpy as np
import google.generativeai as genai
import psycopg2
import threading
import re
import requests
import sys
from flask import Flask, request
from datetime import datetime

# --- LOG AYARI ---
sys.stdout.reconfigure(encoding='utf-8')

# --- AYARLAR ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY')
BINANCE_SECRET = os.environ.get('BINANCE_SECRET_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
HEROKU_APP_URL = os.environ.get('HEROKU_APP_URL')

# --- MODEL SEÇİMİ ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

bot = telebot.TeleBot(BOT_TOKEN)
server = Flask(__name__)

exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET,
    'options': {'defaultType': 'spot', 'adjustForTimeDifference': True},
    'enableRateLimit': True
})

# --- VERİTABANI ---
def db_baglan():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def db_islem(sql, params=None):
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute(sql, params)
        res = None
        if "SELECT" in sql: res = cur.fetchall()
        else: conn.commit()
        cur.close()
        conn.close()
        return res
    except Exception as e:
        print(f"❌ DB Hatası: {e}", flush=True)
        return None

# --- OTOMATİK TAMİRAT ---
def db_baslat():
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                symbol VARCHAR(20) PRIMARY KEY,
                last_signal VARCHAR(50) DEFAULT 'YOK',
                interval_hours REAL DEFAULT 4,
                last_report_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                target_price REAL DEFAULT 0,
                near_target BOOLEAN DEFAULT FALSE
            )
        """)
        conn.commit()
        try:
            cur.execute("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS target_price REAL DEFAULT 0;")
            conn.commit()
        except: pass
        try:
            cur.execute("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS near_target BOOLEAN DEFAULT FALSE;")
            conn.commit()
        except: pass
        cur.close()
        conn.close()
    except Exception as e:
        print(f"🔥 DB Başlatma Hatası: {e}", flush=True)

db_baslat()

# --- ANALİZ MOTORU ---
def get_technical_data(symbol):
    try:
        if "/" not in symbol: symbol += "/USDT"
        ticker = exchange.fetch_ticker(symbol)
        price = ticker['last']
        bars = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=100)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        
        last_vol = df['volume'].iloc[-1]
        avg_vol = df['volume'].rolling(window=20).mean().iloc[-1]
        vol_change = ((last_vol - avg_vol) / avg_vol) * 100
        
        df['obv'] = (pd.Series(np.where(df['close'] > df['close'].shift(1), df['volume'], 
                       np.where(df['close'] < df['close'].shift(1), -df['volume'], 0))).cumsum())
        obv_sinyal = "POZİTİF" if df['obv'].iloc[-1] > df['obv'].iloc[-5] else "NEGATİF"

        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        ema50 = df['close'].ewm(span=50, adjust=False).mean()
        trend = "YÜKSELİŞ" if price > ema50.iloc[-1] else "DÜŞÜŞ"

        data_text = (f"FİYAT: {price}\n"
                     f"TREND: {trend}\n"
                     f"RSI (1S): {rsi.iloc[-1]:.1f}\n"
                     f"HACİM DEĞİŞİMİ: %{vol_change:.1f}\n"
                     f"PARA AKIŞI (OBV): {obv_sinyal}")
        return data_text, price, rsi.iloc[-1], vol_change
    except Exception as e:
        return None, 0, 0, 0

def ask_gemini(symbol, data):
    try:
        prompt = (f"GÖREV: Askeri Finans Raporu. Coin: {symbol}.\n"
                  f"VERİLER:\n{data}\n"
                  f"EMİR: Net bir karar ver (AL/SAT/BEKLE). Hacim destekliyor mu? Kısa ve Askeri dilde yaz.")
        response = model.generate_content(prompt)
        return response.text.replace("**", "")
    except Exception as e:
        return f"⚠️ Manuel Analiz: {data}"

# --- FLASK ---
@server.route('/' + BOT_TOKEN, methods=['POST'])
def getMessage():
    bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode('utf-8'))])
    return "!", 200

@server.route("/")
def webhook():
    bot.remove_webhook()
    bot.set_webhook(url=HEROKU_APP_URL + BOT_TOKEN)
    return "ONLINE v5.1 (Emotional)", 200

@bot.message_handler(func=lambda m: True)
def handle_message(m):
    try:
        text = m.text.upper()
        
        # Coin Tespiti
        words = text.split()
        found_coin = None
        COMMON = ["BTC", "ETH", "SOL", "AAVE", "AVAX", "XRP", "LTC", "LINK", "DOGE", "SHIB"]
        for w in words:
            clean = w.strip(".,!?")
            if clean in COMMON or (len(clean) > 2 and clean.isalpha() and clean not in ["HER", "DAKIKA", "SAAT", "ANALIZ", "HEDEF"]):
                found_coin = clean
                break
        
        if found_coin:
            symbol = f"{found_coin}/USDT"

            # 1. NÖBET
            zaman = re.search(r'(HER|\d+)\s*(SAAT|DK|DAKIKA|DAK)', text)
            if zaman:
                miktar = zaman.group(1)
                birim = zaman.group(2)
                sure = 1 if miktar == "HER" else int(miktar)
                interval = sure / 60.0 if "DK" in birim or "DAK" in birim else float(sure)
                
                db_islem("INSERT INTO watchlist (symbol, interval_hours) VALUES (%s, %s) ON CONFLICT (symbol) DO UPDATE SET interval_hours = %s", (symbol, interval, interval))
                bot.reply_to(m, f"✅ Emredersiniz Paşam! {found_coin} nöbeti başladı.")
                
                if interval <= 0.05:
                    data, prc, rsi, vol = get_technical_data(symbol)
                    if data: bot.send_message(m.chat.id, ask_gemini(symbol, data))
                return

            # 2. HEDEF
            hedef = re.search(r'(\d+(\.\d+)?)\s*(DOLAR|USDT|HEDEF|FIYAT)', text)
            if hedef:
                fiyat = float(hedef.group(1))
                db_islem("INSERT INTO watchlist (symbol, target_price, near_target) VALUES (%s, %s, FALSE) ON CONFLICT (symbol) DO UPDATE SET target_price = %s, near_target = FALSE", (symbol, fiyat, fiyat))
                bot.reply_to(m, f"🎯 {found_coin} Hedefi kilitlendi Paşam: {fiyat}")
                return

            # 3. MANUEL ANALİZ
            if "ANALIZ" in text or "DURUM" in text:
                bot.reply_to(m, f"🔎 {found_coin} cephesi taranıyor Komutanım...")
                data, prc, rsi, vol = get_technical_data(symbol)
                if data:
                    cevap = ask_gemini(symbol, data)
                    bot.send_message(m.chat.id, cevap)
                return

        # --- NORMAL SOHBET (BURASI DÜZELTİLDİ) ---
        if not m.text.startswith("/"):
            try:
                # PAŞAM BURAYI DEĞİŞTİRDİK ARTIK TRİP ATMAYACAK
                prompt = (f"Sen Vedat Paşa'nın sadık, hevesli ve disiplinli yaverisin. "
                          f"Kullanıcı: '{m.text}'. "
                          f"Cevabın: Samimi, saygılı ve 'Paşam' veya 'Komutanım' hitabıyla olsun. "
                          f"Asla 'yapay zekayım' deme. Asla sadece 'Sağ olun' gibi kısa kesme. Biraz moral ver.")
                
                res = model.generate_content(prompt).text
                bot.reply_to(m, res.replace("**", ""))
            except: pass
            
    except Exception as e:
        print(f"Hata: {e}", flush=True)

# --- KULE ---
def watch_tower():
    print("👀 Nöbetçi Kulesi Devrede...", flush=True)
    last_ping = time.time()
    
    while True:
        try:
            if time.time() - last_ping > 1200:
                if HEROKU_APP_URL: requests.get(HEROKU_APP_URL)
                last_ping = time.time()

            rows = db_islem("SELECT symbol, interval_hours, last_report_time, target_price, near_target FROM watchlist")
            if rows:
                now = datetime.now()
                for r in rows:
                    sym, interval, last_time, target, near_flag = r
                    try:
                        ticker = exchange.fetch_ticker(sym)
                        price = ticker['last']
                        if target and target > 0:
                            diff = abs(price - target) / target * 100
                            if diff < 0.2:
                                bot.send_message(CHAT_ID, f"🚨 VURULDU PAŞAM! {sym}: {price}")
                                db_islem("UPDATE watchlist SET target_price = 0 WHERE symbol = %s", (sym,))
                            elif diff < 1.0 and not near_flag:
                                bot.send_message(CHAT_ID, f"⚠️ {sym} hedefe yaklaştı ({price})")
                                db_islem("UPDATE watchlist SET near_target = TRUE WHERE symbol = %s", (sym,))
                    except: pass

                    if interval:
                        gecen = (now - last_time).total_seconds() / 3600 if last_time else 999
                        if gecen >= interval:
                            data, prc, rsi, vol = get_technical_data(sym)
                            if data:
                                cevap = ask_gemini(sym, data)
                                db_islem("UPDATE watchlist SET last_report_time = NOW() WHERE symbol = %s", (sym,))
                                bot.send_message(CHAT_ID, f"⏰ {sym} RAPORU:\n{cevap}")
                                time.sleep(2)
            time.sleep(20)
        except Exception as e:
            time.sleep(20)

if __name__ == "__main__":
    t = threading.Thread(target=watch_tower)
    t.start()
    server.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
            
