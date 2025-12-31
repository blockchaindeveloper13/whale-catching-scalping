import ccxt
import os
import json

# API AYARLARI
API_KEY = os.environ.get('BINANCE_API_KEY')
API_SECRET = os.environ.get('BINANCE_SECRET_KEY')

print("--- KALİBRASYON BAŞLIYOR ---")

# 1. BAĞLANTIYI KUR
exchange_futures = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {'defaultType': 'future'}
})

# 2. SEMBOL FORMATI TESTİ
symbol_slash = 'BTC/USDT'
symbol_clean = 'BTCUSDT'

print(f"\n1. FORMAT TESTİ (Hedef: {symbol_clean})")
try:
    # Binance genelde 'BTCUSDT' ister
    # Long/Short Oranı çekmeyi deneyelim
    ls_data = exchange_futures.fapiDataGetTopLongShortAccountRatio({
        'symbol': symbol_clean,  # Düz format
        'period': '5m',
        'limit': 1
    })
    print(f"✅ 'BTCUSDT' ile veri geldi: {ls_data[0]['longAccount']}")
except Exception as e:
    print(f"❌ 'BTCUSDT' Hatası: {e}")

# 3. ZAMAN DİLİMİ TESTİ (15m vs 1h Farklı mı?)
print(f"\n2. ZAMAN DİLİMİ TESTİ")
try:
    ls_15m = exchange_futures.fapiDataGetTopLongShortAccountRatio({'symbol': 'BTCUSDT', 'period': '15m', 'limit': 1})
    ls_1h = exchange_futures.fapiDataGetTopLongShortAccountRatio({'symbol': 'BTCUSDT', 'period': '1h', 'limit': 1})
    
    val_15 = ls_15m[0]['longAccount']
    val_1h = ls_1h[0]['longAccount']
    
    print(f"🔹 15 Dakika Değeri: {val_15}")
    print(f"🔹 1 Saatlik Değer: {val_1h}")
    
    if val_15 != val_1h:
        print("✅ SİSTEM BAŞARILI! (Veriler farklı geliyor)")
    else:
        print("⚠️ UYARI! (Veriler aynı, parametre hatası olabilir)")

except Exception as e:
    print(f"HATA: {e}")

print("\n--- KALİBRASYON BİTTİ ---")

