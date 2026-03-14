import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), '../app/painel_admin/auth.db')
DB_PATH = os.path.abspath(DB_PATH)

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

c.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = c.fetchall()

print("Tabelas no banco auth.db:")
for t in tables:
    print(f"- {t[0]}")

conn.close()
