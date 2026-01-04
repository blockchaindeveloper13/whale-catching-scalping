import ccxt
import time
import telebot
import os
import pandas as pd
import numpy as np
# --- YENİ KÜTÜPHANE ---
from google import genai
from google.genai import types
# ----------------------
import psycopg2
import threading
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

# --- GEMINI CLIENT ---
try:
    client = genai.Client(api_key=GEMINI_API_KEY)
    print("✅ GEMINI: Gözler (Vision), Beyin (Thinking) ve İnternet (Search) AKTİF!")
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

try:
    conn = db_baglan()
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS price_alarms (id SERIAL PRIMARY KEY, symbol VARCHAR(20), target_price REAL, direction VARCHAR(10))")
    conn.commit()
    conn.close()
except: pass

# --- TEKNİK ANALİZ ---
def get_financial_report(symbol):
    if "/" not in symbol: symbol += "/USDT"
    report = f"--- 💼 {symbol} TEKNİK RAPOR ---\n"
    try:
        bars = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=30)
        df = pd.DataFrame(bars, columns=['time', 'o', 'h', 'l', 'c', 'v'])
        
        delta = df['c'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rsi = 100 - (100 / (1 + gain/loss))
        
        report += f"💰 Fiyat: {df['c'].iloc[-1]}\n"
        report += f"📈 RSI (4H): {rsi.iloc[-1]:.1f}\n"
    except: pass
    return report

# --- YAPAY ZEKA BEYNİ (FOTOĞRAF + SEARCH + THINKING) ---
# Parametreleri güncelledik: Artık image_data alabiliyor
def ask_gemini_unified(chat_id, user_input, image_data=None, mime_type=None, system_instruction=None):
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []
    
    history = conversation_history[chat_id]
    bugun = datetime.now().strftime("%d %B %Y (%A)")
    
    # --- Prompt ---
    base_prompt = (
        f"BUGÜNÜN TARİHİ: {bugun}. \n"
        "Sen Vedat Paşa'nın Finans Danışmanısın. Zeki, otoriter ve risk uzmanısın.\n"
        "GÖREVLERİN:\n"
        "1. Eğer RESİM geldiyse: Grafiği yorumla, formasyonları bul.\n"
        "2. Eğer GÜNCEL VERİ sorulursa: Google Search kullan.\n"
        "3. Her cevaptan önce DERİNLEMESİNE DÜŞÜN.\n"
        "4. 'Paşam' diye hitap et.\n"
        "5. EMOJİ KULLANIMI: Tamamen özgürsün. Duygularını (Kızgınlık, Uyarı, Onay, Alay) yansıtmak için emoji kullanabilirsin. Ancak zorlama, sadece gerektiği yerde ve gerektiği kadar kullan. Palyaço gibi görünme, Komutan gibi görün.\n"
    )
    
    if system_instruction:
        base_prompt += f"\n\nTEKNİK VERİ:\n{system_instruction}"

    # İçerik Listesi (Görseli buraya ekleyeceğiz)
    contents_to_send = [base_prompt]

    # Geçmişi metin olarak ekle
    history_text = "\nGEÇMİŞ SOHBET:\n" + "\n".join([h for h in history if isinstance(h, str)])
    contents_to_send.append(history_text)
    
    # Kullanıcı mesajı
    contents_to_send.append(f"Kullanıcı: {user_input}")

    # --- FOTOĞRAF VARSA EKLE ---
    if image_data:
        image_part = types.Part.from_bytes(
            data=image_data,
            mime_type=mime_type
        )
        contents_to_send.append(image_part)

    try:
        response = client.models.generate_content(
            model='gemini-3-pro-preview',
            contents=contents_to_send,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                response_modalities=["TEXT"],
                thinking_config=types.ThinkingConfig(include_thoughts=True) # Düşünme Açık
            )
        )
        
        final_answer = ""
        thought_log = ""

        # Ayıklama
        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'thought') and part.thought is True:
                    thought_log += part.text
                else:
                    final_answer += part.text

        # Temizlik
        if thought_log: thought_log = thought_log.replace("**", "").replace("##", "")
        if final_answer: final_answer = final_answer.replace("**", "").replace("##", "")

        # 1. Mesaj: Düşünce
        if thought_log:
            try:
                bot.send_message(chat_id, f"🧠 [ZİHİN TARAMASI]:\n\n{thought_log[:2000]}")
            except: pass

        # 2. Cevap
        if not final_answer: final_answer = "Görseli inceledim ama söze dökemedim Paşam."
        
        # Hafızaya ekle (Resmi hafızada tutmuyoruz, sadece metni)
        history.append(f"Sen: {final_answer}")
        conversation_history[chat_id] = history
        
        return final_answer

    except Exception as e:
        print(f"HATA: {e}")
        return f"⚠️ Paşam, Görsel/Zihin Hatası: {e}"

# --- MENÜ ---
def main_menu():
    m = InlineKeyboardMarkup(row_width=2)
    m.add(InlineKeyboardButton("📈 BTC", callback_data="analiz_BTC"), InlineKeyboardButton("🚀 AAVE", callback_data="analiz_AAVE"))
    m.add(InlineKeyboardButton("🗑️ Temizle", callback_data="hafiza_sil"))
    return m

@bot.message_handler(commands=['start'])
def welcome(m):
    bot.reply_to(m, "Paşam, Gözlerim, Beynim ve İnternetim aktif. Grafik atın, soru sorun.", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    chat_id = call.message.chat.id
    if call.data == "hafiza_sil":
        conversation_history[chat_id] = []
        bot.answer_callback_query(call.id, "Temizlendi!")
        bot.send_message(chat_id, "Hafıza sıfırlandı Paşam.")
    elif call.data.startswith("analiz_"):
        coin = call.data.split("_")[1]
        bot.send_message(chat_id, f"📊 {coin} geliyor...")
        rapor = get_financial_report(f"{coin}/USDT")
        cevap = ask_gemini_unified(chat_id, f"{coin} yorumla.", system_instruction=rapor)
        bot.send_message(chat_id, cevap)

# --- SOHBET VE FOTOĞRAF YAKALAYICI (BURASI ÇOK ÖNEMLİ) ---
# content_types=['photo', 'text'] diyerek hem resmi hem yazıyı yakalıyoruz
@bot.message_handler(content_types=['photo', 'text'])
def handle_all(message):
    chat_id = message.chat.id
    
    # Eğer resim varsa altına yazılan yazıyı al (caption), yoksa normal mesajı al
    user_text = message.caption if message.caption else (message.text if message.text else "Bu resmi yorumla.")
    
    image_data = None
    mime_type = None

    # FOTOĞRAF İŞLEME
    if message.photo:
        bot.send_chat_action(chat_id, 'typing') # "Yazıyor..." görünsün
        try:
            # Telegram'dan en kaliteli versiyonu indir
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            
            image_data = downloaded_file
            mime_type = "image/jpeg"
            bot.reply_to(message, "📸 Grafik inceleniyor Paşam, bekleyin...")
        except Exception as e:
            bot.reply_to(message, f"Resim indirilemedi: {e}")
            return

    # EĞER SADECE METİNSE
    elif not message.text.startswith("/"):
        # Normal sohbet
        pass
    else:
        return # Komutsa (/start) işlem yapma

    # GEMINI'YE GÖNDER
    cevap = ask_gemini_unified(chat_id, user_text, image_data=image_data, mime_type=mime_type)
    
    # Cevabı gönder (Düşünce zaten fonksiyon içinde gönderildi)
    bot.send_message(chat_id, cevap)

# --- SERVER ---
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
    server.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))

