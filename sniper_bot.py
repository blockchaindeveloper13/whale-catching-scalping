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

# --- 2. YARDIMCI MOTORLAR ---

def calculate_rsi_from_df(df, period=14):
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

# Parabolic SAR
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

# Alıcı/Satıcı Baskısı
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

# --- 3. DETAYLI ANALİZ ---
def stratejik_analiz(symbol):
    try:
        # A) 15 Dakikalık Veri (Trend ve Anlık Durum için)
        bars_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=100)
        df_15m = pd.DataFrame(bars_15m, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        # B) 1 Saatlik Veri (Hacim ve RSI için)
        bars_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=72) # 3 gün geriye
        df_1h = pd.DataFrame(bars_1h, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        # C) 4 Saatlik Veri (RSI için)
        bars_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=30)
        df_4h = pd.DataFrame(bars_4h, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        # D) Günlük Veri (RSI için)
        bars_1d = exchange.fetch_ohlcv(symbol, timeframe='1d', limit=30)
        df_1d = pd.DataFrame(bars_1d, columns=['t', 'o', 'h', 'l', 'c', 'v'])

        # --- HESAPLAMALAR ---
        
        # 1. RSI HESAPLAMALARI (Çoklu Zaman)
        rsi_15m = calculate_rsi_from_df(df_15m)
        rsi_1h = calculate_rsi_from_df(df_1h)
        rsi_4h = calculate_rsi_from_df(df_4h)
        rsi_1d = calculate_rsi_from_df(df_1d)

        # 2. TREND (EMA & SAR - 15m)
        ema50 = df_15m['close'].ewm(span=50, adjust=False).mean().iloc[-1]
        fiyat = df_15m['close'].iloc[-1]
        sar_series, trend_yonu = calculate_sar(df_15m['h'], df_15m['l'])
        
        ana_yon = "YUKARI 🚀" if fiyat > ema50 else "AŞAĞI 🔻"
        if trend_yonu == -1: ana_yon = "AŞAĞI 🔻"

        # 3. SAHA DURUMU
        baski_durumu, baski_puani = analyze_dominance(df_15m)

        # 4. HACİM DERİNLİĞİ (3 Günlük Ortalamaya Göre)
        vol_3day_avg = df_1h['v'].mean()
        if vol_3day_avg == 0: vol_3day_avg = 1
        
        vol_1h = df_1h['v'].iloc[-1]
        vol_4h = df_1h['v'].iloc[-4:].sum() / 4
        vol_12h = df_1h['v'].iloc[-12:].sum() / 12
        
        kat_1h = vol_1h / vol_3day_avg
        kat_4h = vol_4h / vol_3day_avg
        kat_12h = vol_12h / vol_3day_avg

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
            'kat_12h': round(kat_12h, 1),
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

    bot.send_message(CHAT_ID, "🎖️ KOMUTANIM! Radar v9.5 Devrede. ÇOKLU RSI ve DERİN ANALİZ Başladı! 🔭")
    
    YASAKLI = ['USDC', 'FDUSD', 'TUSD', 'USDP', 'EUR', 'DAI', 'AEUR', 'USDE']

    while True:
        try:
            print("🔄 Genelkurmay Analizi (v9.5)...")
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
                # 1. SUSTURUCU
                if symbol in sinyal_gecmisi:
                    if time.time() - sinyal_gecmisi[symbol] < 3600: continue
                
                try:
                    # 2. HIZLI ELEME (Noise Filter)
                    bars = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=5)
                    if not bars: continue
                    vol = [x[5] for x in bars]
                    # Hacim artışı yoksa hiç detaya girme
                    if vol[-1] < (sum(vol[:-1])/4) * 2.0: continue 

                    # 3. DETAYLI STRATEJİK ANALİZ
                    veri = stratejik_analiz(symbol)
                    if not veri: continue
                    
                    # --- FİLTRELER ---
                    trend_onayi = (veri['fiyat'] > veri['ema50'])
                    dip_firsati = (veri['rsi_15m'] < 35) or (veri['rsi_4h'] < 35) # 4 Saatlik dip de önemli
                    
                    if not (trend_onayi or dip_firsati): continue
                    if veri['kat_4h'] < 1.5: continue

                    # --- RAPORLAMA ---
                    coin_ismi = symbol.split('/')[0]
                    
                    mesaj = (
                        f"🐋 **GENELKURMAY RAPORU v9.5** 🚨\n\n"
                        f"💎 **{coin_ismi}** ({veri['fiyat']} $)\n"
                        f"🧭 **Trend:** {veri['ana_yon']}\n"
                        f"⚔️ **Saha:** {veri['baski_durumu']} (%{veri['baski_puani']})\n\n"
                        
                        f"📊 **HACİM İSTİHBARATI:**\n"
                        f"   • 1 Saatlik: {veri['kat_1h']} KAT 📈\n"
                        f"   • 4 Saatlik: {veri['kat_4h']} KAT\n\n"
                        
                        f"🌡️ **RSI RADARI (Çoklu Zaman):**\n"
                        f"   • 15 Dakika: {veri['rsi_15m']}\n"
                        f"   • 1 Saat: {veri['rsi_1h']}\n"
                        f"   • 4 Saat: {veri['rsi_4h']}\n"
                        f"   • GÜNLÜK: {veri['rsi_1d']}\n\n"
                        
                        f"🧠 **KOMUTAN YORUMU:**\n"
                    )
                    
                    # AKILLI YORUM SİSTEMİ
                    if veri['rsi_1d'] > 85:
                        mesaj += "⚠️ DİKKAT: Günlükte çok şişmiş! Büyük düşüş riski var. Sadece vur-kaç yap! 🛑"
                    elif veri['rsi_4h'] < 30:
                        mesaj += "✅ FIRSAT: 4 Saatlikte DİPTE! Dönüş başlarsa büyük kazandırır. 🎣"
                    elif veri['baski_puani'] > 70 and veri['ana_yon'] == "YUKARI 🚀":
                        mesaj += "🔥 SALDIRI: Alıcılar baskın, trend yukarı, RSI makul. Tam hedef! 🎯"
                    elif "AŞAĞI" in veri['ana_yon']:
                        mesaj += "🛡️ DEFANS: Hacim var ama trend henüz dönmedi. Takipte kal."
                    else:
                        mesaj += "Trend pozitif, hacim destekli. İzlemeye al! ✅"

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
        
