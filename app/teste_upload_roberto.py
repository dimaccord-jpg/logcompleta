
import sys
import os
import logging

# Garante que o pacote 'app' seja encontrado
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

try:
    from app import web  # Isso garante que o app Flask seja carregado
    from app.infra import get_id_localidade_por_chave
except Exception as import_err:
    print(f"Erro de importação: {import_err}")
    sys.exit(1)

logging.basicConfig(level=logging.DEBUG, format='%(levelname)s:%(message)s')

chaves_teste = [
    'cariacica-es',
    'vitoria-es',
    'serra-es',
    'sao paulo-sp',
]

print("Testando get_id_localidade_por_chave para chaves de localidade:")

try:
    with web.app.app_context():
        for chave in chaves_teste:
            print(f"\nTestando chave: {chave}")
            try:
                id_cidade = get_id_localidade_por_chave(chave)
                print(f"Resultado para '{chave}': {id_cidade}")
            except Exception as e:
                print(f"Erro ao buscar '{chave}': {e}")
except Exception as e:
    print(f"Erro ao executar teste no contexto Flask: {e}")

print("\nTeste finalizado.")