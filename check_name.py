import sqlite3, os
db = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.db')
conn = sqlite3.connect(db)
r = conn.execute('SELECT name FROM devices WHERE ip="192.168.10.83"').fetchone()
name = r[0]
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fix_result2.txt'), 'w', encoding='utf-8') as f:
    f.write('Name: ' + name + '\n')
    f.write('Bytes: ' + name.encode('utf-8').hex() + '\n')
    for ch in name:
        f.write('U+' + format(ord(ch), '04X') + '\n')
conn.close()