import ccxt
import time
import telebot
import os
import psycopg2
import pandas as pd
import numpy as np
import random
from datetime import datetime, timedelta

# --- 1. AYARLAR ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
DATABASE_URL = os.environ.get('DATABASE_URL')

exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'} 
})

bot = telebot.TeleBot(BOT_TOKEN)

# --- 2. VERİTABANI ---
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, sslmode='require')

def tabloyu_kur():
    try:
        conn = get_db_connection()
        cur = conn.cursor()
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
    except Exception as e:
        print(f"❌ DB Hatası: {e}")

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
    except:
        pass

# --- 3. ÇOKLU ZAMAN DİLİMİ ANALİZİ ---
def detayli_analiz_yap(symbol):
    rapor = {}
    timeframes = ['15m', '1h', '4h', '1d']
    
    try:
        for tf in timeframes:
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=30)
            if not bars: return None
            
            df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            close = df['close']
            volume = df['volume']
            
            # RSI Hesapla
            delta = close.diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            
            # Hacim Ortalaması
            avg_vol = volume.iloc[-21:-1].mean()
            if avg_vol == 0: avg_vol = 1
            vol_change = volume.iloc[-1] / avg_vol
            
            # Fiyat Değişimi
            price_change = ((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]) * 100
            
            rapor[tf] = {
                'rsi': rsi.iloc[-1],
                'vol_kat': vol_change,
                'price_change': price_change,
                'close': close.iloc[-1]
            }
            
        return rapor
    except Exception as e:
        return None

# --- 4. ANA OPERASYON ---
def keskin_nisanci_goreve():
    # DÜZELTME BURADA: Değişkeni fonksiyonun içine aldık!
    sinyal_gecmisi = {} 
    
    tabloyu_kur()
    bot.send_message(CHAT_ID, "🎖️ KOMUTANIM! General Modu Devrede. Susturucu Takıldı, Duvar Analizi Başladı! 🚀")
    
    # Yasaklı Coinler
    YASAKLI = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'EUR', 'DAI', 'AEUR', 'USDE']

    while True:
        try:
            print("🔄 Piyasa Taranıyor (General Scan)...")
            markets = exchange.load_markets()
            
            hedefler = [
                s for s in markets 
                if s.endswith('/USDT') 
                and markets[s]['active']
                and not any(x in s for x in ['UP/', 'DOWN/', 'BULL/', 'BEAR/'])
                and s.split('/')[0] not in YASAKLI
            ]
            
            random.shuffle(hedefler)
            
            for symbol in hedefler:
                # 1. SUSTURUCU KONTROLÜ
                if symbol in sinyal_gecmisi:
                    gecen_sure = time.time() - sinyal_gecmisi[symbol]
                    if gecen_sure < 3600: # 1 Saat (3600 saniye) geçmediyse atla
                        continue
                
                try:
                    # 2. HIZLI TARAMA (Sadece 15dk Hacim)
                    bars = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=21)
                    if not bars: continue
                    vol = [x[5] for x in bars]
                    last_vol = vol[-1]
                    avg_vol = sum(vol[:-1]) / 20
                    if avg_vol == 0: avg_vol = 1
                    
                    # Hacim 5 Kat Artmışsa -> Detaylı Analize Gir
                    if last_vol > (avg_vol * 5.0):
                        
                        data = detayli_analiz_yap(symbol)
                        if not data: continue
                        
                        d15m = data['15m']
                        d1h = data['1h']
                        d4h = data['4h']
                        d1d = data['1d']
                        
                        # Fiyat Filtresi (Çok ucuzları ele)
                        if d15m['close'] < 0.0001: continue
                        
                        # DUVAR ANALİZİ
                        duvar_var = False
                        duvar_mesaji = "Yol Açık 🟢"
                        # Hacim çok yüksek ama fiyat kımıldamıyorsa (%1 altı)
                        if d15m['vol_kat'] > 5 and abs(d15m['price_change']) < 1.0:
                            duvar_var = True
                            duvar_mesaji = "⚠️ DUVAR VAR! (Hacim Yüksek, Fiyat Gitmiyor) 🧱"

                        # RSI Filtresi (Tepedekileri ele)
                        if d4h['rsi'] > 85: continue

                        # RAPORLA
                        coin_ismi = symbol.split('/')[0]
                        
                        mesaj = (
                            f"🐋 DETAYLI BALİNA RAPORU! 🚨\n\n"
                            f"💎 **{coin_ismi}** ({d15m['close']} $)\n"
                            f"🧱 **Durum:** {duvar_mesaji}\n\n"
                            f"⚡ **15 Dakika (Anlık):**\n"
                            f"   • Hacim: {round(d15m['vol_kat'], 1)} KAT 🚀\n"
                            f"   • Değişim: %{round(d15m['price_change'], 2)}\n"
                            f"   • RSI: {round(d15m['rsi'], 1)}\n\n"
                            f"🕰️ **GENEL TREND:**\n"
                            f"   • 1 Saat RSI: {round(d1h['rsi'], 1)}\n"
                            f"   • 4 Saat RSI: {round(d4h['rsi'], 1)}\n"
                            f"   • Günlük RSI: {round(d1d['rsi'], 1)}\n"
                        )
                        
                        # KOMUTAN YORUMU
                        if duvar_var:
                            mesaj += "\n🛑 DİKKAT: Baskı var, duvarın kırılmasını bekle!"
                        elif d4h['rsi'] < 40:
                            mesaj += "\n✅ FIRSAT: Büyük resimde diplerde, dönüş başlıyor olabilir!"
                        elif d1h['vol_kat'] > 3:
                            mesaj += "\n🔥 GÜÇLÜ: Hem saatlikte hem 15dk'da hacim var!"
                        else:
                            mesaj += "\n🛡️ BİLGİ: Kısa vadeli hareket, stoplu git."

                        bot.send_message(CHAT_ID, mesaj, parse_mode='Markdown')
                        sinyali_kaydet(symbol, d15m['close'], "GENERAL_ANALYSIS", f"Vol:{round(d15m['vol_kat'],1)}")
                        
                        # Listeye Ekle (Susturucu Başlasın)
                        sinyal_gecmisi[symbol] = time.time()
                        
                        time.sleep(5)

                except:
                    continue
            
            print("💤 Tur bitti. Mola...")
            
            # Listeyi Temizle (24 saatten eskileri sil)
            simdi = time.time()
            # HATA ÇIKARAN KISIM DÜZELTİLDİ:
            yeni_liste = {}
            for k, v in sinyal_gecmisi.items():
                if simdi - v < 86400:
                    yeni_liste[k] = v
            sinyal_gecmisi = yeni_liste
            
            time.sleep(120)

        except Exception as e:
            print(f"Genel Hata: {e}")
            time.sleep(30)

if __name__ == "__main__":
    keskin_nisanci_goreve()
                    
