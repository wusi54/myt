import urllib.request, json

# Test 1: set_custom_devinfo with ALL fields in devinfo string
ip = '192.168.10.83'
name = 'c2f2faac6f13dd9235a0fccc40d9a473_2_T1002'

devinfo_data = {
    'imei': '123456789012345',
    'imsi': '460000000000000',
    'androidId': 'test123456',
    'iccid': '89860000000000000000',
    'seriesNum': 'SN1234567890',
    'wifiMac': '02:00:00:00:00:01',
    'btaddr': '02:00:00:00:00:02',
    'brand': 'Xiaomi',
    'model': 'Mi 14',
    'device': 'mix',
    'manufacturer': 'Xiaomi',
    'build_fingerprint': 'Xiaomi/mix/mix:14/UP1A.231005.007/123456:user/release-keys'
}

body = json.dumps({'devinfo': json.dumps(devinfo_data, ensure_ascii=False)}).encode('utf-8')
req = urllib.request.Request(
    'http://192.168.10.83:83/set_custom_devinfo/' + ip + '/' + name,
    data=body,
    headers={'Content-Type': 'application/json'},
    method='POST'
)
try:
    resp = urllib.request.urlopen(req)
    print('Test 1 (all in devinfo string):', resp.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    print('Test 1 error:', e.code, e.read().decode('utf-8')[:300])

# Test 2: fields as query params + minimal devinfo
params = 'imei=123456789012345&imsi=460000000000000&androidId=test123456'
body2 = json.dumps({'devinfo': json.dumps({'brand': 'Xiaomi', 'model': 'Mi 14'}, ensure_ascii=False)}).encode('utf-8')
req2 = urllib.request.Request(
    'http://192.168.10.83:83/set_custom_devinfo/' + ip + '/' + name + '?' + params,
    data=body2,
    headers={'Content-Type': 'application/json'},
    method='POST'
)
try:
    resp2 = urllib.request.urlopen(req2)
    print('Test 2 (query+devinfo):', resp2.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    print('Test 2 error:', e.code, e.read().decode('utf-8')[:300])

# Test 3: ALL fields as query params, NO body
req3 = urllib.request.Request(
    'http://192.168.10.83:83/set_custom_devinfo/' + ip + '/' + name + '?' + params,
    method='POST'
)
try:
    resp3 = urllib.request.urlopen(req3)
    print('Test 3 (query only, no body):', resp3.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    print('Test 3 error:', e.code, e.read().decode('utf-8')[:300])

# Test 4: just one simple devinfo field
body4 = json.dumps({'devinfo': '{"imei":"123456789012345"}'}).encode('utf-8')
req4 = urllib.request.Request(
    'http://192.168.10.83:83/set_custom_devinfo/' + ip + '/' + name,
    data=body4,
    headers={'Content-Type': 'application/json'},
    method='POST'
)
try:
    resp4 = urllib.request.urlopen(req4)
    print('Test 4 (simple):', resp4.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    print('Test 4 error:', e.code, e.read().decode('utf-8')[:300])

# Test 5: body with devinfo as direct JSON object (NOT string)
body5 = json.dumps({'devinfo': {'imei': '123456789012345', 'brand': 'Xiaomi'}}).encode('utf-8')
req5 = urllib.request.Request(
    'http://192.168.10.83:83/set_custom_devinfo/' + ip + '/' + name,
    data=body5,
    headers={'Content-Type': 'application/json'},
    method='POST'
)
try:
    resp5 = urllib.request.urlopen(req5)
    print('Test 5 (devinfo as object):', resp5.read().decode('utf-8'))
except urllib.error.HTTPError as e:
    print('Test 5 error:', e.code, e.read().decode('utf-8')[:300])
except Exception as e:
    print('Test 5 exception:', e)