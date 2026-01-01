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

# Yapay Zeka
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash') 

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
        
        # Tablo oluştur
        cur.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                symbol VARCHAR(20) PRIMARY KEY,
                last_signal VARCHAR(50) DEFAULT 'YOK',
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                interval_hours INT DEFAULT 4,
                last_report_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_analysis TEXT
            )
        """)
        
        # Migration (Kara Kutu sütunu yoksa ekle)
        cur.execute("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS last_analysis TEXT")
        
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Veritabanı ve Kara Kutu Hazır!")
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

# --- YENİ: ANALİZİ KAYDET VE SİNYALİ GÜNCELLE ---
def db_analiz_kaydet(symbol, sinyal, metin):
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("""
            UPDATE watchlist 
            SET last_signal = %s, last_analysis = %s 
            WHERE symbol = %s
        """, (sinyal, metin, symbol))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e: print(f"Kayıt Hatası: {e}")

# --- YENİ: ESKİ ANALİZİ OKU ---
def db_eski_analizi_oku(symbol):
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("SELECT last_analysis FROM watchlist WHERE symbol = %s", (symbol,))
        row = cur.fetchone()
        conn.close()
        if row and row[0]: return row[0]
        return None
    except: return None

# --- YENİ: HAFIZAYI TEMİZLE (LİSTE KALIR) ---
def db_hafizayi_temizle():
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("UPDATE watchlist SET last_signal = 'YOK', last_analysis = NULL")
        conn.commit()
        cur.close()
        conn.close()
        return True
    except: return False

# --- 3. GELİŞMİŞ TEKNİK ANALİZ ---
def calculate_technicals(df):
    if len(df) < 50: return None
    
    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # EMA 50
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
    
    # MACD
    exp1 = df['close'].ewm(span=12, adjust=False).mean()
    exp2 = df['close'].ewm(span=26, adjust=False).mean()
    df['macd'] = exp1 - exp2
    df['signal'] = df['macd'].ewm(span=9, adjust=False).mean()

    # BOLLINGER
    df['sma20'] = df['close'].rolling(window=20).mean()
    df['std'] = df['close'].rolling(window=20).std()
    df['upper_bb'] = df['sma20'] + (df['std'] * 2)
    df['lower_bb'] = df['sma20'] - (df['std'] * 2)
    
    # HACİM
    vol_avg = df['volume'].rolling(window=20).mean()
    df['vol_change'] = df['volume'] / vol_avg

    # PIVOT (Destek/Direnç)
    df['pivot'] = (df['high'] + df['low'] + df['close']) / 3
    df['r1'] = (2 * df['pivot']) - df['low']
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
            
            trend_yonu = 'YUKARI' if tech['close'] > tech['ema50'] else 'ASAGI'
            macd_durum = 'AL' if tech['macd'] > tech['signal'] else 'SAT'
            
            bb_durum = "Normal"
            if tech['close'] > tech['upper_bb']: bb_durum = "Tavan (Satis?)"
            elif tech['close'] < tech['lower_bb']: bb_durum = "Taban (Alim?)"
            
            report_text += (f"--- ZAMAN DİLİMİ: [{tf}] ---\n"
                            f"Fiyat: {tech['close']}\n"
                            f"KRITIK DESTEK (S1): {tech['s1']:.4f}\n"
                            f"KRITIK DIRENC (R1): {tech['r1']:.4f}\n"
                            f"RSI: {tech['rsi']:.1f}\n"
                            f"Trend: {trend_yonu}\n"
                            f"MACD: {macd_durum}\n"
                            f"Bollinger: {bb_durum}\n"
                            f"Hacim: {tech['vol_change']:.1f}x\n\n")
                            
        return report_text, current_price
    except: return None, 0

def ask_gemini(symbol, report, last_signal):
    try:
        prompt = (f"Sen Vedat Bey'in Finans Danismanisin. \n"
                  f"KURALLAR: Asla yildiz (**) kullanma. Net konus. Destek/Direncleri yorumla.\n"
                  f"Coin: {symbol}. Eski Sinyal: {last_signal}. \n"
                  f"Veriler:\n{report}\n"
                  f"SONUC: (AL / SAT / BEKLE) emri ver.")
        return model.generate_content(prompt).text
    except Exception as e: return f"Hata: {e}"

# --- 4. TELEGRAM VE HAFIZALI SOHBET ---
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

# HAFIZA TEMİZLEME KOMUTU
@bot.message_handler(commands=['unut', 'temizle'])
def komut_unut(m):
    if db_hafizayi_temizle():
        bot.reply_to(m, "🧹 Hafıza silindi Komutanım! Listeyi korudum, analizleri unuttum.")
    else: bot.reply_to(m, "Hata oluştu.")

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
        if any(x in text for x in ["GENEL", "PIYASA", "HEPSI", "SNIPER"]):
            rows = db_liste_getir_full()
            bot.reply_to(message, f"SNIPER MODU AKTIF! {len(rows)} hedef taranıyor...")
            for r in rows:
                sym = r[0]
                last_sig = r[1]
                report, price = get_full_report(sym)
                if report:
                    yorum = ask_gemini(sym, report, last_sig)
                    
                    # 💾 KAYIT ANI
                    new_sig = "AL" if "AL" in yorum else "SAT" if "SAT" in yorum else "BEKLE"
                    db_analiz_kaydet(sym, new_sig, yorum)
                    
                    bot.send_message(message.chat.id, f"HEDEF: {sym}\n{yorum}")
                    time.sleep(4) 
            bot.send_message(message.chat.id, "TARAMA BİTTİ.")
            return

        # --- B. COIN İŞLEMLERİ ---
        if bulunan_coin:
            symbol = f"{bulunan_coin}/USDT"

            # 1. İPTAL
            if any(x in text for x in ["SIL", "IPTAL", "BIRAK", "SUS"]) and "AL" not in text:
                db_coin_cikar(symbol)
                bot.reply_to(message, f"{bulunan_coin} takibi bırakıldı.")
                return 

            # 2. ZAMAN AYARI
            saat_tespiti = re.search(r'(\d+)\s*(SAAT)', text)
            if saat_tespiti:
                yeni_saat = int(saat_tespiti.group(1))
                db_coin_ekle(symbol)
                db_saat_guncelle(symbol, yeni_saat)
                bot.reply_to(message, f"{symbol} her {yeni_saat} saatte bir raporlanacak.")
                return

            # 3. YENİ ANALİZ İSTEĞİ (ZORLA ANALİZ YAPTIRMA)
            tetikleyiciler = ["ANALIZ", "DURUM", "NE OLUR", "YORUMLA", "BAK", "RAPOR", "FIYAT", "VAR MI"]
            if any(x in text for x in tetikleyiciler):
                bot.reply_to(message, f"{bulunan_coin} inceleniyor...")
                report, price = get_full_report(symbol)
                if report:
                    yorum = ask_gemini(symbol, report, "Bilinmiyor")
                    
                    # 💾 KAYIT ANI
                    new_sig = "AL" if "AL" in yorum else "SAT" if "SAT" in yorum else "BEKLE"
                    db_analiz_kaydet(symbol, new_sig, yorum)
                    
                    bot.send_message(message.chat.id, f"{symbol} RAPORU:\n\n{yorum}")
                else:
                    bot.reply_to(message, "Veri alınamadı.")
                return
            
            # 4. HAFIZADAN KONUŞMA (OKUMA ANI)
            eski_analiz = db_eski_analizi_oku(symbol)
            if eski_analiz:
                prompt = (f"Sen Vedat Bey'in Finans Danışmanısın. \n"
                          f"Kullanıcı: '{message.text}'\n"
                          f"SENİN ESKİ RAPORUN ({symbol}):\n"
                          f"'{eski_analiz}'\n\n"
                          f"GÖREV: Eski raporunu hatırla ve buna göre cevap ver. Yeni analiz yapma.")
                
                cevap = model.generate_content(prompt).text
                bot.reply_to(message, cevap)
                return

        # --- C. NORMAL SOHBET ---
        if message.text.startswith('/'): return
        
        prompt = (f"Sen Vedat Bey'in Finans Danismanisin. Mesaj: '{message.text}'. "
                  f"Kısa, profesyonel cevap ver. Asla AI olduğunu söyleme.")
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
    msg = "📋 TAKİP LİSTESİ\n\n"
    for r in rows:
        sym, last_sig, interval, last_time = r
        interval = interval if interval else 4
        msg += f"🔹 {sym}: {interval} Saatte bir. (Sinyal: {last_sig})\n"
    bot.reply_to(m, msg)

@bot.message_handler(commands=['sil'])
def komut_sil(m):
    try:
        sym = m.text.split()[1].upper()
        if "/" not in sym: sym += "/USDT"
        db_coin_cikar(sym)
        bot.reply_to(m, f"🗑️ {sym} silindi.")
    except: pass

# --- 5. SONSUZ DÖNGÜ (AUTO SCANNER) ---
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
                        
                        # 💾 KAYIT ANI
                        new_sig = "AL" if "AL" in res else "SAT" if "SAT" in res else "BEKLE"
                        db_analiz_kaydet(sym, new_sig, res)
                        
                        bot.send_message(CHAT_ID, f"OTOMATIK RAPOR ({interval} Saat): {sym}\n{res}")
                        db_zaman_damgasi_vur(sym)
            time.sleep(300) 
        except Exception as e:
            print(f"Scanner Hatası: {e}")
            time.sleep(60)

if __name__ == "__main__":
    t = threading.Thread(target=scanner_loop)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    server.run(host="0.0.0.0", port=port)
    
