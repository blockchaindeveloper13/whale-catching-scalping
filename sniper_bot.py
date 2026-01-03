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
model = genai.GenerativeModel('gemini-1.5-flash') # Hızlı ve Ucuz Model

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

# Tabloyu Güncelle (Fiyat Hedefi ve Yaklaştı Bilgisi Eklendi)
try:
    conn = db_baglan()
    cur = conn.cursor()
    # Eğer tablo yoksa oluştur
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
    # Eski tabloda target_price yoksa ekle (Hata almamak için)
    try: cur.execute("ALTER TABLE watchlist ADD COLUMN target_price REAL DEFAULT 0")
    except: pass
    try: cur.execute("ALTER TABLE watchlist ADD COLUMN near_target BOOLEAN DEFAULT FALSE")
    except: pass
    
    conn.commit()
    conn.close()
except: pass

# --- ANALİZ FONKSİYONLARI ---
def get_technical_data(symbol):
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = ticker['last']
        report = f"Anlık Fiyat: {price}\n"
        
        # Sadece 1 Saatlik grafik verisi çek (Hız ve Tasarruf için)
        bars = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        
        # RSI Hesapla
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = rsi.iloc[-1]
        
        report += f"RSI (1S): {current_rsi:.1f}\n"
        return report, price
    except: return None, 0

def ask_gemini(symbol, data):
    try:
        prompt = (f"Askeri Rapor Ver. Coin: {symbol}.\nVeriler:\n{data}\n"
                  f"Sadece şu formatta yaz: [YÖN: AL/SAT/BEKLE] - [SEBEP: Kısa bir cümle]")
        return model.generate_content(prompt).text.replace("**", "")
    except: return "İstihbarat alınamadı."

# --- FLASK VE KOMUTLAR ---
@server.route('/' + BOT_TOKEN, methods=['POST'])
def getMessage():
    bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode('utf-8'))])
    return "!", 200

@server.route("/")
def webhook():
    bot.remove_webhook()
    bot.set_webhook(url=HEROKU_APP_URL + BOT_TOKEN)
    return "ONLINE", 200

@bot.message_handler(func=lambda m: True)
def handle_message(m):
    text = m.text.upper()
    
    # 1. HIZLI KOMUT: EĞER "ANALIZ" VARSA LİSTEYE BAKMADAN CEVAPLA
    # Örn: "SOL ANALIZ" veya "XYZ DURUM"
    if "ANALIZ" in text or "DURUM" in text or "NEDIR" in text:
        # Mesajın içindeki coini bulmaya çalış
        words = text.split()
        symbol = None
        for w in words:
            if "/" in w: symbol = w
            elif len(w) >= 3 and w not in ["ANALIZ", "DURUM", "NEDIR", "COIN"]:
                symbol = f"{w}/USDT"
                break
        
        if symbol:
            bot.reply_to(m, f"🔎 {symbol} için anlık istihbarat toplanıyor (Liste dışı)...")
            data, price = get_technical_data(symbol)
            if data:
                res = ask_gemini(symbol, data)
                bot.send_message(m.chat.id, f"RAPOR ({symbol}):\n{res}")
            else:
                bot.reply_to(m, "Borsada bu coin bulunamadı Paşam.")
            return

    # 2. AYARLAR (LİSTE İÇİN)
    # Örn: "AAVE 15 DK" veya "AAVE HEDEF 100"
    words = text.split()
    coin_name = None
    for w in words:
        if "/" not in w and len(w) > 2 and w not in ["SAAT", "DK", "DAKIKA", "HEDEF", "FIYAT"]:
             # Basit bir kontrol, veritabanına sorabiliriz veya varsayabiliriz
             coin_name = w
             break
    
    if coin_name:
        symbol = f"{coin_name}/USDT"
        
        # A) ZAMAN AYARI
        zaman = re.search(r'(\d+)\s*(SAAT|DK|DAKIKA)', text)
        if zaman:
            sure = int(zaman.group(1))
            birim = zaman.group(2)
            # Veritabanına her zaman SAAT cinsinden kaydediyoruz
            interval = sure / 60.0 if "DK" in birim or "DAK" in birim else float(sure)
            
            db_islem("INSERT INTO watchlist (symbol, interval_hours) VALUES (%s, %s) ON CONFLICT (symbol) DO UPDATE SET interval_hours = %s", (symbol, interval, interval))
            bot.reply_to(m, f"✅ {coin_name} rapor sıklığı ayarlandı: {sure} {birim}.")
            return

        # B) HEDEF FİYAT AYARI
        hedef = re.search(r'(HEDEF|FIYAT)\s*(\d+(\.\d+)?)', text)
        if hedef:
            fiyat = float(hedef.group(2))
            db_islem("INSERT INTO watchlist (symbol, target_price, near_target) VALUES (%s, %s, FALSE) ON CONFLICT (symbol) DO UPDATE SET target_price = %s, near_target = FALSE", (symbol, fiyat, fiyat))
            bot.reply_to(m, f"🎯 {coin_name} için HEDEF KİLİTLENDİ: {fiyat} USDT.\nYaklaşınca ve vurunca haber vereceğim.")
            return

# --- ARKA PLAN NÖBETÇİSİ ---
def watch_tower():
    print("Nöbetçi Kulesi Devrede.")
    last_ping = time.time()
    
    while True:
        try:
            # 1. KALP MASAJI (Her 20 dk - Bedava)
            if time.time() - last_ping > 1200:
                if HEROKU_APP_URL: requests.get(HEROKU_APP_URL)
                last_ping = time.time()

            # 2. TARAMA (Her 60 Saniye - Bedava Fiyat Kontrolü)
            rows = db_islem("SELECT symbol, interval_hours, last_report_time, target_price, near_target FROM watchlist")
            
            if rows:
                now = datetime.now()
                for r in rows:
                    sym, interval, last_time, target, near_flag = r
                    
                    # --- A. FİYAT ALARMI KONTROLÜ (BİNANCE - BEDAVA) ---
                    try:
                        ticker = exchange.fetch_ticker(sym)
                        price = ticker['last']
                        
                        if target and target > 0:
                            diff_percent = abs(price - target) / target * 100
                            
                            # Durum 1: Tam İsabet (%0.1 fark)
                            if diff_percent < 0.1:
                                bot.send_message(CHAT_ID, f"🚨 HEDEF VURULDU PAŞAM!\n{sym} Fiyatı: {price}\nHedef: {target}")
                                # Hedefi sıfırla ki tekrar tekrar çalmasın
                                db_islem("UPDATE watchlist SET target_price = 0 WHERE symbol = %s", (sym,))
                            
                            # Durum 2: Yaklaştı (%1 fark) ve daha önce haber vermediysek
                            elif diff_percent < 1.0 and not near_flag:
                                bot.send_message(CHAT_ID, f"⚠️ HEDEFE YAKLAŞTIK!\n{sym} Fiyat: {price} (Hedefe %1 kaldı)")
                                db_islem("UPDATE watchlist SET near_target = TRUE WHERE symbol = %s", (sym,))
                    except: pass

                    # --- B. RAPOR ZAMANI KONTROLÜ ---
                    # Sadece süre dolduysa Gemini'ye sor (Mühimmat Tasarrufu)
                    if interval:
                        gecen_saat = (now - last_time).total_seconds() / 3600 if last_time else 999
                        if gecen_saat >= interval:
                            # Sadece zamanı gelince yapay zekayı çağır
                            data, prc = get_technical_data(sym)
                            if data:
                                res = ask_gemini(sym, data)
                                db_islem("UPDATE watchlist SET last_report_time = NOW() WHERE symbol = %s", (sym,))
                                bot.send_message(CHAT_ID, f"⏰ OTOMATİK RAPOR ({sym}):\n{res}")
                                time.sleep(1) # Yüklenmemek için

            # 60 Saniye bekle (Bu döngü her dakika çalışır)
            time.sleep(60)
            
        except Exception as e:
            print(f"Hata: {e}")
            time.sleep(60)

if __name__ == "__main__":
    t = threading.Thread(target=watch_tower)
    t.start()
    server.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

