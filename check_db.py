import sqlite3, os
db = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.db')
conn = sqlite3.connect(db)
r = conn.execute('SELECT name, port, container_count, host_ver FROM devices').fetchall()
for row in r:
    out = 'name repr: ' + repr(row[0]) + ' | hex: ' + row[0].encode('utf-8').hex() if row[0] else 'NULL'
    out += ' | port: ' + str(row[1]) + ' | containers: ' + str(row[2]) + ' | ver: ' + str(row[3])
    print(out)
conn.close()