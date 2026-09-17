"""Exceções de domínio da Fase 2 Multiuser (capacidade, vínculo, ciclo)."""
from __future__ import annotations


class CapacidadeEsgotadaError(ValueError):
    """Vínculos ativos já ocupam a quantity contratada; novo assento recusado."""


class VinculoInconsistenteError(ValueError):
    """User/Conta/Franquia incompatíveis com o vínculo operacional solicitado."""


class CicloNaoDeterminavelError(ValueError):
    """Ciclo canônico da Conta não pode ser determinado sem inventar datas."""


class DivergenciaImpeditivaError(ValueError):
    """Divergência que impede o write (ex.: reduzir quantity abaixo dos ativos)."""


class ConviteNaoAutorizadoError(ValueError):
    """Somente Contratante ativo da própria Conta pode criar/reenviar convite."""


class ConviteInvalidoError(ValueError):
    """Token ou convite persistido inválido; não revela existência de e-mail."""


class ConviteExpiradoError(ValueError):
    """Assinatura temporal ou expires_at persistido já venceu."""


class ConviteEmailDivergenteError(ValueError):
    """E-mail autenticado não corresponde ao destinatário do convite."""


class ConviteContratoIncompativelError(ValueError):
    """User possui contrato pago ou vínculo organizacional incompatível com a transferência."""


class ConviteJaVinculadoError(ValueError):
    """Destinatário já possui vínculo ativo na Conta convidante (resposta idempotente)."""


class ConviteContratanteProprioError(ValueError):
    """Não é permitido converter o Contratante da Conta em Membro via convite."""


class GestaoMultiuserNaoAutorizadaError(ValueError):
    """Somente o Contratante ativo da Conta pode acessar a gestão Multiuser."""


class AumentoMultiuserInvalidoError(ValueError):
    """Solicitação de aumento inválida (quantidade, redução ou dados ausentes)."""


class AumentoMultiuserDivergenteError(ValueError):
    """Divergência quantity local/Stripe impede o aumento automático."""


class AumentoMultiuserCicloIndeterminadoError(ValueError):
    """Ciclo comercial da Conta não é determinável; aumento bloqueado."""


class AumentoExcepcionalNaoAutorizadoError(ValueError):
    """Somente administrador da plataforma (User.is_admin) pode decidir."""


class AumentoExcepcionalInvalidoError(ValueError):
    """Solicitação excepcional inválida ou transição não permitida."""


class AumentoExcepcionalConflitoError(ValueError):
    """CAS/idempotência: decisão conflitante ou versão desatualizada."""


class AumentoExcepcionalCorrelacaoError(ValueError):
    """Webhook extraordinário sem correlação suficiente (fail-closed)."""


class RevogacaoMultiuserNaoAutorizadaError(ValueError):
    """Somente o Contratante ativo da própria Conta pode revogar um Membro."""


class RevogacaoMultiuserInvalidaError(ValueError):
    """Revogação recusada (alvo inválido, Contratante ou vínculo incompatível)."""


class ReducaoMultiuserInvalidaError(ValueError):
    """Solicitação de redução futura inválida ou abaixo do mínimo/ativos."""


class ReducaoMultiuserConflitoError(ValueError):
    """Já existe redução pendente conflitante para a Conta."""


class TitularidadeNaoAutorizadaError(ValueError):
    """Somente administrador da plataforma pode solicitar/decidir titularidade."""


class TitularidadeInvalidaError(ValueError):
    """Candidato ou estado incompatível com a transferência de titularidade."""


class TitularidadeConflitoError(ValueError):
    """CAS/idempotência: decisão conflitante ou versão desatualizada."""


class NotificacaoInternaNaoAutorizadaError(ValueError):
    """User só acessa as próprias notificações internas."""
