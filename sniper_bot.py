import ccxt
import time
import telebot
import os
import pandas as pd

# --- AYARLAR ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
API_KEY = os.environ.get('BINANCE_API_KEY')
API_SECRET = os.environ.get('BINANCE_SECRET_KEY')

# BAĞLANTILAR
exchange_spot = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {'defaultType': 'spot'}, 'enableRateLimit': True
})
exchange_futures = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {'defaultType': 'future'}, 'enableRateLimit': True
})

bot = telebot.TeleBot(BOT_TOKEN)
OI_HAFIZA = {} 

def get_analysis_data(symbol, is_top_40):
    try:
        clean_symbol = symbol.replace('/', '')
        
        # 1. SPOT İSTİHBARATI
        try:
            bars = exchange_spot.fetch_ohlcv(symbol, timeframe='15m', limit=50)
        except: return None 

        df = pd.DataFrame(bars, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        
        vol_avg = df['v'].mean()
        vol_ratio = df['v'].iloc[-1] / vol_avg if vol_avg > 0 else 0
        current_price = df['close'].iloc[-1]

        # 2. FUTURES İSTİHBARATI
        long_pct = 0; short_pct = 0; open_interest = 0; funding_rate = 0; has_futures = False

        if is_top_40:
            try:
                ls_data = exchange_futures.fapiDataGetTopLongShortAccountRatio({
                    'symbol': clean_symbol, 'period': '15m', 'limit': 1
                })
                if ls_data:
                    item = ls_data[0] if isinstance(ls_data, list) else ls_data
                    long_pct = float(item['longAccount']) * 100
                    short_pct = float(item['shortAccount']) * 100
                    
                    oi_data = exchange_futures.fetch_open_interest(clean_symbol)
                    open_interest = float(oi_data['openInterestAmount'])
                    funding = exchange_futures.fetch_funding_rate(clean_symbol)
                    funding_rate = funding['fundingRate'] * 100
                    has_futures = True
            except:
                has_futures = False
        
        return {
            'symbol': symbol, 'price': current_price,
            'rsi': rsi.iloc[-1], 'vol_ratio': vol_ratio,
            'has_futures': has_futures,
            'long_pct': long_pct, 'short_pct': short_pct,
            'open_interest': open_interest, 'funding': funding_rate
        }
    except: return None

def general_tarama():
    bot.send_message(CHAT_ID, "🎖️ KOMUTANIM! Radar v19 (AKTİF MOD) Devrede!\n🔍 Eşikler:\n• Hacim: >2.5x (Hassas)\n• RSI: <30 (Dip)\n• L/S: >%53\n🚀 Artık kuş uçsa haberimiz olacak!")
    
    YASAKLI = ['UP/', 'DOWN/', 'BEAR', 'BULL', 'USDC', 'TUSD', 'USDP', 'FDUSD', 'EUR', 'DAI', 'PAXG', 'BUSD', 'USDE', 'USDD']

    while True:
        print("🔄 Tarama Başlıyor (Aktif Mod)...")
        try:
            tickers = exchange_spot.fetch_tickers()
            sorted_tickers = sorted(tickers.items(), key=lambda x: x[1]['quoteVolume'], reverse=True)
            
            hedef_liste = []
            for t in sorted_tickers:
                if t[0].endswith('/USDT') and not any(x in t[0] for x in YASAKLI):
                    hedef_liste.append(t[0])
            
            print(f"🎯 Hedef: {len(hedef_liste)} Coin")
            
            for i, symbol in enumerate(hedef_liste):
                is_top_40 = (i < 40)
                if is_top_40: time.sleep(0.2)
                else: time.sleep(0.1)
                
                data = get_analysis_data(symbol, is_top_40)
                if not data: continue
                
                RAPOR_VAR = False; SEBEP = ""; ICON = ""; YORUM = ""
                
                # --- GEVŞETİLMİŞ FİLTRELER ---
                
                # A) SPOT
                if data['vol_ratio'] > 2.5: # 2.5 Kat Hacim Yeterli
                    RAPOR_VAR = True
                    SEBEP = f"HACİM HAREKETLİLİĞİ ({data['vol_ratio']:.1f}x)"
                    ICON = "🌊"
                    YORUM = "Ortalamanın üzerinde hacim girişi var."
                elif data['rsi'] < 30: # Standart Dip
                    RAPOR_VAR = True
                    SEBEP = f"DİP FIRSATI (RSI: {data['rsi']:.1f})"
                    ICON = "💎"
                    YORUM = "RSI 30'un altında, tepki gelebilir."

                # B) FUTURES (%53 Yeterli)
                if data['has_futures']:
                    if data['long_pct'] > 53: 
                        RAPOR_VAR = True
                        if not SEBEP: SEBEP = f"LONG AĞIRLIKLI (%{data['long_pct']:.1f})"
                        else: YORUM += "\n⚠️ Futures tarafı Longa dönüyor."
                    elif data['short_pct'] > 53:
                        RAPOR_VAR = True
                        if not SEBEP: SEBEP = f"SHORT AĞIRLIKLI (%{data['short_pct']:.1f})"
                        else: YORUM += "\n🚀 Futures tarafı Shorta dönüyor."

                    clean_sym = symbol.replace('/','')
                    prev_oi = OI_HAFIZA.get(clean_sym, data['open_interest'])
                    if clean_sym not in OI_HAFIZA: oi_degisim = 0
                    else: oi_degisim = ((data['open_interest'] - prev_oi) / prev_oi) * 100
                    OI_HAFIZA[clean_sym] = data['open_interest']
                    
                    if abs(oi_degisim) > 3.0: 
                        RAPOR_VAR = True
                        if not SEBEP: SEBEP = f"OI DEĞİŞİMİ (%{oi_degisim:.1f})"
                        ICON = "🐳"

                if RAPOR_VAR:
                    mesaj = (f"🐋 **RADAR TESPİTİ** {ICON}\n🚨 **TİP:** {SEBEP}\n\n💎 **{symbol}** ({data['price']} $)\n")
                    if data['has_futures']:
                        mesaj += (f"📊 **Futures:** L:%{data['long_pct']:.0f} S:%{data['short_pct']:.0f}\n")
                    mesaj += (f"🌊 **Spot:** RSI {data['rsi']:.1f} | Hacim {data['vol_ratio']:.1f}x")
                    
                    bot.send_message(CHAT_ID, mesaj, parse_mode='Markdown')
                    time.sleep(1)

            print("💤 Tur Bitti. Mola...")
            time.sleep(120)

        except Exception as e:
            print(f"Hata: {e}")
            time.sleep(30)

if __name__ == "__main__":
    general_tarama()
            
