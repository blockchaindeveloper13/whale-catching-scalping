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

# "Susturucu" için hafıza (Hangi coine ne zaman sinyal attık?)
sinyal_gecmisi = {} 

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
    
    # İncelemek istediğimiz zaman dilimleri
    timeframes = ['15m', '1h', '4h', '1d']
    
    try:
        for tf in timeframes:
            # Her zaman dilimi için son 30 mumu çek
            bars = exchange.fetch_ohlcv(symbol, timeframe=tf, limit=30)
            df = pd.DataFrame(bars, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            close = df['close']
            volume = df['volume']
            
            # A) RSI Hesapla
            delta = close.diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            rsi = 100 - (100 / (1 + rs))
            
            # B) Hacim Ortalaması (Son 20 mum)
            avg_vol = volume.iloc[-21:-1].mean()
            if avg_vol == 0: avg_vol = 1
            vol_change = volume.iloc[-1] / avg_vol
            
            # C) Fiyat Değişimi (Yüzde)
            price_change = ((close.iloc[-1] - close.iloc[-2]) / close.iloc[-2]) * 100
            
            # Verileri kaydet
            rapor[tf] = {
                'rsi': rsi.iloc[-1],
                'vol_kat': vol_change,
                'price_change': price_change,
                'close': close.iloc[-1],
                'open': df['open'].iloc[-1],
                'high': df['high'].iloc[-1],
                'low': df['low'].iloc[-1]
            }
            
        return rapor
        
    except Exception as e:
        print(f"Analiz Hatası ({symbol}): {e}")
        return None

# --- 4. ANA OPERASYON ---
def keskin_nisanci_goreve():
    tabloyu_kur()
    bot.send_message(CHAT_ID, "🎖️ KOMUTANIM! General Modu Devrede. Çoklu Zaman Analizi ve Duvar Tespiti Başladı! 🚀")
    
    # Yasaklı Coinler (Stablecoinler ve Hacimsizler)
    YASAKLI = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'EUR', 'DAI', 'AEUR', 'USDE']

    while True:
        try:
            print("🔄 Piyasa Taranıyor (General Scan)...")
            markets = exchange.load_markets()
            
            # Filtreleme
            hedefler = [
                s for s in markets 
                if s.endswith('/USDT') 
                and markets[s]['active']
                and not any(x in s for x in ['UP/', 'DOWN/', 'BULL/', 'BEAR/'])
                and s.split('/')[0] not in YASAKLI
            ]
            
            random.shuffle(hedefler)
            
            for symbol in hedefler:
                # 1. TEMİZLİK (Susturucu Kontrolü)
                # Eğer son 1 saat (3600 sn) içinde sinyal attıysak pas geç.
                if symbol in sinyal_gecmisi:
                    gecen_sure = time.time() - sinyal_gecmisi[symbol]
                    if gecen_sure < 3600: 
                        continue
                
                # 2. ÖN KEŞİF (Sadece 15m'ye bak, enerji harcama)
                # Burayı hızlı geçmek için basit analiz yapıyoruz
                try:
                    bars = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=21)
                    if not bars: continue
                    vol = [x[5] for x in bars]
                    last_vol = vol[-1]
                    avg_vol = sum(vol[:-1]) / 20
                    if avg_vol == 0: avg_vol = 1
                    
                    # EĞER HACİM 5 KATINDAN FAZLAYSA -> DETAYLI ANALİZE GİR
                    if last_vol > (avg_vol * 5.0):
                        
                        # --- DETAYLI ANALİZ BAŞLIYOR (1h, 4h, 1d) ---
                        data = detayli_analiz_yap(symbol)
                        if not data: continue
                        
                        # Verileri Çek
                        d15m = data['15m']
                        d1h = data['1h']
                        d4h = data['4h']
                        d1d = data['1d']
                        
                        # --- KRİTERLER ---
                        
                        # 1. Hacim 5 Kat Artmış (15m) - Zaten geçti
                        # 2. Fiyat çok ucuz değil (0.00001 altı riskli)
                        if d15m['close'] < 0.0001: continue
                        
                        # 3. DUVAR ANALİZİ (Wall Detection)
                        # Hacim çok yüksek (>5 kat) AMA Fiyat değişimi çok düşük (< %1) ise Duvar vardır.
                        duvar_var = False
                        duvar_mesaji = "Yol Açık 🟢"
                        
                        if d15m['vol_kat'] > 5 and abs(d15m['price_change']) < 1.0:
                            duvar_var = True
                            duvar_mesaji = "⚠️ DUVAR TESPİT EDİLDİ! (Hacim Var, Fiyat Gitmiyor) 🧱"

                        # 4. RSI KONTROLÜ (Tüm zamanlar)
                        # Eğer 4 saatlik veya Günlük RSI 80'in üzerindeyse çok riskli, sinyal atma.
                        if d4h['rsi'] > 85 or d1d['rsi'] > 85: continue

                        # --- RAPOR OLUŞTUR ---
                        coin_ismi = symbol.split('/')[0]
                        
                        mesaj = (
                            f"🐋 DETAYLI BALİNA RAPORU! 🚨\n\n"
                            f"💎 **{coin_ismi}** ({d15m['close']} $)\n"
                            f"🧱 **Durum:** {duvar_mesaji}\n\n"
                            
                            f"⚡ **15 Dakika (Kıvılcım):**\n"
                            f"   • Hacim: {round(d15m['vol_kat'], 1)} KAT 🚀\n"
                            f"   • Değişim: %{round(d15m['price_change'], 2)}\n"
                            f"   • RSI: {round(d15m['rsi'], 1)}\n\n"
                            
                            f"🕰️ **GENEL TREND (Büyük Resim):**\n"
                            f"   • 1 Saatlik RSI: {round(d1h['rsi'], 1)}\n"
                            f"   • 4 Saatlik RSI: {round(d4h['rsi'], 1)}\n"
                            f"   • Günlük RSI: {round(d1d['rsi'], 1)}\n\n"
                            
                            f"🧠 **KOMUTAN YORUMU:**\n"
                        )
                        
                        # Yorum Ekle
                        if duvar_var:
                            mesaj += "Hacim patladı ama fiyat baskılanıyor. Duvarın kırılmasını bekle! (Riskli) 🛑"
                        elif d4h['rsi'] < 40:
                            mesaj += "Uzun vade diplerde, bu hacim yükselişin habercisi olabilir! (Fırsat) ✅"
                        elif d1h['vol_kat'] > 3:
                            mesaj += "Hem 15dk hem 1 saatlikte hacim var. Hareket güçlü! 🔥"
                        else:
                            mesaj += "Kısa vadeli bir 'Vur-Kaç' hareketi olabilir. Dikkatli ol. 🛡️"

                        # Gönder
                        bot.send_message(CHAT_ID, mesaj, parse_mode='Markdown')
                        
                        # Kaydet
                        sinyali_kaydet(symbol, d15m['close'], "GENERAL_ANALYSIS", f"Vol:{round(d15m['vol_kat'],1)}")
                        
                        # Susturucuya Ekle (Şimdiki zamanı kaydet)
                        sinyal_gecmisi[symbol] = time.time()
                        
                        time.sleep(5) # Telegram spam önlemi

                except Exception as inner_e:
                    continue
            
            print("💤 Tur tamamlandı. 2 dakika mola...")
            # Susturucu listesini temizle (Çok şişmesin diye, 24 saatten eskileri sil)
            simdi = time.time()
            birlestirilecek = {k: v for k, v in sinyal_gecmisi.items() if simdi - v < 86400}
            sinyal_gecmisi = birlestirilecek
            
            time.sleep(120)

        except Exception as e:
            print(f"Genel Hata: {e}")
            time.sleep(30)

if __name__ == "__main__":
    keskin_nisanci_goreve()
