"""
Upload e gerenciamento temporário de dados de fretes para o Roberto Intelligence.
Valida Excel, normaliza texto, vincula IDs de localidade e armazena em sessão.

Amostragem mensal: para grandes volumes, apenas uma amostra representativa por mês
é mantida antes da previsão (roberto_modelo.py continua usando regressão linear
sobre a série temporal agregada por mês). Isso reduz uso de memória e tempo de
processamento sem prejudicar a qualidade da série mensal.

Uploads: os arquivos Excel recebidos são salvos em um diretório dedicado
(`roberto_uploads`) dentro do pacote `app`, separado do diretório usado
pelo Flask-Session. Isso evita qualquer conflito com a pasta de sessão
filesystem e mantém o isolamento da funcionalidade do Roberto.
"""
import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from flask import request, session, jsonify
from werkzeug.datastructures import FileStorage

from app.brain import gerar_chave_busca
from app.infra import get_id_localidade_por_chave

logger = logging.getLogger(__name__)

# Diretório dedicado para uploads temporários do Roberto (isolado do flask_session)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "roberto_uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Amostragem por mês (antes de salvar na sessão):
# - Máximo de registros mantidos por mês; o restante é descartado para otimizar volume.
AMOSTRA_MAX_POR_MES = 20
# - Abaixo deste valor, consideramos baixa representatividade e informamos o usuário.
AMOSTRA_MIN_RECOMENDADO_POR_MES = 10

# Colunas obrigatórias no Excel (nomes exatos após normalização do cabeçalho)
COLUNAS_OBRIGATORIAS = {
    "data_emissao",
    "cidade_origem",
    "uf_origem",
    "cidade_destino",
    "uf_destino",
    "peso_real",
    "valor_nf",
    "valor_frete_total",
    "modal",
}
COLUNA_OPCIONAL_IMPOSTO = "valor_imposto"

# Chave de sessão e TTL para descarte automático (minutos)
SESSION_KEY_UPLOAD = "roberto_upload_data"
SESSION_KEY_UPLOAD_AT = "roberto_upload_at"
UPLOAD_TTL_MINUTOS = 30


def _normalizar_texto(val: Any) -> str:
    """Converte valor para string e coloca em minúsculas, sem remover acentos."""
    if val is None:
        return ""
    return str(val).strip().lower()


def _ler_cabecalho_normalizado(ws) -> list[str]:
    """Lê primeira linha da planilha e retorna colunas em minúsculas."""
    row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
    if not row:
        return []
    return [_normalizar_texto(c) for c in row]


def _validar_arquivo_excel(arquivo: FileStorage) -> tuple[bool, str]:
    """Valida extensão e tipo MIME básico."""
    if not arquivo or not arquivo.filename:
        return False, "Nenhum arquivo enviado."
    fn = (arquivo.filename or "").strip().lower()
    if not fn.endswith(".xlsx"):
        return False, "Apenas arquivos Excel (.xlsx) são aceitos."
    return True, ""


def _validar_colunas(colunas: list[str]) -> tuple[bool, str]:
    """Verifica se todas as colunas obrigatórias estão presentes."""
    conjunto = set(colunas)
    faltando = COLUNAS_OBRIGATORIAS - conjunto
    if faltando:
        return False, f"Colunas obrigatórias ausentes: {', '.join(sorted(faltando))}."
    return True, ""


def _chave_mes(reg: dict) -> str:
    """Extrai chave ano-mês (YYYY-MM) do campo data_emissao do registro."""
    d = reg.get("data_emissao") or ""
    if hasattr(d, "strftime"):
        return d.strftime("%Y-%m")
    s = str(d).strip()[:10]
    if len(s) >= 7:
        return s[:7]
    return ""


def _reduzir_amostra_por_mes(
    linhas: list[dict],
) -> tuple[list[dict], dict[str, Any]]:
    """
    Agrupa fretes válidos por mês e mantém no máximo AMOSTRA_MAX_POR_MES por mês,
    preferindo os mais recentes (por data_emissao). Reduz volume para grandes
    uploads sem perder representatividade da série mensal usada pela regressão linear.

    :param linhas: lista de registros com data_emissao (str ISO ou date).
    :return: (lista reduzida, estatísticas para o usuário).
    """
    por_mes: dict[str, list[dict]] = defaultdict(list)
    for r in linhas:
        chave = _chave_mes(r)
        if chave:
            por_mes[chave].append(r)

    resultado = []
    utilizados_por_mes: dict[str, int] = {}
    descartados_por_mes: dict[str, int] = {}
    meses_baixa_representatividade: list[str] = []

    for mes in sorted(por_mes.keys()):
        registros = por_mes[mes]
        # Ordenar por data_emissao (mais recentes primeiro) para preferir os mais recentes
        def _data_ord(x):
            v = x.get("data_emissao")
            if hasattr(v, "strftime"):
                return v.isoformat() if hasattr(v, "isoformat") else str(v)
            return str(v)[:10] if v else "0000-00-00"

        registros.sort(key=_data_ord, reverse=True)
        total_mes = len(registros)
        manter = registros[:AMOSTRA_MAX_POR_MES]
        descartar = total_mes - len(manter)
        resultado.extend(manter)
        utilizados_por_mes[mes] = len(manter)
        descartados_por_mes[mes] = descartar
        if len(manter) < AMOSTRA_MIN_RECOMENDADO_POR_MES:
            meses_baixa_representatividade.append(mes)

    stats = {
        "registros_recebidos": len(linhas),
        "registros_utilizados": len(resultado),
        "registros_descartados": len(linhas) - len(resultado),
        "por_mes": {
            mes: {"utilizados": utilizados_por_mes[mes], "descartados": descartados_por_mes[mes]}
            for mes in sorted(por_mes.keys())
        },
        "meses_baixa_representatividade": meses_baixa_representatividade,
    }
    return resultado, stats


def processar_upload_frete_excel() -> tuple[dict, int]:
    """
    Processa upload de arquivo Excel (.xlsx): valida colunas, normaliza dados,
    resolve IDs de localidade e armazena em sessão.

    :return: (resposta JSON, código HTTP)
    """
    arquivo = request.files.get("file") or request.files.get("arquivo")
    ok, msg = _validar_arquivo_excel(arquivo)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400

    try:
        import openpyxl
    except ImportError:
        logger.exception("openpyxl não instalado")
        return jsonify({"success": False, "error": "Serviço de planilhas indisponível."}), 500

    # Salva o arquivo em diretório próprio de uploads do Roberto, separado do diretório
    # de sessão do Flask, para evitar conflitos com a pasta usada pelo flask_session.
    nome_base = (arquivo.filename or "upload.xlsx").rsplit(".", 1)[0]
    nome_seguro = "".join(ch for ch in nome_base if ch.isalnum() or ch in ("-", "_")) or "upload"
    nome_arquivo = f"{nome_seguro}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{uuid4().hex}.xlsx"
    caminho_arquivo = os.path.join(UPLOAD_DIR, nome_arquivo)

    try:
        arquivo.save(caminho_arquivo)
    except Exception as e:
        logger.exception("Falha ao salvar arquivo de upload em %s: %s", caminho_arquivo, e)
        return jsonify({"success": False, "error": "Falha ao salvar arquivo de upload."}), 500

    try:
        wb = openpyxl.load_workbook(caminho_arquivo, read_only=True, data_only=True)
        ws = wb.active
        if ws is None:
            return jsonify({"success": False, "error": "Planilha vazia."}), 400
    except Exception as e:
        logger.debug("Erro ao abrir Excel em %s: %s", caminho_arquivo, e)
        return jsonify({"success": False, "error": "Arquivo Excel inválido ou corrompido."}), 400

    colunas = _ler_cabecalho_normalizado(ws)
    ok, msg = _validar_colunas(colunas)
    if not ok:
        return jsonify({"success": False, "error": msg}), 400

    idx_imposto = colunas.index(COLUNA_OPCIONAL_IMPOSTO) if COLUNA_OPCIONAL_IMPOSTO in colunas else None
    indices = {c: colunas.index(c) for c in colunas if c in COLUNAS_OBRIGATORIAS or c == COLUNA_OPCIONAL_IMPOSTO}

    linhas_processadas = []
    erros_linha = []

    try:
        for num_linha, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
            if not any(v is not None and str(v).strip() for v in row):
                continue
            try:
                cidade_origem = _normalizar_texto(row[indices["cidade_origem"]])
                uf_origem = _normalizar_texto(row[indices["uf_origem"]])
                cidade_destino = _normalizar_texto(row[indices["cidade_destino"]])
                uf_destino = _normalizar_texto(row[indices["uf_destino"]])

                # Chave de localidade padronizada: municipio-uf em minúsculo,
                # mantendo acentos e caracteres especiais. Ex.: "cariacica-es".
                chave_origem = f"{cidade_origem}-{uf_origem}"
                chave_destino = f"{cidade_destino}-{uf_destino}"

                id_origem = get_id_localidade_por_chave(chave_origem)
                id_destino = get_id_localidade_por_chave(chave_destino)

                if id_origem is None:
                    erros_linha.append(f"Linha {num_linha}: localidade de origem '{chave_origem}' não encontrada.")
                    continue
                if id_destino is None:
                    erros_linha.append(f"Linha {num_linha}: localidade de destino '{chave_destino}' não encontrada.")
                    continue

                data_val = row[indices["data_emissao"]]
                if hasattr(data_val, "strftime"):
                    data_emissao = data_val
                else:
                    data_str = _normalizar_texto(data_val)
                    data_emissao = None
                    if data_str:
                        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
                            try:
                                data_emissao = datetime.strptime(data_str, fmt).date()
                                break
                            except ValueError:
                                continue
                        if data_emissao is None:
                            erros_linha.append(f"Linha {num_linha}: data inválida '{data_str}'.")
                            continue

                peso = float(row[indices["peso_real"]] or 0)
                valor_nf = float(row[indices["valor_nf"]] or 0)
                valor_frete = float(row[indices["valor_frete_total"]] or 0)
                modal = _normalizar_texto(row[indices["modal"]])
                valor_imposto = None
                if idx_imposto is not None and row[idx_imposto] is not None:
                    try:
                        valor_imposto = float(row[idx_imposto])
                    except (TypeError, ValueError):
                        pass

                linhas_processadas.append({
                    "data_emissao": data_emissao.isoformat() if hasattr(data_emissao, "isoformat") else str(data_emissao),
                    "id_cidade_origem": id_origem,
                    "id_cidade_destino": id_destino,
                    "peso_real": peso,
                    "valor_nf": valor_nf,
                    "valor_frete_total": valor_frete,
                    "valor_imposto": valor_imposto,
                    "modal": modal or "",
                })
            except (IndexError, KeyError, TypeError, ValueError) as e:
                erros_linha.append(f"Linha {num_linha}: erro de dados - {e}.")
    finally:
        # Remove o arquivo físico após o processamento para manter o diretório limpo.
        try:
            if os.path.exists(caminho_arquivo):
                os.remove(caminho_arquivo)
        except Exception:
            logger.debug("Falha ao remover arquivo temporário %s", caminho_arquivo)

    if not linhas_processadas:
        return jsonify({
            "success": False,
            "error": "Nenhuma linha válida após processamento.",
            "detalhes": erros_linha[:20],
        }), 400

    # Amostragem por mês: mantém no máximo AMOSTRA_MAX_POR_MES por mês (mais recentes)
    # para otimizar grandes volumes sem alterar a modelagem (regressão linear em roberto_modelo).
    linhas_reduzidas, stats_amostra = _reduzir_amostra_por_mes(linhas_processadas)

    session[SESSION_KEY_UPLOAD] = linhas_reduzidas
    session[SESSION_KEY_UPLOAD_AT] = datetime.utcnow().isoformat()
    session.modified = True

    aviso_baixa = None
    if stats_amostra.get("meses_baixa_representatividade"):
        aviso_baixa = (
            f"Meses com menos de {AMOSTRA_MIN_RECOMENDADO_POR_MES} fretes (baixa representatividade): "
            + ", ".join(stats_amostra["meses_baixa_representatividade"])
        )

    return jsonify({
        "success": True,
        "registros": len(linhas_reduzidas),
        "registros_recebidos": stats_amostra["registros_recebidos"],
        "registros_utilizados": stats_amostra["registros_utilizados"],
        "registros_descartados": stats_amostra["registros_descartados"],
        "amostragem_por_mes": stats_amostra["por_mes"],
        "aviso_baixa_representatividade": aviso_baixa,
        "avisos": erros_linha[:15] if erros_linha else None,
    }), 200


def get_dados_upload_cliente() -> list[dict] | None:
    """
    Retorna os dados temporários do upload do cliente (lista de dicts com IDs de localidade).
    Retorna None se não houver dados ou se expirados (TTL).
    """
    if not session.get(SESSION_KEY_UPLOAD):
        return None
    at_str = session.get(SESSION_KEY_UPLOAD_AT)
    if at_str:
        try:
            at = datetime.fromisoformat(at_str.replace("Z", "+00:00"))
            if at.tzinfo:
                at = (at.replace(tzinfo=None) - at.utcoffset()) if at.utcoffset() else at.replace(tzinfo=None)
            if datetime.utcnow() - at > timedelta(minutes=UPLOAD_TTL_MINUTOS):
                clear_upload_data()
                return None
        except Exception:
            pass
    return session.get(SESSION_KEY_UPLOAD)


def clear_upload_data() -> None:
    """Remove dados temporários de upload da sessão (descarte seguro)."""
    for key in (SESSION_KEY_UPLOAD, SESSION_KEY_UPLOAD_AT):
        session.pop(key, None)
    session.modified = True


def roberto_clear_upload_endpoint():
    """Endpoint para o frontend limpar dados ao sair da tela /fretes (evento de navegação)."""
    clear_upload_data()
    return jsonify({"success": True}), 200
