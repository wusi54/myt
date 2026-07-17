import sqlite3, os
db = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.db')
conn = sqlite3.connect(db)
c = conn.cursor()
# Use explicit unicode escapes
name = '\u5c0f\u7c7314'
print('Name to set:', repr(name))
c.execute('UPDATE fingerprint_library SET name=? WHERE id=?', (name, 'ce33938b'))
c.execute('UPDATE container_configs SET config_name=? WHERE config_id=?', (name, 'ce33938b'))
conn.commit()
# Verify
r = c.execute('SELECT name FROM fingerprint_library WHERE id="ce33938b"').fetchone()
print('Stored name:', repr(r[0]))
c.close()
conn.close()