import sqlite3

# Caminho absoluto para o banco de localidades
CAMINHO_BANCO = "app/base_localidades.db"  # ajuste se necessário

chaves_teste = [
    'cariacica-es',
    'vitoria-es',
    'serra-es',
    'sao paulo-sp',
]

print("Testando busca direta no banco de localidades:")

try:
    conn = sqlite3.connect(CAMINHO_BANCO)
    cursor = conn.cursor()
    for chave in chaves_teste:
        cursor.execute("SELECT id_cidade FROM de_para_logistica WHERE LOWER(TRIM(chave_busca)) = ?", (chave,))
        row = cursor.fetchone()
        print(f"Chave '{chave}':", row[0] if row else "NÃO ENCONTRADA")
    conn.close()
except Exception as e:
    print("Erro ao acessar o banco:", e)

print("Teste finalizado.")