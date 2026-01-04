import ccxt
import time
import telebot
import os
import pandas as pd
import numpy as np
# --- YENİ NESİL KÜTÜPHANE ---
from google import genai
from google.genai import types
# ---------------------------
import psycopg2
import threading
import requests
import sys
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request
from datetime import datetime

# --- LOG AYARI (HEROKU İÇİN) ---
sys.stdout.reconfigure(encoding='utf-8')

# --- ORTAM DEĞİŞKENLERİ ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY')
BINANCE_SECRET = os.environ.get('BINANCE_SECRET_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
HEROKU_APP_URL = os.environ.get('HEROKU_APP_URL')

# --- YENİ NESİL GEMINI CLIENT KURULUMU ---
try:
    client = genai.Client(api_key=GEMINI_API_KEY)
    print("✅ GEMINI 2.5: Client, Search ve Düşünme Modülü Hazır!")
except Exception as e:
    print(f"⚠️ Client Hatası: {e}")

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

# --- TEKNİK ANALİZ FONKSİYONU ---
def get_financial_report(symbol):
    if "/" not in symbol: symbol += "/USDT"
    report = f"--- 💼 {symbol} TEKNİK RAPOR ---\n"
    
    # Vadeli Fonlama
    try:
        funding = exchange_vadeli.fetch_funding_rate(symbol)
        rate = funding['fundingRate'] * 100
        sentiment = "AŞIRI LONG" if rate > 0.01 else "AŞIRI SHORT" if rate < -0.01 else "NÖTR"
        report += f"📊 Fonlama: %{rate:.4f} ({sentiment})\n"
    except: pass

    # Fiyat ve İndikatörler (4 Saatlik)
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=60)
        df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
        
        # RSI
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + gain/loss))
        
        # Bollinger
        sma20 = df['close'].rolling(20).mean()
        std = df['close'].rolling(20).std()
        upper = sma20 + (std * 2)
        lower = sma20 - (std * 2)
        bb_durum = "DARALMA (Patlama Yakın)" if (upper.iloc[-1]-lower.iloc[-1])/lower.iloc[-1] < 0.05 else "NORMAL"

        report += f"💰 Fiyat: {df['close'].iloc[-1]}\n"
        report += f"📈 RSI (4H): {rsi.iloc[-1]:.1f}\n"
        report += f"📉 Bollinger: {bb_durum}\n"
    except: report += "Veri çekilemedi.\n"
            
    return report

# --- YAPAY ZEKA BEYNİ (ÇİFT MESAJLI - TELEPATİK VERSİYON) ---
# DİKKAT: Bu fonksiyon aşağıda çağırılmadan ÖNCE tanımlanmalıdır.
def ask_gemini_with_memory(chat_id, user_input, system_instruction=None):
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []
    
    history = conversation_history[chat_id]
    bugun = datetime.now().strftime("%d %B %Y (%A)")
    
    # Prompt
    base_prompt = (
        f"BUGÜNÜN TARİHİ: {bugun}. \n"
        "Sen Vedat Paşa'nın Kıdemli Finans Danışmanısın. Zeki, otoriter ve risk yönetimini bilen birisin.\n"
        "GÖREVLERİN:\n"
        "1. Kullanıcı GÜNCEL VERİ sorarsa 'Google Search' kullan.\n"
        "2. Cevabı vermeden önce DERİNLEMESİNE DÜŞÜN. Riskleri analiz et.\n"
        "3. Kullanıcıya 'Paşam' diye hitap et.\n"
    )
    
    if system_instruction:
        base_prompt += f"\n\nTEKNİK RAPOR:\n{system_instruction}"

    history.append(f"Kullanıcı: {user_input}")
    if len(history) > 10: history = history[-10:]
    
    full_context = base_prompt + "\n\nGEÇMİŞ SOHBET:\n" + "\n".join(history)

    try:
        # --- GEMINI 2.5 PRO ÇAĞRISI ---
        response = client.models.generate_content(
            model='gemini-3-pro-preview',
            contents=full_context,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                response_modalities=["TEXT"],
                thinking_config=types.ThinkingConfig(
                    include_thoughts=True # Düşünceyi açıyoruz
                )
            )
        )
        
        final_answer = ""
        thought_log = ""

        # --- AYIKLAMA ---
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'thought') and part.thought is True:
                    thought_log += part.text
                else:
                    final_answer += part.text

                # --- TEMİZLİK (YILDIZLARI SİL) ---
        if thought_log: thought_log = thought_log.replace("**", "").replace("##", "")
        if final_answer: final_answer = final_answer.replace("**", "").replace("##", "")


        # --- 1. MESAJ: DÜŞÜNCE (AYRI GÖNDERİLİR) ---
        if thought_log:
            try:
                log_mesaji = f"🧠 **[ZİHİN TARAMASI - GİZLİ]**\n\n{thought_log}"
                bot.send_message(chat_id, log_mesaji, parse_mode="Markdown")
            except:
                # Markdown hatası verirse düz gönder
                bot.send_message(chat_id, f"🧠 [ZİHİN]:\n{thought_log}")

        # --- 2. CEVAP HAZIRLIĞI ---
        if not final_answer:
            final_answer = "Paşam, çok derin düşündüm ama sonuç metni boş geldi. Zihin raporuna bakın."

        history.append(f"Sen: {final_answer}")
        conversation_history[chat_id] = history
        
        return final_answer

    except Exception as e:
        print(f"HATA: {e}")
        # Hata durumunda yedek model (Düşüncesiz)
        try:
            yedek = client.models.generate_content(model='gemini-3-pro-preview', contents=full_context)
            return f"⚠️ (Yedek Hat) {yedek.text}"
        except:
            return f"⚠️ Paşam, Sistem Çöktü: {e}"

# --- MENÜ ---
def main_menu():
    m = InlineKeyboardMarkup(row_width=2)
    m.add(InlineKeyboardButton("📈 BTC", callback_data="analiz_BTC"), InlineKeyboardButton("💎 ETH", callback_data="analiz_ETH"))
    m.add(InlineKeyboardButton("🚀 AAVE", callback_data="analiz_AAVE"), InlineKeyboardButton("☀️ SOL", callback_data="analiz_SOL"))
    m.add(InlineKeyboardButton("⏰ Alarm Kur", callback_data="alarm_kur"))
    m.add(InlineKeyboardButton("🗑️ Temizle", callback_data="hafiza_sil"))
    return m

@bot.message_handler(commands=['start'])
def welcome(m):
    bot.reply_to(m, "Sayın Vedat Paşam, Finans Masası hazır. Zihin okuma modülü aktif.", reply_markup=main_menu())

# --- BUTONLARI DİNLEME ---
@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    chat_id = call.message.chat.id
    
    if call.data == "hafiza_sil":
        conversation_history[chat_id] = []
        bot.answer_callback_query(call.id, "Temizlendi!")
        bot.send_message(chat_id, "Hafıza sıfırlandı Paşam.")

    elif call.data.startswith("analiz_"):
        coin = call.data.split("_")[1]
        bot.answer_callback_query(call.id, "İnceleniyor...")
        bot.send_message(chat_id, f"📊 {coin} dosyası masama geliyor...")
        
        rapor = get_financial_report(f"{coin}/USDT")
        # FONKSİYON BURADA ÇAĞRILIYOR (Tanımlı olduğu için hata vermez)
        cevap = ask_gemini_with_memory(chat_id, f"Bu {coin} raporunu yorumla.", system_instruction=rapor)
        bot.send_message(chat_id, cevap)

    elif call.data == "alarm_kur":
        msg = bot.send_message(chat_id, "Hangi varlık ve fiyat? (Örn: AAVE 175)")
        bot.register_next_step_handler(msg, set_alarm)

def set_alarm(m):
    try:
        parts = m.text.upper().split()
        sym = parts[0] + "/USDT"
        tgt = float(parts[1])
        cur = exchange.fetch_ticker(sym)['last']
        direc = 'ABOVE' if tgt > cur else 'BELOW'
        db_islem("INSERT INTO price_alarms (symbol, target_price, direction) VALUES (%s, %s, %s)", (sym, tgt, direc))
        bot.reply_to(m, "✅ Alarm kuruldu Paşam.")
    except: bot.reply_to(m, "Format hatalı.")

# --- ALARM DEVRİYESİ ---
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
                            bot.send_message(CHAT_ID, f"🚨 ALARM: {sym} -> {p}")
                            db_islem("DELETE FROM price_alarms WHERE id = %s", (aid,))
                    except: pass
            if HEROKU_APP_URL: requests.get(HEROKU_APP_URL)
            time.sleep(30)
        except: time.sleep(30)

# --- SOHBETİ DİNLEME ---
@bot.message_handler(func=lambda m: True)
def chat_logic(m):
    chat_id = m.chat.id
    # FONKSİYON BURADA ÇAĞRILIYOR
    cevap = ask_gemini_with_memory(chat_id, m.text)
    bot.reply_to(m, cevap)

# --- FLASK SERVER ---
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
        
