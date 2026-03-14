import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), '../app/painel_admin/auth.db')
DB_PATH = os.path.abspath(DB_PATH)

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("PRAGMA table_info(user)")
columns = c.fetchall()

print("Colunas da tabela 'user':")
for col in columns:
    print(f"- {col[1]} ({col[2]})")

conn.close()
