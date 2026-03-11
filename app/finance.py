import yfinance as yf
import json
import requests
from bs4 import BeautifulSoup
from pathlib import Path
from datetime import datetime, timedelta
from app import env_loader

env_loader.load_app_env()
INDICES_FILE = Path(env_loader.resolve_indices_file_path())

def get_live_index(url, selector, fallback_val):
    """Busca genérica para aumentar resiliência de scraping"""
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    try:
        response = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')
        element = soup.select_one(selector)
        if element:
            return "".join(filter(str.isdigit, element.text.strip()))
        return fallback_val
    except Exception as e:
        print(f"⚠️ Erro na fonte {url}: {e}")
        return fallback_val

def atualizar_indices():
    print("\n" + "="*40)
    print("📊 CLEITON FINANCE: ATUALIZANDO MERCADO")
    print("="*40)
    
    novos_dados = {}

    try:
        # 1. Coleta de dados (Mantendo sua lógica original)
        print("💵 Consultando Câmbio e Petróleo...")
        dolar = yf.Ticker("USDBRL=X").history(period="1d")['Close'].iloc[-1]
        petroleo = yf.Ticker("CL=F").history(period="1d")['Close'].iloc[-1]
        
        novos_dados['data'] = datetime.now().strftime("%Y-%m-%d")
        novos_dados['dolar'] = round(dolar, 2)
        novos_dados['petroleo'] = round(petroleo, 2)

        print("🚢 Capturando BDI (Baltic Dry)...")
        novos_dados['bdi'] = get_live_index("https://www.cnbc.com/quotes/.BDI", "span.QuoteStrip-lastPrice", "2117")

        print("📦 Capturando FBX (Freightos)...")
        novos_dados['fbx'] = get_live_index("https://fbx.freightos.com/", ".fbx-index-value", "2280")

        # --- NOVA LÓGICA DE HISTÓRICO (18 MESES) ---
        
        # 2. Carregar arquivo existente ou criar nova estrutura
        if INDICES_FILE.exists():
            with open(INDICES_FILE, 'r', encoding='utf-8') as f:
                try:
                    conteudo = json.load(f)
                    # Se o formato for o antigo (só um dict), converte para o novo
                    if "historico" not in conteudo:
                        historico = []
                    else:
                        historico = conteudo['historico']
                except json.JSONDecodeError:
                    historico = []
        else:
            historico = []

        # 3. Adicionar novos dados (evitando duplicidade no mesmo dia)
        historico = [h for h in historico if h.get('data') != novos_dados['data']]
        historico.append(novos_dados)

        # 4. Regra de Retenção: 18 meses
        data_limite = datetime.now() - timedelta(days=18*30)
        historico_filtrado = [
            h for h in historico 
            if datetime.strptime(h['data'], "%Y-%m-%d") > data_limite
        ]

        # 5. Salva estrutura completa
        final_json = {
            "ultima_atualizacao": novos_dados['data'],
            "historico": sorted(historico_filtrado, key=lambda x: x['data'])
        }

        INDICES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(INDICES_FILE, 'w', encoding='utf-8') as f:
            json.dump(final_json, f, indent=4, ensure_ascii=False)
            
        print(f"✅ Sincronização concluída! Histórico: {len(historico_filtrado)} registros.")
        print("="*40 + "\n")

    except Exception as e:
        print(f"❌ Falha crítica: {e}")

if __name__ == "__main__":
    atualizar_indices()