import ccxt
import time
import telebot
import os
import psycopg2
import pandas as pd
import numpy as np
import random

# --- 1. AYARLAR VE KİMLİK DOĞRULAMA (HEROKU KASASI) ---
# Bu bilgileri kodun içine yazmıyoruz, Heroku ayarlarından çekiyoruz.
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
DATABASE_URL = os.environ.get('DATABASE_URL')

# --- 2. BORSAYA BAĞLAN (BINANCE - HERKESE AÇIK VERİ) ---
# API Key gerekmez çünkü sadece okuma yapıyoruz.
exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'} 
})

bot = telebot.TeleBot(BOT_TOKEN)

# --- 3. VERİTABANI (HAFIZA) MODÜLÜ ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def tabloyu_kur():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        # Tablo yoksa oluştur (ID, Zaman, Coin, Fiyat, Sinyal Tipi, Detay)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS istihbarat (
                id SERIAL PRIMARY KEY,
                zaman TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                coin VARCHAR(20),
                fiyat DECIMAL,
                sinyal VARCHAR(100),
                detay TEXT
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("✅ Veritabanı Hazır: Binance Kayıt Defteri Açıldı.")
    except Exception as e:
        print(f"❌ Veritabanı Hatası: {e}")

def sinyali_kaydet(coin, fiyat, sinyal, detay):
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO istihbarat (coin, fiyat, sinyal, detay) VALUES (%s, %s, %s, %s)",
            (coin, float(fiyat), sinyal, detay)
        )
        conn.commit()
        cur.close()
        conn.close()
        print(f"💾 Kayıt Başarılı: {coin}")
    except Exception as e:
        print(f"❌ Kayıt Hatası: {e}")

# --- 4. TEKNİK ANALİZ BİRİMİ ---
def teknik_analiz_yap(symbol):
    try:
        # Binance'ten son 100 mumu (15 dakikalık) çek
        bars = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
        if not bars: return None, None
        
        df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        close = df['close']
        
        # A) RSI (14) HESAPLAMA
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        
        # B) EMA (200) - TREND YÖNÜ
        df['ema200'] = close.ewm(span=200, adjust=False).mean()
        
        # C) HACİM ANALİZİ
        current_volume = df['volume'].iloc[-1] # Son mumun hacmi
        # Son 20 mumun ortalaması (Son mum hariç)
        avg_volume = df['volume'].iloc[-21:-1].mean() 
        
        # Sıfıra bölünme hatasını önle
        if avg_volume == 0: avg_volume = 1
        
        return df.iloc[-1], avg_volume
    except Exception as e:
        return None, None

# --- 5. ANA OPERASYON (NÖBETÇİ KULESİ) ---
def keskin_nisanci_goreve():
    tabloyu_kur() # Başlarken veritabanını kontrol et
    bot.send_message(CHAT_ID, "🌍 KOMUTANIM! Radar Tüm Binance Piyasasına Açıldı. Balina Avı Başlıyor! 🐋")
    
    while True:
        try:
            print("🔄 Piyasa verileri güncelleniyor (Market Load)...")
            markets = exchange.load_markets()
            
            # --- AKILLI FİLTRELEME (ÇÖPLERİ AT) ---
            hedefler = [
                symbol for symbol in markets 
                if symbol.endswith('/USDT')             # Sadece USDT pariteleri
                and markets[symbol]['active']           # Aktif olanlar
                and 'UP/' not in symbol                 # Kaldıraçlı tokenleri at
                and 'DOWN/' not in symbol
                and 'BULL/' not in symbol
                and 'BEAR/' not in symbol
                and 'USDC/' not in symbol               # Stabil coinleri at
                and 'FDUSD/' not in symbol
                and 'TUSD/' not in symbol
                and 'EUR/' not in symbol
            ]
            
            print(f"🎯 Toplam Taranacak Hedef: {len(hedefler)} Adet")
            
            # Listeyi karıştır ki hep aynı sırayla gitmesin
            random.shuffle(hedefler)
            
            # TARAMAYA BAŞLA
            for symbol in hedefler:
                try:
                    # Analiz Yap
                    data, avg_vol = teknik_analiz_yap(symbol)
                    
                    if data is None: continue 
                    
                    fiyat = data['close']
                    rsi = data['rsi']
                    ema200 = data['ema200']
                    hacim = data['volume']
                    
                    # --- STRATEJİ KURALLARI ---
                    
                    # 1. Trend Pozitif mi? (Fiyat EMA200 üstünde)
                    trend_ok = fiyat > ema200
                    
                    # 2. RSI Uygun mu? (Aşırı şişmemiş, 70 altı)
                    rsi_ok = rsi < 70
                    
                    # 3. BALİNA ALARMI: Hacim ortalamanın 5 KATINA çıktı mı?
                    hacim_katsayisi = hacim / avg_vol
                    balina_var = hacim_katsayisi > 5.0 
                    
                    # 4. Fiyat Filtresi (Çok ucuz coinleri elemek istersen açabilirsin)
                    # fiyat_ok = fiyat > 0.00001

                    # --- TETİK ---
                    if trend_ok and rsi_ok and balina_var:
                        
                        coin_ismi = symbol.split('/')[0]
                        
                        mesaj = (
                            f"🐋 DEV BALİNA ALARMI (BINANCE)! 🚨\n\n"
                            f"💎 Coin: #{coin_ismi}\n"
                            f"💰 Fiyat: {fiyat} $\n"
                            f"📊 Hacim Patlaması: {round(hacim_katsayisi, 1)} KAT! 🚀\n"
                            f"📈 RSI: {round(rsi, 2)}\n"
                            f"🌊 Durum: Okyanusta büyük hareketlilik var!\n"
                        )
                        
                        # 1. GRUBA GÖNDER
                        bot.send_message(CHAT_ID, mesaj)
                        
                        # 2. VERİTABANINA KAYDET
                        sinyali_kaydet(symbol, fiyat, "GLOBAL_WHALE", f"Kat:{round(hacim_katsayisi,1)}")
                        
                        print(f"✅ Sinyal Gönderildi: {symbol}")
                        
                        # Arka arkaya mesaj atıp Telegram'dan ban yememek için bekle
                        time.sleep(3) 

                except Exception as inner_e:
                    # Tek bir coinde hata olursa (delist vs.) devam et
                    continue
            
            # Tüm liste bittiğinde botu biraz dinlendir (API ban yememek için)
            print("💤 Tüm piyasa tarandı. 2 dakika mola...")
            time.sleep(120)

        except Exception as e:
            print(f"⚠️ Genel Hata: {e}")
            time.sleep(30)

if __name__ == "__main__":
    keskin_nisanci_goreve()
          
