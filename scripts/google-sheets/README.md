# Example API Request

curl -X GET "https://ib.mydomain.com/option/greeks?underlying=RMS&expiry=20260619&strike=1700&right=P" -H "X-API-Key: XXXXXXXXXXXXXXX"

# Response

{"symbol":"RMS 20260619 1700.0 P","delta":-0.421727,"gamma":0.00200013,"vega":2.33406,"theta":-0.86126,"implied_vol":0.332212,"underlying_price":0.0,"volume":0,"open_interest":0,"last_price":0.0,"last_date":"2026-05-07 10:15:01"}