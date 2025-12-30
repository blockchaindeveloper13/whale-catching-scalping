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

# BAĞLANTILAR (ARTIK ŞİFRELİ VE YETKİLİ)
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
        
        # --- 1. FUTURES İSTİHBARATI (ARTIK RESMİ YOLLA) ---
        # Anahtar olduğu için artık 'Public' değil 'Private' kapıdan da girebiliriz
        # Ama veri public olduğu için CCXT bunu yetkili şekilde çekecektir.
        
        # YÖNTEM A: Top Trader Ratio (En Değerlisi)
        try:
            ls_data = exchange_futures.fetch_global_long_short_account_ratio(clean_symbol, '15m', 1)
            # Not: CCXT sürümüne göre metod ismi değişebilir, o yüzden 
            # aşağıda 'implicit' (doğrudan) metodları deneyeceğiz.
        except:
            ls_data = None

        # YÖNTEM B: Implicit API Metodu (Daha Garanti)
        if not ls_data:
            try:
                # API Key olduğu için artık request başlıklarını CCXT hazırlar
                # topLongShortAccountRatio endpoint'i
                ls_data = exchange_futures.fapiDataGetTopLongShortAccountRatio({
                    'symbol': clean_symbol,
                    'period': '15m',
                    'limit': 1
                })
            except:
                try:
                    # Yedeğin yedeği: Global Ratio
                    ls_data = exchange_futures.fapiDataGetGlobalLongShortAccountRatio({
                        'symbol': clean_symbol,
                        'period': '15m',
                        'limit': 1
                    })
                except Exception as e:
                    # print(f"⚠️ {symbol} Veri Yok: {e}") 
                    return None

        if not ls_data: return None
        
        # Gelen veri liste mi tek obje mi kontrolü
        if isinstance(ls_data, list):
            data_item = ls_data[0]
        else:
            data_item = ls_data

        long_pct = float(data_item['longAccount']) * 100
        short_pct = float(data_item['shortAccount']) * 100
        
        # Open Interest
        try:
            oi_data = exchange_futures.fetch_open_interest(clean_symbol)
            open_interest = float(oi_data['openInterestAmount'])
            funding = exchange_futures.fetch_funding_rate(clean_symbol)
            funding_rate = funding['fundingRate'] * 100
        except:
            open_interest = 0
            funding_rate = 0

        # --- 2. SPOT İSTİHBARATI ---
        bars = exchange_spot.fetch_ohlcv(symbol, timeframe='15m', limit=50)
        df = pd.DataFrame(bars, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        vol_avg = df['v'].mean()
        vol_ratio = df['v'].iloc[-1] / vol_avg if vol_avg > 0 else 0

        return {
            'long_pct': long_pct,
            'short_pct': short_pct,
            'open_interest': open_interest,
            'funding': funding_rate,
            'rsi': rsi.iloc[-1],
            'vol_ratio': vol_ratio,
            'price': df['close'].iloc[-1]
        }
    except Exception as e:
        # print(f"❌ HATA ({symbol}): {e}")
        return None

def general_tarama():
    bot.send_message(CHAT_ID, "🎖️ KOMUTANIM! Radar v14 (RESMİ API MODU) Devrede!\n🔑 Kimlik: Onaylı\n🎯 Hedef: Balinalar")
    
    YASAKLI_KELIMELER = ['UP/', 'DOWN/', 'BEAR', 'BULL', 'DAI', 'TUSD', 'USDC', 'USDP', 'FDUSD', 'EUR', 'PAXG']

    while True:
        print("🔄 Tarama Başlıyor (API Key Aktif)...")
        try:
            tickers = exchange_spot.fetch_tickers()
            sorted_tickers = sorted(tickers.items(), key=lambda x: x[1]['quoteVolume'], reverse=True)
            
            hedef_liste = [
                t[0] for t in sorted_tickers 
                if '/USDT' in t[0] 
                and not any(x in t[0] for x in YASAKLI_KELIMELER)
            ][:40]
            
            print(f"🎯 Hedef: {len(hedef_liste)} Coin")
            
            for symbol in hedef_liste:
                # API Key olduğu için limitler daha geniştir ama yine de nazik olalım
                time.sleep(0.2) 
                
                data = get_analysis_data(symbol)
                if not data: continue
                
                RAPOR_VAR = False
                SEBEP = ""
                ICON = ""
                YORUM = ""
                
                # KRİTERLER
                if data['long_pct'] > 60:
                    RAPOR_VAR = True
                    SEBEP = f"LONGLAR YIĞILDI (%{data['long_pct']:.1f})"
                    ICON = "⚠️"
                    YORUM = "Tuzak Olabilir (Long Squeeze Risk)!"
                elif data['short_pct'] > 60:
                    RAPOR_VAR = True
                    SEBEP = f"SHORTLAR YIĞILDI (%{data['short_pct']:.1f})"
                    ICON = "🚀"
                    YORUM = "Patlama Olabilir (Short Squeeze Fırsat)!"
                
                clean_sym = symbol.replace('/','')
                prev_oi = OI_HAFIZA.get(clean_sym, data['open_interest'])
                if clean_sym not in OI_HAFIZA: oi_degisim = 0
                else: oi_degisim = ((data['open_interest'] - prev_oi) / prev_oi) * 100
                OI_HAFIZA[clean_sym] = data['open_interest']
                
                if abs(oi_degisim) > 3.0: 
                    RAPOR_VAR = True 
                    SEBEP = f"OI PATLAMASI (%{oi_degisim:.1f})"
                    ICON = "🐳"
                    if not YORUM: YORUM = "Para Girişi Var!"

                if RAPOR_VAR:
                    mesaj = (
                        f"🐋 **GENELKURMAY RAPORU** {ICON}\n"
                        f"🚨 **ALARM:** {SEBEP}\n\n"
                        f"💎 **{symbol}** ({data['price']} $)\n"
                        f"📊 **Futures:** Long %{data['long_pct']:.1f} | Short %{data['short_pct']:.1f}\n"
                        f"💰 **Fonlama:** %{data['funding']:.4f}\n"
                        f"🌊 **Spot:** RSI {data['rsi']:.1f} | Hacim {data['vol_ratio']:.1f}x\n\n"
                        f"🧠 **YORUM:** {YORUM}"
                    )
                    bot.send_message(CHAT_ID, mesaj, parse_mode='Markdown')
                    time.sleep(1)

            print("💤 Mola...")
            time.sleep(120)

        except Exception as e:
            print(f"Döngü Hatası: {e}")
            time.sleep(30)

if __name__ == "__main__":
    general_tarama()
