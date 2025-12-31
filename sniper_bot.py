import ccxt
import telebot
import os
import time

# --- AYARLAR ---
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID')
API_KEY = os.environ.get('BINANCE_API_KEY')
API_SECRET = os.environ.get('BINANCE_SECRET_KEY')

# Sadece Futures Bağlantısı
exchange_futures = ccxt.binance({
    'apiKey': API_KEY, 'secret': API_SECRET,
    'options': {'defaultType': 'future'}, 'enableRateLimit': True
})

bot = telebot.TeleBot(BOT_TOKEN)

def test_et():
    bot.send_message(CHAT_ID, "🛠️ TANI KİTİ ÇALIŞTIRILIYOR...\nL/S Oranları kontrol ediliyor.")
    print("--- TEST BAŞLADI ---")
    
    # Test edilecek coinler (En babaları)
    test_coins = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT']
    
    rapor = "📊 **CANLI VERİ TESTİ**\n\n"
    
    for symbol in test_coins:
        try:
            clean_symbol = symbol.replace('/', '')
            
            # 1. Long/Short Oranı Çek
            ls_data = exchange_futures.fapiDataGetTopLongShortAccountRatio({
                'symbol': clean_symbol, 'period': '5m', 'limit': 1
            })
            
            # 2. Open Interest Çek
            oi_data = exchange_futures.fetch_open_interest(clean_symbol)
            
            if ls_data:
                item = ls_data[0] if isinstance(ls_data, list) else ls_data
                long_pct = float(item['longAccount']) * 100
                short_pct = float(item['shortAccount']) * 100
                oi = float(oi_data['openInterestAmount'])
                
                print(f"✅ {symbol} -> L: %{long_pct:.2f} | S: %{short_pct:.2f}")
                rapor += f"🔹 **{symbol}**\n   L: %{long_pct:.2f} | S: %{short_pct:.2f}\n   OI: {oi:.0f}\n\n"
            else:
                print(f"❌ {symbol} -> Veri Boş Döndü!")
                rapor += f"❌ **{symbol}** -> Veri Çekilemedi (Boş)!\n\n"
                
        except Exception as e:
            print(f"ERROR {symbol}: {e}")
            rapor += f"⚠️ **{symbol}** -> HATA: {str(e)}\n\n"
            
    bot.send_message(CHAT_ID, rapor, parse_mode='Markdown')
    print("--- TEST BİTTİ ---")

if __name__ == "__main__":
    test_et()
    
