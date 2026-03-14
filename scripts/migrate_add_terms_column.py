import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), '../app/painel_admin/auth.db')
DB_PATH = os.path.abspath(DB_PATH)

conn = sqlite3.connect(DB_PATH)
c = conn.cursor()

# Adiciona a coluna accepted_terms_at se não existir
def add_column_if_not_exists():
    c.execute("PRAGMA table_info(user)")
    columns = [row[1] for row in c.fetchall()]
    if 'accepted_terms_at' not in columns:
        c.execute("ALTER TABLE user ADD COLUMN accepted_terms_at DATETIME")
        print("Coluna 'accepted_terms_at' adicionada.")
    else:
        print("Coluna 'accepted_terms_at' já existe.")

# Cria a tabela terms_of_use se não existir
def create_terms_of_use_table():
    c.execute("""
    CREATE TABLE IF NOT EXISTS terms_of_use (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        filename VARCHAR(255) NOT NULL,
        upload_date DATETIME NOT NULL,
        is_active BOOLEAN DEFAULT 1
    )
    """)
    print("Tabela 'terms_of_use' criada ou já existente.")

add_column_if_not_exists()
create_terms_of_use_table()

conn.commit()
conn.close()
print("Migração concluída com sucesso.")
