# -*- coding: utf-8 -*-
"""重建数据库（会清空所有数据）。用法: python rebuild_db.py"""
import db

if __name__ == '__main__':
    db.rebuild_db()
    print('数据库已重建:', db.DB_PATH)
    print('统计:', db.stats())
