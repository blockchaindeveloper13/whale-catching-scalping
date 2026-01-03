import ccxt
import time
import telebot
import os
import pandas as pd
import numpy as np  # <--- İŞTE BU! MERMİYİ EN BAŞA KOYDUK PAŞAM
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

# --- MODEL AYARI (SENİN EMRİNLE 2.5 FLASH) ---
genai.configure(api_key=GEMINI_API_KEY)
try:
    # Eğer Google 2.5 ismini kabul etmezse sistem çökmesin diye koruma
    model = genai.GenerativeModel('gemini-2.5-flash')
except:
    print("2.5 Bulunamadı, 1.5 deneniyor...")
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
        print(f"DB Hatası: {e}")
        return None

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

# --- TAM TEŞEKKÜLLÜ ANALİZ (HACİM + OBV + TEKNİK) ---
def get_technical_data(symbol):
    try:
        if "/" not in symbol: symbol += "/USDT"
        
        # Anlık Fiyat
        ticker = exchange.fetch_ticker(symbol)
        price = ticker['last']
        
        # 100 Mum çek (Hacim ve İndikatörler için)
        bars = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=100)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        
        # --- 1. HACİM ANALİZİ ---
        last_volume = df['volume'].iloc[-1]
        avg_volume = df['volume'].rolling(window=20).mean().iloc[-1]
        vol_change = ((last_volume - avg_volume) / avg_volume) * 100
        
        # OBV Hesapla (NumPy artık başta olduğu için burası çalışacak!)
        df['obv'] = (pd.Series(np.where(df['close'] > df['close'].shift(1), df['volume'], 
                       np.where(df['close'] < df['close'].shift(1), -df['volume'], 0))).cumsum())
        
        obv_trend = "POZİTİF (Para Girişi)" if df['obv'].iloc[-1] > df['obv'].iloc[-5] else "NEGATİF (Para Çıkışı)"
        
        hacim_durumu = ""
        if vol_change > 50: hacim_durumu = "🔥 PATLAMA VAR (Çok Yüksek)"
        elif vol_change > 0: hacim_durumu = "GÜÇLÜ (Ortalama Üstü)"
        else: hacim_durumu = "ZAYIF (Hacimsiz)"

        # --- 2. TEKNİK İNDİKATÖRLER ---
        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        # MACD
        exp1 = df['close'].ewm(span=12, adjust=False).mean()
        exp2 = df['close'].ewm(span=26, adjust=False).mean()
        macd = exp1 - exp2
        signal = macd.ewm(span=9, adjust=False).mean()
        
        # Bollinger
        sma20 = df['close'].rolling(window=20).mean()
        std = df['close'].rolling(window=20).std()
        upper_bb = sma20 + (std * 2)
        lower_bb = sma20 - (std * 2)
        
        # EMA Trend
        ema50 = df['close'].ewm(span=50, adjust=False).mean()

        # Trend Yorumu
        trend = "YÜKSELİŞ (Boğa)" if price > ema50.iloc[-1] else "DÜŞÜŞ (Ayı)"
        
        report = (f"ANLIK FİYAT: {price}\n"
                  f"--- HACİM İSTİHBARATI ---\n"
                  f"1. HACİM: {hacim_durumu} (Değişim: %{vol_change:.1f})\n"
                  f"2. PARA AKIŞI (OBV): {obv_trend}\n"
                  f"--- TEKNİK DURUM ---\n"
                  f"3. TREND (EMA50): {trend}\n"
                  f"4. RSI (1S): {rsi.iloc[-1]:.1f}\n"
                  f"5. MACD: {'AL' if macd.iloc[-1] > signal.iloc[-1] else 'SAT'} Sinyali\n"
                  f"6. BANTLAR: {lower_bb.iloc[-1]:.2f} - {upper_bb.iloc[-1]:.2f}")
        
        return report, price
    except Exception as e:
        print(f"HATA OLUŞTU ({symbol}): {e}") # Hatayı konsola yaz
        return None, 0

def ask_gemini(symbol, data):
    try:
        prompt = (f"GÖREV: Kripto Analizi. Coin: {symbol}.\n"
                  f"VERİLER:\n{data}\n"
                  f"EMİR: Özellikle HACİM verisine bak. Hacim destekliyor mu yoksa balon mu? Karar (AL/SAT/BEKLE) ver.")
        return model.generate_content(prompt).text.replace("**", "")
    except Exception as e: return f"⚠️ Gemini Hatası: {e}"

# --- FLASK VE MESAJLAŞMA ---
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
    words = text.split()
    found_coin = None
    COMMON = ["BTC", "ETH", "SOL", "AAVE", "AVAX", "XRP", "LTC", "LINK", "DOGE", "SHIB"]
    
    for w in words:
        clean = w.strip(".,!?")
        if clean in COMMON or (len(clean) > 2 and clean.isalpha() and clean not in ["HER", "DAKIKA", "SAAT", "ANALIZ"]):
            found_coin = clean
            break
            
    if found_coin:
        symbol = f"{found_coin}/USDT"

        # A) ZAMAN AYARI
        zaman_match = re.search(r'(HER|\d+)\s*(SAAT|DK|DAKIKA|DAK)', text)
        if zaman_match:
            miktar = zaman_match.group(1)
            birim = zaman_match.group(2)
            sure = 1 if miktar == "HER" else int(miktar)
            interval = sure / 60.0 if "DK" in birim or "DAK" in birim else float(sure)
            
            db_islem("INSERT INTO watchlist (symbol, interval_hours) VALUES (%s, %s) ON CONFLICT (symbol) DO UPDATE SET interval_hours = %s", (symbol, interval, interval))
            bot.reply_to(m, f"✅ {found_coin} nöbeti başladı. Hacim destekli analiz yapacağım.")
            
            # Hemen test et
            if interval <= 0.05:
                bot.send_message(m.chat.id, "🚀 Hızlı analiz başlatılıyor...")
                data, prc = get_technical_data(symbol)
                if data: 
                    bot.send_message(m.chat.id, ask_gemini(symbol, data))
                else:
                    bot.send_message(m.chat.id, "⚠️ Analiz yapılamadı. Veri hatası.")
            return

        # B) MANUEL ANALİZ
        if "ANALIZ" in text:
            bot.reply_to(m, f"🔎 {found_coin} inceleniyor...")
            data, prc = get_technical_data(symbol)
            if data: 
                bot.send_message(m.chat.id, ask_gemini(symbol, data))
            else:
                bot.reply_to(m, "⚠️ Veri çekilemedi. Bir aksilik var Paşam.")
            return

    if not m.text.startswith("/"):
        try:
            res = model.generate_content(f"Sen askersin. Mesaj: {m.text}. Kısa cevap.").text
            bot.reply_to(m, res.replace("**", ""))
        except: pass

def watch_tower():
    print("Nöbetçi Kulesi Devrede.")
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
                    
                    if interval:
                        gecen = (now - last_time).total_seconds() / 3600 if last_time else 999
                        if gecen >= interval:
                            data, prc = get_technical_data(sym)
                            if data:
                                res = ask_gemini(sym, data)
                                db_islem("UPDATE watchlist SET last_report_time = NOW() WHERE symbol = %s", (sym,))
                                bot.send_message(CHAT_ID, f"⏰ {sym} DETAYLI RAPOR:\n{res}")
                                time.sleep(2)
            time.sleep(20)
        except Exception as e:
            print(f"Hata: {e}")
            time.sleep(20)

if __name__ == "__main__":
    t = threading.Thread(target=watch_tower)
    t.start()
    server.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
        
