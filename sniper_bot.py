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

# A) DENİZ KUVVETLERİ (SPOT PİYASA)
exchange_spot = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'spot'} 
})

# B) HAVA KUVVETLERİ (FUTURES PİYASA - Sadece Bilgi İçin)
exchange_futures = ccxt.binance({
    'enableRateLimit': True,
    'options': {'defaultType': 'future'} 
})

bot = telebot.TeleBot(BOT_TOKEN)

# --- 2. YARDIMCI MOTORLAR ---

def calculate_rsi_from_df(df, period=14):
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

def calculate_sar(high, low, af_step=0.02, af_max=0.2):
    sar = [0] * len(high)
    trend = [0] * len(high) 
    af = af_step
    ep = high[0]
    sar[0] = low[0]
    trend[0] = 1
    for i in range(1, len(high)):
        prev_sar = sar[i-1]
        if trend[i-1] == 1: 
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = min(sar[i], low[i-1])
            if i > 1: sar[i] = min(sar[i], low[i-2])
            if low[i] < sar[i]: 
                trend[i] = -1
                sar[i] = ep
                ep = low[i]
                af = af_step
            else:
                trend[i] = 1
                if high[i] > ep:
                    ep = high[i]
                    af = min(af + af_step, af_max)
        else: 
            sar[i] = prev_sar + af * (ep - prev_sar)
            sar[i] = max(sar[i], high[i-1])
            if i > 1: sar[i] = max(sar[i], high[i-2])
            if high[i] > sar[i]: 
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

def analyze_dominance(df):
    close = df['close'].iloc[-1]
    low = df['low'].iloc[-1]
    high = df['high'].iloc[-1]
    total_range = high - low
    if total_range == 0: return "Nötr", 50
    buying_power = close - low
    score = (buying_power / total_range) * 100
    if score > 70: return "ALICILAR BASKIN 🟢", score
    elif score < 30: return "SATICILAR BASKIN 🔴", score
    else: return "Çekişmeli / Nötr ⚪", score

# --- YENİ EKLENTİ: HAVA İSTİHBARATI (FUTURES) ---
def get_futures_intel(symbol):
    try:
        # Symbol formatını düzelt (BTC/USDT -> BTCUSDT) çünkü futures API bazen böyle ister
        clean_symbol = symbol.replace('/', '')
        
        # 1. Long/Short Ratio (En Kritik Veri)
        # Binance API'den "Global Long/Short Ratio" çekiyoruz
        ls_data = exchange_futures.fapiPublic_get_global_longshortaccountratio({
            'symbol': clean_symbol,
            'period': '5m',
            'limit': 1
        })
        
        # 2. Funding Rate
        funding = exchange_futures.fetch_funding_rate(symbol)
        
        long_pct = float(ls_data[0]['longAccount']) * 100
        short_pct = float(ls_data[0]['shortAccount']) * 100
        ratio = float(ls_data[0]['longShortRatio'])
        f_rate = funding['fundingRate'] * 100
        
        return {
            'long_pct': round(long_pct, 1),
            'short_pct': round(short_pct, 1),
            'ratio': ratio,
            'funding': round(f_rate, 4)
        }
    except:
        return None # Futures verisi yoksa veya hata varsa boş dön

# --- 3. DETAYLI ANALİZ (SPOT) ---
def stratejik_analiz(symbol):
    try:
        # Veri çekme işlemleri (AYNI KALDI)
        bars_15m = exchange_spot.fetch_ohlcv(symbol, timeframe='15m', limit=100)
        df_15m = pd.DataFrame(bars_15m, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        bars_1h = exchange_spot.fetch_ohlcv(symbol, timeframe='1h', limit=72)
        df_1h = pd.DataFrame(bars_1h, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        bars_4h = exchange_spot.fetch_ohlcv(symbol, timeframe='4h', limit=30)
        df_4h = pd.DataFrame(bars_4h, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        bars_1d = exchange_spot.fetch_ohlcv(symbol, timeframe='1d', limit=30)
        df_1d = pd.DataFrame(bars_1d, columns=['t', 'o', 'h', 'l', 'c', 'v'])

        # Hesaplamalar (AYNI KALDI)
        rsi_15m = calculate_rsi_from_df(df_15m)
        rsi_1h = calculate_rsi_from_df(df_1h)
        rsi_4h = calculate_rsi_from_df(df_4h)
        rsi_1d = calculate_rsi_from_df(df_1d)

        ema50 = df_15m['close'].ewm(span=50, adjust=False).mean().iloc[-1]
        fiyat = df_15m['close'].iloc[-1]
        sar_series, trend_yonu = calculate_sar(df_15m['h'], df_15m['l'])
        
        ana_yon = "YUKARI 🚀" if fiyat > ema50 else "AŞAĞI 🔻"
        if trend_yonu == -1: ana_yon = "AŞAĞI 🔻"

        baski_durumu, baski_puani = analyze_dominance(df_15m)

        vol_3day_avg = df_1h['v'].mean()
        if vol_3day_avg == 0: vol_3day_avg = 1
        
        vol_1h = df_1h['v'].iloc[-1]
        vol_4h = df_1h['v'].iloc[-4:].sum() / 4
        
        kat_1h = vol_1h / vol_3day_avg
        kat_4h = vol_4h / vol_3day_avg

        return {
            'fiyat': fiyat,
            'ana_yon': ana_yon,
            'ema50': ema50,
            'baski_durumu': baski_durumu,
            'baski_puani': round(baski_puani, 1),
            'rsi_15m': round(rsi_15m, 1),
            'rsi_1h': round(rsi_1h, 1),
            'rsi_4h': round(rsi_4h, 1),
            'rsi_1d': round(rsi_1d, 1),
            'kat_1h': round(kat_1h, 1),
            'kat_4h': round(kat_4h, 1),
            'degisim_15m': round(((fiyat - df_15m['o'].iloc[-1])/df_15m['o'].iloc[-1])*100, 2)
        }

    except Exception as e:
        return None

# --- 4. ANA OPERASYON ---
def keskin_nisanci_goreve():
    sinyal_gecmisi = {} 
    
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS istihbarat (id SERIAL PRIMARY KEY, coin VARCHAR(20));")
        conn.commit()
    except:
        pass

    bot.send_message(CHAT_ID, "🎖️ KOMUTANIM! Radar v10 (HAVA+DENİZ) Devrede! Masrafsız Entegrasyon Tamam. 🚁🚢")
    
    YASAKLI = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'EUR', 'DAI', 'AEUR', 'USDE']

    while True:
        try:
            print("🔄 Genelkurmay Analizi (v10)...")
            markets = exchange_spot.load_markets()
            
            hedefler = [
                s for s in markets 
                if s.endswith('/USDT') 
                and markets[s]['active']
                and not any(x in s for x in ['UP/', 'DOWN/', 'BULL/', 'BEAR/'])
                and s.split('/')[0] not in YASAKLI
            ]
            
            random.shuffle(hedefler)
            
            for symbol in hedefler:
                if symbol in sinyal_gecmisi:
                    if time.time() - sinyal_gecmisi[symbol] < 3600: continue
                
                try:
                    # HIZLI ELEME (Filtreleri Gevşettik mi? Hayır, standart koruma)
                    bars = exchange_spot.fetch_ohlcv(symbol, timeframe='1h', limit=5)
                    if not bars: continue
                    vol = [x[5] for x in bars]
                    if vol[-1] < (sum(vol[:-1])/4) * 2.0: continue 

                    # DETAYLI SPOT ANALİZİ
                    veri = stratejik_analiz(symbol)
                    if not veri: continue
                    
                    trend_onayi = (veri['fiyat'] > veri['ema50'])
                    dip_firsati = (veri['rsi_15m'] < 35) or (veri['rsi_4h'] < 35)
                    
                    if not (trend_onayi or dip_firsati): continue
                    
                    # 🔥 BURASI YENİ: Futures İstihbaratını SADECE sinyal varsa çağırıyoruz
                    futures_veri = get_futures_intel(symbol)
                    
                    # --- RAPORLAMA ---
                    coin_ismi = symbol.split('/')[0]
                    
                    mesaj = (
                        f"🐋 **GENELKURMAY RAPORU v10** 🚨\n\n"
                        f"💎 **{coin_ismi}** ({veri['fiyat']} $)\n"
                        f"🧭 **Trend:** {veri['ana_yon']}\n"
                        f"📊 **Hacim:** 1H: {veri['kat_1h']}x | 4H: {veri['kat_4h']}x\n"
                        f"🌡️ **Günlük RSI:** {veri['rsi_1d']} (Genel Yön)\n\n"
                    )
                    
                    # FİNAL KOMUTAN YORUMU (HAVA DESTEKLİ)
                    hava_yorumu = ""
                    if futures_veri:
                        mesaj += (
                            f"✈️ **HAVA SAHASI (Futures):**\n"
                            f"   • Longlar: %{futures_veri['long_pct']} 🟢\n"
                            f"   • Shortlar: %{futures_veri['short_pct']} 🔴\n"
                            f"   • Funding: %{futures_veri['funding']}\n\n"
                        )
                        
                        if futures_veri['long_pct'] > 75:
                            hava_yorumu = "⚠️ DİKKAT: Herkes Long açmış! Tuzak olabilir."
                        elif futures_veri['short_pct'] > 75:
                            hava_yorumu = "🚀 FIRSAT: Short Squeeze (Patlama) ihtimali yüksek!"
                        else:
                            hava_yorumu = "✅ Hava sahası dengeli."
                    
                    mesaj += f"🧠 **KOMUTAN KARARI:**\nSpot: Alıcı baskın (%{veri['baski_puani']}).\n{hava_yorumu} Takip et! 🛡️"

                    bot.send_message(CHAT_ID, mesaj, parse_mode='Markdown')
                    
                    sinyal_gecmisi[symbol] = time.time()
                    time.sleep(4)

                except:
                    continue
            
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
            
