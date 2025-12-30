import ccxt
import time
import telebot
import os
import psycopg2
import pandas as pd
import numpy as np
import random
from datetime import datetime

# --- 1. AYARLAR ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
DATABASE_URL = os.environ.get('DATABASE_URL')

exchange = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'} 
})

bot = telebot.TeleBot(BOT_TOKEN)

# --- 2. HESAPLAMA MOTORLARI ---

# Parabolic SAR Hesaplama Fonksiyonu (Manuel)
def calculate_sar(high, low, af_step=0.02, af_max=0.2):
    # Basit bir SAR simülasyonu
    sar = [0] * len(high)
    trend = [0] * len(high) # 1: Up, -1: Down
    af = af_step
    ep = high[0]
    sar[0] = low[0]
    trend[0] = 1
    
    for i in range(1, len(high)):
        prev_sar = sar[i-1]
        if trend[i-1] == 1: # Uptrend
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = min(sar[i], low[i-1])
            if i > 1: sar[i] = min(sar[i], low[i-2])
            
            if low[i] < sar[i]: # Trend değişimi (Aşağı)
                trend[i] = -1
                sar[i] = ep
                ep = low[i]
                af = af_step
            else:
                trend[i] = 1
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_step, af_max)
        else: # Downtrend
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = max(sar[i], high[i-1])
            if i > 1: sar[i] = max(sar[i], high[i-2])
            
            if high[i] > sar[i]: # Trend değişimi (Yukarı)
                trend[i] = 1
                sar[i] = ep
                ep = high[i]
                af = af_step
            else:
                trend[i] = -1
                if low[i] < ep:
                    ep = low[i]
                    af = min(af + af_step, af_max)
    return pd.Series(sar, index=high.index), trend[-1]

# Alıcı/Satıcı Baskısı (Mum Analizi)
def analyze_dominance(df):
    # Son mumun verileri
    close = df['close'].iloc[-1]
    open_p = df['open'].iloc[-1]
    high = df['high'].iloc[-1]
    low = df['low'].iloc[-1]
    
    # Mumun toplam boyu
    total_range = high - low
    if total_range == 0: return "Nötr", 50
    
    # Alıcı gücü: Kapanışın Low'a uzaklığı
    buying_power = close - low
    
    # Yüzdesel Güç (0-100)
    score = (buying_power / total_range) * 100
    
    if score > 70: return "ALICILAR BASKIN 🟢", score
    elif score < 30: return "SATICILAR BASKIN 🔴", score
    else: return "Çekişmeli / Nötr ⚪", score

# --- 3. DETAYLI ANALİZ ---
def stratejik_analiz(symbol):
    rapor = {}
    timeframes = ['15m', '1h', '4h', '12h'] # 12 Saatlik eklendi
    
    try:
        # 1. TEMEL VERİLERİ ÇEK
        # 15 Dakikalık Veri (Son 100 mum - Trend ve SAR için lazım)
        bars_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
        df_15m = pd.DataFrame(bars_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # 2. TREND ANALİZİ (EMA & SAR) - 15 Dakikalık Üzerinden
        # EMA 50
        ema50 = df_15m['close'].ewm(span=50, adjust=False).mean().iloc[-1]
        fiyat = df_15m['close'].iloc[-1]
        
        # SAR
        sar_series, trend_yonu = calculate_sar(df_15m['high'], df_15m['low'])
        sar_degeri = sar_series.iloc[-1]
        
        # Yön Tayini
        ana_yon = "YUKARI 🚀" if fiyat > ema50 else "AŞAĞI 🔻"
        if trend_yonu == -1: ana_yon = "AŞAĞI 🔻" # SAR Sat veriyorsa negatiftir.
        
        # Alıcı/Satıcı Durumu
        baski_durumu, baski_puani = analyze_dominance(df_15m)
        
        # 3. HACİM ANALİZİ (Çoklu Zaman & 3 Günlük Geçmiş)
        # 3 Gün = 72 Saat. 
        # 1 Saatlik mumlarla 3 gün geriye gidelim (72 mum)
        bars_long = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=72)
        df_long = pd.DataFrame(bars_long, columns=['t', 'o', 'h', 'l', 'c', 'volume'])
        
        # 3 Günlük Ortalama Hacim
        vol_3day_avg = df_long['volume'].mean()
        if vol_3day_avg == 0: vol_3day_avg = 1
        
        # Anlık Hacimlerin Ortalamaya Oranı
        vol_1h = df_long['volume'].iloc[-1]
        vol_4h = df_long['volume'].iloc[-4:].sum() / 4 # Son 4 saatin ortalaması
        vol_12h = df_long['volume'].iloc[-12:].sum() / 12 # Son 12 saatin ortalaması
        
        kat_1h = vol_1h / vol_3day_avg
        kat_4h = vol_4h / vol_3day_avg
        kat_12h = vol_12h / vol_3day_avg

        # RSI Hesapla (15m)
        delta = df_15m['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_val = rsi.iloc[-1]

        # VERİLERİ PAKETLE
        return {
            'fiyat': fiyat,
            'ana_yon': ana_yon,
            'ema50': ema50,
            'baski_durumu': baski_durumu,
            'baski_puani': round(baski_puani, 1),
            'rsi': round(rsi_val, 2),
            'kat_1h': round(kat_1h, 1),
            'kat_4h': round(kat_4h, 1),
            'kat_12h': round(kat_12h, 1),
            'degisim_15m': round(((fiyat - df_15m['open'].iloc[-1])/df_15m['open'].iloc[-1])*100, 2)
        }

    except Exception as e:
        return None

# --- 4. ANA OPERASYON ---
def keskin_nisanci_goreve():
    sinyal_gecmisi = {} 
    
    # DB Bağlantısı (Varsa)
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS istihbarat (id SERIAL PRIMARY KEY, coin VARCHAR(20));")
        conn.commit()
    except:
        pass

    bot.send_message(CHAT_ID, "🎖️ KOMUTANIM! Radar v9.0 Devrede. SAR, EMA ve Derinlik Analizi Başladı! Gürültü Kesildi. 🔇")
    
    YASAKLI = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'EUR', 'DAI', 'AEUR', 'USDE']

    while True:
        try:
            print("🔄 Genelkurmay Analizi Başlıyor...")
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
                # 1. SUSTURUCU (1 Saat)
                if symbol in sinyal_gecmisi:
                    if time.time() - sinyal_gecmisi[symbol] < 3600: continue
                
                try:
                    # 2. HIZLI ELEME (Gürültüyü Burası Kesecek)
                    # Sadece 15m Hacmine değil, 1 Saatlik Hacme de bakıyoruz.
                    bars = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=5)
                    if not bars: continue
                    vol = [x[5] for x in bars]
                    # Son 1 saatlik hacim, önceki 4 saatin ortalamasından en az 2 kat büyük olmalı.
                    # Yoksa 15 dakikalık "fake" yükselişleri eleriz.
                    if vol[-1] < (sum(vol[:-1])/4) * 2.0:
                        continue 

                    # 3. DETAYLI STRATEJİK ANALİZ
                    veri = stratejik_analiz(symbol)
                    if not veri: continue
                    
                    # --- FİLTRELER (GÜRÜLTÜ ÖNLEYİCİ) ---
                    
                    # Kural 1: Yön kesinlikle YUKARI olmalı (EMA üstü) VEYA RSI çok dipte (Fırsat) olmalı.
                    trend_onayi = (veri['fiyat'] > veri['ema50'])
                    dip_firsati = (veri['rsi'] < 35)
                    
                    if not (trend_onayi or dip_firsati): continue
                    
                    # Kural 2: Hacim en az 4 saatlikte de kıpırdamış olmalı (Saman alevi olmasın)
                    if veri['kat_4h'] < 1.5: continue

                    # --- RAPORLAMA ---
                    coin_ismi = symbol.split('/')[0]
                    
                    # Mesaj İkonu Trende Göre
                    ikon = "🟢" if "YUKARI" in veri['ana_yon'] else "🔴"
                    
                    mesaj = (
                        f"🐋 **GENELKURMAY RAPORU v9** 🚨\n\n"
                        f"💎 **{coin_ismi}** ({veri['fiyat']} $)\n"
                        f"🧭 **Trend Yönü:** {veri['ana_yon']}\n"
                        f"⚔️ **Saha Durumu:** {veri['baski_durumu']} (%{veri['baski_puani']})\n\n"
                        
                        f"📊 **HACİM İSTİHBARATI (3 Günlük Ort. Göre):**\n"
                        f"   • 1 Saatlik: {veri['kat_1h']} KAT 📈\n"
                        f"   • 4 Saatlik: {veri['kat_4h']} KAT\n"
                        f"   • 12 Saatlik: {veri['kat_12h']} KAT\n\n"
                        
                        f"📉 **TEKNİK GÖSTERGELER:**\n"
                        f"   • RSI (15m): {veri['rsi']}\n"
                        f"   • 15dk Değişim: %{veri['degisim_15m']}\n\n"
                        
                        f"🧠 **KOMUTAN YORUMU:**\n"
                    )
                    
                    if veri['baski_puani'] > 70 and veri['kat_4h'] > 3:
                        mesaj += "Alıcılar çok baskın ve hacim 4 saate yayılmış. Bu gerçek bir yükseliş! 🔥"
                    elif dip_firsati:
                        mesaj += "Fiyat baskılanmış ama hacim giriyor. Dönüş sinyali olabilir! ✅"
                    elif "AŞAĞI" in veri['ana_yon']:
                        mesaj += "Hacim var ama trend hala aşağı. EMA'yı kırmasını bekle. (Riskli) ⚠️"
                    else:
                        mesaj += "Trend pozitif, hacim destekli. İzlemeye al! 🛡️"

                    bot.send_message(CHAT_ID, mesaj, parse_mode='Markdown')
                    
                    sinyal_gecmisi[symbol] = time.time()
                    time.sleep(4)

                except:
                    continue
            
            # Listeyi Temizle
            simdi = time.time()
            yeni_liste = {k:v for k,v in sinyal_gecmisi.items() if simdi-v < 86400}
            sinyal_gecmisi = yeni_liste
            
            print("💤 Tur bitti. Mola...")
            time.sleep(120)

        except Exception as e:
            print(f"Hata: {e}")
            time.sleep(30)

if __name__ == "__main__":
    keskin_nisanci_goreve()
