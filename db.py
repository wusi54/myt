# -*- coding: utf-8 -*-
"""数据访问层：SQLite 表结构与增删改查封装。

V2.1.0 数据模型：
  devices      盒子/主机设备（主键为自增 index）
  containers   容器实例镜像（本地缓存，支持绑定真机参数/代理）
  real_params  真机参数库（文件内容 MD5 去重）
  proxies      代理池（socks5 代理，状态锁定防止多实例占用）
  app_library  应用库（APK）
  install_logs 安装日志
  container_configs 容器已导入配置记录（保留）
  users        平台用户（登录鉴权，管理员/普通用户）
"""
import os
import json
import time
import uuid
import sqlite3

DB_PATH = os.environ.get('DB_PATH') or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'data.db')


def now():
    return time.strftime('%Y-%m-%d %H:%M:%S')


def gen_id():
    return uuid.uuid4().hex[:12]


def gen_uuid():
    return str(uuid.uuid4())


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ip TEXT NOT NULL,
        port TEXT DEFAULT '83',
        url TEXT DEFAULT '',
        name TEXT DEFAULT '',
        status TEXT DEFAULT 'unknown',
        container_count INTEGER DEFAULT 0,
        host_ver TEXT DEFAULT '',
        systeminfo TEXT DEFAULT '',
        remark TEXT DEFAULT '',
        screen_net TEXT DEFAULT 'wan',
        enabled INTEGER DEFAULT 1,
        max_containers INTEGER DEFAULT 0,
        created TEXT DEFAULT '',
        updated TEXT DEFAULT '',
        UNIQUE(ip, port)
    )''',
    '''CREATE TABLE IF NOT EXISTS containers (
        id TEXT PRIMARY KEY,
        device_id INTEGER DEFAULT 0,
        device_ip TEXT DEFAULT '',
        name TEXT DEFAULT '',
        index_num INTEGER DEFAULT 0,
        short_id TEXT DEFAULT '',
        adb_port TEXT DEFAULT '',
        adb_addr TEXT DEFAULT '',
        image TEXT DEFAULT '',
        state TEXT DEFAULT '',
        status TEXT DEFAULT '',
        and_state INTEGER DEFAULT 0,
        api_port TEXT DEFAULT '',
        ctr_port TEXT DEFAULT '',
        webrtc TEXT DEFAULT '',
        s5_addr TEXT DEFAULT '',
        s5_status INTEGER DEFAULT 0,
        fp_id TEXT DEFAULT '',
        proxy_id TEXT DEFAULT '',
        resolution TEXT DEFAULT '',
        data_path TEXT DEFAULT '',
        screen_status TEXT DEFAULT '',
        created TEXT DEFAULT '',
        updated TEXT DEFAULT '',
        UNIQUE(device_ip, name)
    )''',
    '''CREATE TABLE IF NOT EXISTS real_params (
        id TEXT PRIMARY KEY,
        md5 TEXT UNIQUE,
        uuid TEXT DEFAULT '',
        name TEXT DEFAULT '',
        brand TEXT DEFAULT '',
        model TEXT DEFAULT '',
        status TEXT DEFAULT 'idle',
        in_use INTEGER DEFAULT 0,
        instance_id TEXT DEFAULT '',
        instance_name TEXT DEFAULT '',
        data TEXT DEFAULT '{}',
        files TEXT DEFAULT '[]',
        last_login_time TEXT DEFAULT '',
        last_offline_time TEXT DEFAULT '',
        created TEXT DEFAULT '',
        updated TEXT DEFAULT ''
    )''',
    '''CREATE TABLE IF NOT EXISTS proxies (
        id TEXT PRIMARY KEY,
        ip TEXT NOT NULL,
        port TEXT NOT NULL,
        user TEXT DEFAULT '',
        pwd TEXT DEFAULT '',
        type TEXT DEFAULT 'socks5',
        name TEXT DEFAULT '',
        status TEXT DEFAULT 'idle',
        in_use INTEGER DEFAULT 0,
        instance_id TEXT DEFAULT '',
        instance_name TEXT DEFAULT '',
        latency TEXT DEFAULT '',
        region TEXT DEFAULT '',
        isp TEXT DEFAULT '',
        source TEXT DEFAULT 'manual',
        last_login_time TEXT DEFAULT '',
        last_offline_time TEXT DEFAULT '',
        created TEXT DEFAULT '',
        updated TEXT DEFAULT ''
    )''',
    '''CREATE TABLE IF NOT EXISTS container_configs (
        device_ip TEXT NOT NULL,
        container_name TEXT NOT NULL,
        config_type TEXT DEFAULT '',
        config_id TEXT DEFAULT '',
        config_name TEXT DEFAULT '',
        applied_at TEXT DEFAULT '',
        PRIMARY KEY (device_ip, container_name, config_type)
    )''',
    '''CREATE TABLE IF NOT EXISTS app_library (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        filename TEXT NOT NULL,
        filesize INTEGER DEFAULT 0,
        pkg TEXT DEFAULT '',
        created TEXT DEFAULT ''
    )''',
    '''CREATE TABLE IF NOT EXISTS install_logs (
        id TEXT PRIMARY KEY,
        app_id TEXT NOT NULL,
        app_name TEXT DEFAULT '',
        device_ip TEXT NOT NULL,
        container_name TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        message TEXT DEFAULT '',
        created TEXT DEFAULT ''
    )''',
    '''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'user',
        status TEXT DEFAULT 'active',
        display_name TEXT DEFAULT '',
        last_login TEXT DEFAULT '',
        created TEXT DEFAULT '',
        updated TEXT DEFAULT ''
    )''',
    '''CREATE TABLE IF NOT EXISTS operation_logs (
        id TEXT PRIMARY KEY,
        op_type TEXT NOT NULL,
        target TEXT DEFAULT '',
        detail TEXT DEFAULT '',
        status TEXT DEFAULT '',
        created TEXT DEFAULT ''
    )''',
]


def _ensure_columns(conn):
    """为旧库补齐后加的列（幂等）。"""
    def cols(table):
        return {r[1] for r in conn.execute(f'PRAGMA table_info({table})').fetchall()}
    if 'screen_net' not in cols('devices'):
        conn.execute("ALTER TABLE devices ADD COLUMN screen_net TEXT DEFAULT 'wan'")
    if 'enabled' not in cols('devices'):
        conn.execute("ALTER TABLE devices ADD COLUMN enabled INTEGER DEFAULT 1")
    if 'max_containers' not in cols('devices'):
        conn.execute("ALTER TABLE devices ADD COLUMN max_containers INTEGER DEFAULT 0")
    if 'screen_status' not in cols('containers'):
        conn.execute("ALTER TABLE containers ADD COLUMN screen_status TEXT DEFAULT ''")
    if 'agent_status' not in cols('containers'):
        conn.execute("ALTER TABLE containers ADD COLUMN agent_status TEXT DEFAULT 'unknown'")
    if 'agent_stream_name' not in cols('containers'):
        conn.execute("ALTER TABLE containers ADD COLUMN agent_stream_name TEXT DEFAULT ''")
    if 'agent_last_seen' not in cols('containers'):
        conn.execute("ALTER TABLE containers ADD COLUMN agent_last_seen TEXT DEFAULT ''")


def init_db():
    conn = get_db()
    for sql in SCHEMA:
        conn.execute(sql)
    _ensure_columns(conn)
    conn.commit()
    conn.close()


def rebuild_db():
    """重建数据库：删除所有表后重新创建。"""
    conn = get_db()
    for t in ['devices', 'containers', 'real_params', 'proxies',
              'container_configs', 'app_library', 'install_logs', 'operation_logs', 'users']:
        conn.execute(f'DROP TABLE IF EXISTS {t}')
    conn.commit()
    conn.close()
    init_db()


def rows(sql, params=()):
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def row(sql, params=()):
    conn = get_db()
    try:
        r = conn.execute(sql, params).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def execute(sql, params=()):
    conn = get_db()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        return cur
    finally:
        conn.close()


# ==================== devices ====================
def list_devices():
    return rows('SELECT * FROM devices ORDER BY id DESC')


def get_device(dev_id):
    return row('SELECT * FROM devices WHERE id=?', (dev_id,))


def add_device(ip, port='83', url='', name='', remark='', screen_net='wan', max_containers=0):
    ts = now()
    conn = get_db()
    try:
        cur = conn.execute(
            'INSERT INTO devices (ip,port,url,name,remark,screen_net,max_containers,created,updated) VALUES (?,?,?,?,?,?,?,?,?)',
            (ip, port, url, name or f'{ip}:{port}', remark, screen_net, max_containers, ts, ts))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_device(dev_id, **fields):
    if not fields:
        return
    fields['updated'] = now()
    cols = ', '.join(f'{k}=?' for k in fields)
    execute(f'UPDATE devices SET {cols} WHERE id=?', (*fields.values(), dev_id))


def delete_device(dev_id):
    execute('DELETE FROM devices WHERE id=?', (dev_id,))


# ==================== containers ====================
def list_containers():
    return rows('SELECT * FROM containers ORDER BY device_ip, index_num')


def get_container(cid):
    return row('SELECT * FROM containers WHERE id=?', (cid,))


def upsert_container(c):
    ts = now()
    conn = get_db()
    try:
        cid = c['id']
        # 优先按主键 id 命中
        row = conn.execute('SELECT id FROM containers WHERE id=?', (cid,)).fetchone()
        if not row:
            # id 漂移但物理容器没变（改名 / 早期接口留下 docker-id 导致 id 对不上）：
            # 按稳定槽位 (device_ip,index_num) 归并，再退一步按 (device_ip,name)。
            # 命中则把旧行 id 归并到新 cid（保留 fp_id/proxy_id/created 绑定），避免 INSERT 撞 UNIQUE 约束崩溃。
            idx = c.get('index_num', 0)
            if idx:
                row = conn.execute('SELECT id FROM containers WHERE device_ip=? AND index_num=?',
                                   (c.get('device_ip', ''), idx)).fetchone()
            if not row:
                row = conn.execute('SELECT id FROM containers WHERE device_ip=? AND name=?',
                                   (c.get('device_ip', ''), c.get('name', ''))).fetchone()
        if row:
            old_id = row['id']
            if old_id != cid:
                conn.execute('UPDATE containers SET id=? WHERE id=?', (cid, old_id))
            conn.execute('''UPDATE containers SET device_id=?,device_ip=?,name=?,index_num=?,short_id=?,
                adb_port=?,adb_addr=?,image=?,state=?,status=?,and_state=?,api_port=?,ctr_port=?,webrtc=?,
                s5_addr=?,s5_status=?,resolution=?,data_path=?,updated=? WHERE id=?''',
                (c.get('device_id', 0), c.get('device_ip', ''), c.get('name', ''), c.get('index_num', 0),
                 c.get('short_id', ''), c.get('adb_port', ''), c.get('adb_addr', ''), c.get('image', ''),
                 c.get('state', ''), c.get('status', ''), c.get('and_state', 0), c.get('api_port', ''),
                 c.get('ctr_port', ''), c.get('webrtc', ''), c.get('s5_addr', ''), c.get('s5_status', 0),
                 c.get('resolution', ''), c.get('data_path', ''), ts, cid))
        else:
            conn.execute('''INSERT INTO containers (id,device_id,device_ip,name,index_num,short_id,
                adb_port,adb_addr,image,state,status,and_state,api_port,ctr_port,webrtc,s5_addr,s5_status,
                fp_id,proxy_id,resolution,data_path,created,updated)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (c['id'], c.get('device_id', 0), c.get('device_ip', ''), c.get('name', ''), c.get('index_num', 0),
                 c.get('short_id', ''), c.get('adb_port', ''), c.get('adb_addr', ''), c.get('image', ''),
                 c.get('state', ''), c.get('status', ''), c.get('and_state', 0), c.get('api_port', ''),
                 c.get('ctr_port', ''), c.get('webrtc', ''), c.get('s5_addr', ''), c.get('s5_status', 0),
                 c.get('fp_id', ''), c.get('proxy_id', ''), c.get('resolution', ''), c.get('data_path', ''),
                 ts, ts))
        conn.commit()
    finally:
        conn.close()


def update_container(cid, **fields):
    if not fields:
        return
    fields['updated'] = now()
    cols = ', '.join(f'{k}=?' for k in fields)
    execute(f'UPDATE containers SET {cols} WHERE id=?', (*fields.values(), cid))


def delete_container(cid):
    execute('DELETE FROM containers WHERE id=?', (cid,))


# ==================== real_params ====================
def list_real_params():
    return rows('SELECT * FROM real_params ORDER BY created DESC')


def get_real_param(pid):
    return row('SELECT * FROM real_params WHERE id=?', (pid,))


def get_real_param_by_md5(md5):
    return row('SELECT * FROM real_params WHERE md5=?', (md5,))


def add_real_param(rec):
    ts = now()
    rec.setdefault('id', gen_id())
    rec.setdefault('created', ts)
    rec['updated'] = ts
    cols = ['id', 'md5', 'uuid', 'name', 'brand', 'model', 'status', 'in_use',
            'instance_id', 'instance_name', 'data', 'files', 'last_login_time',
            'last_offline_time', 'created', 'updated']
    execute(f"INSERT INTO real_params ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(rec.get(k, '') for k in cols))
    return rec


def update_real_param(pid, **fields):
    if not fields:
        return
    fields['updated'] = now()
    cols = ', '.join(f'{k}=?' for k in fields)
    execute(f'UPDATE real_params SET {cols} WHERE id=?', (*fields.values(), pid))


def delete_real_param(pid):
    execute('DELETE FROM real_params WHERE id=?', (pid,))


# ==================== proxies ====================
def list_proxies():
    return rows('SELECT * FROM proxies ORDER BY created DESC')


def get_proxy(pid):
    return row('SELECT * FROM proxies WHERE id=?', (pid,))


def get_proxy_by_addr(ip, port):
    return row('SELECT * FROM proxies WHERE ip=? AND port=?', (ip, str(port)))


def add_proxy(p):
    ts = now()
    p.setdefault('id', gen_id())
    p.setdefault('created', ts)
    p['updated'] = ts
    cols = ['id', 'ip', 'port', 'user', 'pwd', 'type', 'name', 'status', 'in_use',
            'instance_id', 'instance_name', 'latency', 'region', 'isp', 'source',
            'last_login_time', 'last_offline_time', 'created', 'updated']
    execute(f"INSERT INTO proxies ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(p.get(k, '') for k in cols))
    return p


def update_proxy(pid, **fields):
    if not fields:
        return
    fields['updated'] = now()
    cols = ', '.join(f'{k}=?' for k in fields)
    execute(f'UPDATE proxies SET {cols} WHERE id=?', (*fields.values(), pid))


def delete_proxy(pid):
    execute('DELETE FROM proxies WHERE id=?', (pid,))


# ==================== container_configs ====================
def set_container_config(device_ip, container_name, config_type, config_id, config_name):
    execute('''INSERT OR REPLACE INTO container_configs
        (device_ip,container_name,config_type,config_id,config_name,applied_at)
        VALUES (?,?,?,?,?,?)''',
        (device_ip, container_name, config_type, config_id, config_name, now()))


def delete_container_config(device_ip, container_name, config_type):
    execute('DELETE FROM container_configs WHERE device_ip=? AND container_name=? AND config_type=?',
            (device_ip, container_name, config_type))


def get_container_configs(device_ip=None, container_name=None):
    if device_ip and container_name:
        return rows('SELECT * FROM container_configs WHERE device_ip=? AND container_name=?',
                    (device_ip, container_name))
    return rows('SELECT * FROM container_configs')


# ==================== app_library ====================
def list_apps():
    return rows('SELECT * FROM app_library ORDER BY created DESC')


def get_app(aid):
    return row('SELECT * FROM app_library WHERE id=?', (aid,))


def add_app(a):
    a.setdefault('id', gen_id())
    a.setdefault('created', now())
    execute('INSERT OR REPLACE INTO app_library (id,name,filename,filesize,pkg,created) VALUES (?,?,?,?,?,?)',
            (a['id'], a['name'], a['filename'], a.get('filesize', 0), a.get('pkg', ''), a['created']))
    return a


def delete_app(aid):
    execute('DELETE FROM app_library WHERE id=?', (aid,))


# ==================== install_logs ====================
def add_install_log(log):
    log.setdefault('id', gen_id())
    log.setdefault('created', now())
    execute('''INSERT OR REPLACE INTO install_logs
        (id,app_id,app_name,device_ip,container_name,status,message,created)
        VALUES (?,?,?,?,?,?,?,?)''',
        (log['id'], log['app_id'], log.get('app_name', ''), log['device_ip'],
         log['container_name'], log.get('status', 'pending'), log.get('message', ''), log['created']))
    return log


def list_install_logs(app_id=None):
    if app_id:
        return rows('SELECT * FROM install_logs WHERE app_id=? ORDER BY created DESC', (app_id,))
    return rows('SELECT * FROM install_logs ORDER BY created DESC')


def add_operation_log(log):
    log.setdefault('id', gen_id())
    log.setdefault('created', now())
    execute('''INSERT INTO operation_logs
        (id,op_type,target,detail,status,created)
        VALUES (?,?,?,?,?,?)''',
        (log['id'], log.get('op_type', ''), log.get('target', ''),
         log.get('detail', ''), log.get('status', ''), log['created']))
    return log


def list_operation_logs(op_type=None, kw=None, limit=500):
    sql = 'SELECT * FROM operation_logs'
    where, params = [], []
    if op_type:
        where.append('op_type=?'); params.append(op_type)
    if kw:
        where.append('(target LIKE ? OR detail LIKE ? OR status LIKE ?)')
        params += [f'%{kw}%', f'%{kw}%', f'%{kw}%']
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY created DESC LIMIT ?'
    params.append(limit)
    return rows(sql, tuple(params))


# ==================== users ====================
def list_users():
    return rows('SELECT * FROM users ORDER BY id')


def get_user(uid):
    return row('SELECT * FROM users WHERE id=?', (uid,))


def get_user_by_username(username):
    return row('SELECT * FROM users WHERE username=?', (username,))


def count_users():
    return row('SELECT COUNT(*) c FROM users')['c']


def add_user(username, password_hash, role='user', display_name=''):
    ts = now()
    conn = get_db()
    try:
        cur = conn.execute(
            '''INSERT INTO users (username,password_hash,role,status,display_name,created,updated)
               VALUES (?,?,?,?,?,?,?)''',
            (username, password_hash, role, 'active', display_name, ts, ts))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update_user(uid, **fields):
    if not fields:
        return
    fields['updated'] = now()
    cols = ', '.join(f'{k}=?' for k in fields)
    execute(f'UPDATE users SET {cols} WHERE id=?', (*fields.values(), uid))


def delete_user(uid):
    execute('DELETE FROM users WHERE id=?', (uid,))


def count_active_admins(exclude_id=None):
    if exclude_id:
        r = row("SELECT COUNT(*) c FROM users WHERE role='admin' AND status='active' AND id<>?",
                (exclude_id,))
    else:
        r = row("SELECT COUNT(*) c FROM users WHERE role='admin' AND status='active'")
    return r['c']


# ==================== 统计 ====================
def stats():
    def count(sql, params=()):
        return row(sql, params)['c']
    return {
        'devices': count('SELECT COUNT(*) c FROM devices'),
        'containers': count('SELECT COUNT(*) c FROM containers'),
        'real_params': count('SELECT COUNT(*) c FROM real_params'),
        'real_params_idle': count("SELECT COUNT(*) c FROM real_params WHERE status='idle'"),
        'real_params_online': count("SELECT COUNT(*) c FROM real_params WHERE status='online'"),
        'proxies': count('SELECT COUNT(*) c FROM proxies'),
        'proxies_idle': count("SELECT COUNT(*) c FROM proxies WHERE status='idle'"),
        'proxies_used': count("SELECT COUNT(*) c FROM proxies WHERE in_use=1"),
        'apps': count('SELECT COUNT(*) c FROM app_library'),
        'users': count('SELECT COUNT(*) c FROM users'),
    }


if __name__ == '__main__':
    init_db()
    print('db initialized at', DB_PATH)
