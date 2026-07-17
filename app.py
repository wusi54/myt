import requests
import json
import time
import uuid
import os
import sqlite3
import threading
import subprocess
from flask import Flask, request, jsonify, render_template, send_file
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False
socketio = SocketIO(app, cors_allowed_origins="*")

# 活跃的投屏流: { sid: { proc, name, adb_addr } }
screen_streams = {}

API_BASE = "http://192.168.10.83:83"
TIMEOUT = 30
UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.db')

os.makedirs(UPLOAD_DIR, exist_ok=True)

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS devices (
        ip TEXT PRIMARY KEY, port TEXT DEFAULT '83', name TEXT DEFAULT '',
        status TEXT DEFAULT 'unknown', container_count INTEGER DEFAULT 0, host_ver TEXT DEFAULT '',
        created TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS proxy_pool (
        id TEXT PRIMARY KEY, ip TEXT NOT NULL, port TEXT NOT NULL,
        user TEXT DEFAULT '', pwd TEXT DEFAULT '', name TEXT DEFAULT '',
        type TEXT DEFAULT 'socks5', created TEXT DEFAULT '',
        latency TEXT DEFAULT '', region TEXT DEFAULT '', isp TEXT DEFAULT ''
    )''')
    # migration: add columns if missing
    for col in ['latency','region','isp']:
        try: c.execute(f'ALTER TABLE proxy_pool ADD COLUMN {col} TEXT DEFAULT \'\'')
        except: pass
    c.execute('''CREATE TABLE IF NOT EXISTS fingerprint_library (
        id TEXT PRIMARY KEY, name TEXT DEFAULT '', data TEXT DEFAULT '{}',
        created TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS real_library (
        id TEXT PRIMARY KEY, name TEXT DEFAULT '', description TEXT DEFAULT '',
        files TEXT DEFAULT '[]', created TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS container_configs (
        device_ip TEXT NOT NULL, container_name TEXT NOT NULL,
        config_type TEXT DEFAULT '', config_id TEXT DEFAULT '',
        config_name TEXT DEFAULT '', applied_at TEXT DEFAULT '',
        PRIMARY KEY (device_ip, container_name)
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS app_library (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, filename TEXT NOT NULL,
        filesize INTEGER DEFAULT 0, pkg TEXT DEFAULT '',
        created TEXT DEFAULT ''
    )''')
    c.execute('''CREATE TABLE IF NOT EXISTS install_logs (
        id TEXT PRIMARY KEY, app_id TEXT NOT NULL, app_name TEXT DEFAULT '',
        device_ip TEXT NOT NULL, container_name TEXT NOT NULL,
        status TEXT DEFAULT 'pending', message TEXT DEFAULT '',
        created TEXT DEFAULT ''
    )''')
    conn.commit()
    conn.close()

APP_UPLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app_uploads')
os.makedirs(APP_UPLOAD_DIR, exist_ok=True)

init_db()

# repair garbled proxy names (Chinese chars lost as '?')
def _repair_garbled_names():
    conn = get_db()
    rows = conn.execute('SELECT id, name, ip, port FROM proxy_pool').fetchall()
    for r in rows:
        if r['name'] and '?' in r['name']:
            fixed = f"{r['ip']}:{r['port']}"
            conn.execute('UPDATE proxy_pool SET name=? WHERE id=?', (fixed, r['id']))
    conn.commit()
    conn.close()
_repair_garbled_names()

# ========== DB helpers: devices ==========
def load_devices():
    conn = get_db()
    rows = conn.execute('SELECT * FROM devices ORDER BY created DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_device(dev):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO devices (ip,port,name,status,container_count,host_ver,created) VALUES (?,?,?,?,?,?,?)',
                 (dev['ip'], dev.get('port','83'), dev.get('name',''), dev.get('status','unknown'),
                  dev.get('container_count',0), dev.get('host_ver',''), dev.get('created',time.strftime('%Y-%m-%d %H:%M:%S'))))
    conn.commit()
    conn.close()

def delete_device(ip):
    conn = get_db()
    conn.execute('DELETE FROM devices WHERE ip = ?', (ip,))
    conn.commit()
    conn.close()

# ========== DB helpers: proxy_pool ==========
def load_proxy_pool():
    conn = get_db()
    rows = conn.execute('SELECT * FROM proxy_pool ORDER BY created DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_proxy_to_db(p):
    conn = get_db()
    conn.execute('''INSERT OR REPLACE INTO proxy_pool
        (id,ip,port,user,pwd,name,type,created,latency,region,isp)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                 (p['id'], p['ip'], p['port'], p.get('user',''), p.get('pwd',''),
                  p['name'], p.get('type','socks5'), p['created'],
                  p.get('latency',''), p.get('region',''), p.get('isp','')))
    conn.commit()
    conn.close()

def delete_proxy_from_db(pid):
    conn = get_db()
    conn.execute('DELETE FROM proxy_pool WHERE id = ?', (pid,))
    conn.commit()
    conn.close()

# ========== DB helpers: fingerprint ==========
def load_fingerprints():
    conn = get_db()
    rows = conn.execute('SELECT * FROM fingerprint_library ORDER BY created DESC').fetchall()
    conn.close()
    result = []
    for r in rows:
        d = json.loads(r['data'])
        d['id'] = r['id']
        d['name'] = r['name']
        d['created'] = r['created']
        result.append(d)
    return result

def save_fingerprint(fp):
    data = {k: v for k, v in fp.items() if k not in ('id','name','created')}
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO fingerprint_library (id,name,data,created) VALUES (?,?,?,?)',
                 (fp['id'], fp['name'], json.dumps(data, ensure_ascii=False), fp.get('created',time.strftime('%Y-%m-%d %H:%M:%S'))))
    conn.commit()
    conn.close()

def delete_fingerprint_from_db(fid):
    conn = get_db()
    conn.execute('DELETE FROM fingerprint_library WHERE id = ?', (fid,))
    conn.commit()
    conn.close()

# ========== DB helpers: real_library ==========
def load_real_library():
    conn = get_db()
    rows = conn.execute('SELECT * FROM real_library ORDER BY created DESC').fetchall()
    conn.close()
    result = []
    for r in rows:
        d = {'id': r['id'], 'name': r['name'], 'description': r['description'],
             'files': json.loads(r['files']), 'created': r['created']}
        result.append(d)
    return result

def save_real_library(entry):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO real_library (id,name,description,files,created) VALUES (?,?,?,?,?)',
                 (entry['id'], entry['name'], entry.get('description',''),
                  json.dumps(entry.get('files',[]), ensure_ascii=False),
                  entry.get('created',time.strftime('%Y-%m-%d %H:%M:%S'))))
    conn.commit()
    conn.close()

def delete_real_library_from_db(lid):
    conn = get_db()
    conn.execute('DELETE FROM real_library WHERE id = ?', (lid,))
    conn.commit()
    conn.close()

# ========== DB helpers: app_library ==========
def load_app_library():
    conn = get_db()
    rows = conn.execute('SELECT * FROM app_library ORDER BY created DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]

def save_app_to_db(app):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO app_library (id,name,filename,filesize,pkg,created) VALUES (?,?,?,?,?,?)',
                 (app['id'], app['name'], app['filename'], app.get('filesize',0),
                  app.get('pkg',''), app.get('created',time.strftime('%Y-%m-%d %H:%M:%S'))))
    conn.commit()
    conn.close()

def delete_app_from_db(aid):
    conn = get_db()
    conn.execute('DELETE FROM app_library WHERE id = ?', (aid,))
    conn.commit()
    conn.close()

# ========== DB helpers: install_logs ==========
def save_install_log(log):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO install_logs (id,app_id,app_name,device_ip,container_name,status,message,created) VALUES (?,?,?,?,?,?,?,?)',
                 (log['id'], log['app_id'], log.get('app_name',''), log['device_ip'], log['container_name'],
                  log['status'], log.get('message',''), log.get('created',time.strftime('%Y-%m-%d %H:%M:%S'))))
    conn.commit()
    conn.close()

def load_install_logs(app_id=None):
    conn = get_db()
    if app_id:
        rows = conn.execute('SELECT * FROM install_logs WHERE app_id=? ORDER BY created DESC', (app_id,)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM install_logs ORDER BY created DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ========== DB helpers: container configs ==========
def set_container_config(device_ip, container_name, config_type, config_id, config_name):
    conn = get_db()
    conn.execute('INSERT OR REPLACE INTO container_configs (device_ip,container_name,config_type,config_id,config_name,applied_at) VALUES (?,?,?,?,?,?)',
                 (device_ip, container_name, config_type, config_id, config_name, time.strftime('%Y-%m-%d %H:%M:%S')))
    conn.commit()
    conn.close()

def get_container_config(device_ip, container_name):
    conn = get_db()
    row = conn.execute('SELECT * FROM container_configs WHERE device_ip=? AND container_name=?',
                       (device_ip, container_name)).fetchone()
    conn.close()
    return dict(row) if row else None

def delete_container_config(device_ip, container_name):
    conn = get_db()
    conn.execute('DELETE FROM container_configs WHERE device_ip=? AND container_name=?',
                 (device_ip, container_name))
    conn.commit()
    conn.close()

def get_all_container_configs():
    conn = get_db()
    rows = conn.execute('SELECT * FROM container_configs').fetchall()
    conn.close()
    return [dict(r) for r in rows]

# Load data on startup
devices = load_devices()
proxy_pool = load_proxy_pool()
fingerprint_library = load_fingerprints()
real_library = load_real_library()
app_library = load_app_library()

def api_get(path, params=None):
    url = f"{API_BASE}{path}"
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        return r.json() if r.text else {"code": 500, "msg": "empty response"}
    except Exception as e:
        return {"code": 500, "msg": str(e)}

def api_post(path, params=None, data=None):
    url = f"{API_BASE}{path}"
    try:
        r = requests.post(url, params=params, json=data, timeout=TIMEOUT)
        return r.json() if r.text else {"code": 500, "msg": "empty response"}
    except Exception as e:
        return {"code": 500, "msg": str(e)}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/devices')
def get_devices():
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    for d in devices:
        try:
            r = requests.get(f"{API_BASE}/dc_api/v1/list/{d['ip']}", timeout=TIMEOUT)
            if r.ok:
                data = r.json().get('data', []) or r.json().get('result', [])
                d['container_count'] = len(data)
                d['status'] = 'online'
            else:
                d['status'] = 'offline'
        except:
            d['status'] = 'offline'
        try:
            r = requests.get(f"{API_BASE}/dc_api/v1/get_host_ver/{d['ip']}", timeout=TIMEOUT)
            if r.ok:
                j = r.json()
                d['host_ver'] = j.get('data', '') or j.get('message', '')
        except:
            pass
    return jsonify({"code": 200, "data": devices, "msg": "ok"})

@app.route('/api/device/add', methods=['POST'])
def add_device():
    global devices
    data = request.json
    ip = data.get('ip', '').strip()
    port = data.get('port', '83').strip()
    name = data.get('name', '').strip()
    if not ip:
        return jsonify({"code": 400, "msg": "IP is required"})
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    for d in devices:
        if d['ip'] == ip:
            d.update({'port': port, 'name': name or ip, 'status': 'unknown'})
            save_device(d)
            return jsonify({"code": 200, "msg": "updated"})
    dev = {"ip": ip, "port": port, "name": name or ip, "status": "unknown",
           "container_count": 0, "host_ver": "", "created": now}
    devices.append(dev)
    save_device(dev)
    return jsonify({"code": 200, "msg": "added"})

@app.route('/api/device/remove', methods=['POST'])
def remove_device():
    global devices
    ip = request.json.get('ip', '')
    devices = [d for d in devices if d['ip'] != ip]
    delete_device(ip)
    return jsonify({"code": 200, "msg": "removed"})

@app.route('/api/device/check/<ip>')
def check_device(ip):
    result = api_get(f"/get/{ip}")
    if result.get('code') == 200:
        return jsonify({"code": 200, "data": result, "msg": "ok"})
    return jsonify({"code": 500, "msg": "device not reachable"})

@app.route('/api/device/check_all', methods=['POST'])
def check_all_devices():
    global devices
    results = {}
    for dev in devices:
        ip = dev['ip']
        try:
            r = requests.get(f"{API_BASE}/get/{ip}", timeout=5)
            if r.ok and r.json().get('code') == 200:
                results[ip] = 'online'
                dev['status'] = 'online'
            else:
                results[ip] = 'offline'
                dev['status'] = 'offline'
        except:
            results[ip] = 'offline'
            dev['status'] = 'offline'
        save_device(dev)
    return jsonify({"code": 200, "data": results, "msg": "ok"})

@app.route('/api/device/host_ver/<ip>')
def host_ver(ip):
    result = api_get(f"/dc_api/v1/get_host_ver/{ip}")
    return jsonify(result)

@app.route('/api/device/network/<ip>')
def network_detail(ip):
    result = api_get(f"/dc_api/v1/get_network_detail/{ip}")
    return jsonify(result)

@app.route('/api/containers/<ip>')
def list_containers(ip):
    result = api_get(f"/dc_api/v1/list/{ip}")
    return jsonify(result)

@app.route('/api/container/<ip>/<name>')
def container_detail(ip, name):
    result = api_get(f"/dc_api/v1/get_android_detail/{ip}/{name}")
    return jsonify(result)

@app.route('/api/container/<ip>/<name>/start', methods=['POST'])
def start_container(ip, name):
    result = api_get(f"/dc_api/v1/run/{ip}/{name}")
    return jsonify(result)

@app.route('/api/container/<ip>/<name>/stop', methods=['POST'])
def stop_container(ip, name):
    result = api_get(f"/dc_api/v1/stop/{ip}/{name}")
    return jsonify(result)

@app.route('/api/container/<ip>/<name>/reset', methods=['POST'])
def reset_container(ip, name):
    result = api_get(f"/dc_api/v1/reset/{ip}/{name}")
    return jsonify(result)

@app.route('/api/container/<ip>/<name>/remove', methods=['POST'])
def remove_container(ip, name):
    result = api_get(f"/dc_api/v1/remove/{ip}/{name}")
    return jsonify(result)

@app.route('/api/container/<ip>/<name>/boot_status')
def boot_status(ip, name):
    result = api_get(f"/dc_api/v1/get_android_boot_status/{ip}/{name}")
    return jsonify(result)

@app.route('/api/container/<ip>/create', methods=['POST'])
def create_container(ip):
    data = request.json
    result = api_get(f"/dc_api/v1/create/{ip}/{data.get('index',0)}/{data.get('name','')}")
    return jsonify(result)

@app.route('/api/container/<ip>/<name>/random_devinfo', methods=['POST'])
def random_devinfo(ip, name):
    result = api_get(f"/random_devinfo/{ip}/{name}")
    return jsonify(result)

@app.route('/api/container/<ip>/<name>/random_devinfo_async', methods=['POST'])
def random_devinfo_async(ip, name):
    result = api_get(f"/random_devinfo_async/{ip}/{name}")
    return jsonify(result)

def _call_set_custom_devinfo(ip, name, params):
    url = f"{API_BASE}/set_custom_devinfo/{ip}/{name}"
    try:
        r = requests.post(url, json=params, timeout=TIMEOUT)
        return r.json() if r.text else {"code": 500, "msg": "empty"}
    except Exception as e:
        return {"code": 500, "msg": str(e)}

@app.route('/api/container/<ip>/<name>/set_custom_devinfo', methods=['POST'])
def set_custom_devinfo(ip, name):
    result = _call_set_custom_devinfo(ip, name, request.json)
    return jsonify(result)

@app.route('/api/container/<ip>/<name>/upload_fingerprint', methods=['POST'])
def upload_fingerprint(ip, name):
    data = request.json
    params = {}
    for key in ['lac', 'cid', 'lat', 'lon', 'mcc', 'mnc', 'phonenumber',
                'country', 'language', 'timezone', 'opercode', 'opername',
                'iccid', 'imsi', 'imei', 'gaid']:
        if key in data and data[key]:
            params[key] = data[key]
    result = api_get(f"/upload_fingerprint/{ip}/{name}", params=params)
    return jsonify(result)

@app.route('/api/container/<ip>/<name>/s5_set', methods=['POST'])
def s5_set(ip, name):
    data = request.json
    params = {"action": data.get("action", "add")}
    for key in ['s5ip', 's5port', 's5user', 's5pwd', 'domain_mode', 'proxy_mode']:
        if key in data and data[key]:
            params[key] = data[key]
    result = api_get(f"/s5_set/{ip}/{name}", params=params)
    return jsonify(result)

@app.route('/api/container/<ip>/<name>/s5_query')
def s5_query(ip, name):
    result = api_get(f"/s5_query/{ip}/{name}")
    return jsonify(result)

@app.route('/api/container/<ip>/<name>/s5_stop', methods=['POST'])
def s5_stop(ip, name):
    result = api_get(f"/s5_stop/{ip}/{name}")
    return jsonify(result)

@app.route('/api/devinfo_list')
def devinfo_list():
    result = api_get("/get_devinfo")
    return jsonify(result)

@app.route('/api/img_list')
def img_list():
    result = api_get("/get_img_list")
    return jsonify(result)

@app.route('/api/export/<ip>/<name>')
def export_container(ip, name):
    local = request.args.get('local', '')
    params = {"local": local} if local else {}
    result = api_get(f"/dc_api/v1/export/{ip}/{name}", params=params)
    return jsonify(result)

@app.route('/api/import/<ip>/<new_name>/<index>', methods=['POST'])
def import_container(ip, new_name, index):
    data = request.json
    params = {}
    if data.get('local'): params['local'] = data['local']
    if data.get('url'): params['url'] = data['url']
    if data.get('rpa_port'): params['rpa_port'] = data['rpa_port']
    result = api_get(f"/dc_api/v1/import/{ip}/{new_name}/{index}", params=params)
    return jsonify(result)

@app.route('/api/container/<ip>/<name>/shell', methods=['POST'])
def shell_command(ip, name):
    cmd = request.json.get('cmd', '')
    result = api_get(f"/shell/{ip}/{name}", params={"cmd": cmd})
    return jsonify(result)

@app.route('/api/device/query_myt')
def query_myt():
    result = api_get("/host_api/v1/query_myt")
    return jsonify(result)

@app.route('/api/device/systeminfo')
def systeminfo():
    result = api_get("/host_api/v1/systeminfo")
    return jsonify(result)

@app.route('/api/containers/all')
def all_containers():
    all_list = []
    configs = get_all_container_configs()
    config_map = {(c['device_ip'], c['container_name']): c for c in configs}
    for dev in devices:
        ip = dev['ip']
        try:
            r = requests.get(f"{API_BASE}/dc_api/v1/list/{ip}", timeout=TIMEOUT)
            if r.ok:
                for c in (r.json().get('data', []) or r.json().get('result', [])):
                    c['device_ip'] = ip
                    key = (ip, c.get('Name') or c.get('name', ''))
                    if key in config_map:
                        c['config_name'] = config_map[key]['config_name']
                        c['config_type'] = config_map[key]['config_type']
                    all_list.append(c)
        except:
            pass
    return jsonify({"code": 200, "data": all_list})

@app.route('/api/container_configs')
def container_configs():
    return jsonify({"code": 200, "data": get_all_container_configs()})

# ========== 真机参数库 ==========
@app.route('/api/fingerprints', methods=['GET'])
def list_fingerprints():
    return jsonify({"code": 200, "data": fingerprint_library})

@app.route('/api/fingerprints', methods=['POST'])
def add_fingerprint():
    global fingerprint_library
    data = request.json
    fp = {"id": str(uuid.uuid4())[:8], "name": data.get('name', '未命名'),
          "created": time.strftime('%Y-%m-%d %H:%M:%S')}
    for key in ['brand','model','device','manufacturer','board','hardware',
        'build_id','build_fingerprint','build_tags','build_type',
        'build_version_release','build_version_sdk','build_description',
        'serialno','mac','imei','meid','imsi','android_id',
        'wifi_mac','bt_mac','baseband_version','bootloader',
        'product','product_name','product_device',
        'lac','cid','lat','lon','mcc','mnc','phonenumber',
        'country','language','timezone','opercode','opername','iccid','gaid']:
        if key in data and data[key]:
            fp[key] = data[key]
    fingerprint_library.append(fp)
    save_fingerprint(fp)
    return jsonify({"code": 200, "msg": "added", "data": fp})

@app.route('/api/fingerprints/<fp_id>', methods=['DELETE'])
def delete_fingerprint(fp_id):
    global fingerprint_library
    fingerprint_library = [f for f in fingerprint_library if f.get('id') != fp_id]
    delete_fingerprint_from_db(fp_id)
    return jsonify({"code": 200, "msg": "deleted"})

@app.route('/api/fingerprints/<fp_id>/apply', methods=['POST'])
def apply_fingerprint(fp_id):
    data = request.json
    ip, name = data.get('ip',''), data.get('name','')
    fp = next((f for f in fingerprint_library if f.get('id') == fp_id), None)
    if not fp: return jsonify({"code": 404, "msg": "fingerprint not found"})
    params = {k: v for k, v in fp.items() if k not in ('id','name','created')}
    result = _call_set_custom_devinfo(ip, name, params)
    set_container_config(ip, name, 'fingerprint', fp_id, fp.get('name',''))
    return jsonify(result)

# ========== 真机库 ==========
@app.route('/api/real_library', methods=['GET'])
def list_real_library():
    return jsonify({"code": 200, "data": real_library})

@app.route('/api/real_library', methods=['POST'])
def add_real_library():
    global real_library
    name = request.form.get('name', '').strip()
    if not name: return jsonify({"code": 400, "msg": "name required"})
    entry = {"id": str(uuid.uuid4())[:8], "name": name,
             "description": request.form.get('description', ''),
             "created": time.strftime('%Y-%m-%d %H:%M:%S'), "files": []}
    if 'file' in request.files:
        for f in request.files.getlist('file'):
            if f.filename:
                safe = f"{uuid.uuid4().hex[:8]}_{f.filename}"
                path = os.path.join(UPLOAD_DIR, safe)
                f.save(path)
                entry["files"].append({"original_name": f.filename, "stored_name": safe,
                                       "path": path, "size": os.path.getsize(path)})
    real_library.append(entry)
    save_real_library(entry)
    return jsonify({"code": 200, "msg": "added", "data": entry})

@app.route('/api/real_library/<lib_id>', methods=['DELETE'])
def delete_real_library(lib_id):
    global real_library
    entry = next((e for e in real_library if e['id'] == lib_id), None)
    if entry:
        for f in entry.get('files', []):
            try: os.remove(f['path'])
            except: pass
    real_library = [e for e in real_library if e['id'] != lib_id]
    delete_real_library_from_db(lib_id)
    return jsonify({"code": 200, "msg": "deleted"})

@app.route('/api/real_library/<lib_id>/apply', methods=['POST'])
def apply_real_library(lib_id):
    data = request.json
    ip, name = data.get('ip',''), data.get('name','')
    entry = next((e for e in real_library if e['id'] == lib_id), None)
    if not entry: return jsonify({"code": 404, "msg": "not found"})
    params = {}
    for f in entry.get('files', []):
        if f['original_name'].endswith('.json') or f['original_name'].endswith('.bin'):
            try:
                with open(f['path'], 'r', encoding='utf-8') as fp:
                    c = json.load(fp)
                    if isinstance(c, dict): params.update(c)
            except: pass
    result = _call_set_custom_devinfo(ip, name, params)
    set_container_config(ip, name, 'real_library', lib_id, entry.get('name',''))
    return jsonify(result)

# ========== 应用管理 ==========
@app.route('/api/apps', methods=['GET'])
def list_apps():
    return jsonify({"code": 200, "data": app_library})

@app.route('/api/apps/upload', methods=['POST'])
def upload_app():
    global app_library
    if 'file' not in request.files:
        return jsonify({"code": 400, "msg": "no file"})
    f = request.files['file']
    if not f.filename or not f.filename.lower().endswith('.apk'):
        return jsonify({"code": 400, "msg": "only .apk files allowed"})
    name = request.form.get('name', '') or f.filename
    fid = str(uuid.uuid4())[:8]
    safe_name = f"{fid}_{f.filename}"
    save_path = os.path.join(APP_UPLOAD_DIR, safe_name)
    f.save(save_path)
    filesize = os.path.getsize(save_path)
    app_entry = {"id": fid, "name": name, "filename": safe_name,
                 "filesize": filesize, "pkg": "",
                 "created": time.strftime('%Y-%m-%d %H:%M:%S')}
    app_library.insert(0, app_entry)
    save_app_to_db(app_entry)
    return jsonify({"code": 200, "msg": "uploaded", "data": app_entry})

@app.route('/api/apps/<app_id>', methods=['DELETE'])
def delete_app(app_id):
    global app_library
    entry = next((a for a in app_library if a['id'] == app_id), None)
    if not entry: return jsonify({"code": 404, "msg": "not found"})
    fpath = os.path.join(APP_UPLOAD_DIR, entry['filename'])
    if os.path.exists(fpath): os.remove(fpath)
    app_library = [a for a in app_library if a['id'] != app_id]
    delete_app_from_db(app_id)
    return jsonify({"code": 200, "msg": "deleted"})

def _install_apk_to_container(api_base, ip, name, apk_path, filename, timeout=300):
    """Upload APK to container via upload_file_android, then pm install"""
    try:
        # Step 1: upload APK file to container
        with open(apk_path, 'rb') as f:
            r = requests.post(f"{api_base}/and_api/v1/upload_file_android/{ip}/{name}",
                              files={'file': (filename, f, 'application/octet-stream')},
                              timeout=timeout)
        upload_resp = r.json() if r.text else {}
        if upload_resp.get('code') != 200:
            return 'failed', f'upload failed: {json.dumps(upload_resp, ensure_ascii=False)}'

        # File is saved to /data/media/0/Download/<filename>
        src = f'/data/media/0/Download/{filename}'
        dst = f'/data/local/tmp/{filename}'

        # Step 2: copy to /data/local/tmp/ (pm install requires files here)
        r2 = requests.post(f"{api_base}/and_api/v1/shell/{ip}/{name}",
                           json={'cmd': f'cp "{src}" "{dst}" && chmod 644 "{dst}" && echo "OK"'},
                           timeout=60)
        copy_msg = (r2.json() if r2.text else {}).get('message', '')
        if 'OK' not in copy_msg:
            return 'failed', f'copy failed: {copy_msg[:200]}'

        # Step 3: pm install
        r3 = requests.post(f"{api_base}/and_api/v1/shell/{ip}/{name}",
                           json={'cmd': f'pm install -r "{dst}" 2>&1'},
                           timeout=timeout)
        install_msg = (r3.json() if r3.text else {}).get('message', '')
        if 'Success' in install_msg or 'success' in install_msg:
            return 'success', install_msg[:500]
        else:
            return 'failed', install_msg[:500]
    except Exception as e:
        return 'failed', str(e)

@app.route('/api/apps/<app_id>/install', methods=['POST'])
def install_app_to_container(app_id):
    data = request.json
    containers = data.get('containers', [])
    if not containers: return jsonify({"code": 400, "msg": "containers list required"})
    entry = next((a for a in app_library if a['id'] == app_id), None)
    if not entry: return jsonify({"code": 404, "msg": "app not found"})
    apk_path = os.path.join(APP_UPLOAD_DIR, entry['filename'])
    if not os.path.exists(apk_path): return jsonify({"code": 404, "msg": "apk file not found"})

    results = []
    for item in containers:
        ip = item.get('ip'); name = item.get('name')
        if not ip or not name: continue
        log_id = str(uuid.uuid4())[:8]
        log_entry = {"id": log_id, "app_id": app_id, "app_name": entry['name'],
                     "device_ip": ip, "container_name": name,
                     "status": "installing", "message": "",
                     "created": time.strftime('%Y-%m-%d %H:%M:%S')}
        save_install_log(log_entry)

        status, msg = _install_apk_to_container(API_BASE, ip, name, apk_path, entry['filename'])
        log_entry['status'] = status
        log_entry['message'] = msg
        save_install_log(log_entry)
        results.append(log_entry)

    return jsonify({"code": 200, "msg": "install completed", "data": results})

@app.route('/api/apps/<app_id>/download')
def download_app(app_id):
    entry = next((a for a in app_library if a['id'] == app_id), None)
    if not entry: return jsonify({"code": 404, "msg": "not found"}), 404
    fpath = os.path.join(APP_UPLOAD_DIR, entry['filename'])
    if not os.path.exists(fpath): return jsonify({"code": 404, "msg": "file not found"}), 404
    return send_file(fpath, as_attachment=True, download_name=entry['filename'], mimetype='application/vnd.android.package-archive')

@app.route('/api/apps/<app_id>/logs', methods=['GET'])
def app_install_logs(app_id):
    logs = load_install_logs(app_id)
    return jsonify({"code": 200, "data": logs})

# ========== 代理池 ==========
@app.route('/api/proxy_pool', methods=['GET'])
def list_proxy_pool():
    return jsonify({"code": 200, "data": proxy_pool})

@app.route('/api/proxy_pool', methods=['POST'])
def add_proxy():
    global proxy_pool
    data = request.json
    if not data.get('ip') or not data.get('port'):
        return jsonify({"code": 400, "msg": "ip and port required"})
    proxy = {"id": str(uuid.uuid4())[:8], "ip": data['ip'].strip(),
             "port": str(data['port']).strip(), "user": data.get('user',''),
             "pwd": data.get('pwd',''),
             "name": data.get('name','') or f"{data['ip']}:{data['port']}",
             "type": data.get('type','socks5'),
             "created": time.strftime('%Y-%m-%d %H:%M:%S')}
    proxy_pool.append(proxy)
    save_proxy_to_db(proxy)
    return jsonify({"code": 200, "msg": "added", "data": proxy})

@app.route('/api/proxy_pool/<proxy_id>', methods=['PUT'])
def update_proxy(proxy_id):
    global proxy_pool
    data = request.json
    proxy = next((p for p in proxy_pool if p['id'] == proxy_id), None)
    if not proxy:
        return jsonify({"code": 404, "msg": "proxy not found"})
    if not data.get('ip') or not data.get('port'):
        return jsonify({"code": 400, "msg": "ip and port required"})
    proxy['ip'] = data['ip'].strip()
    proxy['port'] = str(data['port']).strip()
    proxy['user'] = data.get('user', '')
    proxy['pwd'] = data.get('pwd', '')
    proxy['name'] = data.get('name', '') or f"{data['ip']}:{data['port']}"
    proxy['type'] = data.get('type', 'socks5')
    save_proxy_to_db(proxy)
    return jsonify({"code": 200, "msg": "updated", "data": proxy})

@app.route('/api/proxy_pool/<proxy_id>', methods=['DELETE'])
def delete_proxy(proxy_id):
    global proxy_pool
    proxy_pool = [p for p in proxy_pool if p['id'] != proxy_id]
    delete_proxy_from_db(proxy_id)
    return jsonify({"code": 200, "msg": "deleted"})

@app.route('/api/proxy_pool/<proxy_id>/apply', methods=['POST'])
def apply_proxy(proxy_id):
    data = request.json
    proxy = next((p for p in proxy_pool if p['id'] == proxy_id), None)
    if not proxy: return jsonify({"code": 404, "msg": "proxy not found"})
    params = {"action": "add", "s5ip": proxy['ip'], "s5port": proxy['port'],
              "s5user": proxy.get('user',''), "s5pwd": proxy.get('pwd','')}
    result = api_get(f"/s5_set/{data['ip']}/{data['name']}", params=params)
    return jsonify(result)

@app.route('/api/proxy_pool/<proxy_id>/test', methods=['POST'])
def test_proxy(proxy_id):
    import socket as _socket, time as _time, struct

    proxy = next((p for p in proxy_pool if p['id'] == proxy_id), None)
    if not proxy: return jsonify({"code": 404, "msg": "proxy not found"})

    host, port = proxy['ip'], int(proxy['port'])
    user, pwd = proxy.get('user',''), proxy.get('pwd','')

    def socks5_test():
        try:
            sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
            sock.settimeout(10)
            t0 = _time.time()
            sock.connect((host, port))

            # SOCKS5 handshake
            if user and pwd:
                sock.sendall(struct.pack('!BB', 5, 2) + struct.pack('BB', 0, 2))
            else:
                sock.sendall(struct.pack('!BB', 5, 1) + struct.pack('B', 0))
            ver, nauth = struct.unpack('!BB', sock.recv(2))
            if nauth == 2 and user and pwd:
                ubytes = user.encode(); pbytes = pwd.encode()
                sock.sendall(struct.pack('!BB', 1, len(ubytes)) + ubytes + struct.pack('B', len(pbytes)) + pbytes)
                auth_ver = sock.recv(2)
            elif nauth != 0:
                sock.close()
                return None, 'auth method rejected'

            # CONNECT to httpbin.org
            dst = b'www.baidu.com'
            sock.sendall(struct.pack('!BBBB', 5, 1, 0, 3) + struct.pack('B', len(dst)) + dst + struct.pack('!H', 80))
            resp = sock.recv(10)
            if len(resp) < 2 or resp[1] != 0:
                sock.close()
                return None, f'connect failed: code={resp[1] if len(resp)>1 else -1}'

            # HTTP GET
            sock.sendall(b'GET / HTTP/1.0\r\nHost: www.baidu.com\r\n\r\n')
            sock.recv(1024)
            latency_ms = round((_time.time() - t0) * 1000)
            sock.close()
            return latency_ms, None
        except Exception as e:
            return None, str(e)

    latency_ms, err = socks5_test()
    if err:
        # still try geo IP even if proxy test fails
        region = isp = ''
        try:
            r = requests.get(f'http://ip-api.com/json/{host}?lang=zh-CN', timeout=5)
            if r.status_code == 200:
                d = r.json()
                if d.get('status') == 'success':
                    region = f"{d.get('country','')} {d.get('regionName','')} {d.get('city','')}".strip()
                    isp = d.get('isp','')
        except: pass
        proxy['latency'] = ''
        proxy['region'] = region
        proxy['isp'] = isp
        save_proxy_to_db(proxy)
        return jsonify({"code": 500, "msg": f"test failed: {err}", "data": {"region": region, "isp": isp}})

    # geo IP lookup
    region = isp = ''
    try:
        r = requests.get(f'http://ip-api.com/json/{host}?lang=zh-CN', timeout=5)
        if r.status_code == 200:
            d = r.json()
            if d.get('status') == 'success':
                region = f"{d.get('country','')} {d.get('regionName','')} {d.get('city','')}".strip()
                isp = d.get('isp','')
    except: pass

    proxy['latency'] = f'{latency_ms}ms'
    proxy['region'] = region
    proxy['isp'] = isp
    save_proxy_to_db(proxy)

    return jsonify({
        "code": 200, "msg": "ok",
        "data": {"latency": f"{latency_ms}ms", "region": region, "isp": isp}
    })

@app.route('/api/proxy_pool/container_counts')
def proxy_container_counts():
    import re
    counts = {}
    for dev in devices:
        try:
            r = requests.get(f"{API_BASE}/dc_api/v1/list/{dev['ip']}", timeout=TIMEOUT)
            if r.ok:
                for c in (r.json().get('data', []) or r.json().get('result', [])):
                    s5 = c.get('s5', {}) or {}
                    addr = (s5.get('addr') or '').strip()
                    if addr:
                        # parse socks5://user:pwd@ip:port -> ip:port
                        m = re.search(r'@([^@]+)$', addr)
                        if m:
                            key = m.group(1)
                        else:
                            key = addr
                        counts[key] = counts.get(key, 0) + 1
        except:
            pass
    return jsonify({"code": 200, "data": counts})

@app.route('/api/screen/config')
def screen_config():
    return jsonify({
        "code": 200,
        "max_screens": 4
    })

# ========== Socket.IO 投屏 ==========
@socketio.on('start_screen')
def on_start_screen(data):
    name = data.get('name', '')
    adb_addr = data.get('adb_addr', '')
    if not name or not adb_addr:
        emit('screen_error', {'msg': 'missing name or adb_addr'})
        return

    sid = request.sid

    def stream_worker():
        try:
            proc = subprocess.Popen(
                ['adb', '-s', adb_addr, 'exec-out',
                 'screenrecord', '--output-format=h264',
                 '--size', '480x720',
                 '--bit-rate', '500000',
                 '--time-limit', '180', '-'],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL
            )
            screen_streams[sid] = {'proc': proc, 'name': name, 'adb_addr': adb_addr}

            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                socketio.emit('screen_frame', chunk, to=sid)

            proc.wait()
        except Exception as e:
            socketio.emit('screen_error', {'msg': str(e)}, to=sid)
        finally:
            screen_streams.pop(sid, None)
            socketio.emit('screen_end', {}, to=sid)

    t = threading.Thread(target=stream_worker, daemon=True)
    t.start()

@socketio.on('stop_screen')
def on_stop_screen():
    sid = request.sid
    stream = screen_streams.pop(sid, None)
    if stream:
        try: stream['proc'].kill()
        except: pass

@socketio.on('disconnect')
def on_disconnect():
    sid = request.sid
    stream = screen_streams.pop(sid, None)
    if stream:
        try: stream['proc'].kill()
        except: pass

if __name__ == '__main__':
    socketio.run(app, host='0.0.0.0', port=5001, debug=True, allow_unsafe_werkzeug=True)