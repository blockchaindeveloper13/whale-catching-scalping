import ccxt
import time
import telebot
import os
import pandas as pd
import numpy as np
import google.generativeai as genai
import psycopg2
import threading
from flask import Flask, request
from datetime import datetime

# --- ORTAM DEĞİŞKENLERİ ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY')
BINANCE_SECRET = os.environ.get('BINANCE_SECRET_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
HEROKU_APP_URL = os.environ.get('HEROKU_APP_URL') # Örn: https://senin-app-adin.herokuapp.com/

# --- AYARLAR ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash')
bot = telebot.TeleBot(BOT_TOKEN)
server = Flask(__name__) # Web Sunucusu Başlat

exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET,
    'options': {'defaultType': 'spot', 'adjustForTimeDifference': True},
    'enableRateLimit': True
})

# --- VERİTABANI FONKSİYONLARI ---
def db_baglan():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def db_tablo_olustur():
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                symbol VARCHAR(20) PRIMARY KEY,
                last_signal VARCHAR(50) DEFAULT 'YOK',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("✅ DB Tablosu Hazır!")
    except Exception as e:
        print(f"❌ DB Hatası: {e}")

db_tablo_olustur()

def db_coin_ekle(symbol):
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("INSERT INTO watchlist (symbol) VALUES (%s) ON CONFLICT DO NOTHING", (symbol,))
        conn.commit()
        success = cur.rowcount > 0
        cur.close()
        conn.close()
        return success
    except: return False

def db_coin_cikar(symbol):
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("DELETE FROM watchlist WHERE symbol = %s", (symbol,))
        conn.commit()
        success = cur.rowcount > 0
        cur.close()
        conn.close()
        return success
    except: return False

def db_liste_getir():
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("SELECT symbol, last_signal FROM watchlist")
        rows = cur.fetchall()
        cur.close()
        conn.close()
        return rows
    except: return []

def db_sinyal_guncelle(symbol, yeni_sinyal):
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("UPDATE watchlist SET last_signal = %s WHERE symbol = %s", (yeni_sinyal, symbol))
        conn.commit()
        cur.close()
        conn.close()
    except: pass

# --- TEKNİK ANALİZ VE GEMINI ---
def calculate_technicals(df):
    if len(df) < 50: return None
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    vol_avg = df['volume'].rolling(window=20).mean()
    df['vol_change'] = df['volume'] / vol_avg
    return df.iloc[-1]

def get_full_report(symbol):
    report_text = ""
    current_price = 0
    try:
        # Sadece 1h ve 4h alalım, hız kazanalım
        for tf in ['1h', '4h']:
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=60)
            df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            tech = calculate_technicals(df)
            if tech is None: continue
            current_price = tech['close']
            report_text += (f"[{tf}]\nFiyat: {tech['close']}\nRSI: {tech['rsi']:.1f}\n"
                            f"Trend(EMA50): {'Boğa' if tech['close']>tech['ema50'] else 'Ayı'}\n"
                            f"Hacim: {tech['vol_change']:.1f}x\n\n")
        return report_text, current_price
    except: return None, 0

def ask_gemini(symbol, report, last_signal):
    try:
        prompt = (f"Sen Vedat Paşa'sın. Askeri nizamda konuş. Coin: {symbol}. "
                  f"Eski Sinyal: {last_signal}. Veriler:\n{report}\n"
                  f"Yorumla ve Karar Ver: (AL/SAT/BEKLE). Kısa olsun.")
        return model.generate_content(prompt).text
    except Exception as e: return f"Komutan meşgul: {e}"

# --- WEBHOOK ROTASI (POSTMAN GİBİ ÇALIŞIR) ---
@server.route('/' + BOT_TOKEN, methods=['POST'])
def getMessage():
    # Telegram'dan gelen güncellemeyi al
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "!", 200

@server.route("/")
def webhook():
    # Botu uyandırmak ve Webhook'u set etmek için
    bot.remove_webhook()
    bot.set_webhook(url=HEROKU_APP_URL + BOT_TOKEN)
    return "<h1>VEDAT PASA KOMUTA MERKEZI AKTIF!</h1>", 200

# --- BOT KOMUTLARI ---
@bot.message_handler(commands=['takip'])
def komut_takip(m):
    try:
        sym = m.text.split()[1].upper()
        if "/" not in sym: sym += "/USDT"
        if db_coin_ekle(sym): bot.reply_to(m, f"✅ {sym} eklendi.")
        else: bot.reply_to(m, "Zaten listede.")
    except: bot.reply_to(m, "Hata: /takip BTC")

@bot.message_handler(commands=['sil'])
def komut_sil(m):
    try:
        sym = m.text.split()[1].upper()
        if "/" not in sym: sym += "/USDT"
        db_coin_cikar(sym)
        bot.reply_to(m, f"🗑️ {sym} silindi.")
    except: pass

@bot.message_handler(commands=['liste'])
def komut_liste(m):
    rows = db_liste_getir()
    msg = "📋 LİSTE:\n" + "\n".join([f"{r[0]} ({r[1]})" for r in rows])
    bot.reply_to(m, msg)

@bot.message_handler(commands=['analiz'])
def komut_analiz(m):
    try:
        sym = m.text.split()[1].upper()
        if "/" not in sym: sym += "/USDT"
        bot.reply_to(m, "⏳ Analiz yapılıyor...")
        rep, prc = get_full_report(sym)
        if rep:
            res = ask_gemini(sym, rep, "Bilinmiyor")
            bot.send_message(CHAT_ID, f"🧠 {sym} ANALİZİ:\n{res}")
    except: pass

# --- ARKA PLAN TARAYICI (SCANNER) ---
def scanner_loop():
    while True:
        try:
            # 5 Dakikada bir tara
            rows = db_liste_getir()
            for r in rows:
                sym, last_sig = r
                rep, prc = get_full_report(sym)
                if rep:
                    # Sadece Gemini kotası için biraz bekle
                    time.sleep(2)
                    res = ask_gemini(sym, rep, last_sig)
                    
                    # Eğer önemli bir durum varsa yaz
                    if "AL" in res or "SAT" in res or "ACİL" in res:
                        # Eğer yorum rutin değilse
                        if "Rutin" not in res:
                            bot.send_message(CHAT_ID, f"🚨 OTOMATİK RAPOR: {sym}\n{res}")
                            # Basit sinyal güncellemesi (AL/SAT varsa)
                            new_sig = "AL" if "AL" in res else "SAT" if "SAT" in res else last_sig
                            db_sinyal_guncelle(sym, new_sig)
            
            print("💤 Devriye bitti, mola...")
            time.sleep(300) 
        except Exception as e:
            print(f"Scanner Hatası: {e}")
            time.sleep(60)

# --- ÇALIŞTIRMA ---
if __name__ == "__main__":
    # 1. Scanner'ı ayrı bir iş parçacığında (Thread) başlat
    t = threading.Thread(target=scanner_loop)
    t.start()
    
    # 2. Flask Web Sunucusunu Başlat (Heroku PORT'unu dinle)
    port = int(os.environ.get("PORT", 5000))
    server.run(host="0.0.0.0", port=port)
        
