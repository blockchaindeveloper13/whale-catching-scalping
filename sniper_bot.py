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
import logging # <--- İŞTE KARA KUTU BU
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from flask import Flask, request
from datetime import datetime

# --- LOG AYARI (SİYAH KUTU) ---
# Hem ekrana basacak hem de detayları formatlı gösterecek
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("GenelkurmayLog")

# --- AYARLAR ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY')
BINANCE_SECRET = os.environ.get('BINANCE_SECRET_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
HEROKU_APP_URL = os.environ.get('HEROKU_APP_URL')

# --- MODEL SEÇİMİ ---
genai.configure(api_key=GEMINI_API_KEY)
model_name = 'gemini-3-pro-preview' 

try:
    model = genai.GenerativeModel(model_name)
    logger.info(f"✅ MOTOR TEST EDİLİYOR: {model_name}")
    model.generate_content("Test")
    logger.info(f"✅ MOTOR ÇALIŞTI: {model_name} devrede.")
except Exception as e:
    logger.error(f"⚠️ 3 PRO YETKİSİ YOK! Hata: {e}")
    logger.warning("⚠️ 1.5 PRO YEDEĞİNE GEÇİLİYOR...")
    model = genai.GenerativeModel('gemini-1.5-pro')

bot = telebot.TeleBot(BOT_TOKEN)
server = Flask(__name__)

# --- BORSALAR ---
logger.info("📡 Binance Bağlantıları Kuruluyor...")
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
logger.info("📡 Binance Bağlantısı Hazır.")

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
        if "SELECT" in sql: 
            res = cur.fetchall()
            # logger.info(f"💾 DB OKUMA: {sql} -> {len(res)} satır.") # Çok log yapmasın diye kapalı, gerekirse aç
        else: 
            conn.commit()
            logger.info(f"💾 DB YAZMA/SİLME: {sql} | Param: {params}")
        
        cur.close()
        conn.close()
        return res
    except Exception as e:
        logger.error(f"🔥 DB HATASI: {e} | SQL: {sql}")
        return None

# Tablo Kurulumu
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
    logger.info("✅ Veritabanı Tabloları Kontrol Edildi.")
except Exception as e:
    logger.critical(f"🔥 DB BAŞLATMA HATASI: {e}")

# --- DETAYLI TEKNİK İSTİHBARAT ---
def get_financial_report(symbol):
    logger.info(f"🔍 ANALİZ BAŞLIYOR: {symbol} verileri çekiliyor...")
    if "/" not in symbol: symbol += "/USDT"
    
    report = f"--- 💼 {symbol} DETAYLI FİNANSAL RAPOR ---\n"
    
    # 1. Market Derinliği
    try:
        funding = exchange_vadeli.fetch_funding_rate(symbol)
        rate = funding['fundingRate'] * 100
        sentiment = "AŞIRI LONG (Tuzak Riski)" if rate > 0.01 else "AŞIRI SHORT (Sıkışma Riski)" if rate < -0.01 else "NÖTR"
        report += f"\n📊 MARKET DERİNLİĞİ: Fonlama %{rate:.4f} -> {sentiment}\n"
        logger.info(f"   -> Vadeli Verisi Alındı: %{rate}")
    except Exception as e: 
        logger.warning(f"   -> Vadeli Verisi Alınamadı: {e}")
        report += "\n📊 MARKET: Veri yok (Spot)\n"

    report += "-" * 30 + "\n"

    # 2. Çoklu Zaman Dilimi
    timeframes = ['15m', '1h', '4h', '1d']
    for tf in timeframes:
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=60)
            if not bars or len(bars) < 50:
                logger.error(f"   -> {tf} verisi EKSİK veya BOŞ!")
                continue

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
            
            # BB Sıkışması
            bandwidth = (upper.iloc[-1] - lower.iloc[-1]) / lower.iloc[-1]
            bb_durum = "SIKIŞMA (Patlama Yakın)" if bandwidth < 0.05 else "NORMAL"

            # HACİM (Bitmiş Mum Teyidi)
            vol_completed = df['volume'].iloc[-2]
            vol_avg = df['volume'].iloc[-22:-2].mean()
            vol_ratio = vol_completed / vol_avg if vol_avg > 0 else 0
            vol_text = "GÜÇLÜ HACİM" if vol_ratio > 1.2 else "HACİMSİZ (Tuzak)" if vol_ratio < 0.8 else "NORMAL"

            obv = (pd.Series(np.where(df['close'] > df['close'].shift(1), df['volume'], 
                           np.where(df['close'] < df['close'].shift(1), -df['volume'], 0))).cumsum())
            obv_dir = "POZİTİF (Akümülasyon)" if obv.iloc[-1] > obv.iloc[-10] else "NEGATİF (Dağıtım)"

            report += f"🕒 {tf.upper()} | Fiyat: {df['close'].iloc[-1]}\n"
            report += f"   • RSI: {rsi.iloc[-1]:.1f} | MACD: {'AL' if macd.iloc[-1]>signal.iloc[-1] else 'SAT'}\n"
            report += f"   • Trend: {'BOĞA' if df['close'].iloc[-1] > ema50.iloc[-1] else 'AYI'} | BB: {bb_durum}\n"
            report += f"   • Hacim: {vol_text} (x{vol_ratio:.1f}) | OBV: {obv_dir}\n\n"
            
            logger.info(f"   -> {tf} Verisi Başarılı: RSI={rsi.iloc[-1]:.1f}, Fiyat={df['close'].iloc[-1]}")

        except Exception as e:
            logger.error(f"   -> {tf} Analiz Hatası: {e}")
            pass
            
    logger.info(f"✅ Rapor Hazırlandı ({len(report)} karakter).")
    return report

# --- YAPAY ZEKA BEYNİ ---
def ask_gemini_with_memory(chat_id, user_input, system_instruction=None):
    logger.info(f"🤖 AI SOHBET BAŞLATILIYOR | ChatID: {chat_id}")
    
    if chat_id not in conversation_history:
        conversation_history[chat_id] = []
        logger.info("   -> Yeni hafıza kaydı oluşturuldu.")
    
    history = conversation_history[chat_id]
    history.append({"role": "user", "parts": [user_input]})
    
    # Hafıza Budama
    if len(history) > 30: 
        history = history[-30:]
        logger.info("   -> Hafıza budandı (Son 30 mesaj).")

    # --- PERSONA AYARI (FİNANSÇI) ---
    base_instruction = (
        "SENİN ROLÜN: Vedat Paşa'nın Kıdemli Risk Yöneticisi ve Finans Danışmanısın.\n"
        "KİMLİK: Son derece zeki, analitik, duygusuz ve koruyucu bir finans uzmanısın.\n"
        "HİTAP: Sadece 'Paşam' de. Asla askeri terim kullanma. Kendine 'Bot' deme.\n"
        "GÖREV: Kullanıcıyı piyasa tuzaklarından (Likidite avı, Bull trap) korumak.\n"
        "Eğer veri kötüyse, kullanıcı 'Alayım mı' dese bile 'HAYIR PAŞAM, BU TUZAKTIR' diye sert çık.\n"
        "Borsa jargonunu aktif kullan (Order Block, Supply Zone, Rejection, Likidite, Volatilite).\n"
        "Geçmiş sohbeti hatırla."
    )
    
    if system_instruction:
        full_prompt = f"{base_instruction}\n\nANALİZ VERİLERİ:\n{system_instruction}"
        logger.info("   -> AI'ya Rapor + Talimat gönderiliyor...")
    else:
        full_prompt = base_instruction
        logger.info("   -> AI'ya Sohbet metni gönderiliyor...")

    try:
        chat = model.start_chat(history=history)
        response = chat.send_message(full_prompt)
        text_response = response.text.replace("**", "")
        
        # Loglama: AI'nın ne cevap verdiğini de görelim
        logger.info(f"🤖 AI CEVABI GELDİ: {text_response[:100]}...") # İlk 100 karakteri logla
        
        history.append({"role": "model", "parts": [text_response]})
        conversation_history[chat_id] = history
        return text_response
    except Exception as e:
        logger.error(f"⚠️ AI MODEL HATASI: {e}")
        return f"⚠️ Finansal Hata: {e}"

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
    logger.info(f"👋 Yeni Başlangıç: {m.from_user.username} ({m.chat.id})")
    bot.reply_to(m, "Sayın Vedat Paşam, Risk Masası hazır. Gemini 3 Pro motoru devrede. Duygusallık yok, sadece kazanç var.", reply_markup=main_menu())

@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    chat_id = call.message.chat.id
    user_name = call.message.chat.username
    logger.info(f"🖱️ BUTON TIKLANDI: {call.data} | User: {user_name}")

    if call.data == "hafiza_sil":
        conversation_history[chat_id] = []
        bot.answer_callback_query(call.id, "Hafıza Temizlendi")
        bot.send_message(chat_id, "Geçmişi sildim Paşam. Yeni sayfa açtık.")
        logger.info(f"   -> {user_name} hafızası silindi.")

    elif call.data.startswith("analiz_"):
        coin = call.data.split("_")[1]
        bot.answer_callback_query(call.id, "Veriler İşleniyor...")
        bot.send_message(chat_id, f"📊 {coin} verileri masamda Paşam...")
        
        rapor = get_financial_report(f"{coin}/USDT")
        
        # Loglama: AI'ya giden veriyi görelim (Kablo sağlam mı?)
        # logger.info(f"--- AI'YA GİDEN RAPOR ---\n{rapor}\n-----------------------")
        
        cevap = ask_gemini_with_memory(chat_id, f"{coin} raporunu incele. Tuzak var mı? Alım için güvenli mi?", system_instruction=rapor)
        bot.send_message(chat_id, cevap)

    elif call.data == "alarm_kur":
        msg = bot.send_message(chat_id, "Hangi varlık ve hedef fiyat? (Örn: SOL 145)")
        bot.register_next_step_handler(msg, set_alarm)

def set_alarm(m):
    try:
        parts = m.text.upper().split()
        sym = parts[0] + "/USDT"
        tgt = float(parts[1])
        cur = exchange.fetch_ticker(sym)['last']
        direc = 'ABOVE' if tgt > cur else 'BELOW'
        
        db_islem("INSERT INTO price_alarms (symbol, target_price, direction) VALUES (%s, %s, %s)", (sym, tgt, direc))
        bot.reply_to(m, f"✅ Alarm aktif Paşam: {sym} -> {tgt}")
        logger.info(f"⏰ ALARM KURULDU: {sym} @ {tgt} ({direc})")
    except Exception as e: 
        bot.reply_to(m, "Hatalı format Paşam.")
        logger.error(f"❌ Alarm Kurma Hatası: {e}")

def alarm_patrol():
    logger.info("🔭 ALARM DEVRİYESİ BAŞLATILDI...")
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
                            logger.info(f"🚨 ALARM TETİKLENDİ: {sym} Hedef: {tgt} Güncel: {p}")
                            bot.send_message(CHAT_ID, f"🚨 HEDEF GELDİ PAŞAM!\n{sym}: {p}")
                            db_islem("DELETE FROM price_alarms WHERE id = %s", (aid,))
                    except Exception as e:
                        logger.error(f"Devriye Ticker Hatası ({sym}): {e}")
            
            # Heroku uyutmasın diye ping
            if HEROKU_APP_URL: 
                requests.get(HEROKU_APP_URL)
                # logger.info("Ping atıldı.") # Çok kirletmesin diye kapalı
                
            time.sleep(30)
        except Exception as e:
            logger.error(f"Kule Hatası: {e}")
            time.sleep(30)

@bot.message_handler(func=lambda m: True)
def chat_logic(m):
    text = m.text.upper()
    chat_id = m.chat.id
    logger.info(f"📩 MESAJ ALINDI ({m.from_user.username}): {text}")

    if "ANALIZ" in text:
        words = text.split()
        coin = next((w for w in words if len(w) > 2 and w not in ["ANALIZ", "YAP", "NEDIR"]), None)
        if coin:
            bot.reply_to(m, f"🔎 {coin} bakılıyor Paşam...")
            rapor = get_financial_report(f"{coin}/USDT")
            cevap = ask_gemini_with_memory(chat_id, f"{coin} detaylı analizi.", system_instruction=rapor)
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
    logger.info("🌐 Webhook Online.")
    return "OK", 200

if __name__ == "__main__":
    threading.Thread(target=alarm_patrol).start()
    server.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
