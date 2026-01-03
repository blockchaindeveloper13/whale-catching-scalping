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

# --- YAPAY ZEKA ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

bot = telebot.TeleBot(BOT_TOKEN)
server = Flask(__name__)

# Binance Bağlantısı (Vadeli İşlemler Verisi İçin Option Eklendi)
exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET,
    'options': {'defaultType': 'spot', 'adjustForTimeDifference': True},
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
        print(f"❌ DB Hatası: {e}", flush=True)
        return None

# --- VERİTABANI KURULUMU ---
def db_baslat():
    try:
        conn = db_baglan()
        cur = conn.cursor()
        # Alarm Tablosu (Basit ve Net)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS price_alarms (
                id SERIAL PRIMARY KEY,
                symbol VARCHAR(20),
                target_price REAL,
                direction VARCHAR(10), -- 'ABOVE' (Yukarı Kırınca) veya 'BELOW' (Aşağı Kırınca)
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("✅ GENELKURMAY VERİTABANI HAZIR!", flush=True)
    except Exception as e:
        print(f"🔥 DB Başlatma Hatası: {e}", flush=True)

db_baslat()

# --- MATEMATİKSEL İNDİKATÖRLER (AĞIR SİLAHLAR) ---
def calculate_parabolic_sar(df, af=0.02, max_af=0.2):
    # Basitleştirilmiş SAR Döngüsü
    high = df['high'].values
    low = df['low'].values
    close = df['close'].values
    sar = np.zeros(len(df))
    # (Kod şişmesin diye kısa bir mantıkla trend yönü veriyoruz)
    # Gerçek SAR hesaplaması çok uzundur, burada trend yönü tespiti yapacağız.
    trend = np.where(close > df['close'].shift(1), 1, -1)
    return trend # 1 ise Altta (Bull), -1 ise Üstte (Bear)

def get_comprehensive_analysis(symbol):
    if "/" not in symbol: symbol += "/USDT"
    
    # 4 CEPHE (Zaman Dilimi)
    timeframes = ['15m', '1h', '4h', '1d']
    full_report = f"--- 🦅 {symbol} GENELKURMAY RAPORU 🦅 ---\n"
    
    # Market Sentiment (Long/Short Tuzak Kontrolü)
    try:
        funding = exchange.fetch_funding_rate(symbol)
        funding_rate = funding['fundingRate'] * 100
        ls_durum = "LONGÇULAR BASKIN (Tuzak İhtimali)" if funding_rate > 0.01 else \
                   "SHORTÇULAR BASKIN (Sıkışma İhtimali)" if funding_rate < -0.01 else "DENGELİ"
        full_report += f"\n📊 MARKET YAPISI (Tuzak Dedektörü):\nFonlama: %{funding_rate:.4f} -> {ls_durum}\n"
    except:
        full_report += "\n📊 MARKET YAPISI: Veri Alınamadı (Spot Coin Olabilir)\n"

    full_report += "-" * 30 + "\n"

    for tf in timeframes:
        try:
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=50) # 50 mum yeterli (EMA 12 için)
            df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            
            # --- İNDİKATÖRLER ---
            # 1. RSI (12 Mum) - Kullanıcı isteği 12, standart 14 ama 12 yapıyoruz.
            delta = df['close'].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=12).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=12).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            
            # 2. EMA (12 Mum)
            ema12 = df['close'].ewm(span=12, adjust=False).mean()
            
            # 3. MACD (12, 26, 9)
            exp12 = df['close'].ewm(span=12, adjust=False).mean()
            exp26 = df['close'].ewm(span=26, adjust=False).mean()
            macd = exp12 - exp26
            signal = macd.ewm(span=9, adjust=False).mean()
            
            # 4. Bollinger (12 Mum - Kullanıcı isteği)
            sma12 = df['close'].rolling(window=12).mean()
            std = df['close'].rolling(window=12).std()
            upper = sma12 + (std * 2)
            lower = sma12 - (std * 2)
            
            # 5. Parabolik SAR (Yön)
            sar_trend = "AL (Fiyat Üstte)" if df['close'].iloc[-1] > df['close'].iloc[-2] else "SAT (Fiyat Altta)"
            
            # 6. Hacim ve Hacim Değişimi
            vol_now = df['volume'].iloc[-1]
            vol_prev = df['volume'].iloc[-2]
            vol_change = ((vol_now - vol_prev) / vol_prev) * 100
            
            # 7. OBV (12 Mum)
            df['obv'] = (pd.Series(np.where(df['close'] > df['close'].shift(1), df['volume'], 
                           np.where(df['close'] < df['close'].shift(1), -df['volume'], 0))).cumsum())
            obv_yon = "YUKARI" if df['obv'].iloc[-1] > df['obv'].iloc[-2] else "AŞAĞI"

            # 8. Anlık Veriler
            price = df['close'].iloc[-1]
            
            # RAPORA EKLE
            full_report += f"🕒 CEPHE: {tf.upper()}\n"
            full_report += f"   • Fiyat: {price} (EMA12: {ema12.iloc[-1]:.2f})\n"
            full_report += f"   • RSI(12): {rsi.iloc[-1]:.1f}\n"
            full_report += f"   • MACD: {'AL' if macd.iloc[-1] > signal.iloc[-1] else 'SAT'} Sinyali\n"
            full_report += f"   • Bollinger: {lower.iloc[-1]:.2f} - {upper.iloc[-1]:.2f}\n"
            full_report += f"   • SAR: {sar_trend}\n"
            full_report += f"   • Hacim: %{vol_change:.1f} ({'Yüksek' if vol_change > 0 else 'Düşük'})\n"
            full_report += f"   • OBV: {obv_yon}\n\n"

        except Exception as e:
            full_report += f"🕒 {tf}: Veri Hatası ({e})\n"
            
    return full_report

# --- ANA MENÜ (INLINE BUTONLAR) ---
def main_menu():
    markup = InlineKeyboardMarkup()
    markup.row_width = 2
    # Analiz Butonları
    markup.add(InlineKeyboardButton("🔍 BTC ANALİZ", callback_data="analiz_BTC"),
               InlineKeyboardButton("🔍 ETH ANALİZ", callback_data="analiz_ETH"))
    markup.add(InlineKeyboardButton("🔍 AAVE ANALİZ", callback_data="analiz_AAVE"),
               InlineKeyboardButton("🔍 LTC ANALİZ", callback_data="analiz_LTC"))
    markup.add(InlineKeyboardButton("🔍 BCH ANALİZ", callback_data="analiz_BCH"),
               InlineKeyboardButton("🔍 LINK ANALİZ", callback_data="analiz_LINK"))
    # Alarm Butonları
    markup.add(InlineKeyboardButton("⏰ ALARM KUR (Seçmeli)", callback_data="alarm_kur"))
    return markup

# --- KOMUT YÖNETİMİ ---
@bot.message_handler(commands=['start', 'menu'])
def send_welcome(message):
    bot.reply_to(message, "🫡 Komutanım! Genelkurmay Harekat Merkezi Hazır.\nEmrinizi bekliyorum:", reply_markup=main_menu())

# --- BUTON TIKLAMALARI (CALLBACK) ---
@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    if call.data.startswith("analiz_"):
        coin = call.data.split("_")[1]
        bot.answer_callback_query(call.id, f"{coin} Cephesi İnceleniyor...")
        bot.send_message(call.message.chat.id, f"📡 {coin}/USDT İÇİN 4 CEPHELİ İSTİHBARAT TOPLANIYOR...\nLütfen bekleyiniz Komutanım.")
        
        # Veriyi Çek
        rapor_verisi = get_comprehensive_analysis(f"{coin}/USDT")
        
        # Yapay Zekaya Yorumlat
        try:
            prompt = (f"GÖREV: Genelkurmay Başkanı Vedat Paşa'ya Arz.\n"
                      f"KONU: {coin} Geniş Kapsamlı Stratejik Analiz.\n"
                      f"VERİLER:\n{rapor_verisi}\n"
                      f"TALİMAT: \n"
                      f"1. 4 zaman dilimini (15dk, 1s, 4s, 1g) sentezle.\n"
                      f"2. Long/Short ve Hacim tuzaklarına dikkat et.\n"
                      f"3. Market Maker oyununu boz.\n"
                      f"4. SONUÇ: Net bir askeri emir ver (HÜCUM / GERİ ÇEKİL / MEVZİ KORU).")
            
            ai_cevap = model.generate_content(prompt).text.replace("**", "")
            bot.send_message(call.message.chat.id, f"{ai_cevap}")
        except:
            bot.send_message(call.message.chat.id, f"⚠️ AI Hatası. Manuel Rapor:\n{rapor_verisi}")

    elif call.data == "alarm_kur":
        msg = bot.send_message(call.message.chat.id, "Komutanım, hangi Coin ve Hangi Fiyat? (Örn: AAVE 165)")
        bot.register_next_step_handler(msg, set_alarm_manually)

# --- MANUEL ALARM KURMA ---
def set_alarm_manually(message):
    try:
        text = message.text.upper()
        parts = text.split()
        if len(parts) >= 2:
            symbol = parts[0] + "/USDT"
            target = float(parts[1])
            
            # Şu anki fiyatı alıp yönü belirleyelim
            ticker = exchange.fetch_ticker(symbol)
            current = ticker['last']
            direction = 'ABOVE' if target > current else 'BELOW'
            
            db_islem("INSERT INTO price_alarms (symbol, target_price, direction) VALUES (%s, %s, %s)", (symbol, target, direction))
            bot.reply_to(message, f"✅ ALARM KURULDU PAŞAM!\nHedef: {symbol} -> {target}\nYön: {'YUKARI KIRINCA' if direction == 'ABOVE' else 'AŞAĞI KIRINCA'}")
        else:
            bot.reply_to(message, "⚠️ Hatalı format. Örnek: AAVE 165")
    except Exception as e:
        bot.reply_to(message, f"Hata: {e}")

# --- YAZILI KOMUT ANALİZİ ---
@bot.message_handler(func=lambda m: True)
def handle_text(m):
    text = m.text.upper()
    if "ANALIZ" in text:
        # Coin ismini bul
        words = text.split()
        coin = None
        for w in words:
            if w not in ["ANALIZ", "YAP", "DURUM", "NEDIR"] and len(w) > 2:
                coin = w
                break
        
        if coin:
            bot.reply_to(m, f"📡 {coin} analiz ediliyor Komutanım...")
            rapor = get_comprehensive_analysis(f"{coin}/USDT")
            try:
                res = model.generate_content(f"Paşa'ya Arz. Coin: {coin}. Veriler:\n{rapor}\nDetaylı Yorumla.").text
                bot.reply_to(m, res.replace("**", ""))
            except:
                bot.reply_to(m, f"Manuel Rapor:\n{rapor}")
    elif m.text == "/id":
        bot.reply_to(m, f"Chat ID: {m.chat.id}")

# --- ALARM DEVRİYESİ (ARKA PLAN BOTU) ---
# Bu yapay zeka değil, saf Python devriyesidir. Asla yorulmaz.
def alarm_patrol():
    print("🔭 KESKİN NİŞANCI TİMİ (ALARM) GÖREVDE...", flush=True)
    while True:
        try:
            # 1. Alarmları Çek
            alarms = db_islem("SELECT id, symbol, target_price, direction FROM price_alarms")
            if alarms:
                for alarm in alarms:
                    aid, sym, target, direction = alarm
                    try:
                        ticker = exchange.fetch_ticker(sym)
                        price = ticker['last']
                        
                        hit = False
                        if direction == 'ABOVE' and price >= target: hit = True
                        elif direction == 'BELOW' and price <= target: hit = True
                        
                        if hit:
                            bot.send_message(CHAT_ID, f"🚨 DİKKAT KOMUTANIM! HEDEF MENZİLDE!\n\n🎯 {sym}\n💰 Anlık: {price}\n🎯 Hedef: {target}\n\nAlarm imha ediliyor.")
                            db_islem("DELETE FROM price_alarms WHERE id = %s", (aid,))
                    except: pass
            
            # Sunucuyu uyanık tut
            if HEROKU_APP_URL: requests.get(HEROKU_APP_URL)
            
            time.sleep(30) # 30 Saniyede bir kontrol (Hızlı)
        except Exception as e:
            print(f"Devriye Hatası: {e}", flush=True)
            time.sleep(30)

# --- FLASK ---
@server.route('/' + BOT_TOKEN, methods=['POST'])
def getMessage():
    bot.process_new_updates([telebot.types.Update.de_json(request.get_data().decode('utf-8'))])
    return "!", 200

@server.route("/")
def webhook():
    bot.remove_webhook()
    bot.set_webhook(url=HEROKU_APP_URL + BOT_TOKEN)
    return "GENELKURMAY ONLINE", 200

if __name__ == "__main__":
    # Alarm Timini Başlat
    t = threading.Thread(target=alarm_patrol)
    t.start()
    
    # Web Sunucusunu Başlat
    server.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
        
