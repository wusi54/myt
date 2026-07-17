import sqlite3, os
db = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.db')
conn = sqlite3.connect(db)
name = '\u9b54\u4e91\u817e\u4e3b\u8bbe\u5907'
conn.execute('UPDATE devices SET name=? WHERE ip=?', (name, '192.168.10.83'))
conn.commit()
r = conn.execute('SELECT name FROM devices WHERE ip="192.168.10.83"').fetchone()
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fix_result.txt'), 'w', encoding='utf-8') as f:
    f.write(repr(r[0]))
conn.close()