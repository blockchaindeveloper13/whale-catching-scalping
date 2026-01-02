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

# --- YAPAY ZEKA AYARI (EN GÜÇLÜ MODEL) ---
genai.configure(api_key=GEMINI_API_KEY)

# Paşam, senin dediğin gibi çalışan en iyi modeli seçmesi için sıralı deneme yapıyoruz.
# Eğer 2.5 varsa onu, yoksa 2.0'ı, o da yoksa 1.5'i kullanır. Asla yolda kalmaz.
model_list = ['gemini-2.5-flash', 'gemini-2.0-flash-exp', 'gemini-1.5-flash']
model = None
for m in model_list:
    try:
        model = genai.GenerativeModel(m)
        # Test atışı
        model.generate_content("Test")
        print(f"✅ AKTİF MODEL: {m}")
        break
    except: continue
if not model: model = genai.GenerativeModel('gemini-1.5-flash') # Son çare

bot = telebot.TeleBot(BOT_TOKEN)
server = Flask(__name__)

# Binance Bağlantısı
exchange = ccxt.binance({
    'apiKey': BINANCE_API_KEY, 'secret': BINANCE_SECRET,
    'options': {'defaultType': 'spot', 'adjustForTimeDifference': True},
    'enableRateLimit': True
})

# --- MÜHİMMAT (PORTFÖY) YÜKLEME ---
try:
    markets = exchange.load_markets()
    TUM_COINLER = [symbol.split('/')[0] for symbol in markets if '/USDT' in symbol]
    print(f"✅ Mühimmat Deposu Hazır: {len(TUM_COINLER)} Silah (Coin).")
except Exception as e:
    TUM_COINLER = ["BTC", "ETH", "SOL", "AAVE", "LTC", "LINK", "AVAX", "BNB", "XRP", "ADA"]

# --- 2. VERİTABANI (KARARGAH HAFIZASI) ---
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
                last_report_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_analysis TEXT,
                target_price REAL DEFAULT 0
            )
        """)
        # Eski veritabanı varsa güncelliyoruz
        cur.execute("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS last_analysis TEXT")
        cur.execute("ALTER TABLE watchlist ADD COLUMN IF NOT EXISTS target_price REAL DEFAULT 0")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e: print(f"Karargah Hatası: {e}")

db_baslat() 

def db_islem_yap(sql, params=None):
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

# --- 3. TEKNİK İSTİHBARAT RAPORU ---
def calculate_technicals(df):
    if len(df) < 50: return None
    
    # RSI
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # EMA
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
    
    # PIVOT (Cephe Hattı)
    df['pivot'] = (df['high'] + df['low'] + df['close']) / 3
    df['r1'] = (2 * df['pivot']) - df['low']
    df['s1'] = (2 * df['pivot']) - df['high']

    return df.iloc[-1]

def get_full_report(symbol):
    report_text = ""
    current_price = 0
    try:
        # Fiyatı kesin al
        ticker = exchange.fetch_ticker(symbol)
        current_price = ticker['last']

        for tf in ['1h', '4h']:
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=60)
            df = pd.DataFrame(bars, columns=['time', 'open', 'high', 'low', 'close', 'volume'])
            tech = calculate_technicals(df)
            if tech is None: continue
            
            trend = 'YÜKSELİŞ' if tech['close'] > tech['ema50'] else 'DÜŞÜŞ'
            
            report_text += (f"--- CEPHE HATTI: [{tf}] ---\n"
                            f"Anlık Fiyat: {tech['close']}\n"
                            f"DESTEK HATTI (S1): {tech['s1']:.4f}\n"
                            f"DİRENÇ HATTI (R1): {tech['r1']:.4f}\n"
                            f"RSI (Güç): {tech['rsi']:.1f}\n"
                            f"Trend: {trend}\n"
                            f"Bollinger Alt: {tech['lower_bb']:.4f}\n\n")
                            
        return report_text, current_price
    except: return None, 0

def ask_gemini(symbol, report, last_signal):
    try:
        # --- PERSONA: FİNANSAL KURMAY BAŞKANI ---
        prompt = (f"Sen Vedat Paşa'nın (Kullanıcı) 'Finansal Kurmay Başkanısın'. \n"
                  f"GÖREVİN: Paşa'na piyasadaki durumu askeri bir netlikle raporlamak.\n"
                  f"KURALLAR:\n"
                  f"1. Hitap şeklin daima 'Paşam' veya 'Komutanım' olsun. Samimi ve sadık ol.\n"
                  f"2. Fiyat bilgisi, destek ve dirençler 'Stratejik Veridir'. ASLA GİZLEME, net rakam ver.\n"
                  f"3. Asla 'Devlet Sırrı' veya 'Yatırım tavsiyesi değildir' deme. Sen zaten Paşanın emrindesin.\n"
                  f"4. Asla yıldız (**) kullanma.\n"
                  f"5. Eğer fiyat alarmı sorulursa, düşman gözetleme kulesi gibi net bilgi ver.\n\n"
                  f"Coin: {symbol}. Eski Sinyal: {last_signal}. \n"
                  f"İstihbarat Raporu:\n{report}\n"
                  f"EMİR: Durumu özetle, kritik rakamları ver ve (AL / SAT / BEKLE) emrini sun.")
        
        raw_res = model.generate_content(prompt).text
        clean_res = raw_res.replace("**", "").replace("__", "")
        return clean_res
    except Exception as e: return f"Hata: {e}"

# --- 4. SERVER VE KOMUTLAR ---
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
    return "<h1>VEDAT PAŞA KARARGAHI ONLINE</h1>", 200

# HAFIZA SİLME
@bot.message_handler(commands=['unut', 'temizle'])
def komut_unut(m):
    db_islem_yap("UPDATE watchlist SET last_signal = 'YOK', last_analysis = NULL")
    bot.reply_to(m, "🧹 Hafıza temizlendi Paşam! Eski raporları imha ettim, zihnim berrak.")

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
        
        # --- A. SNIPER MODU (TARAMA) ---
        if any(x in text for x in ["GENEL", "PIYASA", "HEPSI", "SNIPER"]):
            rows = db_islem_yap("SELECT symbol, last_signal, interval_hours, last_report_time, target_price FROM watchlist")
            if not rows: return
            bot.reply_to(message, f"🔭 Sniper Timi görevde Paşam! {len(rows)} hedef taranıyor...")
            for r in rows:
                sym = r[0]
                last_sig = r[1]
                report, price = get_full_report(sym)
                if report:
                    yorum = ask_gemini(sym, report, last_sig)
                    # Kayıt
                    new_sig = "AL" if "AL" in yorum else "SAT" if "SAT" in yorum else "BEKLE"
                    db_islem_yap("UPDATE watchlist SET last_signal = %s, last_analysis = %s WHERE symbol = %s", (new_sig, yorum, sym))
                    
                    bot.send_message(message.chat.id, f"HEDEF RAPORU: {sym}\n{yorum}")
                    time.sleep(4) 
            bot.send_message(message.chat.id, "Tarama tamamlandı Paşam. Emirlerinizi bekliyorum.")
            return

        # --- B. COIN İŞLEMLERİ ---
        if bulunan_coin:
            symbol = f"{bulunan_coin}/USDT"

            # 1. İPTAL / SİL
            if any(x in text for x in ["SIL", "IPTAL", "BIRAK"]) and "AL" not in text:
                db_islem_yap("DELETE FROM watchlist WHERE symbol = %s", (symbol,))
                bot.reply_to(message, f"{bulunan_coin} takibi bırakıldı Paşam.")
                return 

            # 2. ALARM KURMA (FİYAT HEDEFİ)
            hedef_tespiti = re.search(r'(HEDEF|ALARM|FIYAT)\s*(\d+(\.\d+)?)', text)
            if hedef_tespiti:
                fiyat = float(hedef_tespiti.group(2))
                db_islem_yap("INSERT INTO watchlist (symbol, target_price) VALUES (%s, %s) ON CONFLICT (symbol) DO UPDATE SET target_price = %s", (symbol, fiyat, fiyat))
                bot.reply_to(message, f"✅ Anlaşıldı Paşam! {symbol} fiyatı {fiyat} olunca Kırmızı Alarm vereceğim!")
                return

            # 3. ZAMAN AYARI
            saat_tespiti = re.search(r'(\d+)\s*(SAAT)', text)
            if saat_tespiti:
                yeni_saat = int(saat_tespiti.group(1))
                db_islem_yap("INSERT INTO watchlist (symbol, interval_hours) VALUES (%s, %s) ON CONFLICT (symbol) DO UPDATE SET interval_hours = %s", (symbol, yeni_saat, yeni_saat))
                bot.reply_to(message, f"{symbol} için her {yeni_saat} saatte bir istihbarat raporu sunulacak Paşam.")
                return

            # 4. ANALİZ İSTEĞİ (Fiyat Dahil)
            tetikleyiciler = ["ANALIZ", "DURUM", "NE OLUR", "YORUMLA", "BAK", "RAPOR", "VAR MI", "FIYAT", "KAÇ"]
            if any(x in text for x in tetikleyiciler):
                bot.reply_to(message, f"{bulunan_coin} cephesi inceleniyor Paşam...")
                report, price = get_full_report(symbol)
                if report:
                    yorum = ask_gemini(symbol, report, "Bilinmiyor")
                    # Kayıt
                    new_sig = "AL" if "AL" in yorum else "SAT" if "SAT" in yorum else "BEKLE"
                    db_islem_yap("UPDATE watchlist SET last_signal = %s, last_analysis = %s WHERE symbol = %s", (new_sig, yorum, symbol))
                    
                    bot.send_message(message.chat.id, f"{symbol} İSTİHBARATI:\n\n{yorum}")
                else:
                    bot.reply_to(message, "Paşam, borsadan veri alamıyorum. Bağlantıyı kontrol edelim.")
                return
            
                        # 5. HAFIZADAN KONUŞMA (KURMAY ZEKASI - GERÇEKÇİ MOD)
            # Önce veritabanına bağlanıp veriyi çekiyoruz (Okuyamıyor şüphen kalmasın diye)
            row = db_islem_yap("SELECT last_analysis FROM watchlist WHERE symbol = %s", (symbol,))
            
            # Eğer veritabanında kayıt varsa:
            if row and row[0] and row[0][0]:
                eski_analiz = row[0][0] # İşte burası! Veriyi gerçekten okuduğu an.
                
                # Şimdi yapay zekaya "YALAKALIK YAPMA" emri veriyoruz:
                prompt = (f"Sen Vedat Paşa'nın Finansal Kurmayısın.\n"
                          f"GÖREVİN: Aşağıdaki 'GERÇEK RAPOR' verisine sadık kalarak Paşanın sorusunu cevapla.\n"
                          f"⚠️ KRİTİK KURAL: Paşa (Kullanıcı) yanlış bir rakam söylerse (örneğin raporda olmayan '15' gibi), ona uyum sağlama! "
                          f"Kibarca 'Paşam raporda o rakam yok, doğrusu şudur' diyerek DÜZELT.\n\n"
                          f"📂 GERÇEK RAPOR VERİSİ ({symbol}):\n"
                          f"--------------------------------------------------\n"
                          f"{eski_analiz}\n"
                          f"--------------------------------------------------\n\n"
                          f"PAŞANIN SORUSU: '{message.text}'\n"
                          f"CEVAP: Rapor dışına çıkmadan, verilerle konuş ve yorumla.")
                
                try:
                    raw_res = model.generate_content(prompt).text
                    clean_res = raw_res.replace("**", "").replace("__", "")
                    bot.reply_to(message, clean_res)
                except: 
                    bot.reply_to(message, "Paşam raporu yorumlarken teknik bir aksaklık oldu.")
                return
            
            # Eğer veritabanında veri yoksa dürüstçe söylesin:
            else:
                bot.reply_to(message, f"Paşam, {symbol} için henüz bir istihbarat raporu kaydetmemişiz. Önce 'ANALİZ' emri verin ki cepheyi inceleyeyim.")
                return
                

        # --- C. NORMAL SOHBET (YAVER MODU) ---
        if message.text.startswith('/'): return
        
        prompt = (f"Sen Vedat Paşa'nın sadık askeri ve finans yaverisin. Kullanıcı: Vedat Paşa. "
                  f"Mesaj: '{message.text}'. "
                  f"Cevap: Sadık, samimi, disiplinli ve 'Paşam' hitabıyla olsun. "
                  f"Asla 'toplantı' deme. Biz cephedeyiz, işimiz strateji.")
        
        res = model.generate_content(prompt).text
        bot.reply_to(message, res.replace("**", ""))
        
    except Exception as e:
        print(f"Sohbet Hatası: {e}")

# Standart Komutlar
@bot.message_handler(commands=['takip'])
def komut_takip(m):
    try:
        sym = m.text.split()[1].upper()
        if "/" not in sym: sym += "/USDT"
        db_islem_yap("INSERT INTO watchlist (symbol) VALUES (%s) ON CONFLICT (symbol) DO NOTHING", (symbol,))
        bot.reply_to(m, f"✅ {sym} listeye alındı Paşam.")
    except: bot.reply_to(m, "Hata.")

@bot.message_handler(commands=['liste'])
def komut_liste(m):
    rows = db_islem_yap("SELECT symbol, last_signal, interval_hours, last_report_time, target_price FROM watchlist")
    if not rows:
        bot.reply_to(m, "Takip listesi boş Paşam.")
        return
    msg = "📋 OPERASYON LİSTESİ\n\n"
    for r in rows:
        sym, last_sig, interval, last_time, target = r
        interval = interval if interval else 4
        target_msg = f" [HEDEF: {target}]" if target and target > 0 else ""
        msg += f"🔹 {sym}: {interval}s. Sinyal: {last_sig}{target_msg}\n"
    bot.reply_to(m, msg)

# --- 5. ALARM VE TARAMA DÖNGÜSÜ (EKONOMİK MOD) ---
def scanner_loop():
    print("💤 Nöbetçi Kulesi: EKONOMİK MOD (15 Dk Arayla Tarama)...")
    while True:
        try:
            # Veritabanını kontrol et
            rows = db_islem_yap("SELECT symbol, last_signal, interval_hours, last_report_time, target_price FROM watchlist")
            
            # Eğer takip listesi boşsa, sistemi yorma, 15 dk uyu
            if not rows: 
                print("Liste boş, asker istirahatte...")
                time.sleep(900) 
                continue
                
            now = datetime.now()
            
            for r in rows:
                sym, last_sig, interval, last_time, target_price = r
                if interval is None: interval = 4 # Varsayılan 4 saat
                
                # --- A. FİYAT ALARMI KONTROLÜ ---
                # Her döngüde fiyatı Binance'den soruyoruz (Bu ücretsizdir)
                try:
                    ticker = exchange.fetch_ticker(sym)
                    current_price = ticker['last']
                    
                    # Eğer bir HEDEF fiyat belirlenmişse kontrol et
                    if target_price and target_price > 0:
                        # Fiyat hedefe geldiyse veya geçtiyse
                        # Mantık: Hedefin altına mı indi (Short) yoksa üstüne mi çıktı (Long) ayırt etmeden
                        # Sadece "Rakam oraya değdi mi" diye bakıyoruz.
                        fark = abs(current_price - target_price)
                        yuzde_fark = (fark / target_price) * 100
                        
                        # %0.5 tolerans ile yakalarsa haber versin
                        if yuzde_fark < 0.5: 
                            bot.send_message(CHAT_ID, f"🚨 **KIRMIZI ALARM PAŞAM!**\n\n{sym} Hedef Menziline Girdi!\nAnlık Fiyat: {current_price}\nHedef: {target_price}")
                            # Alarmı tekrar çalmaması için veritabanından siliyoruz (0 yapıyoruz)
                            db_islem_yap("UPDATE watchlist SET target_price = 0 WHERE symbol = %s", (sym,))
                except Exception as e:
                    print(f"Fiyat alma hatası ({sym}): {e}")

                # --- B. RAPOR ZAMANI GELDİ Mİ? ---
                gecen_sure = 0
                if last_time:
                    diff = now - last_time
                    gecen_sure = diff.total_seconds() / 3600 # Saate çevir
                else: gecen_sure = 999 

                # Eğer belirlenen saat (örn: 4 saat) dolduysa Analiz yap (Maliyetli kısım burası)
                if gecen_sure >= interval:
                    rep, prc = get_full_report(sym)
                    if rep:
                        # Sisteme yüklenmemek için analiz öncesi 2 sn nefes al
                        time.sleep(2)
                        res = ask_gemini(sym, rep, last_sig)
                        
                        # Kayıt
                        new_sig = "AL" if "AL" in res else "SAT" if "SAT" in res else "BEKLE"
                        db_islem_yap("UPDATE watchlist SET last_signal = %s, last_analysis = %s, last_report_time = NOW() WHERE symbol = %s", (new_sig, res, sym))
                        
                        bot.send_message(CHAT_ID, f"⏰ OTOMATİK DEVRIYE RAPORU: {sym}\n{res}")
            
            # --- KRİTİK DEĞİŞİKLİK BURADA ---
            # Eskiden 60 saniyeydi, şimdi 900 saniye (15 Dakika) yaptık.
            print("Tur tamamlandı. Asker 15 dakika dinlenmeye çekiliyor...")
            time.sleep(900) 
            
        except Exception as e:
            print(f"Scanner Hatası: {e}")
            # Hata olsa bile 15 dk bekle ki log dosyası şişmesin
            time.sleep(900)
                

if __name__ == "__main__":
    t = threading.Thread(target=scanner_loop)
    t.start()
    port = int(os.environ.get("PORT", 5000))
    server.run(host="0.0.0.0", port=port)
    
