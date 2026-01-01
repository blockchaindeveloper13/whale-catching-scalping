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

# Yapay Zeka (Gemini 1.5 Flash - Hızlı ve Cömert)
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash') 

bot = telebot.TeleBot(BOT_TOKEN)
server = Flask(__name__)

# Binance Bağlantısı
exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET,
    'options': {'defaultType': 'spot', 'adjustForTimeDifference': True},
    'enableRateLimit': True
})

# --- MÜHİMMAT YÜKLEME (TÜM COINLER) ---
print("📡 Binance mühimmat deposu sayılıyor...")
try:
    markets = exchange.load_markets()
    TUM_COINLER = [symbol.split('/')[0] for symbol in markets if '/USDT' in symbol]
    print(f"✅ {len(TUM_COINLER)} adet Coin hafızaya yüklendi! Ordu hazır.")
except Exception as e:
    print(f"⚠️ Liste çekilemedi, manuel listeye dönülüyor: {e}")
    TUM_COINLER = ["BTC", "ETH", "SOL", "AAVE", "LTC", "LINK", "AVAX", "BNB", "XRP", "ADA", "DOGE", "SHIB", "PEPE", "ARB", "SUI"]

# --- 2. VERİTABANI YÖNETİMİ ---
def db_baglan():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def db_baslat():
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                symbol VARCHAR(20) PRIMARY KEY,
                last_signal VARCHAR(50) DEFAULT 'YOK',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                interval_hours INT DEFAULT 4,
                last_report_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Veritabanı Hazır!")
    except Exception as e:
        print(f"❌ DB Hatası: {e}")

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

# --- 3. GELİŞMİŞ TEKNİK ANALİZ (DESTEK & DİRENÇ EKLENDİ) ---
def calculate_technicals(df):
    if len(df) < 50: return None
    
    # 1. RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # 2. EMA 50
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    # 3. MACD
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()

    # 4. BOLLINGER
    df['sma20'] = df['close'].rolling(window=20).mean()
    df['std'] = df['close'].rolling(window=20).std()
    df['upper_bb'] = df['sma20'] + (df['std'] * 2)
    df['lower_bb'] = df['sma20'] - (df['std'] * 2)
    
    # 5. HACİM
    vol_avg = df['volume'].rolling(window=20).mean()
    df['vol_change'] = df['volume'] / vol_avg

    # 6. PIVOT POINTS (DESTEK VE DİRENÇ HESAPLAMA)
    # (High + Low + Close) / 3 formülü ile Pivot bulunur
    df['pivot'] = (df['high'] + df['low'] + df['close']) / 3
    # Direnç 1 (R1) = (2 * Pivot) - Low
    df['r1'] = (2 * df['pivot']) - df['low']
    # Destek 1 (S1) = (2 * Pivot) - High
    df['s1'] = (2 * df['pivot']) - df['high']

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
            
            # Yorumlama
            trend_yonu = 'YUKARI (Boga)' if tech['close'] > tech['ema50'] else 'ASAGI (Ayi)'
            macd_durum = 'AL Sinyali' if tech['macd'] > tech['signal'] else 'SAT Baskisi'
            
            # Bollinger Yorumu
            bb_durum = "Normal"
            if tech['close'] > tech['upper_bb']: bb_durum = "Tavani Deldi (Dikkat)"
            elif tech['close'] < tech['lower_bb']: bb_durum = "Tabani Deldi (Dip?)"
            
            # Pivot (Destek/Direnç)
            destek = tech['s1']
            direnc = tech['r1']
            
            report_text += (f"--- ZAMAN DİLİMİ: [{tf}] ---\n"
                            f"Fiyat: {tech['close']}\n"
                            f"KRITIK DESTEK (S1): {destek:.4f}\n"
                            f"KRITIK DIRENC (R1): {direnc:.4f}\n"
                            f"RSI (14): {tech['rsi']:.1f}\n"
                            f"Trend (EMA50): {trend_yonu}\n"
                            f"MACD: {macd_durum}\n"
                            f"Bollinger: {bb_durum}\n"
                            f"Hacim Gücü: {tech['vol_change']:.1f}x\n\n")
                            
        return report_text, current_price
    except: return None, 0

def ask_gemini(symbol, report, last_signal):
    try:
        # PROMPT (GÖRSEL TEMİZLİK VE DESTEK/DİRENÇ VURGUSU)
        prompt = (f"Sen Vedat Bey'in Stratejik Finans Danışmanısın. \n"
                  f"KURALLAR:\n"
                  f"1. ASLA kalın yazı için yıldız (** veya *) KULLANMA. Telegram'da kötü görünüyor. Düz metin yaz.\n"
                  f"2. Duygusuz, net ve profesyonel konuş.\n"
                  f"3. Verilen 'KRITIK DESTEK' ve 'KRITIK DIRENC' seviyelerini mutlaka yorumunda belirt. 'Fiyat desteğe yakın', 'Direnci kırmaya çalışıyor' gibi strateji kur.\n"
                  f"4. Sonunda mutlaka (AL / SAT / BEKLE) emri ver.\n\n"
                  f"Coin: {symbol}. Eski Sinyal: {last_signal}. \n"
                  f"Teknik Veriler:\n{report}")
        return model.generate_content(prompt).text
    except Exception as e: return f"Hata: {e}"

# --- 4. TELEGRAM MODÜLÜ ---
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
    return "<h1>VEDAT PASA KOMUTA MERKEZI AKTIF!</h1>", 200

@bot.message_handler(func=lambda message: True)
def sohbet_et(message):
    try:
        text = message.text.upper()
        kelimeler = text.split()
        
        bulunan_coin = None
        for kelime in kelimeler:
            temiz_kelime = kelime.strip(".,!?") 
            if temiz_kelime in TUM_COINLER:
                bulunan_coin = temiz_kelime
                break
        
        # --- A. SNIPER MODU ---
        sniper_tetikleyiciler = ["GENEL", "PIYASA", "HEPSI", "TUM", "SNIPER"]
        if any(x in text for x in sniper_tetikleyiciler):
            rows = db_liste_getir_full()
            if not rows:
                bot.reply_to(message, "Liste bos efendim.")
                return
            
            bot.reply_to(message, f"SNIPER MODU AKTIF! {len(rows)} hedef taraniyor...")
            for r in rows:
                sym = r[0]
                last_sig = r[1]
                report, price = get_full_report(sym)
                if report:
                    yorum = ask_gemini(sym, report, last_sig)
                    bot.send_message(message.chat.id, f"HEDEF: {sym}\n{yorum}")
                    time.sleep(4) 
                else:
                    bot.send_message(message.chat.id, f"{sym} verisi yok.")
            bot.send_message(message.chat.id, "TARAMA TAMAMLANDI.")
            return

        # --- B. TEKİL COIN ---
        if bulunan_coin:
            symbol = f"{bulunan_coin}/USDT"

            # İPTAL
            iptal_kelimeleri = ["SIL", "IPTAL", "BIRAK", "YETER", "KALDIR", "SUS"]
            # Eğer cümlede iptal kelimesi varsa, AMA "AL/SAT" gibi emirler yoksa sil (Yanlış anlamayı önlemek için)
            if any(x in text for x in iptal_kelimeleri) and "AL" not in text and "SAT" not in text:
                db_coin_cikar(symbol)
                bot.reply_to(message, f"Emredersiniz! {bulunan_coin} takibi sonlandirildi.")
                return 

            # ZAMAN AYARI
            saat_tespiti = re.search(r'(\d+)\s*(SAAT)', text)
            if saat_tespiti:
                yeni_saat = int(saat_tespiti.group(1))
                db_coin_ekle(symbol)
                if db_saat_guncelle(symbol, yeni_saat):
                    bot.reply_to(message, f"Anlasildi! {symbol} her {yeni_saat} saatte bir raporlanacak.")
                return

            # ANALİZ
            analiz_kelimeleri = ["ANALIZ", "DURUM", "NE OLUR", "YORUMLA", "BAK", "RAPOR", "TAKIP", "IZLE", "FIYAT", "VAR MI"]
            if any(x in text for x in analiz_kelimeleri):
                bot.reply_to(message, f"{bulunan_coin} inceleniyor...")
                report, price = get_full_report(symbol)
                if report:
                    yorum = ask_gemini(symbol, report, "Bilinmiyor")
                    bot.send_message(message.chat.id, f"{symbol} DETAYLI TEKNİK RAPOR:\n\n{yorum}")
                else:
                    bot.reply_to(message, f"{symbol} verisi alinamadi.")
                return

        if message.text.startswith('/'): return
        
        prompt = (f"Sen Vedat Bey'in Finans Danismanisin. Mesaj: '{message.text}'. "
                  f"Kisa, profesyonel cevap ver. Yildiz (**) kullanma.")
        response = model.generate_content(prompt)
        bot.reply_to(message, response.text)
        
    except Exception as e:
        print(f"Sohbet Hatası: {e}")

# ... (Komut fonksiyonları /takip, /liste, /sil aynı kalacak, sadece ** işaretlerini kaldırabilirsin içlerinden) ...
# (Kısalık olması için o kısımları tekrarlamadım, eski kodun alt kısmı çalışır ama bu sohbet_et ve ask_gemini önemli)

# --- 5. SONSUZ DÖNGÜ ---
def scanner_loop():
    print("Tarayici Devrede...")
    while True:
        try:
            rows = db_liste_getir_full()
            now = datetime.now()
            for r in rows:
                sym, last_sig, interval, last_time = r
                if interval is None: interval = 4 
                
                gecen_sure = 0
                if last_time:
                    diff = now - last_time
                    gecen_sure = diff.total_seconds() / 3600
                else: gecen_sure = 999 

                if gecen_sure >= interval:
                    rep, prc = get_full_report(sym)
                    if rep:
                        time.sleep(3)
                        res = ask_gemini(sym, rep, last_sig)
                        bot.send_message(CHAT_ID, f"OTOMATIK RAPOR ({interval} Saat): {sym}\n{res}")
                        db_zaman_damgasi_vur(sym)
                        new_sig = "AL" if "AL" in res else "SAT" if "SAT" in res else "BEKLE"
                        db_sinyal_guncelle(sym, new_sig)
            time.sleep(300) 
        except Exception as e:
            print(f"Scanner Hatası: {e}")
            time.sleep(60)

if __name__ == "__main__":
    t = threading.Thread(target=scanner_loop)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    server.run(host="0.0.0.0", port=port)

