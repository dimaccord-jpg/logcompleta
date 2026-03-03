import json
import sys
import os

# Garante que conseguimos importar o módulo local
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

print("--- INICIANDO DIAGNÓSTICO DO AGENTE ROBERTO ---")

try:
    from run_roberto import roberto
    print("✅ Módulo run_roberto importado com sucesso.")
except ValueError as e:
    print(f"❌ Erro de Configuração (API KEY): {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ Erro ao importar o agente: {e}")
    sys.exit(1)

# 1. MOCK: Dados Fictícios de Mercado (Simulando indices.json)
indices_mock = {
    "ultima_atualizacao": "01/03/2026",
    "historico": [
        {"data": "2025-11", "dolar": 5.25, "petroleo": 82, "bdi": 1550},
        {"data": "2025-12", "dolar": 5.30, "petroleo": 85, "bdi": 1600},
        {"data": "2026-01", "dolar": 5.15, "petroleo": 78, "bdi": 1400},
        {"data": "2026-02", "dolar": 5.10, "petroleo": 75, "bdi": 1350}
    ]
}

# 2. MOCK: Dados Fictícios de Frete Real (Simulando o banco de dados)
historico_frete_mock = [
    {"valor": 4500.00, "peso": 12000, "modal": "Rodoviário"},
    {"valor": 4600.00, "peso": 12000, "modal": "Rodoviário"},
    {"valor": 4400.00, "peso": 12000, "modal": "Rodoviário"},
    {"valor": 4300.00, "peso": 12000, "modal": "Rodoviário"}
]

rota_teste = "São Paulo (SP) -> Salvador (BA)"

print(f"🚀 Enviando solicitação para o Gemini (Modelo: gemini-2.0-flash)...")
print(f"Rota: {rota_teste} | Amostras: {len(historico_frete_mock)}")

# 3. EXECUÇÃO
resultado = roberto.analisar_frete(historico_frete_mock, indices_mock, rota_teste)

print("\n" + "="*40)
print("      RESPOSTA DO ROBERTO")
print("="*40)
print(json.dumps(resultado, indent=4, ensure_ascii=False))
print("="*40)
print("\nTeste finalizado.")
