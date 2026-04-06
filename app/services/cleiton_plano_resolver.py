"""
Domínio Cleiton — resolução do plano operacional a partir de dados já existentes no projeto.

Critério de desambiguação (documentado):
- Não existe coluna de plano em `Conta` nem em `Franquia`.
- A fonte adotada é `User.categoria` do operador de referência vinculado à franquia.
- Operador de referência: usuário com menor `id` entre os que têm `franquia_id` igual à franquia
  (primeiro vínculo histórico como proxy estável até existir plano explícito na conta).
- Franquia reservada sistema-interno / operacional-interno: plano `interna` (sem leitura de User).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.extensions import db
from app.models import Franquia, User
from app.services.conta_franquia_service import get_sistema_interno_ids


@dataclass(frozen=True)
class PlanoResolvidoCleiton:
    """Plano normalizado para governança operacional."""

    codigo: str
    fonte: str
    usuario_referencia_id: int | None
    categoria_raw: str | None
    pendencias: tuple[str, ...] = field(default_factory=tuple)


CODIGO_FREE = "free"
CODIGO_STARTER = "starter"
CODIGO_PRO = "pro"
CODIGO_MULTIUSER = "multiuser"
CODIGO_AVULSO = "avulso"
CODIGO_INTERNA = "interna"
CODIGO_UNKNOWN = "unknown"

FONT_INTERNA = "franquia_reservada_sistema"
FONT_USER_CATEGORIA = "user_categoria"


def _norm_cat(s: str | None) -> str:
    return (s or "").strip().lower()


def resolver_plano_operacional_para_franquia(franquia_id: int) -> PlanoResolvidoCleiton:
    """
    Resolve o plano operacional usado pelo motor Cleiton para uma franquia.
    """
    fid = int(franquia_id)
    _cid, sid = get_sistema_interno_ids()
    if sid is not None and fid == int(sid):
        return PlanoResolvidoCleiton(
            codigo=CODIGO_INTERNA,
            fonte=FONT_INTERNA,
            usuario_referencia_id=None,
            categoria_raw=None,
            pendencias=(),
        )

    fr = db.session.get(Franquia, fid)
    if fr is None:
        return PlanoResolvidoCleiton(
            codigo=CODIGO_UNKNOWN,
            fonte=FONT_USER_CATEGORIA,
            usuario_referencia_id=None,
            categoria_raw=None,
            pendencias=("franquia_inexistente",),
        )

    u = (
        User.query.filter(User.franquia_id == fid)
        .order_by(User.id.asc())
        .first()
    )
    if u is None:
        return PlanoResolvidoCleiton(
            codigo=CODIGO_FREE,
            fonte=FONT_USER_CATEGORIA,
            usuario_referencia_id=None,
            categoria_raw=None,
            pendencias=("sem_usuario_vinculado_franquia",),
        )

    raw = _norm_cat(u.categoria)
    pendencias: list[str] = []

    if raw == CODIGO_AVULSO:
        codigo = CODIGO_AVULSO
    elif raw == CODIGO_PRO:
        codigo = CODIGO_PRO
    elif raw in (CODIGO_MULTIUSER, "enterprise"):
        codigo = CODIGO_MULTIUSER
    elif raw in (CODIGO_STARTER, "start", "basico", "básico"):
        codigo = CODIGO_STARTER
    elif raw == CODIGO_FREE or raw == "":
        codigo = CODIGO_FREE
    else:
        codigo = CODIGO_UNKNOWN
        pendencias.append("categoria_nao_mapeada")

    if codigo in (CODIGO_STARTER, CODIGO_PRO, CODIGO_MULTIUSER):
        pendencias.append("modalidade_recorrente_assumida_sem_campo_modalidade")

    return PlanoResolvidoCleiton(
        codigo=codigo,
        fonte=FONT_USER_CATEGORIA,
        usuario_referencia_id=u.id,
        categoria_raw=u.categoria,
        pendencias=tuple(pendencias),
    )
