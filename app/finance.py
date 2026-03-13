import yfinance as yf
import json
import re
import requests
from bs4 import BeautifulSoup
from pathlib import Path
from datetime import datetime, timedelta
from app.settings import settings

INDICES_FILE = Path(settings.indices_file_path)
LEGACY_INDICES_FILE = Path(__file__).resolve().parent / 'indices.json'


def _load_historico(path: Path):
    if not path.exists():
        return []
    try:
        with open(path, 'r', encoding='utf-8') as f:
            conteudo = json.load(f)
        if isinstance(conteudo, dict):
            if isinstance(conteudo.get('historico'), list):
                return conteudo.get('historico') or []
            if all(k in conteudo for k in ('dolar', 'petroleo', 'bdi', 'fbx')):
                return [conteudo]
        return []
    except Exception:
        return []

def get_live_index(url, selector, fallback_val):
    """
    Busca genérica baseada em seletor CSS.
    Mantida para compatibilidade, mas BDI/FBX usam estratégias dedicadas abaixo.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
        )
    }
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        element = soup.select_one(selector)
        if element:
            raw = element.get_text(strip=True)
            # Mantém dígitos, ponto e vírgula para valores decimais.
            cleaned = re.sub(r"[^0-9,.\-]", "", raw)
            return cleaned or fallback_val
        return fallback_val
    except Exception as e:
        print(f"⚠️ Erro na fonte {url}: {e}")
        return fallback_val


def get_bdi_index(fallback_val: str) -> str:
    """
    Coleta o Baltic Dry Index a partir do StockQ (HTML estável em tabela).
    Usa regex no HTML para evitar dependência de classes específicas.
    """
    url = "https://en.stockq.org/index/BDI.php"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        html = resp.text
        # Padrão principal: linha da primeira tabela de índice:
        # | 2028.00 | 56.00 | 2.84% | ...
        m = re.search(
            r"\|\s*([0-9]{3,5}\.[0-9]{2})\s*\|\s*[+\-]?[0-9]+\.[0-9]{2}\s*\|",
            html,
        )
        if not m:
            # Fallback: primeiro número com 3-5 dígitos e duas casas decimais.
            m = re.search(r"([0-9]{3,5}\.[0-9]{2})", html)
        if m:
            return m.group(1)
    except Exception as e:
        print(f"⚠️ Erro ao obter BDI em {url}: {e}")
    return fallback_val


def get_fbx_index(fallback_val: str) -> str:
    """
    Coleta o Freightos Baltic Index (FBX) da página institucional.
    Procura pelo bloco 'Current FBX' seguido de um valor em dólar.
    """
    url = "https://fbx.freightos.com/"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        html = resp.text
        # Padrão principal: 'Current FBX' ... '$1,637.40'
        m = re.search(
            r"Current\s+FBX.*?\$([0-9,]+\.[0-9]+)",
            html,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not m:
            # Fallback: primeiro valor em dólar na página.
            m = re.search(r"\$([0-9,]+\.[0-9]+)", html)
        if m:
            valor = m.group(1).replace(",", "")
            return valor
    except Exception as e:
        print(f"⚠️ Erro ao obter FBX em {url}: {e}")
    return fallback_val

def atualizar_indices():
    print("\n" + "="*40)
    print("📊 CLEITON FINANCE: ATUALIZANDO MERCADO")
    print("="*40)

    novos_dados: dict = {}
    status_indices: dict = {
        "dolar": {"status": "nao_processado", "valor": None},
        "petroleo": {"status": "nao_processado", "valor": None},
        "bdi": {"status": "nao_processado", "valor": None},
        "fbx": {"status": "nao_processado", "valor": None},
    }
    status_global = "falha"
    mensagem_global = ""

    try:
        # 1. Carregar histórico existente para fallback seguro
        historico = _load_historico(INDICES_FILE)
        # Migração suave: se caminho atual ainda não tem histórico, tenta arquivo legado.
        if not historico and LEGACY_INDICES_FILE != INDICES_FILE:
            historico = _load_historico(LEGACY_INDICES_FILE)

        ultimo_registro = historico[-1] if historico else {}

        # 2. Coleta resiliente com fallback no último valor conhecido
        print("💵 Consultando Câmbio e Petróleo...")
        novos_dados["data"] = datetime.now().strftime("%Y-%m-%d")

        # Dólar
        try:
            dolar_hist = yf.Ticker("USDBRL=X").history(period="5d")
            dolar = float(dolar_hist["Close"].dropna().iloc[-1])
            novos_dados["dolar"] = round(dolar, 2)
            status_indices["dolar"] = {"status": "ok", "valor": novos_dados["dolar"]}
        except Exception as e:
            print(f"⚠️ Falha ao coletar Dólar: {e}. Usando último valor conhecido.")
            novos_dados["dolar"] = ultimo_registro.get("dolar", 0.0)
            status_indices["dolar"] = {
                "status": "fallback",
                "valor": novos_dados["dolar"],
            }

        # Petróleo
        try:
            petroleo_hist = yf.Ticker("CL=F").history(period="5d")
            petroleo = float(petroleo_hist["Close"].dropna().iloc[-1])
            novos_dados["petroleo"] = round(petroleo, 2)
            status_indices["petroleo"] = {
                "status": "ok",
                "valor": novos_dados["petroleo"],
            }
        except Exception as e:
            print(f"⚠️ Falha ao coletar Petróleo: {e}. Usando último valor conhecido.")
            novos_dados["petroleo"] = ultimo_registro.get("petroleo", 0.0)
            status_indices["petroleo"] = {
                "status": "fallback",
                "valor": novos_dados["petroleo"],
            }

        # BDI
        print("🚢 Capturando BDI (Baltic Dry)...")
        fallback_bdi = str(ultimo_registro.get("bdi", "-"))
        valor_bdi = get_bdi_index(fallback_bdi)
        novos_dados["bdi"] = valor_bdi
        status_indices["bdi"] = {
            "status": "ok" if valor_bdi != fallback_bdi else "fallback",
            "valor": valor_bdi,
        }

        # FBX
        print("📦 Capturando FBX (Freightos)...")
        fallback_fbx = str(ultimo_registro.get("fbx", "-"))
        valor_fbx = get_fbx_index(fallback_fbx)
        novos_dados["fbx"] = valor_fbx
        status_indices["fbx"] = {
            "status": "ok" if valor_fbx != fallback_fbx else "fallback",
            "valor": valor_fbx,
        }

        # 3. Adicionar novos dados (evitando duplicidade no mesmo dia)
        historico = [h for h in historico if h.get("data") != novos_dados["data"]]
        historico.append(novos_dados)

        # 4. Regra de Retenção: 18 meses
        data_limite = datetime.now() - timedelta(days=18 * 30)
        historico_filtrado = [
            h
            for h in historico
            if datetime.strptime(h["data"], "%Y-%m-%d") > data_limite
        ]

        # 5. Salva estrutura completa
        final_json = {
            "ultima_atualizacao": novos_dados["data"],
            "historico": sorted(historico_filtrado, key=lambda x: x["data"]),
        }

        INDICES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(INDICES_FILE, "w", encoding="utf-8") as f:
            json.dump(final_json, f, indent=4, ensure_ascii=False)

        print(f"✅ Sincronização concluída! Histórico: {len(historico_filtrado)} registros.")
        print("=" * 40 + "\n")
        # Se chegou até aqui, consideramos sucesso (podendo ter usado fallbacks por índice).
        if any(v["status"] == "ok" for v in status_indices.values()):
            status_global = "sucesso_parcial" if any(
                v["status"] == "fallback" for v in status_indices.values()
            ) else "sucesso"
        else:
            status_global = "falha"
        mensagem_global = "Atualização concluída com sucesso." if status_global.startswith("sucesso") else "Atualização concluída apenas com valores de fallback."

    except Exception as e:
        print(f"❌ Falha crítica: {e}")
        status_global = "falha"
        mensagem_global = f"Falha crítica na atualização: {e}"

    return {
        "status_global": status_global,
        "mensagem": mensagem_global,
        "indices": status_indices,
        "arquivo_destino": str(INDICES_FILE),
        "data_referencia": novos_dados.get("data"),
    }

if __name__ == "__main__":
    atualizar_indices()