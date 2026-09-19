from app.extensions import db
from flask_login import UserMixin
from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Retorna datetime UTC naive para compatibilidade com colunas DateTime atuais."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Conta(db.Model):
    """
    Raiz contratual/comercial. Uma conta agrega uma ou mais franquias (unidades operacionais).
    No Multiuser V1 a própria Conta representa a empresa/organização contratante.
    Campos empresariais e quantity local são nullable para preservar contas legadas.
    """

    __tablename__ = "conta"
    __table_args__ = (
        db.Index(
            "uq_conta_cnpj_multiuser_ativa",
            "cnpj",
            unique=True,
            postgresql_where=db.text(
                "cnpj IS NOT NULL AND multiuser_ativa AND status = 'ativa'"
            ),
            sqlite_where=db.text(
                "cnpj IS NOT NULL AND multiuser_ativa AND status = 'ativa'"
            ),
        ),
        db.CheckConstraint(
            "quantidade_assentos_contratados IS NULL OR quantidade_assentos_contratados >= 1",
            name="ck_conta_qtd_assentos_positiva",
        ),
    )

    SLUG_SISTEMA = "sistema-interno"

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False, default="ativa", index=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)

    razao_social = db.Column(db.String(255), nullable=True)
    nome_fantasia = db.Column(db.String(255), nullable=True)
    cnpj = db.Column(db.String(14), nullable=True)
    email_empresarial = db.Column(db.String(150), nullable=True)
    endereco_logradouro = db.Column(db.String(255), nullable=True)
    endereco_numero = db.Column(db.String(20), nullable=True)
    endereco_complemento = db.Column(db.String(120), nullable=True)
    endereco_bairro = db.Column(db.String(120), nullable=True)
    endereco_cidade = db.Column(db.String(120), nullable=True)
    endereco_uf = db.Column(db.String(2), nullable=True)
    endereco_cep = db.Column(db.String(8), nullable=True)

    # Quantity comercial local (assentos contratados). Não sincroniza Stripe nesta fase.
    quantidade_assentos_contratados = db.Column(db.Integer, nullable=True)
    # Marca de uso/ativação Multiuser na raiz contratual; não substitui User.categoria.
    multiuser_ativa = db.Column(db.Boolean, nullable=False, default=False, index=True)

    STATUS_ATIVA = "ativa"
    STATUS_INATIVA = "inativa"


class Franquia(db.Model):
    """
    Unidade operacional de consumo; pertence a uma Conta.
    Fase 2: estado operacional persistido (governança Cleiton) + status de ciclo/limites.
    """

    __tablename__ = "franquia"
    __table_args__ = (db.UniqueConstraint("conta_id", "slug", name="uq_franquia_conta_slug"),)

    SLUG_SISTEMA_OPERACIONAL = "operacional-interno"

    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=False, index=True)
    nome = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(80), nullable=False, index=True)
    status = db.Column(db.String(30), nullable=False, default="active", index=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    limite_total = db.Column(db.Numeric(18, 6), nullable=True)
    consumo_acumulado = db.Column(db.Numeric(18, 6), nullable=False, default=0)
    inicio_ciclo = db.Column(db.DateTime, nullable=True)
    fim_ciclo = db.Column(db.DateTime, nullable=True)
    bloqueio_manual = db.Column(db.Boolean, nullable=False, default=False)

    conta = db.relationship("Conta", backref=db.backref("franquias", lazy="dynamic"))

    STATUS_ACTIVE = "active"
    STATUS_DEGRADED = "degraded"
    STATUS_EXPIRED = "expired"
    STATUS_BLOCKED = "blocked"


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=True)  # None para usuários só OAuth (ex.: Google)
    full_name = db.Column(db.String(150), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    categoria = db.Column(db.String(50), default='free')
    # Campo legado: não participa da governança operacional (controle real em Franquia).
    creditos = db.Column(db.Integer, default=10)
    created_at = db.Column(db.DateTime, default=utcnow_naive)
    last_login_at = db.Column(db.DateTime, nullable=True)
    subscribes_to_newsletter = db.Column(db.Boolean, default=False)
    accepted_terms_at = db.Column(db.DateTime, nullable=True)
    first_audit_completed_at = db.Column(db.DateTime, nullable=True)
    usage_purpose = db.Column(db.String(50), nullable=True)
    job_role = db.Column(db.String(100), nullable=True)
    oauth_provider = db.Column(db.String(50), nullable=True)
    oauth_sub = db.Column(db.String(255), nullable=True)
    # Início do período de trial (null = sem trial); usado com FREEMIUM_TRIAL_DIAS em ConfigRegras
    trial_start_date = db.Column(db.DateTime, nullable=True)
    # Fase 2 etapa 2: vínculo de negócio (conta / franquia operacional padrão do operador)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=False, index=True)
    franquia_id = db.Column(db.Integer, db.ForeignKey("franquia.id"), nullable=False, index=True)
    # Geração de contexto de sessão: incrementada na revogação Multiuser.
    sessao_contexto_geracao = db.Column(db.Integer, nullable=False, default=0)
    conta = db.relationship("Conta", foreign_keys=[conta_id], backref=db.backref("usuarios", lazy="dynamic"))
    franquia = db.relationship("Franquia", foreign_keys=[franquia_id], backref=db.backref("usuarios_operadores", lazy="dynamic"))

    def set_password(self, password: str) -> None:
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')

    def verify_password(self, password: str) -> bool:
        if self.password_hash is None:
            return False
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)


class MultiuserFranquiaCodigo(db.Model):
    """Código de acesso de franquia para operação Multiuser (persistência simples por franquia)."""

    __tablename__ = "multiuser_franquia_codigo"

    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=False, index=True)
    franquia_id = db.Column(db.Integer, db.ForeignKey("franquia.id"), nullable=False, index=True)
    codigo = db.Column(db.String(64), unique=True, nullable=False, index=True)
    ativo = db.Column(db.Boolean, nullable=False, default=True)
    criado_por_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    criado_em = db.Column(db.DateTime, default=utcnow_naive, nullable=False, index=True)

    conta = db.relationship("Conta", backref=db.backref("codigos_multiuser", lazy="dynamic"))
    franquia = db.relationship("Franquia", backref=db.backref("codigos_acesso_multiuser", lazy="dynamic"))
    criado_por = db.relationship("User", foreign_keys=[criado_por_user_id], backref=db.backref("codigos_multiuser_criados", lazy="dynamic"))


class ContaVinculoOrganizacional(db.Model):
    """
    Vínculo associativo User–Conta–Franquia para governança Multiuser.
    Preserva histórico (encerramento não apaga a row). Não substitui User.conta_id/franquia_id.
    """

    __tablename__ = "conta_vinculo_organizacional"
    __table_args__ = (
        db.CheckConstraint(
            "papel IN ('contratante', 'membro')",
            name="ck_conta_vinculo_org_papel",
        ),
        db.CheckConstraint(
            "estado IN ('ativo', 'encerrado')",
            name="ck_conta_vinculo_org_estado",
        ),
        db.CheckConstraint(
            "((papel = 'contratante' AND titular) OR (papel = 'membro' AND NOT titular))",
            name="ck_conta_vinculo_org_papel_titular",
        ),
        db.CheckConstraint(
            "("
            "(estado = 'ativo' AND encerrado_em IS NULL) "
            "OR (estado = 'encerrado' AND encerrado_em IS NOT NULL)"
            ")",
            name="ck_conta_vinculo_org_encerramento",
        ),
        db.Index(
            "uq_conta_vinculo_org_user_ativo",
            "user_id",
            unique=True,
            postgresql_where=db.text("estado = 'ativo'"),
            sqlite_where=db.text("estado = 'ativo'"),
        ),
        db.Index(
            "uq_conta_vinculo_org_franquia_ativo",
            "franquia_id",
            unique=True,
            postgresql_where=db.text("estado = 'ativo'"),
            sqlite_where=db.text("estado = 'ativo'"),
        ),
        db.Index(
            "uq_conta_vinculo_org_contratante_ativo",
            "conta_id",
            unique=True,
            postgresql_where=db.text("estado = 'ativo' AND papel = 'contratante'"),
            sqlite_where=db.text("estado = 'ativo' AND papel = 'contratante'"),
        ),
        db.Index("ix_conta_vinculo_org_conta_estado", "conta_id", "estado"),
    )

    PAPEL_CONTRATANTE = "contratante"
    PAPEL_MEMBRO = "membro"
    PAPEIS_V1 = (PAPEL_CONTRATANTE, PAPEL_MEMBRO)

    ESTADO_ATIVO = "ativo"
    ESTADO_ENCERRADO = "encerrado"
    ESTADOS_V1 = (ESTADO_ATIVO, ESTADO_ENCERRADO)

    ORIGEM_BACKFILL_LEGADO = "backfill_legado"
    ORIGEM_DOMINIO = "dominio"

    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    franquia_id = db.Column(db.Integer, db.ForeignKey("franquia.id"), nullable=False, index=True)
    papel = db.Column(db.String(20), nullable=False, index=True)
    estado = db.Column(db.String(20), nullable=False, default=ESTADO_ATIVO, index=True)
    titular = db.Column(db.Boolean, nullable=False, default=False)
    origem = db.Column(db.String(40), nullable=False, default=ORIGEM_DOMINIO)
    criado_por_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    iniciado_em = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    encerrado_em = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)

    conta = db.relationship(
        "Conta",
        backref=db.backref("vinculos_organizacionais", lazy="dynamic"),
    )
    user = db.relationship(
        "User",
        foreign_keys=[user_id],
        backref=db.backref("vinculos_organizacionais", lazy="dynamic"),
    )
    franquia = db.relationship(
        "Franquia",
        backref=db.backref("vinculos_organizacionais", lazy="dynamic"),
    )
    criado_por = db.relationship(
        "User",
        foreign_keys=[criado_por_user_id],
        backref=db.backref("vinculos_organizacionais_criados", lazy="dynamic"),
    )


class ContaOrganizacionalBackfillInconsistencia(db.Model):
    """Registro determinístico de legado Multiuser que não pôde ser associado com segurança."""

    __tablename__ = "conta_organizacional_backfill_inconsistencia"

    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, nullable=True, index=True)
    franquia_id = db.Column(db.Integer, nullable=True, index=True)
    user_id = db.Column(db.Integer, nullable=True, index=True)
    codigo = db.Column(db.String(80), nullable=False, index=True)
    detalhe = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)


class TermsOfUse(db.Model):
    """Termo de Uso vigente: PDF em storage persistente (data_dir/legal/terms), um ativo por vez."""
    __tablename__ = "terms_of_use"
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    upload_date = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    is_active = db.Column(db.Boolean, default=True, index=True)


class PrivacyPolicy(db.Model):
    """Política de Privacidade vigente: PDF em storage persistente (data_dir/legal/privacy_policies), com histórico."""

    __tablename__ = "privacy_policy"

    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=True)
    upload_date = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    is_active = db.Column(db.Boolean, default=True, index=True, nullable=False)
    uploaded_by_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    file_size_bytes = db.Column(db.Integer, nullable=False, default=0)
    mime_type = db.Column(db.String(120), nullable=True)

    uploaded_by = db.relationship(
        "User",
        foreign_keys=[uploaded_by_user_id],
        backref=db.backref("privacy_policies_uploaded", lazy="dynamic"),
    )


class FreteReal(db.Model):
    # Alterado para 'historico' para separar do banco de cidades
    __tablename__ = 'frete_real'
    
    id = db.Column(db.Integer, primary_key=True)
    data_emissao = db.Column(db.DateTime)
    id_cidade_origem = db.Column(db.Integer)
    id_uf_origem = db.Column(db.Integer)
    id_cidade_destino = db.Column(db.Integer)
    id_uf_destino = db.Column(db.Integer)
    cidade_origem = db.Column(db.String(100))
    uf_origem = db.Column(db.String(2))
    cidade_destino = db.Column(db.String(100))
    uf_destino = db.Column(db.String(2))
    peso_real = db.Column(db.Float)
    valor_nf = db.Column(db.Float)
    valor_frete_total = db.Column(db.Float)
    valor_imposto = db.Column(db.Float)
    modal = db.Column(db.String(50))

class Lead(db.Model):
    __tablename__ = 'leads'
    __table_args__ = (
        db.Index(
            "ix_leads_acquisition_campaign_captured_at",
            "acquisition_campaign",
            "campaign_captured_at",
        ),
    )
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    data_inscricao = db.Column(db.DateTime, default=db.func.current_timestamp())
    # Aquisição (campanha): nullable para preservar leads históricos (ex.: newsletter).
    acquisition_campaign = db.Column(db.String(80), nullable=True)
    acquisition_source = db.Column(db.String(50), nullable=True)
    campaign_captured_at = db.Column(db.DateTime, nullable=True)
    # Jornada futura (schema antecipado; sem uso operacional nesta etapa).
    cta_email_sent_at = db.Column(db.DateTime, nullable=True)
    cta_clicked_at = db.Column(db.DateTime, nullable=True)
    converted_user_id = db.Column(db.Integer, nullable=True)  # referência lógica a User.id
    converted_at = db.Column(db.DateTime, nullable=True)
    followup_count = db.Column(db.Integer, nullable=False, default=0)
    last_followup_sent_at = db.Column(db.DateTime, nullable=True)
    opt_out_at = db.Column(db.DateTime, nullable=True)
    # Ativação pós-cadastro (sequência própria; não reutiliza follow-up pré-cadastro).
    activation_email_1_sent_at = db.Column(db.DateTime, nullable=True)
    activation_email_2_sent_at = db.Column(db.DateTime, nullable=True)
    activation_opt_out_at = db.Column(db.DateTime, nullable=True)
    # Término operacional da jornada de ativação deste converted_user_id.
    # Não é opt-out: não preenche opt_out_at / activation_opt_out_at.
    activation_ended_at = db.Column(db.DateTime, nullable=True)
    activation_ended_for_user_id = db.Column(db.Integer, nullable=True)  # referência lógica a User.id
    # Identidade criptográfica do e-mail original (HMAC-SHA256 hex, 64).
    # Sem plaintext. Não é dedupe, reconciliação, analytics nem newsletter.
    # Não implica opt-out / CommunicationSuppression.
    email_hmac = db.Column(db.String(64), nullable=True)


class CommunicationSuppression(db.Model):
    """
    Memória persistente de opt-out por finalidade, sem plaintext de e-mail.

    Independente de Lead/User: a identidade é HMAC(email normalizado).
    Não substitui Lead.opt_out_at / Lead.activation_opt_out_at nesta fase.
    Backfill histórico controlado: communication_suppression_backfill_service.
    """

    __tablename__ = "communication_suppression"
    __table_args__ = (
        db.UniqueConstraint(
            "email_hmac",
            "purpose",
            name="uq_communication_suppression_email_hmac_purpose",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    email_hmac = db.Column(db.String(64), nullable=False)
    purpose = db.Column(db.String(64), nullable=False)
    suppressed_at = db.Column(db.DateTime, nullable=False)
    source = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)


class NewsletterSubscription(db.Model):
    """
    Autoridade operacional de destinatários da newsletter.

    Independente de Lead, User (sem FK), campanha desktop e CommunicationSuppression.
    Funciona para não-users. User.subscribes_to_newsletter permanece por UX/compatibilidade.

    Semântica de subscribed_at (UTC naive via utcnow_naive):
    - inscrição nova: agora (início da vigência)
    - reinscrição após unsubscribe: agora da nova inscrição (vigência atual)
    - inscrição repetida já ativa: inalterado (idempotente)

    unsubscribed_at preenchido no opt-out; null enquanto a inscrição está ativa.
    source é valor controlado (não é payload livre).
    """

    __tablename__ = "newsletter_subscription"
    __table_args__ = (
        db.UniqueConstraint("email", name="uq_newsletter_subscription_email"),
    )

    SOURCE_PUBLIC_NEWSLETTER = "public_newsletter"
    SOURCE_USER_PREFERENCE = "user_preference"
    SOURCE_USER_PREFERENCE_BACKFILL = "user_preference_backfill"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), nullable=False)
    subscribed_at = db.Column(db.DateTime, nullable=False)
    unsubscribed_at = db.Column(db.DateTime, nullable=True)
    source = db.Column(db.String(80), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False
    )

    @property
    def is_active(self) -> bool:
        return self.unsubscribed_at is None


class DesktopAccessE2ETestRun(db.Model):
    """
    Estado técnico de homologação E2E da jornada Landing Desktop.

    Isolado de Lead/aquisição real: um run por execução, mesmo User/e-mail.
    """

    __tablename__ = "desktop_access_e2e_test_run"

    id = db.Column(db.Integer, primary_key=True)
    run_id = db.Column(db.String(64), unique=True, nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    initial_email_sent_at = db.Column(db.DateTime, nullable=True)
    cta_clicked_at = db.Column(db.DateTime, nullable=True)
    registration_completed_at = db.Column(db.DateTime, nullable=True)
    followup_sent_at = db.Column(db.DateTime, nullable=True)
    opt_out_at = db.Column(db.DateTime, nullable=True)
    first_use_seen_at = db.Column(db.DateTime, nullable=True)
    first_audit_seen_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)
    # Ativação pós-cadastro (E2E Replay — paridade com Lead).
    activation_email_1_sent_at = db.Column(db.DateTime, nullable=True)
    activation_email_2_sent_at = db.Column(db.DateTime, nullable=True)
    activation_opt_out_at = db.Column(db.DateTime, nullable=True)
    activation_sequence_started_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship(
        "User",
        foreign_keys=[user_id],
        backref=db.backref("desktop_access_e2e_test_runs", lazy="dynamic"),
    )


class Pauta(db.Model):
    """Pauta para a Júlia. Scout/import preenchem; Verificador define status_verificacao. Só aprovadas vão para Julia."""
    __tablename__ = 'pautas'
    id = db.Column(db.Integer, primary_key=True)
    titulo_original = db.Column(db.String(500), nullable=False)
    fonte = db.Column(db.String(200))
    link = db.Column(db.Text, unique=True, nullable=False, index=True)
    tipo = db.Column(db.String(20), default='noticia', index=True)  # noticia | artigo
    status = db.Column(db.String(30), default='pendente', index=True)  # pendente | em_processamento | publicada | falha
    mission_id = db.Column(db.String(80), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive)
    # Fase 3: Scout + Verificador
    status_verificacao = db.Column(db.String(30), default='pendente', index=True)  # pendente | aprovado | revisar | rejeitado
    score_confiabilidade = db.Column(db.Float)
    motivo_verificacao = db.Column(db.Text)
    fonte_tipo = db.Column(db.String(30), default='manual')  # rss | api | manual | import_legacy
    hash_conteudo = db.Column(db.String(64), index=True)
    coletado_em = db.Column(db.DateTime)
    verificado_em = db.Column(db.DateTime)
    arquivada = db.Column(db.Boolean, default=False, index=True)


class NoticiaPortal(db.Model):
    __tablename__ = 'noticias_portal'
    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(20), default='noticia', index=True)
    titulo_julia = db.Column(db.String(255), nullable=False)
    subtitulo = db.Column(db.String(500))
    titulo_original = db.Column(db.String(255))
    link = db.Column(db.String(500), unique=True, nullable=False)
    fonte = db.Column(db.String(100))
    resumo_julia = db.Column(db.Text)
    conteudo_completo = db.Column(db.Text)
    url_imagem = db.Column(db.String(500))
    referencias = db.Column(db.Text)
    data_publicacao = db.Column(db.DateTime, default=utcnow_naive, index=True)
    # Etapa 2: lead e qualidade (retrocompatível)
    cta = db.Column(db.Text)
    objetivo_lead = db.Column(db.String(100))
    status_qualidade = db.Column(db.String(30), default='aprovado', index=True)
    origem_pauta = db.Column(db.String(50))
    # Fase 4: Designer + Publisher
    url_imagem_master = db.Column(db.String(500))
    assets_canais_json = db.Column(db.Text)
    status_publicacao = db.Column(db.String(30), default='pendente', index=True)  # pendente | publicado | parcial | falha
    publicado_em = db.Column(db.DateTime)

    def __repr__(self):
        return f"<{self.tipo.capitalize()}: {self.titulo_julia}>"


# --- Camada gerencial (Cleiton): plano ativo, regras, missões, auditoria, publicacao por canal ---

class PublicacaoCanal(db.Model):
    """Registro de publicação por canal (portal, linkedin, instagram, email, ...). FK lógica para noticia_id."""
    __tablename__ = 'publicacao_canal'
    id = db.Column(db.Integer, primary_key=True)
    noticia_id = db.Column(db.Integer, nullable=False, index=True)
    mission_id = db.Column(db.String(80), nullable=True, index=True)
    canal = db.Column(db.String(50), nullable=False, index=True)
    status = db.Column(db.String(30), default='pendente', index=True)  # pendente | publicado | falha | ignorado
    tentativa_atual = db.Column(db.Integer, default=1)
    max_tentativas = db.Column(db.Integer, default=3)
    payload_envio_json = db.Column(db.Text)
    resposta_canal_json = db.Column(db.Text)
    erro_detalhe = db.Column(db.Text)
    criado_em = db.Column(db.DateTime, default=utcnow_naive)
    atualizado_em = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive)


# --- Camada gerencial (Cleiton): plano ativo, regras, missões e auditoria ---

class PlanoEstrategico(db.Model):
    """Plano estratégico ativo: tema da série, objetivo, estágio atual."""
    __tablename__ = 'plano_estrategico'
    id = db.Column(db.Integer, primary_key=True)
    tema_serie = db.Column(db.String(255), nullable=False)
    objetivo = db.Column(db.Text)
    estagio_atual = db.Column(db.String(100))
    ativo = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class ConfigRegras(db.Model):
    """Regras de negócio persistidas: frequência, prioridade, janela de publicação, retries."""
    __tablename__ = 'config_regras'
    id = db.Column(db.Integer, primary_key=True)
    chave = db.Column(db.String(80), unique=True, nullable=False, index=True)
    valor_texto = db.Column(db.String(500))
    valor_inteiro = db.Column(db.Integer)
    valor_real = db.Column(db.Float)
    descricao = db.Column(db.String(255))
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class SerieEditorial(db.Model):
    """
    Série editorial de artigos com tema, objetivo de lead e cadência.
    Permite planejar sequências (ex.: "5 pilares da logística") de forma configurável.
    """
    __tablename__ = 'serie_editorial'

    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(255), nullable=False)
    tema = db.Column(db.String(255), nullable=False)
    objetivo_lead = db.Column(db.String(100))
    cta_base = db.Column(db.Text)
    descricao = db.Column(db.Text)
    cadencia_dias = db.Column(db.Integer, default=1)  # intervalo desejado entre artigos da série
    ativo = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class SerieItemEditorial(db.Model):
    """
    Item da série editorial (ex.: post 1..5 de uma série).
    Conecta planejamento (data_planejada) com a pauta/artigo efetivamente publicado.
    """
    __tablename__ = 'serie_editorial_item'

    id = db.Column(db.Integer, primary_key=True)
    serie_id = db.Column(db.Integer, nullable=False, index=True)
    ordem = db.Column(db.Integer, nullable=False, index=True)
    titulo_planejado = db.Column(db.String(500))
    subtitulo_planejado = db.Column(db.String(500))
    data_planejada = db.Column(db.DateTime, nullable=True, index=True)
    status = db.Column(
        db.String(30),
        default='planejado',
        index=True,
    )  # planejado | em_andamento | publicado | falha | pulado
    pauta_id = db.Column(db.Integer, nullable=True, index=True)
    noticia_id = db.Column(db.Integer, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive)


class MissaoAgente(db.Model):
    """Missão disparada para agente operacional (rastreio e retries)."""
    __tablename__ = 'missao_agente'
    id = db.Column(db.Integer, primary_key=True)
    mission_id = db.Column(db.String(80), unique=True, nullable=False, index=True)
    tipo_missao = db.Column(db.String(50), nullable=False, index=True)
    tema = db.Column(db.String(255))
    prioridade = db.Column(db.Integer, default=5)
    janela_publicacao_inicio = db.Column(db.DateTime)
    janela_publicacao_fim = db.Column(db.DateTime)
    tentativa_atual = db.Column(db.Integer, default=1)
    max_tentativas = db.Column(db.Integer, default=3)
    status = db.Column(db.String(30), default='pendente', index=True)  # pendente, enviado, sucesso, falha
    payload_metadados = db.Column(db.Text)  # JSON do payload enviado
    created_at = db.Column(db.DateTime, default=utcnow_naive)
    concluido_em = db.Column(db.DateTime)


class AuditoriaGerencial(db.Model):
    """Trilha de auditoria de cada decisão do Cleiton (e eventos de purge)."""
    __tablename__ = 'auditoria_gerencial'
    id = db.Column(db.Integer, primary_key=True)
    tipo_decisao = db.Column(db.String(50), nullable=False, index=True)  # orquestracao, dispatch, retry, purge_dados, purge_imagens, insight
    decisao = db.Column(db.String(255))
    contexto_json = db.Column(db.Text)
    resultado = db.Column(db.String(50))  # sucesso, falha, ignorado
    detalhe = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow_naive, index=True)


class OnboardingWordCloudHiddenTerm(db.Model):
    """
    Termos ocultados manualmente pelo admin na nuvem do onboarding.
    Não altera user_terms_normalized em AuditoriaGerencial — filtro só na agregação.
    """
    __tablename__ = 'onboarding_word_cloud_hidden_term'

    id = db.Column(db.Integer, primary_key=True)
    term_normalized = db.Column(db.String(64), nullable=False, index=True)
    is_active = db.Column(db.Boolean, default=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)
    hidden_by_user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True, index=True)
    notes = db.Column(db.String(255), nullable=True)


# --- Fase 5: Customer Insight (métricas por canal, recomendações estratégicas) ---

class InsightCanal(db.Model):
    """Métricas consolidadas por notícia/canal para retroalimentar estratégia (bind gerencial)."""
    __tablename__ = 'insight_canal'
    id = db.Column(db.Integer, primary_key=True)
    noticia_id = db.Column(db.Integer, nullable=False, index=True)
    mission_id = db.Column(db.String(80), nullable=True, index=True)
    canal = db.Column(db.String(50), nullable=False, index=True)
    impressoes = db.Column(db.Integer, default=0)
    cliques = db.Column(db.Integer, default=0)
    ctr = db.Column(db.Float)
    leads_gerados = db.Column(db.Integer, default=0)
    taxa_conversao = db.Column(db.Float)
    engajamento = db.Column(db.Float)
    score_performance = db.Column(db.Float, index=True)
    origem_dado = db.Column(db.String(20), default='mock', index=True)  # mock | api | manual
    coletado_em = db.Column(db.DateTime, default=utcnow_naive, index=True)
    processado_em = db.Column(db.DateTime, default=utcnow_naive)


class RecomendacaoEstrategica(db.Model):
    """Recomendações geradas pelo Customer Insight para o Cleiton (tema, canal, horário, frequência)."""
    __tablename__ = 'recomendacao_estrategica'
    id = db.Column(db.Integer, primary_key=True)
    contexto_json = db.Column(db.Text)
    recomendacao = db.Column(db.Text, nullable=False)
    prioridade = db.Column(db.Integer, default=5)
    status = db.Column(db.String(30), default='pendente', index=True)  # pendente | aplicada | descartada
    criado_em = db.Column(db.DateTime, default=utcnow_naive, index=True)

class BaseLocalidades(db.Model):
    __tablename__ = 'base_localidades'
    uf_nome = db.Column(db.String(100))
    cidade_nome = db.Column(db.String(100))
    chave_busca = db.Column(db.String(255), primary_key=True)
    id_uf = db.Column(db.Integer)
    id_cidade = db.Column(db.Integer)


# --- Fase 1: medicao gerencial de IA (Cleiton governanca + snapshot de custo GCP) ---


class IaConsumoEvento(db.Model):
    """Uma tentativa real de chamada ao SDK Gemini (evento append-only)."""
    __tablename__ = "ia_consumo_evento"

    id = db.Column(db.Integer, primary_key=True)
    occurred_at = db.Column(db.DateTime, nullable=False, index=True, default=utcnow_naive)
    provider = db.Column(db.String(40), nullable=False, index=True)
    operation = db.Column(db.String(40), nullable=False, index=True)
    model = db.Column(db.String(255), nullable=False, default="")
    agent = db.Column(db.String(80), nullable=False, index=True)
    flow_type = db.Column(db.String(80), nullable=False, index=True)
    api_key_label = db.Column(db.String(80), nullable=False, index=True)
    status = db.Column(db.String(40), nullable=False, index=True)
    input_tokens = db.Column(db.Integer, nullable=True)
    output_tokens = db.Column(db.Integer, nullable=True)
    total_tokens = db.Column(db.Integer, nullable=True)
    error_summary = db.Column(db.Text, nullable=True)
    # Fase 2 etapa 1: rastreio identitario (nullable para linhas historicas pre-migracao)
    conta_id = db.Column(db.Integer, nullable=True, index=True)
    franquia_id = db.Column(db.Integer, nullable=True, index=True)
    usuario_id = db.Column(db.Integer, nullable=True, index=True)
    tipo_origem = db.Column(db.String(80), nullable=True, index=True)
    origem_sistema = db.Column(db.Boolean, nullable=True, index=True)


class IaBillingCostSnapshot(db.Model):
    """Snapshot de custo real (BigQuery billing export), para dashboard sem consultar BQ a cada request."""
    __tablename__ = "ia_billing_cost_snapshot"

    id = db.Column(db.Integer, primary_key=True)
    snapshot_at = db.Column(db.DateTime, nullable=False, index=True, default=utcnow_naive)
    reference_date = db.Column(db.Date, nullable=False, index=True)
    month_competence = db.Column(db.String(7), nullable=False, index=True)
    cost_total_month_to_date = db.Column(db.Numeric(18, 6), nullable=False)
    currency = db.Column(db.String(12), nullable=False, default="USD")
    source = db.Column(db.String(80), nullable=False, default="bigquery_billing_export")


# --- Fase 1.1: processamento analitico (nao-LLM), governanca Cleiton ---


class CleitonCostConfig(db.Model):
    """
    Parametros operacionais Cleiton (singleton id=1): custo runtime/referencia Google e
    régua de conversão de créditos (tokens IA, linhas e ms processados por crédito).
    """
    __tablename__ = "cleiton_cost_config"

    id = db.Column(db.Integer, primary_key=True)
    runtime_monthly_cost = db.Column(db.Float, nullable=True)
    month_seconds = db.Column(db.Integer, nullable=False, default=2592000)
    allocation_percent = db.Column(db.Float, nullable=False, default=1.0)
    overhead_factor = db.Column(db.Float, nullable=False, default=1.0)
    cost_per_million_tokens = db.Column(db.Float, nullable=True)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive)
    # Régua de conversão: quanto 1 crédito compra de cada recurso (parametrização operacional).
    credit_tokens_per_credit = db.Column(db.Float, nullable=True)
    credit_lines_per_credit = db.Column(db.Float, nullable=True)
    credit_ms_per_credit = db.Column(db.Float, nullable=True)


class ProcessingEvent(db.Model):
    """Evento de processamento local (ex.: upload Excel + preparacao de sessao para BI)."""
    __tablename__ = "processing_events"

    id = db.Column(db.Integer, primary_key=True)
    occurred_at = db.Column(db.DateTime, nullable=False, index=True, default=utcnow_naive)
    agent = db.Column(db.String(80), nullable=False, index=True)
    flow_type = db.Column(db.String(80), nullable=False, index=True)
    processing_type = db.Column(db.String(40), nullable=False, index=True)
    rows_processed = db.Column(db.Integer, nullable=False, default=0)
    processing_time_ms = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(40), nullable=False, index=True)
    error_summary = db.Column(db.Text, nullable=True)
    # Fase 2 etapa 1: rastreio identitario (nullable para linhas historicas pre-migracao)
    conta_id = db.Column(db.Integer, nullable=True, index=True)
    franquia_id = db.Column(db.Integer, nullable=True, index=True)
    usuario_id = db.Column(db.Integer, nullable=True, index=True)
    tipo_origem = db.Column(db.String(80), nullable=True, index=True)
    origem_sistema = db.Column(db.Boolean, nullable=True, index=True)


class FunnelEvent(db.Model):
    """Evento append-only do funil de produto/vendas para analytics e conversao."""

    __tablename__ = "funnel_event"
    __table_args__ = (
        db.Index(
            "ix_funnel_event_event_source_occurred_at",
            "event_name",
            "source",
            "occurred_at",
        ),
        db.Index(
            "ix_funnel_event_user_event_source",
            "user_id",
            "event_name",
            "source",
        ),
        db.Index("ix_funnel_event_conta_occurred_at", "conta_id", "occurred_at"),
        db.Index("ix_funnel_event_franquia_occurred_at", "franquia_id", "occurred_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=False)
    franquia_id = db.Column(db.Integer, db.ForeignKey("franquia.id"), nullable=False)

    event_name = db.Column(db.String(40), nullable=False)
    source = db.Column(db.String(40), nullable=False)
    occurred_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    idempotency_key = db.Column(db.String(160), nullable=False, unique=True)

    correlation_id = db.Column(db.String(200), nullable=True)
    document_id = db.Column(db.String(120), nullable=True)
    audit_batch_id = db.Column(db.String(120), nullable=True)
    comparison_id = db.Column(db.String(120), nullable=True)
    execution_id = db.Column(db.String(120), nullable=True)
    metadata_json = db.Column(db.JSON, nullable=True)


class CleitonBillingApropriacao(db.Model):
    """
    Apropriação idempotente de billing operacional (ex.: upload Roberto).
    Evita dupla cobrança para a mesma chave de idempotência.
    """

    __tablename__ = "cleiton_billing_apropriacao"

    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, nullable=False, index=True, default=utcnow_naive)
    idempotency_key = db.Column(db.String(160), nullable=False, unique=True, index=True)

    agent = db.Column(db.String(80), nullable=False, index=True)
    flow_type = db.Column(db.String(80), nullable=False, index=True)
    status = db.Column(db.String(40), nullable=False, index=True)
    error_summary = db.Column(db.Text, nullable=True)

    rows_processed = db.Column(db.Integer, nullable=False, default=0)
    processing_time_ms = db.Column(db.Integer, nullable=False, default=0)
    processing_event_id = db.Column(db.Integer, nullable=True, index=True)

    creditos_apropriados = db.Column(db.Numeric(18, 6), nullable=True)
    motivo = db.Column(db.String(80), nullable=True, index=True)

    conta_id = db.Column(db.Integer, nullable=True, index=True)
    franquia_id = db.Column(db.Integer, nullable=True, index=True)
    usuario_id = db.Column(db.Integer, nullable=True, index=True)


class ContaMonetizacaoVinculo(db.Model):
    """
    Vínculo comercial externo da Conta (Stripe-ready), sem alterar a fonte operacional em Franquia.
    Histórico é preservado com múltiplos registros por conta.
    """

    __tablename__ = "conta_monetizacao_vinculo"
    __table_args__ = (
        db.Index(
            "uq_conta_monetizacao_vinculo_conta_ativo_true",
            "conta_id",
            unique=True,
            postgresql_where=db.text("ativo IS TRUE"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=False, index=True)

    provider = db.Column(db.String(40), nullable=False, index=True)
    customer_id = db.Column(db.String(160), nullable=True, index=True)
    subscription_id = db.Column(db.String(160), nullable=True, index=True)
    price_id = db.Column(db.String(160), nullable=True, index=True)
    plano_interno = db.Column(db.String(40), nullable=True, index=True)
    status_contratual_externo = db.Column(db.String(60), nullable=True, index=True)
    vigencia_externa_inicio = db.Column(db.DateTime, nullable=True)
    vigencia_externa_fim = db.Column(db.DateTime, nullable=True)

    ativo = db.Column(db.Boolean, nullable=False, default=True, index=True)
    snapshot_normalizado_json = db.Column(db.Text, nullable=True)
    payload_bruto_sanitizado_json = db.Column(db.Text, nullable=True)

    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False, index=True)
    updated_at = db.Column(
        db.DateTime,
        default=utcnow_naive,
        onupdate=utcnow_naive,
        nullable=False,
    )
    desativado_em = db.Column(db.DateTime, nullable=True)

    conta = db.relationship("Conta", backref=db.backref("monetizacao_vinculos", lazy="dynamic"))


class MonetizacaoFato(db.Model):
    """
    Fato append-only de monetização para auditoria/governança.
    Prepara correlação futura (checkout/webhook), sem automação nesta fase.
    """

    __tablename__ = "monetizacao_fato"
    __table_args__ = (
        db.Index(
            "uq_monetizacao_fato_idempotency_key_not_null",
            "idempotency_key",
            unique=True,
            postgresql_where=db.text("idempotency_key IS NOT NULL"),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    tipo_fato = db.Column(db.String(80), nullable=False, index=True)
    status_tecnico = db.Column(db.String(40), nullable=False, index=True)
    idempotency_key = db.Column(db.String(200), nullable=True, index=True)
    correlation_key = db.Column(db.String(200), nullable=True, index=True)

    timestamp_externo = db.Column(db.DateTime, nullable=True, index=True)
    timestamp_interno = db.Column(db.DateTime, nullable=False, default=utcnow_naive, index=True)

    provider = db.Column(db.String(40), nullable=True, index=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=True, index=True)
    franquia_id = db.Column(db.Integer, db.ForeignKey("franquia.id"), nullable=True, index=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)

    external_event_id = db.Column(db.String(200), nullable=True, index=True)
    customer_id = db.Column(db.String(160), nullable=True, index=True)
    subscription_id = db.Column(db.String(160), nullable=True, index=True)
    price_id = db.Column(db.String(160), nullable=True, index=True)
    invoice_id = db.Column(db.String(160), nullable=True, index=True)
    identificadores_externos_json = db.Column(db.Text, nullable=True)

    snapshot_normalizado_json = db.Column(db.Text, nullable=False)
    payload_bruto_sanitizado_json = db.Column(db.Text, nullable=True)

    conta = db.relationship("Conta", backref=db.backref("fatos_monetizacao", lazy="dynamic"))
    franquia = db.relationship("Franquia", backref=db.backref("fatos_monetizacao", lazy="dynamic"))
    usuario = db.relationship("User", backref=db.backref("fatos_monetizacao", lazy="dynamic"))


class ContaMonetizacaoCheckoutIntencao(db.Model):
    """
    Intenção exclusiva de Checkout Multiuser por Conta.

    Não é vínculo comercial ativo. Não guarda PII nem dados de pagamento.
    MonetizacaoFato permanece append-only; ContaMonetizacaoVinculo permanece o contrato confirmado.
    """

    __tablename__ = "conta_monetizacao_checkout_intencao"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('pendente', 'consumida', 'expirada')",
            name="ck_conta_checkout_intencao_estado",
        ),
        db.Index(
            "uq_conta_checkout_intencao_pendente_multiuser",
            "conta_id",
            unique=True,
            postgresql_where=db.text(
                "estado = 'pendente' AND plano_interno = 'multiuser'"
            ),
            sqlite_where=db.text(
                "estado = 'pendente' AND plano_interno = 'multiuser'"
            ),
        ),
        db.Index(
            "uq_conta_checkout_intencao_correlation",
            "correlation_id",
            unique=True,
        ),
    )

    ESTADO_PENDENTE = "pendente"
    ESTADO_CONSUMIDA = "consumida"
    ESTADO_EXPIRADA = "expirada"
    PLANO_MULTIUSER = "multiuser"

    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=False, index=True)
    franquia_id = db.Column(db.Integer, db.ForeignKey("franquia.id"), nullable=True, index=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    plano_interno = db.Column(db.String(40), nullable=False, default=PLANO_MULTIUSER, index=True)
    estado = db.Column(db.String(20), nullable=False, default=ESTADO_PENDENTE, index=True)
    correlation_id = db.Column(db.String(64), nullable=False)
    stripe_idempotency_key = db.Column(db.String(200), nullable=False)
    checkout_session_id = db.Column(db.String(200), nullable=True, index=True)
    checkout_status = db.Column(db.String(40), nullable=True)
    checkout_expires_at = db.Column(db.DateTime, nullable=True)
    price_id = db.Column(db.String(160), nullable=False)
    quantity_solicitada = db.Column(db.Integer, nullable=False)
    customer_id = db.Column(db.String(160), nullable=True)
    subscription_id = db.Column(db.String(160), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)
    consumida_em = db.Column(db.DateTime, nullable=True)
    expirada_em = db.Column(db.DateTime, nullable=True)

    conta = db.relationship(
        "Conta",
        backref=db.backref("checkout_intencoes_monetizacao", lazy="dynamic"),
    )


class ContaMultiuserConvite(db.Model):
    """
    Convite organizacional Multiuser (Fase 4).

    Reserva uma Franquia livre enquanto pendente e válido. Não ocupa User.
    Token assinado não é persistido. MultiuserFranquiaCodigo permanece legado.
    """

    __tablename__ = "conta_multiuser_convite"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('pendente', 'aceito', 'expirado')",
            name="ck_conta_multiuser_convite_estado",
        ),
        db.Index(
            "uq_conta_convite_franquia_pendente",
            "franquia_id",
            unique=True,
            postgresql_where=db.text("estado = 'pendente'"),
            sqlite_where=db.text("estado = 'pendente'"),
        ),
        db.Index(
            "uq_conta_convite_conta_email_pendente",
            "conta_id",
            "email_destino",
            unique=True,
            postgresql_where=db.text("estado = 'pendente'"),
            sqlite_where=db.text("estado = 'pendente'"),
        ),
    )

    ESTADO_PENDENTE = "pendente"
    ESTADO_ACEITO = "aceito"
    ESTADO_EXPIRADO = "expirado"
    ESTADOS_V1 = (ESTADO_PENDENTE, ESTADO_ACEITO, ESTADO_EXPIRADO)

    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=False, index=True)
    franquia_id = db.Column(db.Integer, db.ForeignKey("franquia.id"), nullable=False, index=True)
    criado_por_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    email_destino = db.Column(db.String(150), nullable=False, index=True)
    estado = db.Column(db.String(20), nullable=False, default=ESTADO_PENDENTE, index=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    enviado_em = db.Column(db.DateTime, nullable=True)
    reenviado_em = db.Column(db.DateTime, nullable=True)
    accepted_at = db.Column(db.DateTime, nullable=True)
    accepted_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)

    conta = db.relationship(
        "Conta",
        backref=db.backref("convites_multiuser", lazy="dynamic"),
    )
    franquia = db.relationship(
        "Franquia",
        backref=db.backref("convites_multiuser", lazy="dynamic"),
    )
    criado_por = db.relationship(
        "User",
        foreign_keys=[criado_por_user_id],
        backref=db.backref("convites_multiuser_criados", lazy="dynamic"),
    )
    accepted_user = db.relationship(
        "User",
        foreign_keys=[accepted_user_id],
        backref=db.backref("convites_multiuser_aceitos", lazy="dynamic"),
    )


class ContaMultiuserCicloAumento(db.Model):
    """
    Contador cumulativo de aumento automático no ciclo comercial da Conta (Fase 5).

    Não deriva de quantity_atual - quantity_original. Reset só na virada
    legítima do ciclo canônico (novo par inicio/fim).
    """

    __tablename__ = "conta_multiuser_ciclo_aumento"
    __table_args__ = (
        db.UniqueConstraint(
            "conta_id",
            "ciclo_inicio",
            "ciclo_fim",
            name="uq_conta_multiuser_ciclo_aumento_ciclo",
        ),
        db.CheckConstraint(
            "acumulado_automatico >= 0",
            name="ck_conta_multiuser_ciclo_aumento_acumulado",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=False, index=True)
    ciclo_inicio = db.Column(db.DateTime, nullable=False)
    ciclo_fim = db.Column(db.DateTime, nullable=False)
    acumulado_automatico = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)

    conta = db.relationship(
        "Conta",
        backref=db.backref("aumentos_automaticos_ciclo", lazy="dynamic"),
    )


class ContaMultiuserAumentoOperacao(db.Model):
    """
    Operação de aumento de assentos (Fase 5).

    Estados mínimos F5: iniciado | stripe_enviado | aprovado_automatico |
    enviado_analise | falha_reconciliacao.
    Não implementa a máquina da Fase 6 (pagamento extraordinário/aprovação ADM).
    """

    __tablename__ = "conta_multiuser_aumento_operacao"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ("
            "'iniciado', 'stripe_enviado', 'aprovado_automatico', "
            "'enviado_analise', 'falha_reconciliacao'"
            ")",
            name="ck_conta_multiuser_aumento_operacao_estado",
        ),
        db.CheckConstraint(
            "quantidade_solicitada >= 1",
            name="ck_conta_multiuser_aumento_operacao_qtd",
        ),
        db.Index(
            "uq_conta_multiuser_aumento_operacao_idempotency",
            "idempotency_key",
            unique=True,
        ),
    )

    ESTADO_INICIADO = "iniciado"
    ESTADO_STRIPE_ENVIADO = "stripe_enviado"
    ESTADO_APROVADO_AUTOMATICO = "aprovado_automatico"
    ESTADO_ENVIADO_ANALISE = "enviado_analise"
    ESTADO_FALHA_RECONCILIACAO = "falha_reconciliacao"
    ESTADOS_V1 = (
        ESTADO_INICIADO,
        ESTADO_STRIPE_ENVIADO,
        ESTADO_APROVADO_AUTOMATICO,
        ESTADO_ENVIADO_ANALISE,
        ESTADO_FALHA_RECONCILIACAO,
    )

    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=False, index=True)
    solicitado_por_user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    idempotency_key = db.Column(db.String(200), nullable=False)
    correlation_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    quantidade_solicitada = db.Column(db.Integer, nullable=False)
    quantity_anterior = db.Column(db.Integer, nullable=False)
    quantity_nova = db.Column(db.Integer, nullable=True)
    estado = db.Column(db.String(40), nullable=False, index=True)
    stripe_customer_id = db.Column(db.String(160), nullable=True)
    stripe_subscription_id = db.Column(db.String(160), nullable=True)
    stripe_subscription_item_id = db.Column(db.String(160), nullable=True)
    stripe_quantity_enviada = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)

    conta = db.relationship(
        "Conta",
        backref=db.backref("aumentos_assentos_operacoes", lazy="dynamic"),
    )
    solicitado_por = db.relationship(
        "User",
        foreign_keys=[solicitado_por_user_id],
        backref=db.backref("aumentos_multiuser_solicitados", lazy="dynamic"),
    )


class ContaMultiuserAumentoExcepcional(db.Model):
    """
    Governança comercial da solicitação excepcional (Fase 6).

    Complementa ContaMultiuserAumentoOperacao (F5) em 1:1. Não altera a
    máquina automática F5. MonetizacaoFato permanece append-only.
    """

    __tablename__ = "conta_multiuser_aumento_excepcional"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ("
            "'em_analise', 'rejeitada', 'aprovada_gratuita', "
            "'aguardando_pagamento', 'pagamento_confirmado', 'liberada', "
            "'expirada', 'reconciliacao_necessaria'"
            ")",
            name="ck_conta_multiuser_aumento_excepcional_estado",
        ),
        db.CheckConstraint(
            "quantidade_solicitada >= 1",
            name="ck_conta_multiuser_aumento_excepcional_qtd_sol",
        ),
        db.CheckConstraint(
            "quantidade_aprovada IS NULL OR "
            "(quantidade_aprovada >= 1 AND quantidade_aprovada <= quantidade_solicitada)",
            name="ck_conta_multiuser_aumento_excepcional_qtd_apr",
        ),
        db.CheckConstraint(
            "versao >= 1",
            name="ck_conta_multiuser_aumento_excepcional_versao",
        ),
        db.UniqueConstraint(
            "operacao_id",
            name="uq_conta_multiuser_aumento_excepcional_operacao",
        ),
        db.UniqueConstraint(
            "correlation_id",
            name="uq_conta_multiuser_aumento_excepcional_correlation",
        ),
        db.Index(
            "uq_conta_multiuser_aumento_excepcional_decisao_idem",
            "decisao_idempotency_key",
            unique=True,
            postgresql_where=db.text("decisao_idempotency_key IS NOT NULL"),
            sqlite_where=db.text("decisao_idempotency_key IS NOT NULL"),
        ),
        db.Index(
            "uq_conta_multiuser_aumento_excepcional_checkout",
            "stripe_checkout_session_id",
            unique=True,
            postgresql_where=db.text("stripe_checkout_session_id IS NOT NULL"),
            sqlite_where=db.text("stripe_checkout_session_id IS NOT NULL"),
        ),
    )

    ESTADO_EM_ANALISE = "em_analise"
    ESTADO_REJEITADA = "rejeitada"
    ESTADO_APROVADA_GRATUITA = "aprovada_gratuita"
    ESTADO_AGUARDANDO_PAGAMENTO = "aguardando_pagamento"
    ESTADO_PAGAMENTO_CONFIRMADO = "pagamento_confirmado"
    ESTADO_LIBERADA = "liberada"
    ESTADO_EXPIRADA = "expirada"
    ESTADO_RECONCILIACAO_NECESSARIA = "reconciliacao_necessaria"
    ESTADOS_V1 = (
        ESTADO_EM_ANALISE,
        ESTADO_REJEITADA,
        ESTADO_APROVADA_GRATUITA,
        ESTADO_AGUARDANDO_PAGAMENTO,
        ESTADO_PAGAMENTO_CONFIRMADO,
        ESTADO_LIBERADA,
        ESTADO_EXPIRADA,
        ESTADO_RECONCILIACAO_NECESSARIA,
    )

    DECISAO_GRATUITA = "gratuita"
    DECISAO_PAGA = "paga"
    DECISAO_REJEITADA = "rejeitada"

    id = db.Column(db.Integer, primary_key=True)
    operacao_id = db.Column(
        db.Integer,
        db.ForeignKey("conta_multiuser_aumento_operacao.id"),
        nullable=False,
        index=True,
    )
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=False, index=True)
    solicitante_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    administrador_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    ciclo_inicio = db.Column(db.DateTime, nullable=False)
    ciclo_fim = db.Column(db.DateTime, nullable=False)
    quantity_atual = db.Column(db.Integer, nullable=False)
    quantidade_solicitada = db.Column(db.Integer, nullable=False)
    quantidade_aprovada = db.Column(db.Integer, nullable=True)
    acumulado_automatico_ciclo = db.Column(db.Integer, nullable=False, default=0)
    estado = db.Column(db.String(40), nullable=False, index=True)
    decisao = db.Column(db.String(20), nullable=True)
    decidido_em = db.Column(db.DateTime, nullable=True)
    decisao_idempotency_key = db.Column(db.String(200), nullable=True)
    correlation_id = db.Column(db.String(64), nullable=False)
    request_id = db.Column(db.String(64), nullable=False, index=True)
    versao = db.Column(db.Integer, nullable=False, default=1)
    preco_unitario_centavos = db.Column(db.Integer, nullable=True)
    instante_calculo = db.Column(db.DateTime, nullable=True)
    timezone_calculo = db.Column(db.String(40), nullable=True)
    dias_totais = db.Column(db.Integer, nullable=True)
    dias_restantes = db.Column(db.Integer, nullable=True)
    valor_calculado_centavos = db.Column(db.Integer, nullable=True)
    versao_formula = db.Column(db.String(40), nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True, index=True)
    stripe_checkout_session_id = db.Column(db.String(200), nullable=True)
    stripe_payment_intent_id = db.Column(db.String(200), nullable=True, index=True)
    stripe_checkout_url = db.Column(db.String(500), nullable=True)
    stripe_customer_id = db.Column(db.String(160), nullable=True)
    liberado_em = db.Column(db.DateTime, nullable=True)
    quantity_nova = db.Column(db.Integer, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)

    operacao = db.relationship(
        "ContaMultiuserAumentoOperacao",
        backref=db.backref("aumento_excepcional", uselist=False),
    )
    conta = db.relationship(
        "Conta",
        backref=db.backref("aumentos_excepcionais", lazy="dynamic"),
    )
    solicitante = db.relationship(
        "User",
        foreign_keys=[solicitante_id],
        backref=db.backref("aumentos_excepcionais_solicitados", lazy="dynamic"),
    )
    administrador = db.relationship(
        "User",
        foreign_keys=[administrador_id],
        backref=db.backref("aumentos_excepcionais_decididos", lazy="dynamic"),
    )


class ContaMultiuserReducaoQuantity(db.Model):
    """
    Redução futura de quantity Multiuser (Fase 7).

    Não altera quantity/Stripe no pedido. Efeito somente no corte canônico.
    Uma única redução pendente por Conta.
    """

    __tablename__ = "conta_multiuser_reducao_quantity"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('pendente', 'efetivada', 'bloqueada_no_corte')",
            name="ck_conta_multiuser_reducao_estado",
        ),
        db.CheckConstraint(
            "quantity_atual_no_pedido >= 1 AND quantity_futura >= 1 "
            "AND quantity_futura < quantity_atual_no_pedido",
            name="ck_conta_multiuser_reducao_qtd",
        ),
        db.CheckConstraint(
            "versao >= 1",
            name="ck_conta_multiuser_reducao_versao",
        ),
        db.Index(
            "uq_conta_multiuser_reducao_pendente",
            "conta_id",
            unique=True,
            postgresql_where=db.text("estado = 'pendente'"),
            sqlite_where=db.text("estado = 'pendente'"),
        ),
        db.Index(
            "uq_conta_multiuser_reducao_idempotency",
            "idempotency_key",
            unique=True,
        ),
    )

    ESTADO_PENDENTE = "pendente"
    ESTADO_EFETIVADA = "efetivada"
    ESTADO_BLOQUEADA_NO_CORTE = "bloqueada_no_corte"
    ESTADOS_V1 = (ESTADO_PENDENTE, ESTADO_EFETIVADA, ESTADO_BLOQUEADA_NO_CORTE)

    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=False, index=True)
    solicitado_por_user_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    quantity_atual_no_pedido = db.Column(db.Integer, nullable=False)
    quantity_futura = db.Column(db.Integer, nullable=False)
    solicitado_em = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    efetivar_em = db.Column(db.DateTime, nullable=False, index=True)
    estado = db.Column(db.String(40), nullable=False, default=ESTADO_PENDENTE, index=True)
    idempotency_key = db.Column(db.String(200), nullable=False)
    correlation_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    versao = db.Column(db.Integer, nullable=False, default=1)
    stripe_customer_id = db.Column(db.String(160), nullable=True)
    stripe_subscription_id = db.Column(db.String(160), nullable=True)
    stripe_subscription_item_id = db.Column(db.String(160), nullable=True)
    efetivada_em = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)

    conta = db.relationship(
        "Conta",
        backref=db.backref("reducoes_quantity", lazy="dynamic"),
    )
    solicitado_por = db.relationship(
        "User",
        foreign_keys=[solicitado_por_user_id],
        backref=db.backref("reducoes_multiuser_solicitadas", lazy="dynamic"),
    )


class NotificacaoInterna(db.Model):
    """Notificação interna mínima V1 (Fase 7). Privada por User. Sem URL externa."""

    __tablename__ = "notificacao_interna"
    __table_args__ = (
        db.UniqueConstraint(
            "user_id",
            "dedup_key",
            name="uq_notificacao_interna_user_dedup",
        ),
        db.Index("ix_notificacao_interna_user_created", "user_id", "created_at"),
        db.Index("ix_notificacao_interna_user_unread", "user_id", "read_at"),
    )

    TIPO_MEMBERSHIP_REVOGADO = "membership_revogado"
    TIPO_REDUCAO_SOLICITADA = "reducao_solicitada"
    TIPO_REDUCAO_EFETIVADA = "reducao_efetivada"
    TIPO_REDUCAO_NAO_EFETIVADA = "reducao_nao_efetivada"
    TIPO_TITULARIDADE_SOLICITADA = "titularidade_solicitada"
    TIPO_TITULARIDADE_APROVADA = "titularidade_aprovada"
    TIPO_TITULARIDADE_REJEITADA = "titularidade_rejeitada"
    TIPO_EXCEPCIONAL_ENVIADO_ANALISE = "excepcional_enviado_analise"
    TIPO_EXCEPCIONAL_APROVADO_GRATUITO = "excepcional_aprovado_gratuito"
    TIPO_EXCEPCIONAL_AGUARDANDO_PAGAMENTO = "excepcional_aguardando_pagamento"
    TIPO_EXCEPCIONAL_REJEITADO = "excepcional_rejeitado"
    TIPO_EXCEPCIONAL_PAGAMENTO_CONFIRMADO = "excepcional_pagamento_confirmado"
    TIPO_EXCEPCIONAL_LIBERADO = "excepcional_liberado"
    TIPO_EXCEPCIONAL_EXPIRADO = "excepcional_expirado"
    TIPO_EXCEPCIONAL_RECONCILIACAO = "excepcional_reconciliacao_necessaria"
    TIPO_CONVITE_RELEVANTE = "convite_relevante"
    TIPOS_V1 = (
        TIPO_MEMBERSHIP_REVOGADO,
        TIPO_REDUCAO_SOLICITADA,
        TIPO_REDUCAO_EFETIVADA,
        TIPO_REDUCAO_NAO_EFETIVADA,
        TIPO_TITULARIDADE_SOLICITADA,
        TIPO_TITULARIDADE_APROVADA,
        TIPO_TITULARIDADE_REJEITADA,
        TIPO_EXCEPCIONAL_ENVIADO_ANALISE,
        TIPO_EXCEPCIONAL_APROVADO_GRATUITO,
        TIPO_EXCEPCIONAL_AGUARDANDO_PAGAMENTO,
        TIPO_EXCEPCIONAL_REJEITADO,
        TIPO_EXCEPCIONAL_PAGAMENTO_CONFIRMADO,
        TIPO_EXCEPCIONAL_LIBERADO,
        TIPO_EXCEPCIONAL_EXPIRADO,
        TIPO_EXCEPCIONAL_RECONCILIACAO,
        TIPO_CONVITE_RELEVANTE,
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=True, index=True)
    tipo = db.Column(db.String(60), nullable=False, index=True)
    mensagem = db.Column(db.String(500), nullable=False)
    cta_interno = db.Column(db.String(80), nullable=True)
    referencia_dominio = db.Column(db.String(120), nullable=True)
    dedup_key = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    read_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship(
        "User",
        backref=db.backref("notificacoes_internas", lazy="dynamic"),
    )
    conta = db.relationship(
        "Conta",
        backref=db.backref("notificacoes_internas", lazy="dynamic"),
    )


class ContaMultiuserTitularidadeSolicitacao(db.Model):
    """
    Transferência administrativa de titularidade Multiuser (Fase 7).

    Não é self-service. State machine própria; AuditoriaGerencial é trilha, não o estado.
    """

    __tablename__ = "conta_multiuser_titularidade_solicitacao"
    __table_args__ = (
        db.CheckConstraint(
            "estado IN ('solicitada', 'aprovada', 'rejeitada')",
            name="ck_conta_multiuser_titularidade_estado",
        ),
        db.CheckConstraint(
            "titular_atual_id != candidato_id",
            name="ck_conta_multiuser_titularidade_distintos",
        ),
        db.CheckConstraint(
            "versao >= 1",
            name="ck_conta_multiuser_titularidade_versao",
        ),
        db.Index(
            "uq_conta_multiuser_titularidade_solicitada",
            "conta_id",
            unique=True,
            postgresql_where=db.text("estado = 'solicitada'"),
            sqlite_where=db.text("estado = 'solicitada'"),
        ),
        db.Index(
            "uq_conta_multiuser_titularidade_idempotency",
            "idempotency_key",
            unique=True,
        ),
        db.Index(
            "uq_conta_multiuser_titularidade_decisao_idem",
            "decisao_idempotency_key",
            unique=True,
            postgresql_where=db.text("decisao_idempotency_key IS NOT NULL"),
            sqlite_where=db.text("decisao_idempotency_key IS NOT NULL"),
        ),
    )

    ESTADO_SOLICITADA = "solicitada"
    ESTADO_APROVADA = "aprovada"
    ESTADO_REJEITADA = "rejeitada"
    ESTADOS_V1 = (ESTADO_SOLICITADA, ESTADO_APROVADA, ESTADO_REJEITADA)

    id = db.Column(db.Integer, primary_key=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=False, index=True)
    titular_atual_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    candidato_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    solicitante_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    motivo = db.Column(db.String(500), nullable=False)
    estado = db.Column(db.String(20), nullable=False, default=ESTADO_SOLICITADA, index=True)
    administrador_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    decidido_em = db.Column(db.DateTime, nullable=True)
    idempotency_key = db.Column(db.String(200), nullable=False)
    correlation_id = db.Column(db.String(64), nullable=False, unique=True, index=True)
    decisao_idempotency_key = db.Column(db.String(200), nullable=True)
    versao = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False, index=True)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)

    conta = db.relationship(
        "Conta",
        backref=db.backref("solicitacoes_titularidade", lazy="dynamic"),
    )
    titular_atual = db.relationship(
        "User",
        foreign_keys=[titular_atual_id],
        backref=db.backref("titularidades_como_titular", lazy="dynamic"),
    )
    candidato = db.relationship(
        "User",
        foreign_keys=[candidato_id],
        backref=db.backref("titularidades_como_candidato", lazy="dynamic"),
    )
    solicitante = db.relationship(
        "User",
        foreign_keys=[solicitante_id],
        backref=db.backref("titularidades_solicitadas", lazy="dynamic"),
    )
    administrador = db.relationship(
        "User",
        foreign_keys=[administrador_id],
        backref=db.backref("titularidades_decididas", lazy="dynamic"),
    )


class HomeCtaExperimentEvent(db.Model):
    """Evento isolado do experimento A/B/C do CTA da Home. Sem PII e sem FunnelEvent."""

    __tablename__ = "home_cta_experiment_event"
    __table_args__ = (
        db.UniqueConstraint(
            "experiment",
            "assignment_id",
            "event_type",
            name="uq_home_cta_experiment_event_assignment_type",
        ),
        db.Index(
            "ix_home_cta_experiment_event_experiment_occurred_at",
            "experiment",
            "occurred_at",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    experiment = db.Column(db.String(40), nullable=False)
    assignment_id = db.Column(db.String(64), nullable=False)
    variant = db.Column(db.String(16), nullable=False)
    event_type = db.Column(db.String(20), nullable=False)
    interaction_origin = db.Column(db.String(20), nullable=True)
    occurred_at = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
