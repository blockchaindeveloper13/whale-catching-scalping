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

# --- YAPAY ZEKA YORUMCUSU ---
def piyasayi_yorumla(long_pct, short_pct):
    # Long Tarafı Baskınsa
    if long_pct > 70:
        return "🔥🔥 **KRİTİK UYARI:** Longlar aşırı şişti! (%70+). Balinalar 'Long Squeeze' (Ani çakılma) yapıp bunları likit edebilir. Ters işlem (Short) kovalamak için fırsat olabilir ama çok riskli!"
    elif long_pct > 60:
        return "🔥 **GÜÇLÜ ALIM:** Piyasa boğa iştahında. Kalabalık 'Yükselecek' diyor. Trende katılınabilir ama dönüşe dikkat et."
    elif long_pct > 53:
        return "🟢 **ALICILAR DEVREDE:** Ufak bir alım baskısı var. Henüz rüzgar sert değil ama yön yukarı dönüyor."
    
    # Short Tarafı Baskınsa
    elif short_pct > 70:
        return "🔥🔥 **KRİTİK UYARI:** Shortlar aşırı yığıldı! (%70+). Fiyatı aniden yukarı fişekleyip (Short Squeeze) bu ayıları avlayabilirler. DİKKAT!"
    elif short_pct > 60:
        return "❄️ **GÜÇLÜ SATIŞ:** Piyasa ayı modunda. Çoğunluk düşüş bekliyor. Düşen bıçak tutulmaz, dönüş sinyali bekle."
    elif short_pct > 53:
        return "🔴 **SATICILAR DEVREDE:** Satış baskısı hakim olmaya başladı. Rüzgar aşağıdan esiyor."
    
    else:
        return "⚖️ **DENGELİ:** Piyasa kararsız. Yön tayini yapmak zor. İzlemede kal."

def get_analysis_data(symbol, is_top_40):
    clean_symbol = symbol.replace('/', '')
    price = 0
    rsi = 50
    vol_ratio = 0
    has_spot_data = False
    
    # 1. SPOT VERİSİ
    try:
        bars = exchange_spot.fetch_ohlcv(symbol, timeframe='15m', limit=50)
        df = pd.DataFrame(bars, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi = rsi.iloc[-1]
        
        vol_avg = df['v'].mean()
        vol_ratio = df['v'].iloc[-1] / vol_avg if vol_avg > 0 else 0
        price = df['close'].iloc[-1]
        has_spot_data = True
    except:
        has_spot_data = False

    # 2. FUTURES VERİSİ (Sadece Top 40)
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
                
                if price == 0:
                    ticker = exchange_futures.fetch_ticker(clean_symbol)
                    price = ticker['last']
                
                oi_data = exchange_futures.fetch_open_interest(clean_symbol)
                open_interest = float(oi_data['openInterestAmount'])
                has_futures = True
        except:
            has_futures = False
    
    if not has_spot_data and not has_futures: return None

    return {
        'symbol': symbol, 'price': price,
        'rsi': rsi, 'vol_ratio': vol_ratio,
        'has_futures': has_futures,
        'long_pct': long_pct, 'short_pct': short_pct,
        'open_interest': open_interest
    }

def general_tarama():
    bot.send_message(CHAT_ID, "🎖️ KOMUTANIM! Radar v23 (İSTİHBARATÇI MOD) Devrede!\n🧠 Bot artık sadece alarm vermiyor, veriyi YORUMLUYOR.\n📊 Futures Top 40 için eşik düşürüldü (%53).")
    
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
                if is_top_40: time.sleep(0.25)
                else: time.sleep(0.1)
                
                data = get_analysis_data(symbol, is_top_40)
                if not data: continue
                
                RAPOR_VAR = False
                YORUM_METNI = ""
                SEBEP_BASLIK = ""
                ICON = ""

                # 1. FUTURES ANALİZİ (Öncelik: İstihbarat)
                if data['has_futures']:
                    # Eşik çok düşük (%53), amaç bilgi vermek
                    if data['long_pct'] > 53 or data['short_pct'] > 53:
                        RAPOR_VAR = True
                        YORUM_METNI = piyasayi_yorumla(data['long_pct'], data['short_pct'])
                        
                        # Başlık Belirle
                        if data['long_pct'] > 53: 
                            SEBEP_BASLIK = f"LONG AĞIRLIKLI (%{data['long_pct']:.1f})"
                            ICON = "🟢" if data['long_pct'] < 60 else "🔥"
                        else: 
                            SEBEP_BASLIK = f"SHORT AĞIRLIKLI (%{data['short_pct']:.1f})"
                            ICON = "🔴" if data['short_pct'] < 60 else "❄️"

                # 2. SPOT ANALİZİ (Hala önemli)
                SPOT_ALERT = False
                if data['vol_ratio'] > 2.5: SPOT_ALERT = True
                if data['rsi'] < 30: SPOT_ALERT = True
                
                # Eğer Futures'ta bir şey yoksa ama Spot'ta varsa raporla
                if not RAPOR_VAR and SPOT_ALERT:
                    RAPOR_VAR = True
                    ICON = "🌊"
                    SEBEP_BASLIK = "SPOT HAREKETLİLİK"
                    YORUM_METNI = "Futures dengeli ama Spot tarafta hareket var."

                # RAPOR GÖNDERİMİ
                # Spam olmasın diye sadece "Spot Sinyali Olanları" VEYA "Futures'ta Ciddi Dengesizlik Olanları (>55)" atalım.
                # %53-%55 arasını her dakika atarsa telefon kilitlenir. 
                # Ama sen "Kriter koyma" dedin, o yüzden Top 40 için %53 üstünü atıyoruz.
                
                if RAPOR_VAR:
                    # Sadece Top 40 ise her türlü raporla (Çünkü sayı az, 40 tane), 
                    # Diğerlerinde sadece Spot sinyali varsa raporla.
                    if is_top_40 or SPOT_ALERT:
                        mesaj = (f"🕵️ **İSTİHBARAT RAPORU** {ICON}\n"
                                 f"📌 **{symbol}** ({data['price']} $)\n\n"
                                 f"📊 **DURUM:** {SEBEP_BASLIK}\n")
                        
                        if data['has_futures']:
                            mesaj += f"⚖️ **Oranlar:** L: %{data['long_pct']:.1f} | S: %{data['short_pct']:.1f}\n"
                        
                        mesaj += f"🌊 **Spot:** RSI {data['rsi']:.1f} | Hacim {data['vol_ratio']:.1f}x\n\n"
                        mesaj += f"🧠 **ANALİZ:**\n{YORUM_METNI}"
                        
                        bot.send_message(CHAT_ID, mesaj, parse_mode='Markdown')
                        time.sleep(1)

            print("💤 Tur Bitti. Mola...")
            time.sleep(120)

        except Exception as e:
            print(f"Hata: {e}")
            time.sleep(30)

if __name__ == "__main__":
    general_tarama()
            
