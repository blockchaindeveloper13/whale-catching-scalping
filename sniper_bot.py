import ccxt
import time
import telebot
import os
import pandas as pd
from datetime import datetime

# --- AYARLAR ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# API ANAHTARLARINI HEROKU'DAN ÇEK
API_KEY = os.environ.get('BINANCE_API_KEY')
API_SECRET = os.environ.get('BINANCE_SECRET_KEY')

# BAĞLANTILAR
exchange_spot = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True
})

exchange_futures = ccxt.binance({
    'apiKey': API_KEY,
    'secret': API_SECRET,
    'options': {'defaultType': 'future'},
    'enableRateLimit': True
})

bot = telebot.TeleBot(BOT_TOKEN)
OI_HAFIZA = {} 

def get_analysis_data(symbol):
    try:
        clean_symbol = symbol.replace('/', '')
        
        # --- 1. SPOT İSTİHBARATI (TEMEL) ---
        try:
            bars = exchange_spot.fetch_ohlcv(symbol, timeframe='15m', limit=50)
        except:
            return None 

        df = pd.DataFrame(bars, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        # Teknik Analiz
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        vol_avg = df['v'].mean()
        vol_ratio = df['v'].iloc[-1] / vol_avg if vol_avg > 0 else 0
        current_price = df['close'].iloc[-1]

        # --- 2. FUTURES İSTİHBARATI (VARSA) ---
        long_pct = 0
        short_pct = 0
        open_interest = 0
        funding_rate = 0
        has_futures = False

        try:
            # Futures verisi çekmeyi dene
            ls_data = exchange_futures.fapiDataGetTopLongShortAccountRatio({
                'symbol': clean_symbol,
                'period': '15m',
                'limit': 1
            })
            
            if ls_data:
                item = ls_data[0] if isinstance(ls_data, list) else ls_data
                long_pct = float(item['longAccount']) * 100
                short_pct = float(item['shortAccount']) * 100
                
                # OI ve Funding
                oi_data = exchange_futures.fetch_open_interest(clean_symbol)
                open_interest = float(oi_data['openInterestAmount'])
                funding = exchange_futures.fetch_funding_rate(clean_symbol)
                funding_rate = funding['fundingRate'] * 100
                has_futures = True
        except:
            has_futures = False

        return {
            'symbol': symbol,
            'price': current_price,
            'rsi': rsi.iloc[-1],
            'vol_ratio': vol_ratio,
            'has_futures': has_futures,
            'long_pct': long_pct,
            'short_pct': short_pct,
            'open_interest': open_interest,
            'funding': funding_rate
        }
    except Exception as e:
        return None

def general_tarama():
    bot.send_message(CHAT_ID, "🎖️ KOMUTANIM! Radar v16 (TAM SAHA PRES) Devrede!\n🌍 Kapsam: TÜM USDT Pariteleri\n🛡️ Filtre: Bull/Bear/Stable Yok\n🚀 Hedef: Okyanusun Tamamı")
    
    # KESKİN FİLTRE LİSTESİ
    YASAKLI_KELIMELER = [
        'UP/', 'DOWN/',       # Kaldıraçlı Tokenlar
        'BEAR', 'BULL',       # Eski tip ETF'ler
        'USDC', 'TUSD',       # Stabil Coinler
        'USDP', 'FDUSD', 
        'EUR', 'DAI', 'PAXG',
        'BUSD', 'USDE', 'USDD' 
    ]

    while True:
        print("🔄 Tüm Piyasa Taranıyor (Full Scan)...")
        try:
            tickers = exchange_spot.fetch_tickers()
            
            # --- FİLTRELEME MOTORU ---
            hedef_liste = []
            for symbol in tickers:
                # 1. Sadece USDT paritesi olsun
                if not symbol.endswith('/USDT'):
                    continue
                
                # 2. Yasaklı kelimeler geçmesin
                if any(yasak in symbol for yasak in YASAKLI_KELIMELER):
                    continue
                
                hedef_liste.append(symbol)
            
            print(f"🎯 Hedef: {len(hedef_liste)} Adet Coin Taranıyor...")
            
            # Tüm listeyi tara
            for symbol in hedef_liste:
                # Listemiz çok kalabalık (300+ coin), ban yememek için nazik olalım
                time.sleep(0.15) 
                
                data = get_analysis_data(symbol)
                if not data: continue
                
                RAPOR_VAR = False
                SEBEP = ""
                ICON = ""
                YORUM = ""
                
                # --- SİNYAL ANALİZİ ---
                
                # A) SPOT SİNYALLERİ (ÖNCELİKLİ)
                if data['vol_ratio'] > 5.0: # Hacim 5 katına çıkmışsa (Çok güçlü sinyal)
                    RAPOR_VAR = True
                    SEBEP = f"SPOT HACİM PATLAMASI ({data['vol_ratio']:.1f}x)"
                    ICON = "🌊"
                    YORUM = "Devasa hacim girişi var! Dikkat!"
                
                elif data['rsi'] < 20: # Aşırı Satım (Dip)
                    RAPOR_VAR = True
                    SEBEP = f"AŞIRI DİP (RSI: {data['rsi']:.1f})"
                    ICON = "💎"
                    YORUM = "Fiyat çok ucuzladı, tepki gelebilir."

                # B) FUTURES SİNYALLERİ (Varsa)
                if data['has_futures']:
                    if data['long_pct'] > 65: 
                        RAPOR_VAR = True
                        if not SEBEP: SEBEP = f"LONG YIĞILMASI (%{data['long_pct']:.1f})"
                        else: YORUM += "\n⚠️ Futures tarafında Long tuzağı riski!"
                    
                    elif data['short_pct'] > 65:
                        RAPOR_VAR = True
                        if not SEBEP: SEBEP = f"SHORT YIĞILMASI (%{data['short_pct']:.1f})"
                        else: YORUM += "\n🚀 Futures tarafında Short Squeeze yakıtı!"

                    # OI Kontrolü
                    clean_sym = symbol.replace('/','')
                    prev_oi = OI_HAFIZA.get(clean_sym, data['open_interest'])
                    if clean_sym not in OI_HAFIZA: oi_degisim = 0
                    else: oi_degisim = ((data['open_interest'] - prev_oi) / prev_oi) * 100
                    OI_HAFIZA[clean_sym] = data['open_interest']
                    
                    if abs(oi_degisim) > 5.0:
                        RAPOR_VAR = True
                        if not SEBEP: 
                            SEBEP = f"OI PATLAMASI (%{oi_degisim:.1f})"
                            ICON = "🐳"

                # --- RAPORLAMA ---
                if RAPOR_VAR:
                    mesaj = (
                        f"🐋 **GENELKURMAY RAPORU** {ICON}\n"
                        f"🚨 **ALARM:** {SEBEP}\n\n"
                        f"💎 **{symbol}** ({data['price']} $)\n"
                    )
                    
                    if data['has_futures']:
                        mesaj += (
                            f"📊 **Futures:** L:%{data['long_pct']:.0f} S:%{data['short_pct']:.0f} | OI Artış: %{oi_degisim:.1f}\n"
                            f"💰 **Fonlama:** %{data['funding']:.4f}\n"
                        )
                    else:
                        mesaj += f"🚫 **Futures:** Yok (Sadece Spot)\n"
                        
                    mesaj += (
                        f"🌊 **Spot:** RSI {data['rsi']:.1f} | Hacim {data['vol_ratio']:.1f}x\n\n"
                        f"🧠 **YORUM:** {YORUM}"
                    )
                    
                    bot.send_message(CHAT_ID, mesaj, parse_mode='Markdown')
                    time.sleep(1) 

            print("💤 Tüm Liste Tarandı. Dinleniyor...")
            time.sleep(120)

        except Exception as e:
            print(f"Döngü Hatası: {e}")
            time.sleep(30)

if __name__ == "__main__":
    general_tarama()
