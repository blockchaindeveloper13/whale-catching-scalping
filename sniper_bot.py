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
from flask import Flask, request
from datetime import datetime

# --- 1. AYARLAR VE KİMLİK BİLGİLERİ ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
BINANCE_API_KEY = os.environ.get('BINANCE_API_KEY')
BINANCE_SECRET = os.environ.get('BINANCE_SECRET_KEY')
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
DATABASE_URL = os.environ.get('DATABASE_URL')
HEROKU_APP_URL = os.environ.get('HEROKU_APP_URL') 

# Yapay Zeka ve Borsa Kurulumu
genai.configure(api_key=GEMINI_API_KEY)
# Paşam 2.5 istediğin için bunu bıraktım ama kota hatası alırsan 1.5'a döneriz.
model = genai.GenerativeModel('gemini-2.5-flash') 
bot = telebot.TeleBot(BOT_TOKEN)
server = Flask(__name__)

exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET,
    'options': {'defaultType': 'spot', 'adjustForTimeDifference': True},
    'enableRateLimit': True
})

# --- 2. VERİTABANI YÖNETİMİ (BEYİN) ---
def db_baglan():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def db_baslat():
    try:
        conn = db_baglan()
        cur = conn.cursor()
        
        # Tablo yoksa oluştur
        cur.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                symbol VARCHAR(20) PRIMARY KEY,
                last_signal VARCHAR(50) DEFAULT 'YOK',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # Sonradan eklenen sütunlar (Migration)
        cur.execute("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS interval_hours INT DEFAULT 4")
        cur.execute("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS last_report_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
        
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Veritabanı Hazır!")
    except Exception as e:
        print(f"❌ DB Kurulum Hatası: {e}")

db_baslat() 

def db_coin_ekle(symbol):
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("INSERT INTO watchlist (symbol) VALUES (%s) ON CONFLICT (symbol) DO NOTHING", (symbol,))
        conn.commit()
        conn.close()
        return True
    except: return False

def db_coin_cikar(symbol):
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("DELETE FROM watchlist WHERE symbol = %s", (symbol,))
        conn.commit()
        conn.close()
        return True
    except: return False

def db_liste_getir_full():
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("SELECT symbol, last_signal, interval_hours, last_report_time FROM watchlist")
        rows = cur.fetchall()
        conn.close()
        return rows
    except: return []

def db_saat_guncelle(symbol, saat):
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("UPDATE watchlist SET interval_hours = %s WHERE symbol = %s", (saat, symbol))
        conn.commit()
        conn.close()
        return True
    except: return False

def db_zaman_damgasi_vur(symbol):
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("UPDATE watchlist SET last_report_time = NOW() WHERE symbol = %s", (symbol,))
        conn.commit()
        conn.close()
    except: pass

def db_sinyal_guncelle(symbol, sinyal):
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("UPDATE watchlist SET last_signal = %s WHERE symbol = %s", (sinyal, symbol))
        conn.commit()
        conn.close()
    except: pass

# --- 3. TEKNİK ANALİZ MOTORU ---
def calculate_technicals(df):
    if len(df) < 50: return None
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    vol_avg = df['volume'].rolling(window=20).mean()
    df['vol_change'] = df['volume'] / vol_avg
    return df.iloc[-1]

def get_full_report(symbol):
    report_text = ""
    current_price = 0
    try:
        for tf in ['1h', '4h']:
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=60)
            df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            tech = calculate_technicals(df)
            if tech is None: continue
            current_price = tech['close']
            report_text += (f"[{tf}]\nFiyat: {tech['close']}\nRSI: {tech['rsi']:.1f}\n"
                            f"Trend(EMA50): {'Boğa' if tech['close']>tech['ema50'] else 'Ayı'}\n"
                            f"Hacim Gücü: {tech['vol_change']:.1f}x\n\n")
        return report_text, current_price
    except: return None, 0

def ask_gemini(symbol, report, last_signal):
    try:
        # FİNANSAL DANIŞMAN (WOLF MODE)
        prompt = (f"Sen Vedat Bey'in ÖZEL Kripto Danışmanısın. Bir yapay zeka gibi konuşma. "
                  f"Duygusuz, net, profesyonel ve hafif ukala bir Wall Street kurdu gibi konuş. "
                  f"ASLA 'Yatırım tavsiyesi veremem', 'Ben bir dil modeliyim' deme. YASAK. "
                  f"Coin: {symbol}. Eski Sinyal: {last_signal}. \n"
                  f"Teknik Veriler:\n{report}\n"
                  f"GÖREVİN: Verileri sert bir dille yorumla, riskleri belirt ve sonunda mutlaka (AL / SAT / BEKLE) şeklinde net bir emir ver.")
        return model.generate_content(prompt).text
    except Exception as e: return f"Danışman şu an meşgul (Kota veya Hata): {e}"

# --- 4. TELEGRAM VE SOHBET MODÜLÜ ---
@server.route('/' + BOT_TOKEN, methods=['POST'])
def getMessage():
    json_string = request.get_data().decode('utf-8')
    update = telebot.types.Update.de_json(json_string)
    bot.process_new_updates([update])
    return "!", 200

@server.route("/")
def webhook():
    bot.remove_webhook()
    bot.set_webhook(url=HEROKU_APP_URL + BOT_TOKEN)
    return "<h1>VEDAT PASA KOZMIK ODASI AKTIF!</h1>", 200

# AKILLI SOHBET, SNIPER VE EMİR YAKALAYICI
@bot.message_handler(func=lambda message: True)
def sohbet_et(message):
    try:
        text = message.text.upper()
        
        # --- A. SNIPER MODU (GENEL TARAMA) ---
        # "Genel durum", "Piyasa ne alemde", "Hepsini tara", "Sniper" gibi lafları yakalar
        sniper_tetikleyiciler = ["GENEL", "PIYASA", "HEPSI", "TUM", "SNIPER", "LISTE DURUM"]
        if any(x in text for x in sniper_tetikleyiciler):
            rows = db_liste_getir_full()
            if not rows:
                bot.reply_to(message, "⚠️ Listeniz boş efendim. Önce /takip ile coin ekleyin.")
                return
            
            bot.reply_to(message, f"🔭 **SNIPER MODU AKTİF!**\nListendeki {len(rows)} hedef taranıyor. Raporlar birazdan yağıyor...")
            
            for r in rows:
                sym = r[0]
                last_sig = r[1]
                
                # Veriyi çek
                report, price = get_full_report(sym)
                if report:
                    # Gemini'ye sor
                    yorum = ask_gemini(sym, report, last_sig)
                    bot.send_message(message.chat.id, f"🎯 **HEDEF: {sym}**\n{yorum}", parse_mode='Markdown')
                    # Kota dolmasın diye her analiz arası 4 saniye bekle (Hayati!)
                    time.sleep(4) 
                else:
                    bot.send_message(message.chat.id, f"⚠️ {sym} verisi çekilemedi.")
            
            bot.send_message(message.chat.id, "✅ **TÜM HEDEFLER TARANDI KOMUTANIM!**")
            return

        # --- B. TEKİL COIN İŞLEMLERİ ---
        COINLER = ["BTC", "ETH", "SOL", "AAVE", "LTC", "LINK", "AVAX", "XLM", "SUI", "BCH", "XRP", "DOGE"]
        bulunan_coin = None
        for coin in COINLER:
            if coin in text:
                bulunan_coin = coin
                break
        
        if bulunan_coin:
            symbol = f"{bulunan_coin}/USDT"

            # 1. İPTAL EMRİ
            iptal_kelimeleri = ["SIL", "IPTAL", "BIRAK", "YETER", "KALDIR", "SUS"]
            if any(x in text for x in iptal_kelimeleri):
                db_coin_cikar(symbol)
                bot.reply_to(message, f"❌ Emredersiniz! **{bulunan_coin}** takibi sonlandırıldı.")
                return 

            # 2. ZAMAN AYARLAMA (Örn: "AAVE 3 SAAT")
            saat_tespiti = re.search(r'(\d+)\s*(SAAT)', text)
            if saat_tespiti:
                yeni_saat = int(saat_tespiti.group(1))
                db_coin_ekle(symbol)
                if db_saat_guncelle(symbol, yeni_saat):
                    bot.reply_to(message, f"✅ Anlaşıldı! **{symbol}** her **{yeni_saat} saatte bir** raporlanacak.")
                return

            # 3. ANLIK ANALİZ
            analiz_kelimeleri = ["ANALIZ", "DURUM", "NE OLUR", "YORUMLA", "BAK", "RAPOR", "TAKIP", "IZLE", "FIYAT"]
            if any(x in text for x in analiz_kelimeleri):
                bot.reply_to(message, f"🔎 {bulunan_coin} inceleniyor...")
                report, price = get_full_report(symbol)
                if report:
                    yorum = ask_gemini(symbol, report, "Bilinmiyor")
                    bot.send_message(message.chat.id, f"📊 **{symbol} RAPORU:**\n\n{yorum}", parse_mode='Markdown')
                else:
                    bot.reply_to(message, "❌ Veri yok.")
                return

        # --- C. NORMAL SOHBET (DANIŞMAN) ---
        if message.text.startswith('/'): return
        
        prompt = (f"Sen Vedat Bey'in 'Özel Finans Danışmanısın'. "
                  f"Kullanıcı: '{message.text}'. "
                  f"Kısa, zeki, Wall Street jargonlu cevap ver. AI olduğunu söyleme.")
        
        response = model.generate_content(prompt)
        bot.reply_to(message, response.text)
        
    except Exception as e:
        print(f"Sohbet Hatası: {e}")

# Standart Komutlar
@bot.message_handler(commands=['takip'])
def komut_takip(m):
    try:
        sym = m.text.split()[1].upper()
        if "/" not in sym: sym += "/USDT"
        db_coin_ekle(sym)
        bot.reply_to(m, f"✅ {sym} listeye eklendi.")
    except: bot.reply_to(m, "Örn: /takip AAVE")

@bot.message_handler(commands=['liste'])
def komut_liste(m):
    rows = db_liste_getir_full()
    if not rows:
        bot.reply_to(m, "Listeniz boş.")
        return
    msg = "📋 **TAKİP LİSTESİ**\n\n"
    for r in rows:
        sym, last_sig, interval, last_time = r
        interval = interval if interval else 4
        msg += f"🔹 **{sym}**: {interval} Saatte bir. (Sinyal: {last_sig})\n"
    bot.reply_to(m, msg, parse_mode='Markdown')

@bot.message_handler(commands=['sil'])
def komut_sil(m):
    try:
        sym = m.text.split()[1].upper()
        if "/" not in sym: sym += "/USDT"
        db_coin_cikar(sym)
        bot.reply_to(m, f"🗑️ {sym} silindi.")
    except: pass

# --- 5. SONSUZ DÖNGÜ (AJAN TARAYICI) ---
def scanner_loop():
    print("🚀 Tarayıcı Devrede...")
    while True:
        try:
            rows = db_liste_getir_full()
            now = datetime.now()

            for r in rows:
                sym, last_sig, interval, last_time = r
                if interval is None: interval = 4 
                
                # Süre Hesabı
                gecen_sure = 0
                if last_time:
                    diff = now - last_time
                    gecen_sure = diff.total_seconds() / 3600
                else: gecen_sure = 999 

                # ZAMANI GELDİYSE RAPORLA
                if gecen_sure >= interval:
                    rep, prc = get_full_report(sym)
                    if rep:
                        time.sleep(3) # Kota dostu bekleme
                        res = ask_gemini(sym, rep, last_sig)
                        
                        baslik = f"⏰ **OTOMATİK RAPOR ({interval} Saat):** {sym}"
                        bot.send_message(CHAT_ID, f"{baslik}\n{res}", parse_mode='Markdown')
                        
                        db_zaman_damgasi_vur(sym)
                        new_sig = "AL" if "AL" in res else "SAT" if "SAT" in res else "BEKLE"
                        db_sinyal_guncelle(sym, new_sig)
            
            time.sleep(300) # 5 dk mola

        except Exception as e:
            print(f"Scanner Hatası: {e}")
            time.sleep(60)

# --- 6. BAŞLATMA ---
if __name__ == "__main__":
    t = threading.Thread(target=scanner_loop)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    server.run(host="0.0.0.0", port=port)
