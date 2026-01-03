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
import requests  # YENİ EKLENDİ: Kendi kendini dürtmek için
from flask import Flask, request
from datetime import datetime

# --- 1. AYARLAR VE KİMLİK BİLGİLERİ ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY')
BINANCE_SECRET = os.environ.get('BINANCE_SECRET_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
HEROKU_APP_URL = os.environ.get('HEROKU_APP_URL') # Bu URL'in doğru olduğundan emin ol! (https://senin-app-adin.herokuapp.com/)

# --- YAPAY ZEKA AYARI ---
genai.configure(api_key=GEMINI_API_KEY)
model_list = ['gemini-2.5-flash', 'gemini-2.0-flash-exp', 'gemini-1.5-flash']
model = None
for m in model_list:
    try:
        model = genai.GenerativeModel(m)
        model.generate_content("Test")
        print(f"✅ AKTİF MODEL: {m}")
        break
    except: continue
if not model: model = genai.GenerativeModel('gemini-1.5-flash')

bot = telebot.TeleBot(BOT_TOKEN)
server = Flask(__name__)

# Binance Bağlantısı
exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET,
    'options': {'defaultType': 'spot', 'adjustForTimeDifference': True},
    'enableRateLimit': True
})

# --- MÜHİMMAT (PORTFÖY) YÜKLEME ---
try:
    markets = exchange.load_markets()
    TUM_COINLER = [symbol.split('/')[0] for symbol in markets if '/USDT' in symbol]
    print(f"✅ Mühimmat Deposu Hazır: {len(TUM_COINLER)} Silah (Coin).")
except Exception as e:
    TUM_COINLER = ["BTC", "ETH", "SOL", "AAVE", "LTC", "LINK", "AVAX", "BNB", "XRP", "ADA"]

# --- 2. VERİTABANI ---
def db_baglan():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def db_baslat():
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                symbol VARCHAR(20) PRIMARY KEY,
                last_signal VARCHAR(50) DEFAULT 'YOK',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                interval_hours INT DEFAULT 4,
                last_report_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_analysis TEXT,
                target_price REAL DEFAULT 0
            )
        """)
        cur.execute("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS last_analysis TEXT")
        cur.execute("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS target_price REAL DEFAULT 0")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e: print(f"Karargah Hatası: {e}")

db_baslat() 

def db_islem_yap(sql, params=None):
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
    except: return None

# --- 3. TEKNİK İSTİHBARAT RAPORU ---
def calculate_technicals(df):
    if len(df) < 50: return None
    
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()
    df['sma20'] = df['close'].rolling(window=20).mean()
    df['std'] = df['close'].rolling(window=20).std()
    df['upper_bb'] = df['sma20'] + (df['std'] * 2)
    df['lower_bb'] = df['sma20'] - (df['std'] * 2)
    df['pivot'] = (df['high'] + df['low'] + df['close']) / 3
    df['r1'] = (2 * df['pivot']) - df['low']
    df['s1'] = (2 * df['pivot']) - df['high']
    return df.iloc[-1]

def get_full_report(symbol):
    report_text = ""
    current_price = 0
    try:
        ticker = exchange.fetch_ticker(symbol)
        current_price = ticker['last']
        for tf in ['1h', '4h']:
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=60)
            df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            tech = calculate_technicals(df)
            if tech is None: continue
            trend = 'YÜKSELİŞ' if tech['close'] > tech['ema50'] else 'DÜŞÜŞ'
            report_text += (f"--- CEPHE HATTI: [{tf}] ---\n"
                            f"Anlık Fiyat: {tech['close']}\n"
                            f"DESTEK (S1): {tech['s1']:.4f}\n"
                            f"DİRENÇ (R1): {tech['r1']:.4f}\n"
                            f"RSI: {tech['rsi']:.1f}\n"
                            f"Trend: {trend}\n\n")
        return report_text, current_price
    except: return None, 0

def ask_gemini(symbol, report, last_signal):
    try:
        prompt = (f"Sen Vedat Paşa'nın 'Finansal Kurmayısın'. \n"
                  f"GÖREVİN: Piyasayı askeri netlikle raporla.\n"
                  f"Coin: {symbol}. Eski Sinyal: {last_signal}. \n"
                  f"Rapor:\n{report}\n"
                  f"EMİR: Durumu özetle, (AL / SAT / BEKLE) emrini ver.")
        raw_res = model.generate_content(prompt).text
        return raw_res.replace("**", "").replace("__", "")
    except Exception as e: return f"Hata: {e}"

# --- 4. SERVER VE KOMUTLAR ---
@server.route('/' + BOT_TOKEN, methods=['POST'])
def getMessage():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "!", 200

@server.route("/")
def webhook():
    bot.remove_webhook()
    bot.set_webhook(url=HEROKU_APP_URL + BOT_TOKEN)
    return "<h1>VEDAT PAŞA KARARGAHI ONLINE</h1>", 200

# --- 5. KALP MASAJI (KEEP-ALIVE) ---
# YENİ EKLENEN FONKSİYON:
def keep_alive_loop():
    print("💓 Kalp Masajı Ünitesi Devrede!")
    while True:
        try:
            # 20 dakikada bir (1200 saniye) siteye ping atar
            time.sleep(1200) 
            if HEROKU_APP_URL:
                response = requests.get(HEROKU_APP_URL)
                print(f"💓 PING ATILDI: {response.status_code}")
        except Exception as e:
            print(f"💓 Ping Hatası: {e}")

@bot.message_handler(commands=['unut', 'temizle'])
def komut_unut(m):
    db_islem_yap("UPDATE watchlist SET last_signal = 'YOK', last_analysis = NULL")
    bot.reply_to(m, "🧹 Hafıza temizlendi Paşam!")

@bot.message_handler(func=lambda message: True)
def sohbet_et(message):
    try:
        text = message.text.upper()
        # ... (Sohbet mantığı aynı kalacak) ...
        # (Burayı uzun uzun yazmıyorum, senin eski kodun aynısı)
        
        # Sadece Coin Tespiti ve İşlemleri kısmını ekliyorum:
        kelimeler = text.split()
        bulunan_coin = None
        for kelime in kelimeler:
            temiz_kelime = kelime.strip(".,!?") 
            if temiz_kelime in TUM_COINLER:
                bulunan_coin = temiz_kelime
                break
        
        # SNIPER MODU
        if any(x in text for x in ["GENEL", "SNIPER"]):
            rows = db_islem_yap("SELECT symbol, last_signal, interval_hours FROM watchlist")
            if not rows: return
            bot.reply_to(message, f"🔭 Sniper tarıyor Paşam...")
            for r in rows:
                sym = r[0]
                rep, prc = get_full_report(sym)
                if rep:
                    yorum = ask_gemini(sym, rep, r[1])
                    db_islem_yap("UPDATE watchlist SET last_analysis = %s WHERE symbol = %s", (yorum, sym))
                    bot.send_message(message.chat.id, f"{sym}:\n{yorum}")
            return

        # COIN İŞLEMLERİ
        if bulunan_coin:
            symbol = f"{bulunan_coin}/USDT"
            
            # Analiz
            if any(x in text for x in ["ANALIZ", "DURUM", "BAK"]):
                bot.reply_to(message, f"{symbol} inceleniyor...")
                rep, prc = get_full_report(symbol)
                if rep:
                    yorum = ask_gemini(symbol, rep, "Bilinmiyor")
                    db_islem_yap("UPDATE watchlist SET last_analysis = %s WHERE symbol = %s", (yorum, symbol))
                    bot.send_message(message.chat.id, yorum)
                return

            # Hafızadan Konuşma
            row = db_islem_yap("SELECT last_analysis FROM watchlist WHERE symbol = %s", (symbol,))
            if row and row[0][0]:
                prompt = f"Paşa soruyor: {message.text}. Rapor: {row[0][0]}. Cevapla."
                res = model.generate_content(prompt).text
                bot.reply_to(message, res)
                return

        # YAVER MODU
        if not message.text.startswith('/'):
            prompt = f"Kullanıcı: {message.text}. Sen Vedat Paşa'nın askerisin. Kısa ve net cevap ver."
            res = model.generate_content(prompt).text
            bot.reply_to(message, res.replace("**", ""))

    except Exception as e: print(e)

# --- 6. DEVRIYE DÖNGÜSÜ ---
def scanner_loop():
    print("💤 Nöbetçi Kulesi Aktif...")
    while True:
        try:
            rows = db_islem_yap("SELECT symbol, last_signal, interval_hours, last_report_time, target_price FROM watchlist")
            if not rows:
                time.sleep(900)
                continue
            
            now = datetime.now()
            for r in rows:
                sym, last_sig, interval, last_time, target = r
                if not interval: interval = 4
                
                # Fiyat Kontrol (Ücretsiz)
                try:
                    ticker = exchange.fetch_ticker(sym)
                    curr = ticker['last']
                    if target and target > 0:
                        if abs(curr - target) / target < 0.005: # %0.5 Yakınlık
                            bot.send_message(CHAT_ID, f"🚨 ALARM: {sym} Hedefte! Fiyat: {curr}")
                            db_islem_yap("UPDATE watchlist SET target_price = 0 WHERE symbol = %s", (sym,))
                except: pass

                # Rapor Kontrol
                gecen = (now - last_time).total_seconds() / 3600 if last_time else 999
                if gecen >= interval:
                    rep, prc = get_full_report(sym)
                    if rep:
                        time.sleep(2)
                        res = ask_gemini(sym, rep, last_sig)
                        db_islem_yap("UPDATE watchlist SET last_signal = 'OK', last_analysis = %s, last_report_time = NOW() WHERE symbol = %s", (res, sym))
                        bot.send_message(CHAT_ID, f"⏰ OTOMATİK: {sym}\n{res}")

            time.sleep(900) # 15 Dakika uyu
        except: time.sleep(900)

if __name__ == "__main__":
    # İki ayrı thread başlatıyoruz: Biri tarama, biri kalp masajı
    t1 = threading.Thread(target=scanner_loop)
    t1.start()
    
    t2 = threading.Thread(target=keep_alive_loop)
    t2.start()
    
    port = int(os.environ.get("PORT", 5000))
    server.run(host="0.0.0.0", port=port)
        
    
