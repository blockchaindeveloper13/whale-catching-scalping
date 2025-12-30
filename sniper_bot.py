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
exchange_spot = ccxt.binance({'options': {'defaultType': 'spot'}})
exchange_futures = ccxt.binance({'options': {'defaultType': 'future'}})

bot = telebot.TeleBot(BOT_TOKEN)

# HAFIZA (Önceki değerleri kıyaslamak için)
OI_HAFIZA = {} 

# --- YARDIMCI ANALİZ MOTORLARI ---
def get_analysis_data(symbol):
    try:
        # 1. FUTURES İSTİHBARATI (ÖNCELİKLİ)
        # Long/Short Oranı (Global)
        ls_data = exchange_futures.fapiPublic_get_global_longshortaccountratio({'symbol': symbol.replace('/',''), 'period': '15m', 'limit': 1})
        long_pct = float(ls_data[0]['longAccount']) * 100
        short_pct = float(ls_data[0]['shortAccount']) * 100
        ls_ratio = float(ls_data[0]['longShortRatio'])
        
        # Open Interest (Anlık Para)
        oi_data = exchange_futures.fetch_open_interest(symbol.replace('/',''))
        open_interest = float(oi_data['openInterestAmount'])
        
        # Funding Rate
        funding = exchange_futures.fetch_funding_rate(symbol.replace('/',''))
        funding_rate = funding['fundingRate'] * 100

        # 2. SPOT İSTİHBARATI (TEKNİK DESTEK)
        # RSI ve Fiyat Trendi
        bars = exchange_spot.fetch_ohlcv(symbol, timeframe='15m', limit=50)
        df = pd.DataFrame(bars, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        current_rsi = rsi.iloc[-1]
        
        # Hacim Katı (Son bar hacmi / Ortalama hacim)
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
    except:
        return None

# --- KOMUTANIN GÖZÜ (ANA OPERASYON) ---
def general_tarama():
    bot.send_message(CHAT_ID, "🎖️ KOMUTANIM! Radar v13 (GENERAL MODU) Devrede!\n🚀 Hedef: %60 Yığılma ve Balina Avı\n⏱️ Hız: 2 Dakika\n⚔️ Cephe: Futures Öncelikli")
    
    while True:
        print("🔄 Tüm Cepheler Taranıyor (Spot + Futures)...")
        
        try:
            # 1. HEDEF BELİRLEME (Hacimli Coinler)
            # Tüm çöp coinleri tarayıp vakit kaybetmeyelim. Binance'in en hareketli 40 coinini al.
            tickers = exchange_spot.fetch_tickers()
            sorted_tickers = sorted(tickers.items(), key=lambda x: x[1]['quoteVolume'], reverse=True)
            # USDT paritesi olan, kaldıraçlı token olmayan (UP/DOWN) ilk 40 coin
            hedef_liste = [t[0] for t in sorted_tickers if '/USDT' in t[0] and 'UP' not in t[0] and 'DOWN' not in t[0]][:40]
            
            for symbol in hedef_liste:
                data = get_analysis_data(symbol)
                if not data: continue
                
                # --- STRATEJİ MERKEZİ ---
                
                RAPOR_VAR = False
                SEBEP = ""
                ICON = ""
                YORUM = ""
                
                # 1. SENARYO: BALİNA YIĞILMASI (Long/Short > %60)
                # Burası senin "Başladığı yer burası olur" dediğin nokta.
                if data['long_pct'] > 60:
                    RAPOR_VAR = True
                    SEBEP = f"LONGLAR YIĞILDI (%{data['long_pct']:.1f})"
                    ICON = "⚠️"
                    YORUM = "Kasa Longları patlatmak isteyebilir (Düşüş Tuzağı)!"
                elif data['short_pct'] > 60:
                    RAPOR_VAR = True
                    SEBEP = f"SHORTLAR YIĞILDI (%{data['short_pct']:.1f})"
                    ICON = "🚀"
                    YORUM = "Kasa Shortları patlatmak isteyebilir (Squeeze/Yükseliş)!"
                
                # 2. SENARYO: OPEN INTEREST PATLAMASI (Balina Girişi)
                # Önceki veriye göre değişim var mı?
                clean_sym = symbol.replace('/','')
                prev_oi = OI_HAFIZA.get(clean_sym, data['open_interest'])
                oi_degisim = ((data['open_interest'] - prev_oi) / prev_oi) * 100
                OI_HAFIZA[clean_sym] = data['open_interest'] # Hafızayı güncelle
                
                if abs(oi_degisim) > 3.0: # %3'lük ani değişim (2 dakikada büyük rakamdır)
                    RAPOR_VAR = True # Eğer oran %60 olmasa bile, balina girdiyse raporla
                    SEBEP = f"OI PATLAMASI (%{oi_degisim:.1f})"
                    ICON = "🐳"
                    if not YORUM: YORUM = "Fiyat sabitken para giriyor. Büyük hareket yakın!"

                # 3. SENARYO: SPOT BALİNA (Hacim Desteği)
                if data['vol_ratio'] > 3.0:
                    RAPOR_VAR = True
                    if not SEBEP: SEBEP = "SPOT HACİM PATLAMASI"
                    YORUM += "\nSpot tarafta da güçlü alım/satım var. Destekli hareket."

                # --- BİLDİRİM GÖNDER ---
                # Sadece RAPOR_VAR ise mesaj at (Spam engelleme)
                if RAPOR_VAR:
                    
                    # Ekstra Filtre: Çok sık mesaj atmamak için
                    # (Burada her sinyali atıyorum çünkü 'Total Savaş' istedin)
                    
                    mesaj = (
                        f"🐋 **GENELKURMAY İSTİHBARATI** {ICON}\n"
                        f"🚨 **ALARM:** {SEBEP}\n\n"
                        f"💎 **{symbol}** ({data['price']} $)\n"
                        f"📊 **Futures Dengesi:**\n"
                        f"   • Long: %{data['long_pct']:.1f} 🟢\n"
                        f"   • Short: %{data['short_pct']:.1f} 🔴\n"
                        f"   • Fonlama: %{data['funding']:.4f}\n"
                        f"🌊 **Spot Verisi:**\n"
                        f"   • RSI (15m): {data['rsi']:.1f}\n"
                        f"   • Hacim Gücü: {data['vol_ratio']:.1f}x\n\n"
                        f"🧠 **KOMUTAN YORUMU:**\n{YORUM}"
                    )
                    
                    bot.send_message(CHAT_ID, mesaj, parse_mode='Markdown')
                    time.sleep(1) # Telegram limiti için bekle

            print("💤 Tur Tamamlandı. 2 Dakika Mola...")
            time.sleep(120)

        except Exception as e:
            print(f"Hata: {e}")
            time.sleep(30)

if __name__ == "__main__":
    general_tarama()
    
