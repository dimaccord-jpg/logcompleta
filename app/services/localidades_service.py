from typing import Optional, Dict, Any

from app.models import BaseLocalidades


def buscar_localidade(cidade: str, uf: str) -> Optional[Dict[str, Any]]:
    """
    Busca informações de localidade (cidade e UF) na base_localidades
    a partir do nome da cidade e da sigla da UF.

    Retorna um dicionário com:
        - id_cidade (int)
        - id_uf (int)
        - cidade_texto (str)
        - uf_texto (str)

    ou None caso não seja encontrada correspondência.
    """
    if not cidade or not uf:
        return None

    cidade_normalizada = cidade.strip().lower()
    uf_normalizada = uf.strip().lower()

    chave_busca = f"{cidade_normalizada}-{uf_normalizada}"

    registro = BaseLocalidades.query.filter_by(chave_busca=chave_busca).first()
    if registro is None:
        return None

    return {
        "id_cidade": registro.id_cidade,
        "id_uf": registro.id_uf,
        "cidade_texto": registro.cidade_nome,
        "uf_texto": registro.uf_nome,
    }

