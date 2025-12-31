import ccxt
import pandas as pd
import os

# API AYARLARI
API_KEY = os.environ.get('BINANCE_API_KEY')
API_SECRET = os.environ.get('BINANCE_SECRET_KEY')

print("--- SPOT VERİ TESTİ BAŞLIYOR ---")

# 1. SPOT BAĞLANTISI (ÖZEL AYARLI)
exchange_spot = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {
        'defaultType': 'spot', 
        'adjustForTimeDifference': True # <--- KİLİT NOKTA BU!
    },
    'enableRateLimit': True
})

symbol = 'BTC/USDT'

try:
    # 2. VERİYİ ÇEK (Günlük Mumlar)
    print(f"📡 {symbol} için mum verisi isteniyor...")
    bars = exchange_spot.fetch_ohlcv(symbol, timeframe='1d', limit=30)
    
    # 3. VERİ GELDİ Mİ?
    if not bars or len(bars) == 0:
        print("❌ HATA: Hiç veri gelmedi! Liste boş.")
    else:
        print(f"✅ BAŞARILI: {len(bars)} adet mum verisi indirildi.")
        
        # DataFrame'e çevir
        df = pd.DataFrame(bars, columns=['t', 'o', 'h', 'l', 'c', 'v'])
        last_price = df['close'].iloc[-1]
        last_vol = df['v'].iloc[-1]
        
        # --- RSI HESAPLA (Manuel) ---
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        rsi_val = rsi.iloc[-1]
        
        # --- HACİM ORANI HESAPLA ---
        vol_avg = df['v'].rolling(window=14).mean().iloc[-2] # Dünkü ortalama
        vol_ratio = last_vol / vol_avg if vol_avg > 0 else 0
        
        print("\n📊 --- SONUÇLAR ---")
        print(f"💰 Fiyat: {last_price} $")
        print(f"📈 RSI (14): {rsi_val:.2f}  (Hedef: 0.0 OLMAMALI)")
        print(f"wv Hacim: {last_vol:.2f}")
        print(f"🌊 Hacim Artışı: {vol_ratio:.2f}x (Hedef: 0.0 OLMAMALI)")

except Exception as e:
    print(f"❌ KRİTİK HATA: {e}")

print("---------------------------")
        
