import ccxt
import time
import telebot
import os
import pandas as pd
import numpy as np
# --- GENAI KÜTÜPHANESİ ---
from google import genai
from google.genai import types
# -------------------------
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
    print("✅ GEMINI: Gözler, Beyin, İnternet ve ÇELİK HAFIZA Aktif!")
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

# --- VERİTABANI BAĞLANTISI ---
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

# --- TABLO KURULUMLARI (HAFIZA BURADA SAKLANACAK) ---
try:
    conn = db_baglan()
    cur = conn.cursor()
    
    # 1. Alarm Tablosu
    cur.execute("""
        CREATE TABLE IF NOT EXISTS price_alarms (
            id SERIAL PRIMARY KEY,
            symbol VARCHAR(20),
            target_price REAL,
            direction VARCHAR(10)
        )
    """)
    
    # 2. SOHBET GEÇMİŞİ TABLOSU (YENİ GÜÇ) 🧠💾
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT,
            role VARCHAR(10), -- 'user' veya 'model'
            content TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    conn.commit()
    conn.close()
    print("✅ Veritabanı Tabloları Hazır (Alarmlar + Sohbet Geçmişi)")
except Exception as e:
    print(f"Tablo Kurulum Hatası: {e}")

# --- HAFIZA FONKSİYONLARI ---
def save_message(chat_id, role, content):
    """Mesajı veritabanına kaydeder."""
    db_islem("INSERT INTO chat_history (chat_id, role, content) VALUES (%s, %s, %s)", (chat_id, role, content))

def get_history(chat_id, limit=20):
    """Son N mesajı veritabanından çeker."""
    rows = db_islem("SELECT role, content FROM chat_history WHERE chat_id = %s ORDER BY id DESC LIMIT %s", (chat_id, limit))
    if rows:
        # Veritabanından tersten (yeni -> eski) çektik, şimdi düzeltelim (eski -> yeni)
        return rows[::-1]
    return []

def clear_history(chat_id):
    """Hafızayı temizler (Format Atar)."""
    db_islem("DELETE FROM chat_history WHERE chat_id = %s", (chat_id,))

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

# --- YAPAY ZEKA BEYNİ (FOTOĞRAF + SEARCH + THINKING + KALICI HAFIZA) ---
def ask_gemini_unified(chat_id, user_input, image_data=None, mime_type=None, system_instruction=None):
    
    bugun = datetime.now().strftime("%d %B %Y (%A)")
    
    # --- Prompt (DUYGUSAL SERBESTİYET EKLİ) ---
    base_prompt = (
        f"BUGÜNÜN TARİHİ: {bugun}. \n"
        "Sen Vedat Paşa'nın Finans Danışmanısın. Zeki, otoriter, risk uzmanı ve hafif iğneleyici birisin.\n"
        "GÖREVLERİN:\n"
        "1. Eğer RESİM geldiyse: Grafiği yorumla, formasyonları bul.\n"
        "2. Eğer GÜNCEL VERİ sorulursa: Google Search kullan.\n"
        "3. Her cevaptan önce DERİNLEMESİNE DÜŞÜN.\n"
        "4. 'Paşam' diye hitap et.\n"
        "5. EMOJİ KULLANIMI: Tamamen özgürsün. Duygularını (Kızgınlık, Uyarı, Onay, Alay) yansıtmak için emoji kullanabilirsin. Ancak zorlama, sadece gerektiği yerde ve gerektiği kadar kullan.\n"
    )
    
    if system_instruction:
        base_prompt += f"\n\nTEKNİK VERİ:\n{system_instruction}"

    # --- GEÇMİŞİ VERİTABANINDAN ÇEK ---
    db_history = get_history(chat_id, limit=20) # Son 20 mesajı hatırla
    history_text = "\nGEÇMİŞ SOHBET (VERİTABANI):\n"
    for role, content in db_history:
        rol_adi = "Kullanıcı" if role == 'user' else "Sen"
        history_text += f"{rol_adi}: {content}\n"

    # İçerik Listesi
    contents_to_send = [base_prompt, history_text]
    contents_to_send.append(f"Kullanıcı: {user_input}")

    # Fotoğraf varsa
    if image_data:
        image_part = types.Part.from_bytes(data=image_data, mime_type=mime_type)
        contents_to_send.append(image_part)
        # DB'ye resim verisini kaydetmiyoruz (şişmesin diye), sadece resim atıldığını not düşüyoruz
        save_message(chat_id, 'user', f"{user_input} [GÖRSEL İÇERİYOR]")
    else:
        save_message(chat_id, 'user', user_input)

    try:
        # --- GEMINI ÇAĞRISI ---
        response = client.models.generate_content(
            model='gemini-3-pro-preview',
            contents=contents_to_send,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                response_modalities=["TEXT"],
                thinking_config=types.ThinkingConfig(include_thoughts=True)
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

        # 1. Düşünce Mesajı
        if thought_log:
            try:
                bot.send_message(chat_id, f"🧠 [ZİHİN TARAMASI]:\n\n{thought_log[:2000]}")
            except: pass

        # 2. Ana Cevap
        if not final_answer: final_answer = "Düşündüm ama söze dökemedim Paşam."
        
        # CEVABI VERİTABANINA KAYDET (Kalıcı Hafıza)
        save_message(chat_id, 'model', final_answer)
        
        return final_answer

    except Exception as e:
        print(f"HATA: {e}")
        return f"⚠️ Paşam, Sistem Hatası: {e}"

# --- MENÜ ---
def main_menu():
    m = InlineKeyboardMarkup(row_width=2)
    m.add(InlineKeyboardButton("📈 BTC", callback_data="analiz_BTC"), InlineKeyboardButton("🚀 AAVE", callback_data="analiz_AAVE"))
    m.add(InlineKeyboardButton("🗑️ HAFIZAYI SİL", callback_data="hafiza_sil"))
    return m

@bot.message_handler(commands=['start'])
def welcome(m):
    bot.reply_to(m, "Paşam; Hafızam çelik, gözlerim keskin, emojilerim serbest! Emret.", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    chat_id = call.message.chat.id
    if call.data == "hafiza_sil":
        clear_history(chat_id) # DB'den siler
        bot.answer_callback_query(call.id, "Temizlendi!")
        bot.send_message(chat_id, "Geçmişi yaktım Paşam. Beyaz bir sayfa açtık. 🏳️")
    elif call.data.startswith("analiz_"):
        coin = call.data.split("_")[1]
        bot.send_message(chat_id, f"📊 {coin} dosyası inceleniyor... 🕵️")
        rapor = get_financial_report(f"{coin}/USDT")
        cevap = ask_gemini_unified(chat_id, f"{coin} yorumla.", system_instruction=rapor)
        bot.send_message(chat_id, cevap)

# --- SOHBET VE FOTOĞRAF YAKALAYICI ---
@bot.message_handler(content_types=['photo', 'text'])
def handle_all(message):
    chat_id = message.chat.id
    user_text = message.caption if message.caption else (message.text if message.text else "Bu resmi yorumla.")
    
    image_data = None
    mime_type = None

    if message.photo:
        bot.send_chat_action(chat_id, 'typing')
        try:
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            image_data = downloaded_file
            mime_type = "image/jpeg"
            bot.reply_to(message, "📸 Görüntü işleniyor Paşam... 👁️")
        except Exception as e:
            bot.reply_to(message, f"Resim hatası: {e}")
            return

    elif not message.text.startswith("/"):
        pass
    else: return 

    cevap = ask_gemini_unified(chat_id, user_text, image_data=image_data, mime_type=mime_type)
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

