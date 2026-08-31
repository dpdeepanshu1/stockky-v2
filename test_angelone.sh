#!/bin/bash
set -a
source .env
set +a

echo "=== Step 1: Generate fresh TOTP ==="
TOTP=$(python3 -c "import pyotp, os; print(pyotp.TOTP(os.environ['ANGELONE_TOTP_SECRET']).now())")
echo "TOTP: $TOTP"

echo "=== Step 2: Login ==="
curl -s -X POST "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword" \
  -H "Content-Type: application/json" -H "Accept: application/json" \
  -H "X-PrivateKey: $ANGELONE_API_KEY" -H "X-UserType: USER" -H "X-SourceID: WEB" \
  -H "X-ClientLocalIP: 127.0.0.1" -H "X-ClientPublicIP: 130.210.2.159" \
  -H "X-MACAddress: 00:00:00:00:00:00" \
  -d "{\"clientcode\":\"$ANGELONE_CLIENT_ID\",\"password\":\"$ANGELONE_MPIN\",\"totp\":\"$TOTP\"}" \
  -o /tmp/angelone_login.json
cat /tmp/angelone_login.json | python3 -m json.tool

STATUS=$(python3 -c "import json; d=json.load(open('/tmp/angelone_login.json')); print(d.get('status'))")

if [ "$STATUS" != "True" ]; then
  echo ""
  echo "!!! LOGIN FAILED — stop here, do not proceed to data test. !!!"
  echo "Actual server message above tells us the real remaining cause."
  exit 1
fi

echo ""
echo "=== Step 3: Login succeeded. Extracting JWT ==="
JWT=$(python3 -c "import json; print(json.load(open('/tmp/angelone_login.json'))['data']['jwtToken'])")
echo "JWT acquired (length check): $(echo -n $JWT | wc -c) chars"

echo ""
echo "=== Step 4: THE REAL TEST — fetch actual live market data (RELIANCE LTP) ==="
curl -s -X POST "https://apiconnect.angelone.in/rest/secure/angelbroking/order/v1/getLtpData" \
  -H "Authorization: Bearer $JWT" -H "X-PrivateKey: $ANGELONE_API_KEY" \
  -H "X-UserType: USER" -H "X-SourceID: WEB" \
  -H "X-ClientLocalIP: 127.0.0.1" -H "X-ClientPublicIP: 130.210.2.159" \
  -H "X-MACAddress: 00:00:00:00:00:00" \
  -H "Content-Type: application/json" -H "Accept: application/json" \
  -d '{"exchange":"NSE","tradingsymbol":"RELIANCE-EQ","symboltoken":"2885"}' \
  | python3 -m json.tool

echo ""
echo "=== If you see 'status: true' and a real ltp price above, AngelOne is fully working. ==="
