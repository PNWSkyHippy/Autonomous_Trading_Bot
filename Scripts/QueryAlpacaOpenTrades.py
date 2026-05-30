import os
from dotenv import load_dotenv
load_dotenv()
import requests
headers = {'APCA-API-KEY-ID': os.getenv('ALPACA_API_KEY'), 'APCA-API-SECRET-KEY': os.getenv('ALPACA_SECRET_KEY')}
r = requests.get('https://paper-api.alpaca.markets/v2/positions', headers=headers)
import json
positions = r.json()
if isinstance(positions, list):
    print('Alpaca open positions:')
    for p in positions:
        print(p.get('symbol'), p.get('qty'), p.get('current_price'))
else:
    print(positions)
