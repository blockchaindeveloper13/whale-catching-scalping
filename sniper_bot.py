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

# --- MODEL SEÇİMİ (KESİN OLARAK PRO - EN ZEKİSİ) ---
genai.configure(api_key=GEMINI_API_KEY)
model_name = 'gemini-3-pro-preview' # Analiz derinliği için şart
# --- YENİ NESİL KOD (İNTERNETLİ) ---
tools_list = [
    {"google_search_retrieval": {
        "dynamic_retrieval_config": {
            "mode": "dynamic",  # Gerekirse ara, gerekmezse arama
            "dynamic_threshold": 0.3
        }
    }}
]

try:
    # İşte sihirli değnek burada: tools parametresini ekliyoruz
    model = genai.GenerativeModel(model_name, tools=tools_list)
except:
    # Yedek modelde de tool desteği varsa ekleriz
    model = genai.GenerativeModel('gemini-3-pro-preview', tools=tools_list)
    

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

# --- UZUN SÜRELİ HAFIZA (RAM) ---
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

# Tablo Kurulumu
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

# --- DERİN TEKNİK ANALİZ (FİNANSÇI GÖZÜ) ---
def get_financial_report(symbol):
    if "/" not in symbol: symbol += "/USDT"
    
    report = f"--- 💼 {symbol} FİNANSAL DURUM RAPORU ---\n"
    
    # 1. Market Psikolojisi (Vadeli)
    try:
        funding = exchange_vadeli.fetch_funding_rate(symbol)
        rate = funding['fundingRate'] * 100
        sentiment = "AŞIRI LONG (Tuzak Riski)" if rate > 0.01 else "AŞIRI SHORT (Sıkışma Riski)" if rate < -0.01 else "NÖTR"
        report += f"\n📊 MARKET DERİNLİĞİ: Fonlama %{rate:.4f} -> {sentiment}\n"
    except: report += "\n📊 MARKET: Veri yok (Spot olabilir)\n"

    report += "-" * 30 + "\n"

    # 2. Çoklu Zaman Dilimi Analizi
    timeframes = ['15m', '1h', '4h', '1d']
    for tf in timeframes:
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=60)
            df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            
            # --- İNDİKATÖRLER ---
            # RSI (14 Standart)
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rsi = 100 - (100 / (1 + gain/loss))
            
            # EMA
            ema50 = df['close'].ewm(span=50, adjust=False).mean()
            
            # MACD
            exp12 = df['close'].ewm(span=12, adjust=False).mean()
            exp26 = df['close'].ewm(span=26, adjust=False).mean()
            macd = exp12 - exp26
            signal = macd.ewm(span=9, adjust=False).mean()
            
            # Bollinger
            sma20 = df['close'].rolling(20).mean()
            std = df['close'].rolling(20).std()
            upper = sma20 + (std * 2)
            lower = sma20 - (std * 2)
            bb_durum = "DARALMA (Patlama Yakın)" if (upper.iloc[-1]-lower.iloc[-1])/lower.iloc[-1] < 0.05 else "NORMAL"

            # HACİM (Bitmiş Mum Analizi)
            vol_completed = df['volume'].iloc[-2]
            vol_avg = df['volume'].iloc[-22:-2].mean()
            vol_ratio = vol_completed / vol_avg if vol_avg > 0 else 0
            vol_text = "HACİM DESTEKLİ" if vol_ratio > 1.2 else "HACİMSİZ (Güvensiz)" if vol_ratio < 0.8 else "NORMAL"

            # OBV Trend
            obv = (pd.Series(np.where(df['close'] > df['close'].shift(1), df['volume'], 
                           np.where(df['close'] < df['close'].shift(1), -df['volume'], 0))).cumsum())
            obv_dir = "POZİTİF" if obv.iloc[-1] > obv.iloc[-10] else "NEGATİF"

            report += f"🕒 {tf.upper()} | Fiyat: {df['close'].iloc[-1]}\n"
            report += f"   • RSI: {rsi.iloc[-1]:.1f} | MACD: {'AL' if macd.iloc[-1]>signal.iloc[-1] else 'SAT'}\n"
            report += f"   • Trend: {'BOĞA' if df['close'].iloc[-1] > ema50.iloc[-1] else 'AYI'} | BB: {bb_durum}\n"
            report += f"   • Hacim: {vol_text} (x{vol_ratio:.1f}) | OBV: {obv_dir}\n\n"
        except: pass
            
    return report

# --- YAPAY ZEKA BEYNİ (SOHBET GEÇMİŞİ YÖNETİMİ) ---
def ask_gemini_with_memory(chat_id, user_input, system_instruction=None):
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []
    
    history = conversation_history[chat_id]
    history.append({"role": "user", "parts": [user_input]})
    
    if len(history) > 30: history = history[-30:]

    # Sistem Talimatı (Persona)
    base_instruction = (
        "SENİN ROLÜN: Vedat Paşa'nın Kıdemli Baş Finans Danışmanısın.\n"
        "KİMLİK: Çok zeki, otoriter, risk yönetimi uzmanı, hafif iğneleyici ama saygılı birisin.\n"
        "HİTAP: Kullanıcıya sadece 'Paşam' diye hitap et.\n"
        "GÖREV: Kullanıcının duygusal kararlar almasını ENGELLE. Verilere bak. Yanlışsa 'YANLIŞ' de.\n"
        "Eğer kullanıcı 'Alayım mı' derse ve veriler kötüyse, onu sert bir dille uyar ve durdur.\n"
        "Askeri terimleri bırak, borsa/finans jargonunu (Likidite, Volatilite, Manipülasyon, Order Block) kullan.\n"
        "Geçmiş konuşmaları asla unutma, onlara referans ver."
    )
    
    if system_instruction:
        full_prompt = f"{base_instruction}\n\nEK BİLGİ / RAPOR:\n{system_instruction}"
    else:
        full_prompt = base_instruction

    try:
        chat = model.start_chat(history=history)
        response = chat.send_message(full_prompt)
        text_response = response.text.replace("**", "")
        
        history.append({"role": "model", "parts": [text_response]})
        conversation_history[chat_id] = history
        return text_response
    except Exception as e:
        return f"⚠️ Finansal Sistem Hatası: {e}"

# --- MENÜ ---
def main_menu():
    m = InlineKeyboardMarkup(row_width=2)
    m.add(InlineKeyboardButton("📈 BTC Analiz", callback_data="analiz_BTC"), InlineKeyboardButton("💎 ETH Analiz", callback_data="analiz_ETH"))
    m.add(InlineKeyboardButton("🚀 AAVE Analiz", callback_data="analiz_AAVE"), InlineKeyboardButton("☀️ SOL Analiz", callback_data="analiz_SOL"))
    m.add(InlineKeyboardButton("⏰ Fiyat Alarmı Kur", callback_data="alarm_kur"))
    m.add(InlineKeyboardButton("🗑️ HAFIZAYI SİL (RESET)", callback_data="hafiza_sil"))
    return m

@bot.message_handler(commands=['start'])
def welcome(m):
    bot.reply_to(m, "Sayın Vedat Paşam, Finans Masası hazır. Portföyünüzü yönetmeye geldim. Duygusallığa yer yok, sadece matematik.", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    chat_id = call.message.chat.id
    
    if call.data == "hafiza_sil":
        conversation_history[chat_id] = []
        bot.answer_callback_query(call.id, "✅ Hafıza Formatlandı!")
        bot.send_message(chat_id, "Geçmişi sildim Paşam. Temiz bir sayfa açtık. Şimdi stratejimiz ne?")

    elif call.data.startswith("analiz_"):
        coin = call.data.split("_")[1]
        bot.answer_callback_query(call.id, "Veriler Çekiliyor...")
        bot.send_message(chat_id, f"📊 {coin} dosyası masama geliyor Paşam. Bekleyiniz...")
        
        rapor = get_financial_report(f"{coin}/USDT")
        cevap = ask_gemini_with_memory(chat_id, f"Bu {coin} raporunu yorumla. Alım fırsatı mı yoksa tuzak mı? Beni yönlendir.", system_instruction=rapor)
        bot.send_message(chat_id, cevap)

    elif call.data == "alarm_kur":
        msg = bot.send_message(chat_id, "Hangi varlık ve hangi fiyat Paşam? (Örn: AAVE 175)")
        bot.register_next_step_handler(msg, set_alarm)

def set_alarm(m):
    try:
        parts = m.text.upper().split()
        sym = parts[0] + "/USDT"
        tgt = float(parts[1])
        cur = exchange.fetch_ticker(sym)['last']
        direc = 'ABOVE' if tgt > cur else 'BELOW'
        db_islem("INSERT INTO price_alarms (symbol, target_price, direction) VALUES (%s, %s, %s)", (sym, tgt, direc))
        bot.reply_to(m, f"✅ Not alındı Paşam. {sym} {tgt} seviyesine gelince masanıza bilgi düşecek.")
    except: bot.reply_to(m, "Format hatalı Paşam. Tekrar deneyin.")

def alarm_patrol():
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
                            bot.send_message(CHAT_ID, f"🚨 DİKKAT PAŞAM! FİYAT HEDEFTE!\n{sym}: {p}\nHedef: {tgt}")
                            db_islem("DELETE FROM price_alarms WHERE id = %s", (aid,))
                    except: pass
            if HEROKU_APP_URL: requests.get(HEROKU_APP_URL)
            time.sleep(30)
        except: time.sleep(30)

@bot.message_handler(func=lambda m: True)
def chat_logic(m):
    text = m.text.upper()
    chat_id = m.chat.id
    
    if "ANALIZ" in text:
        words = text.split()
        coin = next((w for w in words if len(w) > 2 and w not in ["ANALIZ", "YAP", "NEDIR"]), None)
        if coin:
            bot.reply_to(m, f"🔎 {coin} inceleniyor Paşam...")
            rapor = get_financial_report(f"{coin}/USDT")
            cevap = ask_gemini_with_memory(chat_id, f"Şu {coin} raporuna bak ve bana net bir strateji çiz.", system_instruction=rapor)
            bot.send_message(chat_id, cevap)
            return

    if not m.text.startswith("/"):
        cevap = ask_gemini_with_memory(chat_id, m.text)
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
            
