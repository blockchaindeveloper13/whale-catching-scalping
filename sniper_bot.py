import ccxt
import time
import telebot
import os
import pandas as pd
from datetime import datetime

# --- AYARLAR ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')

# Spot ve Futures Bağlantıları
exchange_spot = ccxt.binance({
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True
})
exchange_futures = ccxt.binance({
    'options': {'defaultType': 'future'},
    'enableRateLimit': True
})

bot = telebot.TeleBot(BOT_TOKEN)

# HAFIZA
OI_HAFIZA = {} 

# --- YARDIMCI ANALİZ MOTORLARI ---
def get_analysis_data(symbol):
    try:
        clean_symbol = symbol.replace('/', '')
        
        # --- 1. FUTURES İSTİHBARATI (DÜZELTİLDİ: SNAKE_CASE KULLANILDI) ---
        # Önce "Top Trader" (Balina) verisini deniyoruz. Bu daha kalitelidir.
        try:
            ls_data = exchange_futures.fapiData_get_top_long_short_account_ratio({
                'symbol': clean_symbol, 
                'period': '15m', 
                'limit': 1
            })
        except:
            # Eğer Top Trader yoksa Global veriyi dene (Yedek Plan)
            try:
                ls_data = exchange_futures.fapiData_get_global_long_short_account_ratio({
                    'symbol': clean_symbol, 
                    'period': '15m', 
                    'limit': 1
                })
            except Exception as e:
                # İkisi de olmazsa bu coin Vadeli'de yoktur veya veri vermiyordur.
                # print(f"⚠️ {clean_symbol} Futures verisi alınamadı: {e}")
                return None

        if not ls_data: return None
            
        long_pct = float(ls_data[0]['longAccount']) * 100
        short_pct = float(ls_data[0]['shortAccount']) * 100
        ls_ratio = float(ls_data[0]['longShortRatio'])
        
        # Open Interest
        oi_data = exchange_futures.fetch_open_interest(clean_symbol)
        open_interest = float(oi_data['openInterestAmount'])
        
        # Funding Rate
        funding = exchange_futures.fetch_funding_rate(clean_symbol)
        funding_rate = funding['fundingRate'] * 100

        # --- 2. SPOT İSTİHBARATI ---
        bars = exchange_spot.fetch_ohlcv(symbol, timeframe='15m', limit=50)
        df = pd.DataFrame(bars, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = rsi.iloc[-1]
        
        vol_avg = df['v'].mean()
        vol_ratio = df['v'].iloc[-1] / vol_avg if vol_avg > 0 else 0

        return {
            'long_pct': long_pct,
            'short_pct': short_pct,
            'ls_ratio': ls_ratio,
            'open_interest': open_interest,
            'funding': funding_rate,
            'rsi': current_rsi,
            'vol_ratio': vol_ratio,
            'price': df['close'].iloc[-1]
        }
    except Exception as e:
        print(f"❌ HATA ({symbol}): {e}")
        return None

# --- KOMUTANIN GÖZÜ (ANA OPERASYON) ---
def general_tarama():
    bot.send_message(CHAT_ID, "🎖️ KOMUTANIM! Radar v13.3 (KESİN ÇÖZÜM) Devrede!\n🚀 Hedef: Balinalar (Top Traders)\n🔧 Düzeltme: Snake_Case Syntax")
    
    while True:
        print("🔄 Tüm Cepheler Taranıyor...")
        
        try:
            tickers = exchange_spot.fetch_tickers()
            sorted_tickers = sorted(tickers.items(), key=lambda x: x[1]['quoteVolume'], reverse=True)
            hedef_liste = [t[0] for t in sorted_tickers if '/USDT' in t[0] and 'UP' not in t[0] and 'DOWN' not in t[0]][:40]
            
            print(f"🎯 Hedef Listesi ({len(hedef_liste)} Coin) Taranıyor...")
            
            for symbol in hedef_liste:
                data = get_analysis_data(symbol)
                if not data: continue
                
                # --- STRATEJİ MERKEZİ ---
                RAPOR_VAR = False
                SEBEP = ""
                ICON = ""
                YORUM = ""
                
                # 1. BALİNA YIĞILMASI (%60 Kuralı)
                if data['long_pct'] > 60:
                    RAPOR_VAR = True
                    SEBEP = f"BALİNA LONGLARI (%{data['long_pct']:.1f})"
                    ICON = "⚠️"
                    YORUM = "Balinalar Long açmış. Kasa terse vurabilir (Tuzak)!"
                elif data['short_pct'] > 60:
                    RAPOR_VAR = True
                    SEBEP = f"BALİNA SHORTLARI (%{data['short_pct']:.1f})"
                    ICON = "🚀"
                    YORUM = "Balinalar Short açmış. Kasa yukarı sürebilir (Squeeze)!"
                
                # 2. OPEN INTEREST PATLAMASI
                clean_sym = symbol.replace('/','')
                prev_oi = OI_HAFIZA.get(clean_sym, data['open_interest'])
                if clean_sym not in OI_HAFIZA: oi_degisim = 0
                else: oi_degisim = ((data['open_interest'] - prev_oi) / prev_oi) * 100
                OI_HAFIZA[clean_sym] = data['open_interest']
                
                if abs(oi_degisim) > 3.0: 
                    RAPOR_VAR = True 
                    SEBEP = f"OI PATLAMASI (%{oi_degisim:.1f})"
                    ICON = "🐳"
                    if not YORUM: YORUM = "Fiyat sabitken para giriyor. Büyük hareket yakın!"

                # 3. SPOT HACİM
                if data['vol_ratio'] > 3.0:
                    RAPOR_VAR = True
                    if not SEBEP: SEBEP = "SPOT HACİM PATLAMASI"
                    YORUM += "\nSpot hacim desteği var."

                if RAPOR_VAR:
                    mesaj = (
                        f"🐋 **GENELKURMAY İSTİHBARATI** {ICON}\n"
                        f"🚨 **ALARM:** {SEBEP}\n\n"
                        f"💎 **{symbol}** ({data['price']} $)\n"
                        f"📊 **Futures (Balina) Dengesi:**\n"
                        f"   • Long: %{data['long_pct']:.1f} 🟢\n"
                        f"   • Short: %{data['short_pct']:.1f} 🔴\n"
                        f"   • Fonlama: %{data['funding']:.4f}\n"
                        f"🌊 **Spot Verisi:**\n"
                        f"   • RSI (15m): {data['rsi']:.1f}\n"
                        f"   • Hacim Gücü: {data['vol_ratio']:.1f}x\n\n"
                        f"🧠 **KOMUTAN YORUMU:**\n{YORUM}"
                    )
                    bot.send_message(CHAT_ID, mesaj, parse_mode='Markdown')
                    time.sleep(1)

            print("💤 Tur Tamamlandı. 2 Dakika Mola...")
            time.sleep(120)

        except Exception as e:
            print(f"Genel Döngü Hatası: {e}")
            time.sleep(30)

if __name__ == "__main__":
    general_tarama()
    
