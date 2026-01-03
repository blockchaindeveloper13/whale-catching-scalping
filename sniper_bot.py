import ccxt
import time
import telebot
import os
import pandas as pd
import google.generativeai as genai
import psycopg2
import threading
import re
import requests
from flask import Flask, request
from datetime import datetime

# --- AYARLAR ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY')
BINANCE_SECRET = os.environ.get('BINANCE_SECRET_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
HEROKU_APP_URL = os.environ.get('HEROKU_APP_URL')

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
    except: return None

# Tablo Kurulumu
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
    conn.close()
except: pass

# --- ANALİZ ---
def get_technical_data(symbol):
    try:
        # Sembolü zorla düzelt (AAVE -> AAVE/USDT)
        if "/" not in symbol: symbol += "/USDT"
        
        ticker = exchange.fetch_ticker(symbol)
        price = ticker['last']
        report = f"Anlık Fiyat: {price}\n"
        
        bars = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        report += f"RSI (1S): {rsi.iloc[-1]:.1f}\n"
        return report, price
    except Exception as e: 
        print(f"Veri Hatası ({symbol}): {e}")
        return None, 0

def ask_gemini(symbol, data):
    try:
        prompt = (f"Askeri Rapor. Coin: {symbol}. Veri:\n{data}\n"
                  f"Kısa Özet: [AL/SAT/BEKLE] - [Gerekçe]")
        return model.generate_content(prompt).text.replace("**", "")
    except: return "İstihbarat sunucusu cevap vermiyor."

# --- FLASK ---
@server.route('/' + BOT_TOKEN, methods=['POST'])
def getMessage():
    bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode('utf-8'))])
    return "!", 200

@server.route("/")
def webhook():
    bot.remove_webhook()
    bot.set_webhook(url=HEROKU_APP_URL + BOT_TOKEN)
    return "ONLINE", 200

# --- MESAJ YÖNETİMİ (BEYİN) ---
@bot.message_handler(func=lambda m: True)
def handle_message(m):
    text = m.text.upper() # Her şeyi büyük harfe çevir
    
    # 1. COIN ADINI BUL
    # Basit mantık: İçinde "/" geçen veya kelime listesinde olan (Basitleştirildi)
    words = text.split()
    found_coin = None
    
    # Yaygın coinleri manuel tanıtalım ki garanti olsun
    COMMON_COINS = ["BTC", "ETH", "SOL", "AAVE", "AVAX", "XRP", "LTC", "LINK", "DOGE", "SHIB", "PEPE", "ARB", "OP", "SUI"]
    
    for w in words:
        clean_w = w.strip(".,!?")
        if clean_w in COMMON_COINS or (len(clean_w) > 2 and clean_w.isalpha() and clean_w not in ["HER", "DAKIKA", "SAAT", "ANALIZ", "DOLAR", "OLUNCA", "HABER"]):
            found_coin = clean_w
            break
            
    if found_coin:
        symbol = f"{found_coin}/USDT"

        # A) ZAMAN AYARI ("Her dakika", "15 dk", "Her saat")
        # Regex: "HER" kelimesini veya Sayıları yakalar
        zaman_match = re.search(r'(HER|\d+)\s*(SAAT|DK|DAKIKA|DAK)', text)
        
        if zaman_match:
            miktar_str = zaman_match.group(1)
            birim = zaman_match.group(2)
            
            # "HER" dediyse 1 demektir
            sure = 1 if miktar_str == "HER" else int(miktar_str)
            
            # Interval hesapla (Veritabanı SAAT tutar)
            # Eğer dakika ise 60'a böl.
            interval = sure / 60.0 if "DK" in birim or "DAK" in birim else float(sure)
            
            db_islem("INSERT INTO watchlist (symbol, interval_hours) VALUES (%s, %s) ON CONFLICT (symbol) DO UPDATE SET interval_hours = %s", (symbol, interval, interval))
            
            # Cevap ver
            msg_sure = f"{sure} DAKİKA" if interval < 1 else f"{sure} SAAT"
            bot.reply_to(m, f"✅ {found_coin} nöbeti güncellendi: {msg_sure} aralıkla rapor vereceğim.")
            
            # "Her dakika" gibi agresif bir şeyse hemen bir analiz patlat ki çalıştığını görsün
            if interval <= 0.05: # 3 dakikadan azsa
                bot.send_message(m.chat.id, f"🚀 Hızlı mod testi başlatılıyor...")
                data, prc = get_technical_data(symbol)
                if data:
                    res = ask_gemini(symbol, data)
                    bot.send_message(m.chat.id, res)
            return

        # B) HEDEF FİYAT AYARI ("170 Dolar", "170 olursa", "Hedef 170")
        # Regex: Sayıyı yakala, yanında DOLAR, OLUNCA, OLURSA, HEDEF var mı bak
        hedef_match = re.search(r'(\d+(\.\d+)?)\s*(DOLAR|USDT|OLUNCA|OLURSA|HEDEF|FIYAT)', text)
        # Veya tersten: "HEDEF 170"
        hedef_match_2 = re.search(r'(HEDEF|FIYAT)\s*(\d+(\.\d+)?)', text)
        
        final_hedef = None
        if hedef_match: final_hedef = float(hedef_match.group(1))
        elif hedef_match_2: final_hedef = float(hedef_match_2.group(2))
        
        if final_hedef:
            db_islem("INSERT INTO watchlist (symbol, target_price, near_target) VALUES (%s, %s, FALSE) ON CONFLICT (symbol) DO UPDATE SET target_price = %s, near_target = FALSE", (symbol, final_hedef, final_hedef))
            bot.reply_to(m, f"🎯 {found_coin} için HEDEF KİLİTLENDİ: {final_hedef} USDT.\nOraya gelince haber vereceğim Paşam.")
            return

        # C) MANUEL SORGULAMA ("Analiz", "Durum", "Nedir")
        # Yukarıdakiler yoksa ve analiz istiyorsa
        if "ANALIZ" in text or "DURUM" in text or "NEDIR" in text:
            bot.reply_to(m, f"🔎 {found_coin} inceleniyor...")
            data, prc = get_technical_data(symbol)
            if data:
                res = ask_gemini(symbol, data)
                bot.send_message(m.chat.id, res)
            else:
                bot.reply_to(m, "⚠️ İstihbarat alınamadı. Coin ismini doğru yazdığından emin ol Paşam.")
            return

    # Coin bulunamadıysa ve komut değilse normal sohbet
    if not m.text.startswith("/"):
        try:
            res = model.generate_content(f"Sen askersin. Kullanıcı: {m.text}. Kısa cevap ver.").text
            bot.reply_to(m, res.replace("**", ""))
        except: pass

# --- NÖBETÇİ KULESİ ---
def watch_tower():
    print("Nöbetçi Kulesi Devrede.")
    last_ping = time.time()
    
    while True:
        try:
            # PING (Her 20 dk)
            if time.time() - last_ping > 1200:
                if HEROKU_APP_URL: requests.get(HEROKU_APP_URL)
                last_ping = time.time()

            # TARAMA (Her 60 Saniye)
            rows = db_islem("SELECT symbol, interval_hours, last_report_time, target_price, near_target FROM watchlist")
            if rows:
                now = datetime.now()
                for r in rows:
                    sym, interval, last_time, target, near_flag = r
                    
                    # 1. FİYAT KONTROL
                    try:
                        ticker = exchange.fetch_ticker(sym)
                        price = ticker['last']
                        
                        if target and target > 0:
                            diff = abs(price - target) / target * 100
                            if diff < 0.2: # %0.2 vurdu say
                                bot.send_message(CHAT_ID, f"🚨 HEDEF VURULDU PAŞAM!\n{sym}: {price}\nHedef: {target}")
                                db_islem("UPDATE watchlist SET target_price = 0 WHERE symbol = %s", (sym,))
                            elif diff < 1.0 and not near_flag:
                                bot.send_message(CHAT_ID, f"⚠️ {sym} hedefe yaklaştı ({price})!")
                                db_islem("UPDATE watchlist SET near_target = TRUE WHERE symbol = %s", (sym,))
                    except: pass
                    
                    # 2. ZAMANLI RAPOR
                    if interval:
                        gecen = (now - last_time).total_seconds() / 3600 if last_time else 999
                        if gecen >= interval:
                            data, prc = get_technical_data(sym)
                            if data:
                                res = ask_gemini(sym, data)
                                db_islem("UPDATE watchlist SET last_report_time = NOW() WHERE symbol = %s", (sym,))
                                bot.send_message(CHAT_ID, f"⏰ {sym} RAPORU:\n{res}")
                                time.sleep(1)

            time.sleep(60)
        except: time.sleep(60)

if __name__ == "__main__":
    t = threading.Thread(target=watch_tower)
    t.start()
    server.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
        
