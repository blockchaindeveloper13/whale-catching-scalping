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
import logging
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request
from datetime import datetime

# --- LOG AYARI (DETAYLI) ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(message)s', # Sadeleştirdim ki veriyi net gör
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("RawDataLogger")

# --- AYARLAR ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY')
BINANCE_SECRET = os.environ.get('BINANCE_SECRET_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
HEROKU_APP_URL = os.environ.get('HEROKU_APP_URL')

# --- MODEL SEÇİMİ (GEMINI 3 PRO) ---
genai.configure(api_key=GEMINI_API_KEY)
model_name = 'gemini-3-pro-preview' 
try:
    model = genai.GenerativeModel(model_name)
    logger.info(f"✅ AI MOTORU: {model_name}")
except:
    model = genai.GenerativeModel('gemini-1.5-pro')

bot = telebot.TeleBot(BOT_TOKEN)
server = Flask(__name__)

# --- BORSALAR ---
exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET,
    'options': {'defaultType': 'spot', 'adjustForTimeDifference': True},
    'enableRateLimit': True
})

exchange_vadeli = ccxt.binance({
    'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET,
    'options': {'defaultType': 'future', 'adjustForTimeDifference': True},
    'enableRateLimit': True
})

# --- HAFIZA ---
conversation_history = {}

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

try:
    db_islem("""
        CREATE TABLE IF NOT EXISTS price_alarms (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20),
            target_price REAL,
            direction VARCHAR(10),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
except: pass

# --- DETAYLI TEKNİK İSTİHBARAT ---
def get_financial_report(symbol):
    logger.info(f"==========================================")
    logger.info(f"🚀 ANALİZ BAŞLIYOR: {symbol}")
    if "/" not in symbol: symbol += "/USDT"
    
    report = f"--- 💼 {symbol} DETAYLI FİNANSAL RAPOR ---\n"
    
    # 1. Market Derinliği (HAM VERİ LOGLU)
    try:
        funding = exchange_vadeli.fetch_funding_rate(symbol)
        
        # --- İŞTE İSTEDİĞİN HAM VERİ ---
        logger.info(f"🦕 [HAM VERİ] VADELİ FONLAMA PAKETİ:\n{funding}") 
        # -------------------------------
        
        rate = funding['fundingRate'] * 100
        sentiment = "AŞIRI LONG (Tuzak)" if rate > 0.01 else "AŞIRI SHORT (Sıkışma)" if rate < -0.01 else "NÖTR"
        report += f"\n📊 MARKET DERİNLİĞİ: Fonlama %{rate:.4f} -> {sentiment}\n"
    except Exception as e: 
        logger.error(f"❌ Vadeli Veri Hatası: {e}")
        report += "\n📊 MARKET: Veri yok (Spot)\n"

    report += "-" * 30 + "\n"

    # 2. Çoklu Zaman Dilimi (HAM VERİ LOGLU)
    timeframes = ['15m', '1h', '4h', '1d']
    for tf in timeframes:
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=60)
            
            # --- İŞTE İSTEDİĞİN HAM VERİ (MUM DİZİLERİ) ---
            # Hepsini basarsak log kilitlenir, SON 3 MUMU olduğu gibi basıyorum
            # Format: [Zaman, Açılış, Yüksek, Düşük, Kapanış, Hacim]
            logger.info(f"🦕 [HAM VERİ] {tf} SON 3 MUM (Raw Candle Data):\n{bars[-3:]}")
            # ----------------------------------------------

            df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            
            # İndikatör Hesaplamaları
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi = 100 - (100 / (1 + gain/loss))
            
            ema50 = df['close'].ewm(span=50, adjust=False).mean()
            
            exp12 = df['close'].ewm(span=12, adjust=False).mean()
            exp26 = df['close'].ewm(span=26, adjust=False).mean()
            macd = exp12 - exp26
            signal = macd.ewm(span=9, adjust=False).mean()
            
            sma20 = df['close'].rolling(20).mean()
            std = df['close'].rolling(20).std()
            upper = sma20 + (std * 2)
            lower = sma20 - (std * 2)
            
            bandwidth = (upper.iloc[-1] - lower.iloc[-1]) / lower.iloc[-1]
            bb_durum = "SIKIŞMA" if bandwidth < 0.05 else "NORMAL"

            vol_completed = df['volume'].iloc[-2]
            vol_avg = df['volume'].iloc[-22:-2].mean()
            vol_ratio = vol_completed / vol_avg if vol_avg > 0 else 0
            vol_text = "GÜÇLÜ" if vol_ratio > 1.2 else "ZAYIF" if vol_ratio < 0.8 else "NORMAL"

            obv = (pd.Series(np.where(df['close'] > df['close'].shift(1), df['volume'], 
                           np.where(df['close'] < df['close'].shift(1), -df['volume'], 0))).cumsum())
            obv_dir = "POZİTİF" if obv.iloc[-1] > obv.iloc[-10] else "NEGATİF"

            report += f"🕒 {tf.upper()} | Fiyat: {df['close'].iloc[-1]}\n"
            report += f"   • RSI: {rsi.iloc[-1]:.1f} | MACD: {'AL' if macd.iloc[-1]>signal.iloc[-1] else 'SAT'}\n"
            report += f"   • Trend: {'BOĞA' if df['close'].iloc[-1] > ema50.iloc[-1] else 'AYI'} | BB: {bb_durum}\n"
            report += f"   • Hacim: {vol_text} (x{vol_ratio:.1f}) | OBV: {obv_dir}\n\n"
        except Exception as e:
            logger.error(f"❌ {tf} Analiz Hatası: {e}")
            pass
            
    logger.info(f"✅ Rapor Bitti.")
    return report

# --- YAPAY ZEKA ---
def ask_gemini_with_memory(chat_id, user_input, system_instruction=None):
    if chat_id not in conversation_history: conversation_history[chat_id] = []
    
    history = conversation_history[chat_id]
    history.append({"role": "user", "parts": [user_input]})
    if len(history) > 30: history = history[-30:]

    base_instruction = (
        "SENİN ROLÜN: Vedat Paşa'nın Kıdemli Risk Yöneticisi.\n"
        "KİMLİK: Duygusuz, analitik, koruyucu. Sadece 'Paşam' de.\n"
        "GÖREV: Kullanıcıyı tuzaklardan koru. Veri kötüyse 'ALMAYIN' de.\n"
        "Finansal terimler kullan."
    )
    
    full_prompt = f"{base_instruction}\n\nRAPOR:\n{system_instruction}" if system_instruction else base_instruction

    try:
        chat = model.start_chat(history=history)
        response = chat.send_message(full_prompt)
        text_response = response.text.replace("**", "")
        
        # AI CEVABINI DA LOGLUYORUZ
        logger.info(f"🤖 AI Cevabı (İlk 50 karakter): {text_response[:50]}...")
        
        history.append({"role": "model", "parts": [text_response]})
        conversation_history[chat_id] = history
        return text_response
    except Exception as e:
        logger.error(f"❌ AI Hatası: {e}")
        return f"⚠️ AI Hatası: {e}"

# --- MENÜ ---
def main_menu():
    m = InlineKeyboardMarkup(row_width=2)
    m.add(InlineKeyboardButton("📈 BTC", callback_data="analiz_BTC"), InlineKeyboardButton("💎 ETH", callback_data="analiz_ETH"))
    m.add(InlineKeyboardButton("🚀 AAVE", callback_data="analiz_AAVE"), InlineKeyboardButton("☀️ SOL", callback_data="analiz_SOL"))
    m.add(InlineKeyboardButton("⏰ Alarm Kur", callback_data="alarm_kur"))
    m.add(InlineKeyboardButton("🗑️ HAFIZA SİL", callback_data="hafiza_sil"))
    return m

@bot.message_handler(commands=['start'])
def welcome(m):
    bot.reply_to(m, "Sayın Vedat Paşam, Risk Masası hazır.", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    chat_id = call.message.chat.id
    if call.data == "hafiza_sil":
        conversation_history[chat_id] = []
        bot.answer_callback_query(call.id, "Temizlendi")
        bot.send_message(chat_id, "Geçmiş silindi Paşam.")
    elif call.data.startswith("analiz_"):
        coin = call.data.split("_")[1]
        bot.answer_callback_query(call.id, "Çekiliyor...")
        bot.send_message(chat_id, f"📊 {coin} ham verileri kontrol ediliyor Paşam...")
        rapor = get_financial_report(f"{coin}/USDT")
        cevap = ask_gemini_with_memory(chat_id, f"{coin} yorumla.", system_instruction=rapor)
        bot.send_message(chat_id, cevap)
    elif call.data == "alarm_kur":
        msg = bot.send_message(chat_id, "Hangi varlık ve fiyat?")
        bot.register_next_step_handler(msg, set_alarm)

def set_alarm(m):
    try:
        parts = m.text.upper().split()
        sym = parts[0] + "/USDT"
        tgt = float(parts[1])
        
        # HAM VERİ LOGU (ALARM İÇİN)
        ticker_data = exchange.fetch_ticker(sym)
        logger.info(f"🦕 [HAM VERİ] ALARM İÇİN ANLIK TICKER:\n{ticker_data}")
        
        cur = ticker_data['last']
        direc = 'ABOVE' if tgt > cur else 'BELOW'
        db_islem("INSERT INTO price_alarms (symbol, target_price, direction) VALUES (%s, %s, %s)", (sym, tgt, direc))
        bot.reply_to(m, f"✅ Alarm: {sym} -> {tgt}")
    except: bot.reply_to(m, "Format hatası.")

def alarm_patrol():
    logger.info("🔭 ALARM TİMİ GÖREVDE.")
    while True:
        try:
            alarms = db_islem("SELECT id, symbol, target_price, direction FROM price_alarms")
            if alarms:
                for a in alarms:
                    aid, sym, tgt, d = a
                    try:
                        p = exchange.fetch_ticker(sym)['last']
                        hit = (d == 'ABOVE' and p >= tgt) or (d == 'BELOW' and p <= tgt)
                        if hit:
                            logger.info(f"🚨 ALARM TETİKLENDİ! {sym} HAM FİYAT: {p}")
                            bot.send_message(CHAT_ID, f"🚨 HEDEF GELDİ PAŞAM!\n{sym}: {p}")
                            db_islem("DELETE FROM price_alarms WHERE id = %s", (aid,))
                    except: pass
            if HEROKU_APP_URL: requests.get(HEROKU_APP_URL)
            time.sleep(30)
        except: time.sleep(30)

@bot.message_handler(func=lambda m: True)
def chat_logic(m):
    if "ANALIZ" in m.text.upper():
        # Basit coin bulma
        parts = m.text.split()
        coin = parts[0] if len(parts[0]) > 2 else "BTC" # Basit mantık
        bot.reply_to(m, f"🔎 {coin} bakılıyor...")
        rapor = get_financial_report(f"{coin}/USDT")
        cevap = ask_gemini_with_memory(m.chat.id, m.text, system_instruction=rapor)
        bot.send_message(m.chat.id, cevap)
    elif not m.text.startswith("/"):
        cevap = ask_gemini_with_memory(m.chat.id, m.text)
        bot.reply_to(m, cevap)

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

