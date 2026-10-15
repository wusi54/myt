# -*- coding: utf-8 -*-
"""登录鉴权：JWT(HS256) + 密码哈希 + 权限装饰器。

不依赖第三方库：JWT 用标准库 hmac/hashlib/base64 实现，
密码哈希复用 Flask 自带的 werkzeug.security。
"""
import os
import time
import json
import hmac
import base64
import hashlib
from functools import wraps

from flask import request, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash

# JWT 签名密钥：生产环境请通过环境变量 SECRET_KEY 覆盖
SECRET_KEY = os.environ.get('SECRET_KEY') or 'myt-dev-secret-change-me'
# token 有效期（秒），默认 7 天
TOKEN_TTL = int(os.environ.get('TOKEN_TTL', str(7 * 24 * 3600)))
COOKIE_NAME = 'myt_token'


# ==================== 密码 ====================
def hash_password(pw):
    return generate_password_hash(pw)


def verify_password(pw, pw_hash):
    try:
        return check_password_hash(pw_hash, pw)
    except Exception:
        return False


# ==================== JWT ====================
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


def _b64d(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + '=' * (-len(seg) % 4))


def make_token(user):
    now = int(time.time())
    header = {'alg': 'HS256', 'typ': 'JWT'}
    payload = {
        'uid': user['id'],
        'username': user['username'],
        'role': user.get('role', 'user'),
        'iat': now,
        'exp': now + TOKEN_TTL,
    }
    seg = (_b64e(json.dumps(header, separators=(',', ':')).encode()) + '.' +
           _b64e(json.dumps(payload, separators=(',', ':')).encode()))
    sig = hmac.new(SECRET_KEY.encode(), seg.encode(), hashlib.sha256).digest()
    return seg + '.' + _b64e(sig)


def decode_token(token):
    if not token:
        return None
    try:
        seg, sig = token.rsplit('.', 1)
        expected = hmac.new(SECRET_KEY.encode(), seg.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64d(sig), expected):
            return None
        payload = json.loads(_b64d(seg.split('.')[1]))
        if int(payload.get('exp', 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def get_token():
    """优先取 Authorization: Bearer，其次取 cookie。"""
    auth_header = request.headers.get('Authorization', '')
    if auth_header.startswith('Bearer '):
        return auth_header[7:].strip()
    return request.cookies.get(COOKIE_NAME) or ''


def public_user(u):
    if not u:
        return None
    return {
        'id': u['id'],
        'username': u['username'],
        'role': u.get('role', 'user'),
        'status': u.get('status', 'active'),
        'display_name': u.get('display_name', ''),
        'last_login': u.get('last_login', ''),
        'created': u.get('created', ''),
    }


def current_user():
    """返回当前登录用户（公众字段）或 None。"""
    import db
    payload = decode_token(get_token())
    if not payload:
        return None
    u = db.get_user(payload.get('uid'))
    if not u or u.get('status') != 'active':
        return None
    return public_user(u)


def ensure_default_admin(username='admin', password='admin123'):
    """首次运行且没有任何用户时，创建默认管理员。"""
    import db
    if db.count_users() == 0:
        db.add_user(username, hash_password(password), 'admin', '超级管理员')
        return True
    return False


# ==================== 装饰器 ====================
def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not getattr(g, 'user', None):
            return jsonify({'code': 401, 'msg': '未登录或登录已过期', 'data': None}), 401
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = getattr(g, 'user', None)
        if not u:
            return jsonify({'code': 401, 'msg': '未登录或登录已过期', 'data': None}), 401
        if u.get('role') != 'admin':
            return jsonify({'code': 403, 'msg': '需要管理员权限', 'data': None}), 403
        return fn(*args, **kwargs)
    return wrapper
