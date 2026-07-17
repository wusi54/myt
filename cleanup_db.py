import sqlite3, os
db = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.db')
conn = sqlite3.connect(db)
conn.execute('DELETE FROM fingerprint_library WHERE id="ce33938b"')
conn.execute('DELETE FROM container_configs WHERE config_id="ce33938b"')
conn.commit()
print('Cleaned')
conn.close()