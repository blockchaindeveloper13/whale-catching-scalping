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
    print("✅ GEMINI: Düşünce Modu, Funding Rate ve Emir Tahtası Gözleri AKTİF!")
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
# Vadeli İşlemler (Funding Rate için)
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

# --- TABLO KURULUMLARI ---
try:
    conn = db_baglan()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id SERIAL PRIMARY KEY,
            chat_id BIGINT,
            role VARCHAR(10),
            content TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    print("✅ Veritabanı Hazır")
except Exception as e:
    print(f"Tablo Kurulum Hatası: {e}")

# --- HAFIZA FONKSİYONLARI ---
def save_message(chat_id, role, content):
    db_islem("INSERT INTO chat_history (chat_id, role, content) VALUES (%s, %s, %s)", (chat_id, role, content))

def get_history(chat_id, limit=20):
    rows = db_islem("SELECT role, content FROM chat_history WHERE chat_id = %s ORDER BY id DESC LIMIT %s", (chat_id, limit))
    if rows: return rows[::-1]
    return []

def clear_history(chat_id):
    db_islem("DELETE FROM chat_history WHERE chat_id = %s", (chat_id,))

# --- GELİŞMİŞ TEKNİK ANALİZ (DENİZ KUVVETLERİ + İSTİHBARAT) ⚓🕵️‍♂️ ---
def calculate_indicators(df):
    try:
        close = df['c']
        volume = df['v']
        
        # 1. RSI (12)
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(12).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(12).mean()
        df['rsi'] = 100 - (100 / (1 + gain/loss))

        # 2. EMA & Trend
        df['ema_50'] = close.ewm(span=50, adjust=False).mean()
        df['ema_200'] = close.ewm(span=200, adjust=False).mean()

        # 3. MACD
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        df['macd'] = ema12 - ema26
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()

        # 4. Bollinger
        sma20 = close.rolling(window=20).mean()
        std20 = close.rolling(window=20).std()
        df['bb_upper'] = sma20 + (std20 * 2)
        df['bb_lower'] = sma20 - (std20 * 2)

        # 5. OBV (Balina Dedektörü)
        df['obv'] = (np.sign(close.diff()) * volume).fillna(0).cumsum()

        return df
    except Exception as e:
        print(f"İndikatör Hatası: {e}")
        return df

def get_deep_financial_report(symbol):
    if "/" not in symbol: symbol += "/USDT"
    symbol = symbol.upper()
    
    report = f"--- ⚓ {symbol} TAM TEŞEKKÜLLÜ İSTİHBARAT RAPORU ⚓ ---\n"
    report += f"⏰ Zaman: {datetime.now().strftime('%H:%M')}\n\n"
    
    try:
        # 1. SPOT FİYAT VE HACİM
        exchange.load_markets()
        if symbol not in exchange.markets:
            return f"⚠️ UYARI: {symbol} Binance Spot piyasasında bulunamadı."

        ticker = exchange.fetch_ticker(symbol)
        current_price = ticker['last']
        report += f"💰 FİYAT: ${current_price}\n"
        report += f"📊 Değişim (24s): %{ticker['percentage']:.2f}\n"
        report += f"💧 Hacim (24s): ${ticker['quoteVolume']:,.0f}\n\n"

        # 2. VADELİ İŞLEMLER İSTİHBARATI (FUNDING RATE) 🧨
        try:
            # Vadeli sembolü genelde aynıdır ama bazen farklılık olabilir, düz mantık deniyoruz
            funding_info = exchange_vadeli.fetch_funding_rate(symbol)
            funding_rate = funding_info['fundingRate'] * 100 # Yüzdeye çevir
            next_funding = datetime.fromtimestamp(funding_info['fundingTimestamp'] / 1000).strftime('%H:%M')
            
            funding_yorum = "NÖTR 😐"
            if funding_rate > 0.01: funding_yorum = "AŞIRI LONG (Düşüş Riski) 🔴"
            if funding_rate < 0: funding_yorum = "SHORT SQUEEZE İHTİMALİ (Yükseliş) 🟢"
            
            report += f"🧨 --- [VADELİ İSTİHBARATI] ---\n"
            report += f"   • Funding Rate: %{funding_rate:.4f} ({funding_yorum})\n"
            report += f"   • Sıradaki Ödeme: {next_funding}\n\n"
        except:
            report += "   • (Bu coin Vadeli İşlemlerde bulunamadı)\n\n"

        # 3. EMİR TAHTASI (ORDER BOOK) - CEPHE SAVAŞI ⚔️
        try:
            order_book = exchange.fetch_order_book(symbol, limit=5) # İlk 5 kademe
            bids = order_book['bids'] # Alıcılar
            asks = order_book['asks'] # Satıcılar
            
            bid_vol = sum([x[1] for x in bids])
            ask_vol = sum([x[1] for x in asks])
            ratio = bid_vol / ask_vol if ask_vol > 0 else 1
            
            duvar_yorum = "ALICILAR GÜÇLÜ 💪" if ratio > 1.2 else ("SATICILAR BASKIN 🐻" if ratio < 0.8 else "DENGELİ ⚖️")

            report += f"⚔️ --- [EMİR DEFTERİ ANALİZİ] ---\n"
            report += f"   • Durum: {duvar_yorum} (Oran: {ratio:.2f})\n"
            report += f"   • En Yakın Destek (Alış): ${bids[0][0]}\n"
            report += f"   • En Yakın Direnç (Satış): ${asks[0][0]}\n\n"
        except:
            pass

        # 4. TEKNİK İNDİKATÖRLER (ÇOKLU ZAMAN DİLİMİ)
        timeframes = ['15m', '1h', '4h', '1d']
        for tf in timeframes:
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=100)
            df = pd.DataFrame(bars, columns=['time', 'o', 'h', 'l', 'c', 'v'])
            df = calculate_indicators(df)
            last = df.iloc[-1]
            
            trend = "YÜKSELİŞ 🟢" if last['ema_50'] > last['ema_200'] else "DÜŞÜŞ 🔴"
            
            report += f"🔻 --- [{tf} TEKNİK VERİ] ---\n"
            report += f"   • RSI (12): {last['rsi']:.2f}\n"
            report += f"   • Trend: {trend}\n"
            report += f"   • Bollinger: Alt[{last['bb_lower']:.2f}] - Üst[{last['bb_upper']:.2f}]\n"
            
            obv_degisim = last['obv'] - df.iloc[-5]['obv']
            obv_yorum = "Balina Girişi 🐳" if obv_degisim > 0 else "Para Çıkışı ⚠️"
            report += f"   • OBV: {obv_yorum}\n\n"

    except Exception as e:
        report += f"⚠️ Analiz Hatası: {e}\n"
    
    return report

# --- YAPAY ZEKA BEYNİ (DÜŞÜNME + GÖRME + HAFIZA) 🧠 ---
# --- YAPAY ZEKA BEYNİ (DÜŞÜNME + GÖRME + HAFIZA + TEMİZLİK) 🧠 ---
def ask_gemini_unified(chat_id, user_input, image_data=None, mime_type=None, system_instruction=None):
    bugun = datetime.now().strftime("%d %B %Y (%A)")
    
    base_prompt = (
        f"BUGÜNÜN TARİHİ: {bugun}. \n"
        "Sen Vedat Paşa'nın Finans Danışmanısın. Zeki, otoriter, risk uzmanı ve hafif iğneleyici birisin.\n"
        "GÖREVLERİN:\n"
        "1. **Funding Rate Yorumla:** Pozitifse longçulara dikkat et, negatifse short squeeze bekle.\n"
        "2. **Order Book Yorumla:** Alım duvarı mı var, satış baskısı mı?\n"
        "3. Her cevaptan önce DERİNLEMESİNE DÜŞÜN (Thinking Mode).\n"
        "4. 'Paşam' diye hitap et.\n"
        "5. EMOJİLERİ BOL KULLAN.\n"
    )
    
    if system_instruction:
        base_prompt += f"\n\n🛑 PİYASA İSTİHBARATI (BİNANCE CANLI):\n{system_instruction}"

    db_history = get_history(chat_id, limit=20)
    history_text = "\nGEÇMİŞ SOHBET:\n"
    for role, content in db_history:
        rol_adi = "Kullanıcı" if role == 'user' else "Sen"
        history_text += f"{rol_adi}: {content}\n"

    contents_to_send = [base_prompt, history_text, f"Kullanıcı: {user_input}"]

    if image_data:
        image_part = types.Part.from_bytes(data=image_data, mime_type=mime_type)
        contents_to_send.append(image_part)
        save_message(chat_id, 'user', f"{user_input} [GÖRSEL İÇERİYOR]")
    else:
        save_message(chat_id, 'user', user_input)

    try:
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

        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if hasattr(part, 'thought') and part.thought is True:
                    thought_log += part.text
                else:
                    final_answer += part.text

        # --- TEMİZLİK OPERASYONU (ZIMPARA) ---
        if thought_log: 
            thought_log = thought_log.replace("**", "").replace("##", "").replace("###", "")
        
        if final_answer: 
            # Yıldızları sil, Başlık karelerini sil, Tireleri madde imine çevir
            final_answer = final_answer.replace("**", "").replace("##", "").replace("###", "").replace("- ", "• ")

        if thought_log:
            try:
                bot.send_message(chat_id, f"🧠 [ZİHİN TARAMASI]:\n\n{thought_log[:2000]}")
            except: pass

        if not final_answer: final_answer = "Düşündüm ama söze dökemedim Paşam."
        
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
    bot.reply_to(m, "Paşam; Funding Rate, Emir Tahtası ve Derin Hafıza devrede! Emret.", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    chat_id = call.message.chat.id
    if call.data == "hafiza_sil":
        clear_history(chat_id)
        bot.answer_callback_query(call.id, "Temizlendi!")
        bot.send_message(chat_id, "Geçmişi yaktım Paşam. Beyaz bir sayfa açtık. 🏳️")
    elif call.data.startswith("analiz_"):
        coin = call.data.split("_")[1]
        bot.send_message(chat_id, f"⚓ {coin} için Derin İstihbarat (Funding/Depth) Toplanıyor... 📡")
        rapor = get_deep_financial_report(f"{coin}/USDT")
        cevap = ask_gemini_unified(chat_id, f"{coin} detaylı yorumla.", system_instruction=rapor)
        bot.send_message(chat_id, cevap)

@bot.message_handler(content_types=['photo', 'text'])
def handle_all(message):
    chat_id = message.chat.id
    user_input = message.caption if message.caption else (message.text if message.text else "")
    
    image_data = None
    mime_type = None
    system_instruction = "" 

    if user_input and len(user_input) < 20: 
        words = user_input.split()
        if words:
            potential_coin = words[0].upper()
            if 2 <= len(potential_coin) <= 6 and potential_coin.isalpha():
                 bot.send_chat_action(chat_id, 'typing')
                 # YENİ ÖZELLİK: Coin ismini görünce Funding Rate ve Order Book çekiyor
                 system_instruction = get_deep_financial_report(potential_coin)
                 if "bulunamadı" not in system_instruction:
                     bot.reply_to(message, f"⚓ {potential_coin} Radar Kilitlendi! İstihbarat Geliyor...")

    if message.photo:
        bot.send_chat_action(chat_id, 'typing')
        try:
            file_info = bot.get_file(message.photo[-1].file_id)
            downloaded_file = bot.download_file(file_info.file_path)
            image_data = downloaded_file
            mime_type = "image/jpeg"
        except Exception as e:
            bot.reply_to(message, f"Resim hatası: {e}")
            return

    if not user_input and not image_data: return 

    cevap = ask_gemini_unified(chat_id, user_input, image_data=image_data, mime_type=mime_type, system_instruction=system_instruction)
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

