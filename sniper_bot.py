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
    TUM_COINLER = ["BTC", "ETH", "SOL", "AAVE", "LTC", "LINK", "AVAX", "BNB", "XRP", "ADA", "DOGE", "SHIB"]

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
                last_analysis TEXT,
                target_price REAL DEFAULT 0
            )
        """)
        
        # Migration (Yeni sütunlar)
        cur.execute("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS last_analysis TEXT")
        cur.execute("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS target_price REAL DEFAULT 0")
        
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Veritabanı, Kara Kutu ve Alarm Sistemi Hazır!")
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
        cur.execute("SELECT symbol, last_signal, interval_hours, last_report_time, target_price FROM watchlist")
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

def db_hedef_fiyat_guncelle(symbol, fiyat):
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("UPDATE watchlist SET target_price = %s WHERE symbol = %s", (fiyat, symbol))
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

def db_analiz_kaydet(symbol, sinyal, metin):
    try:
        conn = db_baglan()
        cur = conn.cursor()
        cur.execute("UPDATE watchlist SET last_signal = %s, last_analysis = %s WHERE symbol = %s", (sinyal, metin, symbol))
        conn.commit()
        cur.close()
        conn.close()
    except: pass

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

def db_hafizayi_temizle():
    try:
        conn = db_baglan()
        cur = conn.cursor()
        # Analizleri siler, Hedef Fiyatları (Alarm) SIFIRLAR MI? Hayır, alarmlar kalsın.
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
                            f"Bollinger: {bb_durum}\n\n")
                            
        return report_text, current_price
    except: return None, 0

def ask_gemini(symbol, report, last_signal):
    try:
        # KOMUTAN MODU PROMPT
        prompt = (f"Sen Vedat Paşa'nın (Kullanıcı-Asker) Genelkurmay Başkanısın. \n"
                  f"MİSYONUN: Askerini duygusal hatalardan korumak, disipline etmek ve net strateji vermek.\n"
                  f"YASAKLAR: 1) Asla ** (yıldız) kullanma. 2) 'Yatırım tavsiyesi değildir' deme. 3) 'Toplantı yapalım' deme.\n"
                  f"ÜSLUP: Sert, babacan, koruyucu, net. Askerin basireti bağlandığında onu uyar.\n"
                  f"Coin: {symbol}. Eski Sinyal: {last_signal}. \n"
                  f"Veriler:\n{report}\n"
                  f"EMİR: Durumu yorumla ve sonunda (AL / SAT / BEKLE) emrini ver.")
        
        raw_res = model.generate_content(prompt).text
        # --- YILDIZ TEMİZLİĞİ ---
        clean_res = raw_res.replace("**", "").replace("__", "")
        return clean_res
    except Exception as e: return f"Hata: {e}"

# --- 4. TELEGRAM VE SOHBET ---
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
    return "<h1>KOMUTA MERKEZI AKTIF!</h1>", 200

# HAFIZA SİLME
@bot.message_handler(commands=['unut', 'temizle'])
def komut_unut(m):
    db_hafizayi_temizle()
    bot.reply_to(m, "🧹 Hafıza silindi Asker! Geçmişi unuttum, yeni emirlere hazırım.")

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
            bot.reply_to(message, f"🔭 SNIPER DEVREDE! {len(rows)} hedef taranıyor...")
            for r in rows:
                sym = r[0]
                last_sig = r[1]
                report, price = get_full_report(sym)
                if report:
                    yorum = ask_gemini(sym, report, last_sig)
                    # Kayıt
                    new_sig = "AL" if "AL" in yorum else "SAT" if "SAT" in yorum else "BEKLE"
                    db_analiz_kaydet(sym, new_sig, yorum)
                    
                    bot.send_message(message.chat.id, f"HEDEF: {sym}\n{yorum}")
                    time.sleep(4) 
            bot.send_message(message.chat.id, "TARAMA TAMAMLANDI ASKER.")
            return

        # --- B. COIN İŞLEMLERİ ---
        if bulunan_coin:
            symbol = f"{bulunan_coin}/USDT"

            # 1. İPTAL EMRİ
            if any(x in text for x in ["SIL", "IPTAL", "BIRAK"]) and "AL" not in text:
                db_coin_cikar(symbol)
                bot.reply_to(message, f"{bulunan_coin} takibi bırakıldı.")
                return 

            # 2. ALARM KURMA (FİYAT HEDEFİ)
            # Örn: "AAVE HEDEF 200" veya "BTC ALARM 90000"
            hedef_tespiti = re.search(r'(HEDEF|ALARM|FIYAT)\s*(\d+(\.\d+)?)', text)
            if hedef_tespiti:
                fiyat = float(hedef_tespiti.group(2))
                db_coin_ekle(symbol)
                db_hedef_fiyat_guncelle(symbol, fiyat)
                bot.reply_to(message, f"✅ ANLAŞILDI ASKER! {symbol} fiyatı {fiyat} olunca kırmızı alarm vereceğim!")
                return

            # 3. ZAMAN AYARI (PERİYODİK RAPOR)
            saat_tespiti = re.search(r'(\d+)\s*(SAAT)', text)
            if saat_tespiti:
                yeni_saat = int(saat_tespiti.group(1))
                db_coin_ekle(symbol)
                db_saat_guncelle(symbol, yeni_saat)
                bot.reply_to(message, f"{symbol} her {yeni_saat} saatte bir raporlanacak.")
                return

            # 4. ANALİZ İSTEĞİ (SIFIRDAN)
            tetikleyiciler = ["ANALIZ", "DURUM", "NE OLUR", "YORUMLA", "BAK", "RAPOR", "VAR MI"]
            if any(x in text for x in tetikleyiciler):
                bot.reply_to(message, f"{bulunan_coin} cephesi inceleniyor...")
                report, price = get_full_report(symbol)
                if report:
                    yorum = ask_gemini(symbol, report, "Bilinmiyor")
                    # Kayıt
                    new_sig = "AL" if "AL" in yorum else "SAT" if "SAT" in yorum else "BEKLE"
                    db_analiz_kaydet(symbol, new_sig, yorum)
                    
                    bot.send_message(message.chat.id, f"{symbol} RAPORU:\n\n{yorum}")
                else:
                    bot.reply_to(message, "Veri alınamadı.")
                return
            
            # 5. HAFIZADAN SOHBET (Eski Analiz Üzerine Konuşma)
            eski_analiz = db_eski_analizi_oku(symbol)
            if eski_analiz:
                prompt = (f"Sen Vedat Paşa'nın Komutanısın. Askerin (Vedat) sana soru sordu: '{message.text}'\n"
                          f"SENİN ESKİ RAPORUN ({symbol}):\n"
                          f"'{eski_analiz}'\n\n"
                          f"GÖREV: Eski raporunu hatırla, çelişkiye düşme. Askeri motive et veya uyar. Yıldız kullanma.")
                
                raw_res = model.generate_content(prompt).text
                clean_res = raw_res.replace("**", "").replace("__", "")
                bot.reply_to(message, clean_res)
                return

        # --- C. NORMAL SOHBET (KOMUTAN MODU) ---
        if message.text.startswith('/'): return
        
        prompt = (f"Sen Vedat Paşa'nın (Kullanıcı) Genelkurmay Başkanısın. "
                  f"Kullanıcı mesajı: '{message.text}'. "
                  f"Buna sert, disiplinli ama koruyucu bir komutan gibi cevap ver. "
                  f"Asla 'toplantı', 'arama' deme. Yıldız kullanma. Hata yapmasına izin verme.")
        
        raw_res = model.generate_content(prompt).text
        clean_res = raw_res.replace("**", "").replace("__", "")
        bot.reply_to(message, clean_res)
        
    except Exception as e:
        print(f"Sohbet Hatası: {e}")

# Standart Komutlar
@bot.message_handler(commands=['takip'])
def komut_takip(m):
    try:
        sym = m.text.split()[1].upper()
        if "/" not in sym: sym += "/USDT"
        db_coin_ekle(sym)
        bot.reply_to(m, f"✅ {sym} takibe alındı.")
    except: bot.reply_to(m, "Hata.")

@bot.message_handler(commands=['liste'])
def komut_liste(m):
    rows = db_liste_getir_full()
    if not rows:
        bot.reply_to(m, "Liste boş asker.")
        return
    msg = "📋 TAKİP LİSTESİ\n\n"
    for r in rows:
        sym, last_sig, interval, last_time, target = r
        interval = interval if interval else 4
        target_msg = f" (HEDEF: {target})" if target > 0 else ""
        msg += f"🔹 {sym}: {interval}s. Sinyal: {last_sig}{target_msg}\n"
    bot.reply_to(m, msg)

@bot.message_handler(commands=['sil'])
def komut_sil(m):
    try:
        sym = m.text.split()[1].upper()
        if "/" not in sym: sym += "/USDT"
        db_coin_cikar(sym)
        bot.reply_to(m, f"🗑️ {sym} silindi.")
    except: pass

# --- 5. SONSUZ DÖNGÜ (TARAMA & ALARM) ---
def scanner_loop():
    print("Nöbetçi Devrede...")
    while True:
        try:
            # Döngü çok hızlı çalışsın ki fiyat alarmını kaçırmayalım (1 dakika)
            # Ama raporu sadece saati gelince atsın.
            rows = db_liste_getir_full()
            now = datetime.now()
            
            for r in rows:
                sym, last_sig, interval, last_time, target_price = r
                if interval is None: interval = 4
                
                # Fiyatı çek (Hızlı kontrol)
                try:
                    ticker = exchange.fetch_ticker(sym)
                    current_price = ticker['last']
                    
                    # 1. ALARM KONTROLÜ
                    if target_price > 0:
                        # Eğer fiyata ulaşıldıysa (Yukarı veya aşağı yönlü yakalama)
                        # Basit mantık: Hedef fiyata %1 yakınsa veya geçtiyse uyar
                        # Karmaşıklık olmasın diye: Hedefin üzerine çıktıysa (Long) veya altına indiyse (Short) mantığı yerine
                        # Hedefi "VURDU" mantığı yapalım. Kullanıcı hedefi 200 dediyse ve fiyat 200 olduysa.
                        if current_price >= target_price:
                            bot.send_message(CHAT_ID, f"🚨 **ALARM:** {sym} HEDEFİ VURDU!\nFiyat: {current_price}\nHedef: {target_price}")
                            # Alarmı sıfırla ki tekrar tekrar çalmasın
                            db_hedef_fiyat_guncelle(sym, 0)
                except: pass

                # 2. PERİYODİK RAPOR KONTROLÜ
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
                        
                        # Kayıt
                        new_sig = "AL" if "AL" in res else "SAT" if "SAT" in res else "BEKLE"
                        db_analiz_kaydet(sym, new_sig, res)
                        
                        bot.send_message(CHAT_ID, f"⏰ OTOMATİK RAPOR ({interval}s): {sym}\n{res}")
                        db_zaman_damgasi_vur(sym)
            
            time.sleep(60) # Her dakika fiyatları kontrol et
        except Exception as e:
            print(f"Scanner Hatası: {e}")
            time.sleep(60)

if __name__ == "__main__":
    t = threading.Thread(target=scanner_loop)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    server.run(host="0.0.0.0", port=port)
              
