import os
from app import web  # importa o app Flask
from app.infra import get_id_localidade_por_chave
from app.extensions import db

print("\n--- TESTE DE BIND DO BANCO DE LOCALIDADES (SQLAlchemy) ---\n")

with web.app.app_context():
    try:
        engine = db.engines.get("localidades")
        if engine is not None:
            print("DEBUG: URL do engine localidades:", str(engine.url))
        else:
            print("DEBUG: Engine 'localidades' não encontrado!")
    except Exception as e:
        print("DEBUG: Falha ao obter engine localidades:", e)

    # Teste de busca de chave
    chave = 'cariacica-es'
    try:
        id_cidade = get_id_localidade_por_chave(chave)
        print(f"Resultado para '{chave}': {id_cidade}")
    except Exception as e:
        print(f"Erro ao buscar '{chave}': {e}")

print("\n--- FIM DO TESTE ---\n")