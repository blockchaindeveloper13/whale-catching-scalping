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
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
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

# --- MODEL SEÇİMİ (PRO ÖNCELİKLİ) ---
genai.configure(api_key=GEMINI_API_KEY)
aday_modeller = ['gemini-1.5-pro', 'gemini-2.5-flash'] # Önce Zeki, Sonra Hızlı
model = None
for aday in aday_modeller:
    try:
        m = genai.GenerativeModel(aday)
        m.generate_content("T")
        model = m
        print(f"✅ AKTİF BEYİN: {aday}")
        break
    except: continue
if not model: model = genai.GenerativeModel('gemini-1.5-flash')

bot = telebot.TeleBot(BOT_TOKEN)
server = Flask(__name__)

# --- KRİTİK DEĞİŞİKLİK: ÇİFT HATLI BAĞLANTI ---

# 1. SPOT HATTI (Fiyat ve İndikatörler için)
exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET,
    'options': {'defaultType': 'spot', 'adjustForTimeDifference': True},
    'enableRateLimit': True
})

# 2. VADELİ HATTI (Sadece Fonlama/Market Yapısı için)
exchange_vadeli = ccxt.binance({
    'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET,
    'options': {'defaultType': 'future', 'adjustForTimeDifference': True}, # <--- Future yaptık
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

# Tablo Kurulum
try:
    conn = db_baglan()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_alarms (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20),
            target_price REAL,
            direction VARCHAR(10),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
except: pass

# --- İNDİKATÖRLER ---
def get_comprehensive_analysis(symbol):
    if "/" not in symbol: symbol += "/USDT"
    
    full_report = f"--- 🦅 {symbol} GENELKURMAY RAPORU 🦅 ---\n"
    
    # --- 1. MARKET YAPISI (VADELİ HATTINDAN ÇEKİLİYOR) ---
    try:
        # Vadeli borsasından soruyoruz
        funding = exchange_vadeli.fetch_funding_rate(symbol)
        funding_rate = funding['fundingRate'] * 100
        
        # Yorumlama
        ls_durum = ""
        if funding_rate > 0.01: ls_durum = "LONGÇULAR ÇOK (Tuzak İhtimali)"
        elif funding_rate < -0.01: ls_durum = "SHORTÇULAR ÇOK (Sıkışma İhtimali)"
        else: ls_durum = "DENGELİ"
        
        full_report += f"\n📊 MARKET YAPISI (Vadeli):\nFonlama: %{funding_rate:.4f} -> {ls_durum}\n"
    except Exception as e:
        full_report += f"\n📊 MARKET YAPISI: Veri Yok ({e})\n"

    full_report += "-" * 30 + "\n"

    # --- 2. TEKNİK ANALİZ (SPOT HATTINDAN) ---
    timeframes = ['15m', '1h', '4h', '1d']
    
    for tf in timeframes:
        try:
            # 50 mum çekiyoruz
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=50)
            df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            
            # --- İNDİKATÖRLER ---
            
            # RSI (12)
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=12).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=12).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            
            # EMA (12)
            ema12 = df['close'].ewm(span=12, adjust=False).mean()
            
            # MACD
            exp12 = df['close'].ewm(span=12, adjust=False).mean()
            exp26 = df['close'].ewm(span=26, adjust=False).mean()
            macd = exp12 - exp26
            signal = macd.ewm(span=9, adjust=False).mean()
            
            # Bollinger (12)
            sma12 = df['close'].rolling(window=12).mean()
            std = df['close'].rolling(window=12).std()
            upper = sma12 + (std * 2)
            lower = sma12 - (std * 2)
            
            # SAR (Basit Trend)
            sar_trend = "AL (Fiyat Üstte)" if df['close'].iloc[-1] > df['close'].iloc[-2] else "SAT (Fiyat Altta)"
            
            # --- DÜZELTİLMİŞ HACİM ANALİZİ ---
            # Son mum (canlı) değil, BİTMİŞ MUM (-2) baz alınır
            vol_completed = df['volume'].iloc[-2]
            # Ortalamayı geçmiş 20 bitmiş mumdan al
            vol_avg = df['volume'].iloc[-22:-2].mean()
            
            vol_change = 0
            if vol_avg > 0:
                vol_change = ((vol_completed - vol_avg) / vol_avg) * 100
            
            # OBV
            df['obv'] = (pd.Series(np.where(df['close'] > df['close'].shift(1), df['volume'], 
                           np.where(df['close'] < df['close'].shift(1), -df['volume'], 0))).cumsum())
            obv_yon = "YUKARI" if df['obv'].iloc[-1] > df['obv'].iloc[-2] else "AŞAĞI"

            price = df['close'].iloc[-1]
            
            full_report += f"🕒 CEPHE: {tf.upper()}\n"
            full_report += f"   • Fiyat: {price}\n"
            full_report += f"   • RSI(12): {rsi.iloc[-1]:.1f}\n"
            full_report += f"   • MACD: {'AL' if macd.iloc[-1] > signal.iloc[-1] else 'SAT'}\n"
            full_report += f"   • SAR: {sar_trend}\n"
            full_report += f"   • Hacim (Biten Mum): %{vol_change:.1f} ({'Yüksek' if vol_change > 0 else 'Düşük'})\n"
            full_report += f"   • OBV: {obv_yon}\n\n"

        except Exception as e:
            full_report += f"🕒 {tf}: Veri Hatası\n"
            
    return full_report

# --- MENU VE KOMUTLAR ---
def main_menu():
    markup = InlineKeyboardMarkup()
    markup.row_width = 2
    markup.add(InlineKeyboardButton("🔍 BTC", callback_data="analiz_BTC"),
               InlineKeyboardButton("🔍 ETH", callback_data="analiz_ETH"))
    markup.add(InlineKeyboardButton("🔍 AAVE", callback_data="analiz_AAVE"),
               InlineKeyboardButton("🔍 SOL", callback_data="analiz_SOL"))
    markup.add(InlineKeyboardButton("⏰ ALARM KUR", callback_data="alarm_kur"))
    return markup

@bot.message_handler(commands=['start', 'menu'])
def send_welcome(message):
    bot.reply_to(message, "🫡 Komutanım! Karargah Hazır.", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    if call.data.startswith("analiz_"):
        coin = call.data.split("_")[1]
        bot.answer_callback_query(call.id, "İstihbarat Toplanıyor...")
        bot.send_message(call.message.chat.id, f"📡 {coin} analiz ediliyor... (PRO Model)")
        
        rapor = get_comprehensive_analysis(f"{coin}/USDT")
        
        try:
            prompt = (f"GÖREV: Genelkurmay Arzı. Coin: {coin}.\n"
                      f"VERİLER:\n{rapor}\n"
                      f"EMİR: 4 zaman dilimini, Vadeli verisini ve Hacmi birleştir.\n"
                      f"Net Askeri Karar Ver (SALDIR / BEKLE / GERİ ÇEKİL).")
            res = model.generate_content(prompt).text.replace("**", "")
            bot.send_message(call.message.chat.id, res)
        except:
            bot.send_message(call.message.chat.id, f"⚠️ AI Hatası. Manuel Rapor:\n{rapor}")

    elif call.data == "alarm_kur":
        msg = bot.send_message(call.message.chat.id, "Komutanım, hangi Coin ve Fiyat? (Örn: AAVE 165)")
        bot.register_next_step_handler(msg, set_alarm)

def set_alarm(message):
    try:
        parts = message.text.upper().split()
        symbol = parts[0] + "/USDT"
        target = float(parts[1])
        
        current = exchange.fetch_ticker(symbol)['last']
        direction = 'ABOVE' if target > current else 'BELOW'
        
        db_islem("INSERT INTO price_alarms (symbol, target_price, direction) VALUES (%s, %s, %s)", (symbol, target, direction))
        bot.reply_to(message, f"✅ Alarm Kuruldu: {symbol} -> {target}")
    except: bot.reply_to(message, "Hata. Format: AAVE 165")

# --- ALARM TİMİ ---
def alarm_patrol():
    while True:
        try:
            alarms = db_islem("SELECT id, symbol, target_price, direction FROM price_alarms")
            if alarms:
                for a in alarms:
                    aid, sym, tgt, direct = a
                    try:
                        price = exchange.fetch_ticker(sym)['last']
                        hit = (direct == 'ABOVE' and price >= tgt) or (direct == 'BELOW' and price <= tgt)
                        if hit:
                            bot.send_message(CHAT_ID, f"🚨 HEDEF VURULDU PAŞAM!\n{sym}: {price}\nHedef: {tgt}")
                            db_islem("DELETE FROM price_alarms WHERE id = %s", (aid,))
                    except: pass
            if HEROKU_APP_URL: requests.get(HEROKU_APP_URL)
            time.sleep(30)
        except: time.sleep(30)

@server.route('/' + BOT_TOKEN, methods=['POST'])
def getMessage():
    bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode('utf-8'))])
    return "!", 200

@server.route("/")
def webhook():
    bot.remove_webhook()
    bot.set_webhook(url=HEROKU_APP_URL + BOT_TOKEN)
    return "OK", 200

if __name__ == "__main__":
    threading.Thread(target=alarm_patrol).start()
    server.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
