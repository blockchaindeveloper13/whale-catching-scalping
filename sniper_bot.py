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

# --- YARDIMCI FONKSİYON: RSI HESAPLA ---
def calculate_rsi(df, period=14):
    if df.empty: return 50.0
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
        'vol_ratio_1d': 0, # Günlük Hacim Artışı
        'futures_data': {}, # 15m, 1h, 4h L/S oranları
        'has_futures': False,
        'spot_success': False
    }

    # 1. SPOT VERİLERİ (RSI ve HACİM)
    try:
        # A) Günlük Veri (Hacim ve RSI 1d için)
        # 14 günlük ortalama için limit=20 yeterli
        bars_1d = exchange_spot.fetch_ohlcv(symbol, timeframe='1d', limit=30)
        df_1d = pd.DataFrame(bars_1d, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        data['price'] = df_1d['close'].iloc[-1]
        data['rsi_1d'] = calculate_rsi(df_1d)
        
        # Hacim Analizi (Son gün vs 14 günlük ortalama)
        # Son mum (bugün) tamamlanmamış olabilir ama "run rate"e bakarız
        vol_current = df_1d['v'].iloc[-1]
        vol_avg = df_1d['v'].rolling(window=14).mean().iloc[-2] # Dünü baz al
        
        if vol_avg > 0:
            data['vol_ratio_1d'] = vol_current / vol_avg
        else:
            data['vol_ratio_1d'] = 0

        # B) 4 Saatlik ve 1 Saatlik Veri (Sadece RSI için)
        # API Limitini korumak için, eğer "Genel Tarama"daysak (Top 40 değilse)
        # her zaman hepsini çekmeyebiliriz ama sen istedin, çekiyoruz.
        
        bars_4h = exchange_spot.fetch_ohlcv(symbol, timeframe='4h', limit=20)
        data['rsi_4h'] = calculate_rsi(pd.DataFrame(bars_4h, columns=['t','o','h','l','c','v']))

        bars_1h = exchange_spot.fetch_ohlcv(symbol, timeframe='1h', limit=20)
        data['rsi_1h'] = calculate_rsi(pd.DataFrame(bars_1h, columns=['t','o','h','l','c','v']))
        
        data['spot_success'] = True

    except Exception as e:
        # print(f"Spot Hatası {symbol}: {e}")
        pass

    # 2. FUTURES VERİLERİ (Çoklu Zaman Dilimi)
    # Sadece Top 40 veya Spot'ta sinyal verenler için detaylı bakılabilir
    # Ama kodun sadeliği için Top 40'a her zaman bakacağız.
    if is_top_40:
        try:
            frames = ['15m', '1h', '4h']
            for frame in frames:
                ls_data = exchange_futures.fapiDataGetTopLongShortAccountRatio({
                    'symbol': clean_symbol, 'period': frame, 'limit': 1
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
    bot.send_message(CHAT_ID, "🎖️ KOMUTANIM! Radar v24 (STRATEJİK DERİNLİK) Devrede!\n📊 Zaman Dilimleri: 15dk, 1s, 4s, Günlük\n🌊 Odak: Günlük Hacmi 2x Artanlar ve Çoklu RSI")
    
    YASAKLI = ['UP/', 'DOWN/', 'BEAR', 'BULL', 'USDC', 'TUSD', 'USDP', 'FDUSD', 'EUR', 'DAI', 'PAXG', 'BUSD', 'USDE', 'USDD']

    while True:
        print("🔄 Detaylı Tarama Başlıyor...")
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
                
                # API limitine saygı (Çok fazla istek atıyoruz artık)
                sleep_time = 0.3 if is_top_40 else 0.2
                time.sleep(sleep_time)
                
                data = get_multiframe_data(symbol, is_top_40)
                
                # --- SİNYAL KONTROL ---
                RAPOR_VAR = False
                BASLIK = ""
                ICON = ""
                
                # 1. GÜNLÜK HACİM PATLAMASI (En Önemlisi)
                if data['vol_ratio_1d'] > 2.0:
                    RAPOR_VAR = True
                    BASLIK = f"GÜNLÜK HACİM PATLAMASI ({data['vol_ratio_1d']:.1f}x)"
                    ICON = "🌊"
                
                # 2. RSI DİP (Çoklu Teyit)
                # Eğer hem 1s hem 4s RSI düşükse sağlam diptir
                elif data['rsi_1h'] < 30 and data['rsi_4h'] < 35:
                    RAPOR_VAR = True
                    BASLIK = f"GÜÇLÜ DİP SİNYALİ"
                    ICON = "💎"

                # 3. FUTURES TUZAĞI (Sadece Top 40)
                # 4 Saatlikte büyük bir yığılma varsa trenddir.
                if data['has_futures']:
                    f_4h = data['futures_data'].get('4h', {'long': 50, 'short': 50})
                    if f_4h['long'] > 65:
                        RAPOR_VAR = True
                        if not BASLIK: 
                            BASLIK = f"4S LONG YIĞILMASI (%{f_4h['long']:.1f})"
                            ICON = "🔥"
                    elif f_4h['short'] > 65:
                        RAPOR_VAR = True
                        if not BASLIK: 
                            BASLIK = f"4S SHORT YIĞILMASI (%{f_4h['short']:.1f})"
                            ICON = "❄️"

                # RAPORLAMA (Sadece Sinyal Varsa veya Top 40'ta Ciddi Hareket Varsa)
                if RAPOR_VAR:
                    mesaj = (f"🕵️ **İSTİHBARAT RAPORU** {ICON}\n"
                             f"📌 **{symbol}** ({data['price']} $)\n"
                             f"📢 **SİNYAL:** {BASLIK}\n\n")
                    
                    # Spot Detayları
                    mesaj += (f"🌊 **SPOT ANALİZİ:**\n"
                              f"• Vol (Günlük): {data['vol_ratio_1d']:.1f}x (Ortalamanın Katı)\n"
                              f"• RSI (1s): {data['rsi_1h']:.1f}\n"
                              f"• RSI (4s): {data['rsi_4h']:.1f}\n"
                              f"• RSI (Gün): {data['rsi_1d']:.1f}\n\n")
                    
                    # Futures Detayları (Varsa)
                    if data['has_futures']:
                        f15 = data['futures_data'].get('15m', {'long':0, 'short':0})
                        f1h = data['futures_data'].get('1h', {'long':0, 'short':0})
                        f4h = data['futures_data'].get('4h', {'long':0, 'short':0})
                        
                        mesaj += (f"⚖️ **VADELİ ORANLARI (L/S):**\n"
                                  f"• 15dk: %{f15['long']:.1f} / %{f15['short']:.1f}\n"
                                  f"• 1 Sa: %{f1h['long']:.1f} / %{f1h['short']:.1f}\n"
                                  f"• 4 Sa: %{f4h['long']:.1f} / %{f4h['short']:.1f}\n")
                        
                    bot.send_message(CHAT_ID, mesaj, parse_mode='Markdown')

            print("💤 Tur Bitti. Mola...")
            time.sleep(120)

        except Exception as e:
            print(f"Genel Hata: {e}")
            time.sleep(30)

if __name__ == "__main__":
    general_tarama()
