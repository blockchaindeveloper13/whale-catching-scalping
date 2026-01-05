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
# --- GELİŞMİŞ TEKNİK ANALİZ (DENİZ KUVVETLERİ) ⚓ ---
def calculate_indicators(df):
    """Verilen DataFrame için indikatörleri hesaplar."""
    try:
        # Fiyat ve Hacim
        close = df['c']
        volume = df['v']
        
        # 1. RSI (12 Periyot - Paşa'nın İsteği)
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(12).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(12).mean()
        df['rsi'] = 100 - (100 / (1 + gain/loss))

        # 2. EMA (50 ve 200) - Trend Yönü
        df['ema_50'] = close.ewm(span=50, adjust=False).mean()
        df['ema_200'] = close.ewm(span=200, adjust=False).mean()

        # 3. MACD (12, 26, 9)
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        df['macd'] = ema12 - ema26
        df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()

        # 4. Bollinger Bantları (20, 2)
        sma20 = close.rolling(window=20).mean()
        std20 = close.rolling(window=20).std()
        df['bb_upper'] = sma20 + (std20 * 2)
        df['bb_lower'] = sma20 - (std20 * 2)

        # 5. OBV (On-Balance Volume) - BALİNA DEDEKTÖRÜ 🐳
        # Hacmin fiyata etkisini ölçer. Yükseliş hacimli mi, sahte mi?
        df['obv'] = (np.sign(close.diff()) * volume).fillna(0).cumsum()

        return df
    except Exception as e:
        print(f"İndikatör Hatası: {e}")
        return df

def get_deep_financial_report(symbol):
    """15dk, 1s, 4s ve 1g periyotlarında DERİN ANALİZ yapar."""
    if "/" not in symbol: symbol += "/USDT"
    symbol = symbol.upper()
    
    report = f"--- ⚓ {symbol} DENİZ KUVVETLERİ RAPORU ⚓ ---\n"
    report += f"⏰ Rapor Zamanı: {datetime.now().strftime('%H:%M')}\n\n"
    
    timeframes = ['15m', '1h', '4h', '1d']
    
    try:
        # Önce sembol var mı kontrol et (Hata almamak için)
        exchange.load_markets()
        if symbol not in exchange.markets:
            return f"⚠️ UYARI: {symbol} Binance Spot piyasasında bulunamadı. Sadece grafik/Google ile analiz edebilirim."

        ticker = exchange.fetch_ticker(symbol)
        current_price = ticker['last']
        report += f"💰 ANLIK FİYAT: ${current_price}\n"
        report += f"📊 24s Değişim: %{ticker['percentage']:.2f}\n"
        report += f"💧 24s Hacim (USDT): ${ticker['quoteVolume']:,.0f}\n\n"

        for tf in timeframes:
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=100) # Son 100 mum
            df = pd.DataFrame(bars, columns=['time', 'o', 'h', 'l', 'c', 'v'])
            df = calculate_indicators(df)
            
            last = df.iloc[-1]
            prev = df.iloc[-2] # Bir önceki mum (kırılım teyidi için)
            
            # Trend Yorumu (Basit Lojik)
            trend = "YÜKSELİŞ 🟢" if last['ema_50'] > last['ema_200'] else "DÜŞÜŞ 🔴"
            
            # Verileri Rapora Ekle
            report += f"🔻 --- [{tf} PERİYOT] ---\n"
            report += f"   • RSI (12): {last['rsi']:.2f}\n"
            report += f"   • MACD: {last['macd']:.4f} (Sinyal: {last['macd_signal']:.4f})\n"
            report += f"   • Bollinger: Alt[{last['bb_lower']:.2f}] - Üst[{last['bb_upper']:.2f}]\n"
            report += f"   • Trend (EMA50/200): {trend}\n"
            
            # OBV Yorumu (Hacim Artışı Var mı?)
            obv_degisim = last['obv'] - df.iloc[-5]['obv'] # Son 5 mumdaki OBV değişimi
            obv_yorum = "Balina Girişi Var 🐳" if obv_degisim > 0 else "Hacim Zayıf/Çıkış Var ⚠️"
            report += f"   • Hacim Analizi (OBV): {obv_yorum}\n\n"

    except Exception as e:
        report += f"⚠️ Veri Çekme Hatası: {e}\n(Bu coin Binance'de listeli olmayabilir veya API hatası.)"
    
    return report

# --- GÜNCELLENMİŞ MESAJ YAKALAYICI (HER COİNİ TANIR) ---
@bot.message_handler(content_types=['photo', 'text'])
def handle_all(message):
    chat_id = message.chat.id
    user_input = message.caption if message.caption else (message.text if message.text else "")
    
    image_data = None
    mime_type = None
    system_instruction = "" # Yapay zekaya gidecek teknik veri

    # 1. Metin Analizi: Kullanıcı "X analiz" dedi mi?
    # Örnek: "SOL analiz", "Pepe ne olur?", "Analiz btc"
    if user_input and len(user_input) < 20: # Kısa mesajsa coin ismi olabilir
        words = user_input.split()
        potential_coin = words[0].upper() # İlk kelimeyi coin varsayalım
        
        # Eğer kelime 2-6 harf arasındaysa ve rakam içermiyorsa analiz deneyelim
        if 2 <= len(potential_coin) <= 6 and potential_coin.isalpha():
             bot.send_chat_action(chat_id, 'typing')
             system_instruction = get_deep_financial_report(potential_coin)
             if "bulunamadı" not in system_instruction:
                 bot.reply_to(message, f"⚓ {potential_coin} için Donanma Verileri Çekildi! Analiz Başlıyor...")

    # 2. Fotoğraf Varsa
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

    # Eğer sadece sohbetse ve coin yoksa boş geç
    if not user_input and not image_data: return 

    # 3. GEMINI'YE GÖNDER (Veri + Resim + Metin)
    cevap = ask_gemini_unified(chat_id, user_input, image_data=image_data, mime_type=mime_type, system_instruction=system_instruction)
    bot.send_message(chat_id, cevap)
    

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

