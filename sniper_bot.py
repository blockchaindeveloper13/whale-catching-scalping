import ccxt
import time
import telebot
import os
import pandas as pd
import numpy as np

# --- AYARLAR ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
API_KEY = os.environ.get('BINANCE_API_KEY')
API_SECRET = os.environ.get('BINANCE_SECRET_KEY')

# BAĞLANTILAR (TIME SYNC EKLENDİ - SORUNSUZ)
exchange_spot = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {
        'defaultType': 'spot',
        'adjustForTimeDifference': True 
    },
    'enableRateLimit': True
})

exchange_futures = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {
        'defaultType': 'future',
        'adjustForTimeDifference': True
    },
    'enableRateLimit': True
})

bot = telebot.TeleBot(BOT_TOKEN)

# --- YARDIMCI FONKSİYON: RSI HESAPLA ---
def calculate_rsi(df, period=14):
    if df.empty or len(df) < period: return 50.0
    # Sütun isimleri artık tam ('close'), hata vermez.
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

# --- VERİ TOPLAMA MERKEZİ ---
def get_multiframe_data(symbol, is_top_40):
    clean_symbol = symbol.replace('/', '') 
    
    data = {
        'symbol': symbol, 'price': 0,
        'rsi_1h': 0, 'rsi_4h': 0, 'rsi_1d': 0,
        'vol_ratio_1d': 0, 
        'futures_data': {}, 
        'has_futures': False,
        'spot_success': False
    }

    # 1. SPOT VERİLERİ (HATA DÜZELTİLDİ)
    try:
        # GÜNLÜK VERİ
        bars_1d = exchange_spot.fetch_ohlcv(symbol, timeframe='1d', limit=30)
        
        if bars_1d and len(bars_1d) > 0:
            # İŞTE DÜZELTME BURADA: Sütun isimlerini uzun yazdık
            df_1d = pd.DataFrame(bars_1d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            data['price'] = df_1d['close'].iloc[-1]
            data['rsi_1d'] = calculate_rsi(df_1d)
            
            # Hacim Analizi
            vol_current = df_1d['volume'].iloc[-1]
            vol_avg = df_1d['volume'].iloc[:-1].rolling(window=14).mean().iloc[-1] 
            
            if vol_avg > 0:
                data['vol_ratio_1d'] = vol_current / vol_avg
            else:
                data['vol_ratio_1d'] = 1.0
            
            # Top 40 ise diğer zaman dilimlerine de bak
            if is_top_40:
                bars_4h = exchange_spot.fetch_ohlcv(symbol, timeframe='4h', limit=20)
                df_4h = pd.DataFrame(bars_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                data['rsi_4h'] = calculate_rsi(df_4h)

                bars_1h = exchange_spot.fetch_ohlcv(symbol, timeframe='1h', limit=20)
                df_1h = pd.DataFrame(bars_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                data['rsi_1h'] = calculate_rsi(df_1h)
            
            data['spot_success'] = True
            
    except Exception as e:
        # print(f"Spot Hatası {symbol}: {e}")
        pass

    # 2. FUTURES VERİLERİ (Zaten Çalışıyor)
    if is_top_40:
        try:
            frames = ['15m', '1h', '4h']
            for frame in frames:
                ls_data = exchange_futures.fapiDataGetTopLongShortAccountRatio({
                    'symbol': clean_symbol, 
                    'period': frame, 
                    'limit': 1
                })
                if ls_data:
                    item = ls_data[0] if isinstance(ls_data, list) else ls_data
                    data['futures_data'][frame] = {
                        'long': float(item['longAccount']) * 100,
                        'short': float(item['shortAccount']) * 100
                    }
            if data['futures_data']:
                data['has_futures'] = True
        except:
            data['has_futures'] = False
            
    return data

def general_tarama():
    bot.send_message(CHAT_ID, "🎖️ KOMUTANIM! Radar v25 (AVCI MODU) Devrede!\n✅ Sütun İsimleri Düzeltildi.\n✅ Veri Akışı Teyit Edildi.\n🚀 Hedefler Taranıyor...")
    
    YASAKLI = ['UP/', 'DOWN/', 'BEAR', 'BULL', 'USDC', 'TUSD', 'USDP', 'FDUSD', 'EUR', 'DAI', 'PAXG', 'BUSD', 'USDE', 'USDD']

    while True:
        print("🔄 Tarama Başlıyor...")
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
                
                time.sleep(0.3 if is_top_40 else 0.15)
                
                data = get_multiframe_data(symbol, is_top_40)
                
                if not data['spot_success']: continue

                RAPOR_VAR = False
                BASLIK = ""
                ICON = ""
                YORUM = ""
                
                # 1. GÜNLÜK HACİM PATLAMASI (>2.0x)
                if data['vol_ratio_1d'] > 2.0:
                    RAPOR_VAR = True
                    BASLIK = f"GÜNLÜK HACİM PATLAMASI ({data['vol_ratio_1d']:.1f}x)"
                    ICON = "🌊"
                    YORUM = "Coin'e bugün normalin 2 katından fazla para girmiş."
                
                # 2. RSI DİP (1 Saatlikte 30 altı)
                elif is_top_40 and data['rsi_1h'] < 30 and data['rsi_1h'] > 0:
                    RAPOR_VAR = True
                    BASLIK = f"RSI DİP SİNYALİ ({data['rsi_1h']:.1f})"
                    ICON = "💎"
                    YORUM = "Kısa vadede aşırı satım var."

                # 3. FUTURES TUZAĞI (Sadece Top 40)
                if data['has_futures']:
                    f_4h = data['futures_data'].get('4h', {'long': 50, 'short': 50})
                    # %70 Long veya Short varsa haber ver
                    if f_4h['long'] > 70:
                        RAPOR_VAR = True
                        if not BASLIK: 
                            BASLIK = f"AŞIRI LONG YIĞILMASI (%{f_4h['long']:.1f})"
                            ICON = "🔥"
                            YORUM = "4 Saatlikte herkes Long açmış. Squeeze riski!"
                    elif f_4h['short'] > 70:
                        RAPOR_VAR = True
                        if not BASLIK:
                            BASLIK = f"AŞIRI SHORT YIĞILMASI (%{f_4h['short']:.1f})"
                            ICON = "❄️"
                            YORUM = "4 Saatlikte herkes Short açmış. Patlama yukarı olabilir!"

                # RAPORLAMA
                if RAPOR_VAR:
                    mesaj = (f"🕵️ **İSTİHBARAT RAPORU** {ICON}\n"
                             f"📌 **{symbol}** ({data['price']} $)\n"
                             f"📢 **DURUM:** {BASLIK}\n\n")
                    
                    mesaj += (f"🌊 **SPOT VERİLERİ:**\n"
                              f"• Günlük Hacim: {data['vol_ratio_1d']:.1f}x\n"
                              f"• RSI (1s): {data['rsi_1h']:.1f}\n"
                              f"• RSI (Gün): {data['rsi_1d']:.1f}\n\n")
                    
                    if data['has_futures']:
                        f15 = data['futures_data'].get('15m', {'long':0, 'short':0})
                        f1h = data['futures_data'].get('1h', {'long':0, 'short':0})
                        f4h = data['futures_data'].get('4h', {'long':0, 'short':0})
                        
                        mesaj += (f"⚖️ **VADELİ ORANLARI (L/S):**\n"
                                  f"• 15dk: %{f15['long']:.1f} / %{f15['short']:.1f}\n"
                                  f"• 1 Sa: %{f1h['long']:.1f} / %{f1h['short']:.1f}\n"
                                  f"• 4 Sa: %{f4h['long']:.1f} / %{f4h['short']:.1f}\n")
                    
                    mesaj += f"🧠 **YORUM:** {YORUM}"
                        
                    bot.send_message(CHAT_ID, mesaj, parse_mode='Markdown')
                    time.sleep(1)

            print("💤 Tur Bitti. Mola...")
            time.sleep(120)

        except Exception as e:
            print(f"Genel Hata: {e}")
            time.sleep(30)

if __name__ == "__main__":
    general_tarama()
