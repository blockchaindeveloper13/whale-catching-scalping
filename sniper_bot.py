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
    'options': { 'defaultType': 'spot', 'adjustForTimeDifference': True },
    'enableRateLimit': True
})

exchange_futures = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': { 'defaultType': 'future', 'adjustForTimeDifference': True },
    'enableRateLimit': True
})

bot = telebot.TeleBot(BOT_TOKEN)

# --- YARDIMCI FONKSİYONLAR ---
def calculate_rsi(df, period=14):
    if df.empty or len(df) < period: return 50.0
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

def get_multiframe_data(symbol, is_top_40):
    clean_symbol = symbol.replace('/', '') 
    
    data = {
        'symbol': symbol, 'price': 0,
        'rsi_1h': 0, 'rsi_1d': 0,
        'vol_ratio_1d': 0, 
        'futures_data': {}, 
        'has_futures': False,
        'spot_success': False
    }

    # 1. SPOT VERİLERİ
    try:
        bars_1d = exchange_spot.fetch_ohlcv(symbol, timeframe='1d', limit=30)
        if bars_1d and len(bars_1d) > 0:
            df_1d = pd.DataFrame(bars_1d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            data['price'] = df_1d['close'].iloc[-1]
            data['rsi_1d'] = calculate_rsi(df_1d)
            
            vol_current = df_1d['volume'].iloc[-1]
            vol_avg = df_1d['volume'].iloc[:-1].rolling(window=14).mean().iloc[-1] 
            data['vol_ratio_1d'] = vol_current / vol_avg if vol_avg > 0 else 1.0
            
            # Sadece Top 40 için 1 Saatlik RSI
            if is_top_40:
                bars_1h = exchange_spot.fetch_ohlcv(symbol, timeframe='1h', limit=20)
                df_1h = pd.DataFrame(bars_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                data['rsi_1h'] = calculate_rsi(df_1h)
            
            data['spot_success'] = True
    except: pass

    # 2. FUTURES VERİLERİ (Trend Kontrolü)
    if is_top_40:
        try:
            # 15dk: Anlık
            # 1h ve 4h: Bir önceki tamamlanmış mum (limit=2)
            ls_15m = exchange_futures.fapiDataGetTopLongShortAccountRatio({'symbol': clean_symbol, 'period': '15m', 'limit': 1})
            ls_1h = exchange_futures.fapiDataGetTopLongShortAccountRatio({'symbol': clean_symbol, 'period': '1h', 'limit': 2})
            ls_4h = exchange_futures.fapiDataGetTopLongShortAccountRatio({'symbol': clean_symbol, 'period': '4h', 'limit': 2})

            if ls_15m and ls_1h and ls_4h:
                item_15 = ls_15m[0] if isinstance(ls_15m, list) else ls_15m
                # Sonraki mumları (tamamlanmış olanları) al
                item_1h = ls_1h[0] if isinstance(ls_1h, list) and len(ls_1h) > 1 else ls_1h[-1]
                item_4h = ls_4h[0] if isinstance(ls_4h, list) and len(ls_4h) > 1 else ls_4h[-1]

                data['futures_data']['15m'] = {'long': float(item_15['longAccount']) * 100, 'short': float(item_15['shortAccount']) * 100}
                data['futures_data']['1h'] = {'long': float(item_1h['longAccount']) * 100, 'short': float(item_1h['shortAccount']) * 100}
                data['futures_data']['4h'] = {'long': float(item_4h['longAccount']) * 100, 'short': float(item_4h['shortAccount']) * 100}
                
                data['has_futures'] = True
        except:
            data['has_futures'] = False
            
    return data

def general_tarama():
    # Başlangıç mesajını sadeleştirdik
    bot.send_message(CHAT_ID, "✅ **Sistem Aktif (v27)**\nAlgoritma başlatıldı. Piyasa taranıyor...")
    
    YASAKLI = ['UP/', 'DOWN/', 'BEAR', 'BULL', 'USDC', 'TUSD', 'USDP', 'FDUSD', 'EUR', 'DAI', 'PAXG', 'BUSD', 'USDE', 'USDD']

    while True:
        print("🔄 Tarama sürüyor...")
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
                
                # 1. GÜNLÜK HACİM
                if data['vol_ratio_1d'] > 2.0:
                    RAPOR_VAR = True
                    BASLIK = f"HACİM ARTIŞI ({data['vol_ratio_1d']:.1f}x)"
                    ICON = "📊"
                    YORUM = "Günlük hacim ortalamanın üzerinde."
                
                # 2. RSI DİP
                elif is_top_40 and data['rsi_1h'] < 30 and data['rsi_1h'] > 0:
                    RAPOR_VAR = True
                    BASLIK = f"AŞIRI SATIM / DİP (RSI: {data['rsi_1h']:.1f})"
                    ICON = "📉"
                    YORUM = "RSI göstergesi dip seviyede, tepki potansiyeli."

                # 3. FUTURES TUZAĞI (Sadece Top 40)
                if data['has_futures']:
                    f_4h = data['futures_data'].get('4h', {'long': 50, 'short': 50})
                    f_15m = data['futures_data'].get('15m', {'long': 50, 'short': 50})
                    
                    if f_4h['long'] > 65: 
                        if f_15m['short'] > 55: 
                            RAPOR_VAR = True
                            BASLIK = "DÜZELTME BİTİŞ SİNYALİ"
                            ICON = "🟢"
                            YORUM = f"Ana Trend Long (%{f_4h['long']:.0f}) fakat kısa vade Short (%{f_15m['short']:.0f}). Düşüş alım fırsatı olabilir."

                    elif f_4h['short'] > 65: 
                        if f_15m['long'] > 55: 
                             RAPOR_VAR = True
                             BASLIK = "TEPKİ YÜKSELİŞİ (SATIŞ FIRSATI)"
                             ICON = "🔴"
                             YORUM = f"Ana Trend Short (%{f_4h['short']:.0f}) fakat kısa vade Long (%{f_15m['long']:.0f}). Yükseliş satış fırsatı olabilir."

                # RAPORLAMA (Sadeleştirilmiş Format)
                if RAPOR_VAR:
                    mesaj = (f"🔔 **PİYASA UYARISI** {ICON}\n"
                             f"📌 **{symbol}** ({data['price']} $)\n"
                             f"📋 **TİP:** {BASLIK}\n\n")
                    
                    mesaj += (f"📊 **SPOT VERİLERİ:**\n"
                              f"• Hacim (Gün): {data['vol_ratio_1d']:.1f}x\n"
                              f"• RSI (1s): {data['rsi_1h']:.1f}\n"
                              f"• RSI (Gün): {data['rsi_1d']:.1f}\n\n")
                    
                    if data['has_futures']:
                        f15 = data['futures_data'].get('15m', {'long':0, 'short':0})
                        f1h = data['futures_data'].get('1h', {'long':0, 'short':0})
                        f4h = data['futures_data'].get('4h', {'long':0, 'short':0})
                        
                        mesaj += (f"⚖️ **VADELİ ORANLARI:**\n"
                                  f"• 15dk (Anlık): L:%{f15['long']:.1f} S:%{f15['short']:.1f}\n"
                                  f"• 1Sa (Önceki): L:%{f1h['long']:.1f} S:%{f1h['short']:.1f}\n"
                                  f"• 4Sa (Trend):  L:%{f4h['long']:.1f} S:%{f4h['short']:.1f}\n")
                    
                    mesaj += f"💡 **ANALİZ:** {YORUM}"
                        
                    bot.send_message(CHAT_ID, mesaj, parse_mode='Markdown')
                    time.sleep(1)

            print("💤 Tur Tamamlandı. Bekleme modu...")
            time.sleep(120)

        except Exception as e:
            print(f"Hata: {e}")
            time.sleep(30)

if __name__ == "__main__":
    general_tarama()
