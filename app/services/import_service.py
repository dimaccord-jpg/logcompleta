"""
Serviço de importação de dados e atualização de índices.
Carga de operação (de_para_logistica), tabelas de frete e leitura/persistência de log de índices.
"""
import os
import csv
import io
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models import FreteReal

logger = logging.getLogger(__name__)

# Diretório de logs de importação (env ou fallback)
LOG_DIR_ENV_KEY = "LOG_DIR"


def get_log_dir() -> str:
    """Retorna diretório para logs de importação. Cria se não existir."""
    log_dir = os.getenv(LOG_DIR_ENV_KEY) or os.path.join(os.getcwd(), "logs_fallback")
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def processar_importacao_operacao(file: FileStorage) -> tuple[int, int, str | None, str | None]:
    """
    Processa arquivo .txt/.csv de operação (de_para_logistica).
    Colunas esperadas: uf_nome, cidade_nome, chave_busca, id_uf, id_cidade.
    Retorna (sucessos, falhas, path_sucesso, path_erro) ou (0, 0, None, None) em erro crítico.
    path_* são caminhos dos arquivos de log gerados.
    """
    if not file or not file.filename:
        return 0, 0, None, None
    fn = file.filename.strip().lower()
    if not (fn.endswith(".txt") or fn.endswith(".csv")):
        return 0, 0, None, None
    try:
        log_dir = get_log_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path_sucesso = os.path.join(log_dir, f"sucesso_{timestamp}.txt")
        path_erro = os.path.join(log_dir, f"erro_{timestamp}.txt")
        conteudo = file.read().decode("utf-8-sig")
        stream = io.StringIO(conteudo)
        leitor = csv.DictReader(stream, delimiter=",")
        sucessos, falhas = 0, 0
        engine = db.engines["localidades"]
        with engine.connect() as connection:
            for linha in leitor:
                uf_nome = linha.get("uf_nome")
                cidade_nome = linha.get("cidade_nome")
                chave_origem = linha.get("chave_busca")
                id_uf = linha.get("id_uf")
                id_cidade = linha.get("id_cidade")
                if not chave_origem:
                    continue
                chave_proc = str(chave_origem).strip().lower()
                query_check = text(
                    "SELECT 1 FROM de_para_logistica WHERE chave_busca = :ch"
                )
                existe = connection.execute(
                    query_check, {"ch": chave_proc}
                ).fetchone()
                if existe:
                    with open(path_erro, "a", encoding="utf-8") as f:
                        f.write(f"BLOQUEADO: Chave [{chave_proc}] já existe no banco.\n")
                    falhas += 1
                    continue
                try:
                    query_ins = text("""
                        INSERT INTO de_para_logistica (uf_nome, cidade_nome, chave_busca, id_uf, id_cidade)
                        VALUES (:uf, :cid, :ch, :i_uf, :i_cid)
                    """)
                    connection.execute(
                        query_ins,
                        {
                            "uf": uf_nome,
                            "cid": cidade_nome,
                            "ch": chave_proc,
                            "i_uf": id_uf,
                            "i_cid": id_cidade,
                        },
                    )
                    connection.commit()
                    with open(path_sucesso, "a", encoding="utf-8") as f:
                        f.write(f"SUCESSO: {chave_proc} cadastrada.\n")
                    sucessos += 1
                except Exception as e_row:
                    with open(path_erro, "a", encoding="utf-8") as f:
                        f.write(f"ERRO TÉCNICO [{chave_proc}]: {str(e_row)}\n")
                    falhas += 1
        return sucessos, falhas, path_sucesso, path_erro
    except Exception as e:
        logger.exception("Erro crítico ao processar importação operação: %s", e)
        return 0, 0, None, None


def processar_importacao_tabelas(
    file: FileStorage,
) -> tuple[int, list[str], str | None]:
    """
    Processa arquivo .txt de tabelas de frete.
    Colunas esperadas: cidade_origem, uf_origem, cidade_destino, uf_destino,
    data_emissao (formato %d/%m/%Y), peso_real, valor_nf, valor_frete_total, valor_imposto, modal.
    Retorna (sucessos, linhas_com_erro, None) ou (0, [], mensagem_erro_critico).
    """
    if not file or not file.filename or not file.filename.strip().lower().endswith(".txt"):
        return 0, [], "Arquivo .txt inválido."
    try:
        file.seek(0)
        conteudo = file.read().decode("utf-8")
        stream = io.StringIO(conteudo)
        leitor = csv.DictReader(stream)
        sucessos = 0
        linhas_com_erro = []
        engine_loc = db.engines["localidades"]
        for i, linha in enumerate(leitor, start=1):
            try:
                cid_orig = (linha.get("cidade_origem") or "").strip()
                uf_orig = (linha.get("uf_origem") or "").strip()
                cid_dest = (linha.get("cidade_destino") or "").strip()
                uf_dest = (linha.get("uf_destino") or "").strip()
                chave_orig = f"{cid_orig.lower()}-{uf_orig.lower()}"
                chave_dest = f"{cid_dest.lower()}-{uf_dest.lower()}"
                with engine_loc.connect() as conn:
                    res_orig = conn.execute(
                        text("SELECT id_cidade FROM de_para_logistica WHERE chave_busca = :c"),
                        {"c": chave_orig},
                    ).fetchone()
                    res_dest = conn.execute(
                        text("SELECT id_cidade FROM de_para_logistica WHERE chave_busca = :c"),
                        {"c": chave_dest},
                    ).fetchone()
                if not res_orig or not res_dest:
                    falha = chave_orig if not res_orig else chave_dest
                    linhas_com_erro.append(
                        f"Linha {i}: Localidade '{falha}' não encontrada."
                    )
                    continue
                data_str = (linha.get("data_emissao") or "").strip()
                data_obj = None
                if data_str:
                    try:
                        data_obj = datetime.strptime(
                            data_str, "%d/%m/%Y"
                        ).date()
                    except ValueError:
                        linhas_com_erro.append(
                            f"Linha {i}: Formato de data inválido ({data_str})."
                        )
                        continue
                novo_frete = FreteReal(
                    data_emissao=data_obj,
                    id_cidade_origem=res_orig[0],
                    id_cidade_destino=res_dest[0],
                    cidade_origem=cid_orig,
                    uf_origem=uf_orig,
                    cidade_destino=cid_dest,
                    uf_destino=uf_dest,
                    peso_real=float(linha.get("peso_real") or 0),
                    valor_nf=float(linha.get("valor_nf") or 0),
                    valor_frete_total=float(linha.get("valor_frete_total") or 0),
                    valor_imposto=(
                        float(linha["valor_imposto"])
                        if linha.get("valor_imposto")
                        else None
                    ),
                    modal=(linha.get("modal") or "").lower(),
                )
                db.session.add(novo_frete)
                sucessos += 1
            except Exception as e_row:
                linhas_com_erro.append(f"Linha {i}: Erro inesperado - {str(e_row)}")
        db.session.commit()
        return sucessos, linhas_com_erro, None
    except Exception as e:
        db.session.rollback()
        logger.exception("Erro crítico ao processar importação tabelas: %s", e)
        return 0, [], str(e)
