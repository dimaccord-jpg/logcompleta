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
    """
    cadastro_origem distingue a conta criada pela conclusão do canal.
    NULL preserva o cadastro web e os usuários anteriores. O valor fechado
    permite perfil completo sem usage_purpose, que o canal não coleta.
    """

    CADASTRO_ORIGEM_WHATSAPP = "onboarding_whatsapp"
    _SQL_CADASTRO_ORIGEM = (
        "cadastro_origem IS NULL OR cadastro_origem = 'onboarding_whatsapp'"
    )

    __table_args__ = (
        db.CheckConstraint(_SQL_CADASTRO_ORIGEM, name="ck_user_cadastro_origem"),
    )

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
    cadastro_origem = db.Column(db.String(40), nullable=True)
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


class OnboardingRespostaDeclarada(db.Model):
    """
    Estado atual de uma resposta complementar de onboarding declarada pelo usuário.

    Uma linha por (user, question_key). A definição da entrevista fica em código
    versionado; esta tabela não cria coluna por pergunta. Usuários sem entrevista
    simplesmente não têm linhas.

    Não é inferência de IA, não autoriza billing e não segmenta Growth.
    """

    __tablename__ = "onboarding_resposta_declarada"

    ORIGEM_CADASTRO_WEB = "cadastro_web"
    ORIGEM_ONBOARDING_WHATSAPP = "onboarding_whatsapp"
    ORIGEM_PERFIL_USUARIO = "perfil_usuario"
    ORIGENS = (
        ORIGEM_CADASTRO_WEB,
        ORIGEM_ONBOARDING_WHATSAPP,
        ORIGEM_PERFIL_USUARIO,
    )
    _SQL_ORIGEM = "origem IN ({})".format(", ".join(f"'{v}'" for v in ORIGENS))

    __table_args__ = (
        db.UniqueConstraint(
            "user_id",
            "question_key",
            name="uq_onboarding_resposta_user_pergunta",
        ),
        db.CheckConstraint(_SQL_ORIGEM, name="ck_onboarding_resposta_origem"),
        db.CheckConstraint(
            "length(question_key) > 0",
            name="ck_onboarding_resposta_question_key",
        ),
        db.CheckConstraint(
            "length(answer_key) > 0",
            name="ck_onboarding_resposta_answer_key",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    question_key = db.Column(db.String(80), nullable=False)
    answer_key = db.Column(db.String(80), nullable=False)
    origem = db.Column(db.String(40), nullable=False)
    taxonomia_versao = db.Column(db.String(40), nullable=False)
    declarada_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    atualizada_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)

    user = db.relationship(
        "User",
        backref=db.backref(
            "respostas_onboarding",
            lazy="dynamic",
            cascade="all, delete-orphan",
        ),
    )


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
    régua de conversão de créditos (tokens IA, linhas, ms e interações WhatsApp por crédito).
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
    interacoes_whatsapp_por_credito = db.Column(db.Float, nullable=True)


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
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=True)
    franquia_id = db.Column(db.Integer, db.ForeignKey("franquia.id"), nullable=True)

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


class GrowthExperiment(db.Model):
    """Registro administrativo manual de experimento e aprendizado de Growth.

    Texto livre, sem vínculo a FunnelEvent, campanhas externas ou usuários.
    """

    __tablename__ = "growth_experiment"

    STATUS_PLANNED = "planned"
    STATUS_RUNNING = "running"
    STATUS_COMPLETED = "completed"
    STATUS_LEARNING_RECORDED = "learning_recorded"
    STATUSES = (
        STATUS_PLANNED,
        STATUS_RUNNING,
        STATUS_COMPLETED,
        STATUS_LEARNING_RECORDED,
    )
    STATUS_LABELS = {
        STATUS_PLANNED: "Planejado",
        STATUS_RUNNING: "Executando",
        STATUS_COMPLETED: "Concluído",
        STATUS_LEARNING_RECORDED: "Aprendizado registrado",
    }

    id = db.Column(db.Integer, primary_key=True)
    hypothesis = db.Column(db.Text, nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    origin_campaign = db.Column(db.String(255), nullable=True)
    change_description = db.Column(db.Text, nullable=True)
    primary_metric = db.Column(db.String(255), nullable=False)
    observed_result = db.Column(db.Text, nullable=True)
    evidence = db.Column(db.Text, nullable=True)
    interpretation = db.Column(db.Text, nullable=True)
    decision = db.Column(db.Text, nullable=True)
    next_action = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(32), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=utcnow_naive,
        onupdate=utcnow_naive,
        nullable=False,
    )

    @property
    def status_label(self) -> str:
        return self.STATUS_LABELS.get(self.status or "", self.status or "")


# --- Central de Plugins (SCRUM-222, lote 1): domínio e persistência ---
#
# Catálogo interno de integrações homologadas pela LogCompleta.
# Não representa provider, OAuth, webhook nem material secreto.
#
# Códigos persistidos em português, no mesmo padrão de estado de
# ContaMonetizacaoCheckoutIntencao / ContaVinculoOrganizacional.
# Equivalência com os nomes do card (não são valores de coluna):
#   disponivel = available
#   aguardando_configuracao = pending_configuration
#   conectado = connected
#   requer_atencao = attention_required
#   desabilitado = disabled
#   bloqueado = blocked
#   desconectado = disconnected
#
# AuditoriaGerencial permanece trilha de decisão do Cleiton. A Central
# grava PluginEventoCentral, sem contexto_json e sem payload externo.
# Franquia não é proprietária de conexão: não há franquia_id nestas tabelas.


class Plugin(db.Model):
    """Integração homologada pela LogCompleta. Não é marketplace nem código de terceiro.

    suporta_titularidade_* declara o que o serviço pode criar. O banco não
    cruza essa declaração com plugin_conexao.titularidade.
    """

    __tablename__ = "plugin"

    STATUS_RASCUNHO = "rascunho"
    STATUS_DISPONIVEL = "disponivel"
    STATUS_DESABILITADO = "desabilitado"
    STATUS_BLOQUEADO = "bloqueado"
    STATUSES = (
        STATUS_RASCUNHO,
        STATUS_DISPONIVEL,
        STATUS_DESABILITADO,
        STATUS_BLOQUEADO,
    )
    _SQL_STATUS = "status IN ({})".format(", ".join(f"'{v}'" for v in STATUSES))

    __table_args__ = (
        db.UniqueConstraint("slug", name="uq_plugin_slug"),
        db.CheckConstraint(_SQL_STATUS, name="ck_plugin_status"),
        db.CheckConstraint(
            "(suporta_titularidade_pessoal OR suporta_titularidade_corporativa)",
            name="ck_plugin_titularidade_suportada",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), nullable=False)
    nome = db.Column(db.String(255), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), nullable=False, default=STATUS_RASCUNHO, index=True)
    suporta_titularidade_pessoal = db.Column(db.Boolean, nullable=False, default=False)
    suporta_titularidade_corporativa = db.Column(db.Boolean, nullable=False, default=False)
    # Chave estável do adapter futuro. Não é módulo importável nem contrato JSON.
    adapter_key = db.Column(db.String(80), nullable=False, index=True)
    criado_por_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)

    criado_por = db.relationship(
        "User",
        foreign_keys=[criado_por_user_id],
        backref=db.backref("plugins_catalogo_criados", lazy="dynamic"),
    )

    def aceita_titularidade(self, titularidade: str) -> bool:
        if titularidade == PluginConexao.TITULARIDADE_PESSOAL:
            return bool(self.suporta_titularidade_pessoal)
        if titularidade == PluginConexao.TITULARIDADE_CORPORATIVA:
            return bool(self.suporta_titularidade_corporativa)
        return False


class PluginCapability(db.Model):
    """
    Capability declarada por um plugin homologado.

    politica_maxima é o teto da LogCompleta. Não é a concessão do provedor
    nem a permissão efetiva do usuário. Não há taxonomia universal de API.
    """

    __tablename__ = "plugin_capability"

    STATUS_DISPONIVEL = "disponivel"
    STATUS_DESABILITADO = "desabilitado"
    STATUS_BLOQUEADO = "bloqueado"
    STATUSES = (STATUS_DISPONIVEL, STATUS_DESABILITADO, STATUS_BLOQUEADO)

    POLITICA_NEGADA = "negada"
    POLITICA_PERMITIDA = "permitida"
    POLITICAS = (POLITICA_NEGADA, POLITICA_PERMITIDA)

    NATUREZA_ATIVA = "ativa"
    NATUREZA_PASSIVA = "passiva"
    NATUREZAS = (NATUREZA_ATIVA, NATUREZA_PASSIVA)

    _SQL_STATUS = "status IN ({})".format(", ".join(f"'{v}'" for v in STATUSES))
    _SQL_POLITICA = "politica_maxima IN ({})".format(", ".join(f"'{v}'" for v in POLITICAS))
    _SQL_NATUREZA = "natureza IN ({})".format(", ".join(f"'{v}'" for v in NATUREZAS))

    __table_args__ = (
        db.UniqueConstraint("plugin_id", "chave", name="uq_plugin_capability_plugin_chave"),
        db.CheckConstraint(_SQL_STATUS, name="ck_plugin_capability_status"),
        db.CheckConstraint(_SQL_POLITICA, name="ck_plugin_capability_politica_maxima"),
        db.CheckConstraint(_SQL_NATUREZA, name="ck_plugin_capability_natureza"),
    )

    id = db.Column(db.Integer, primary_key=True)
    plugin_id = db.Column(db.Integer, db.ForeignKey("plugin.id"), nullable=False, index=True)
    chave = db.Column(db.String(80), nullable=False)
    nome = db.Column(db.String(255), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(30), nullable=False, default=STATUS_DISPONIVEL, index=True)
    politica_maxima = db.Column(db.String(20), nullable=False)
    natureza = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)

    plugin = db.relationship(
        "Plugin",
        backref=db.backref("capabilities", lazy="dynamic"),
    )


class PluginConexao(db.Model):
    """
    Conexão de um usuário com um plugin homologado.

    Titularidade pessoal: proprietário é o User (conta_id nulo).
    Titularidade corporativa: proprietário é a Conta; user_id continua sendo
    o titular do slot (no máximo uma conexão que ocupa slot por plugin).

    usuario_originador_id é o usuário que criou esta conexão. O serviço o
    preenche na criação e não o reescreve. Pode ser outro User que não o
    titular do slot. Não é o executor de ações posteriores: cada
    PluginEventoCentral guarda o originador daquele evento.

    O banco garante a forma da linha (pessoal sem conta_id, corporativa com
    conta_id). A compatibilidade com Plugin.suporta_titularidade_* é regra
    do serviço. Escrita direta fora do domínio não é API suportada.

    Estados que ocupam o slot único: todos, exceto desconectado.
    Credencial inválida não apaga a linha nem força desconectado.
    """

    __tablename__ = "plugin_conexao"

    TITULARIDADE_PESSOAL = "pessoal"
    TITULARIDADE_CORPORATIVA = "corporativa"
    TITULARIDADES = (TITULARIDADE_PESSOAL, TITULARIDADE_CORPORATIVA)

    ESTADO_DISPONIVEL = "disponivel"
    ESTADO_AGUARDANDO_CONFIGURACAO = "aguardando_configuracao"
    ESTADO_CONECTADO = "conectado"
    ESTADO_REQUER_ATENCAO = "requer_atencao"
    ESTADO_DESABILITADO = "desabilitado"
    ESTADO_BLOQUEADO = "bloqueado"
    ESTADO_DESCONECTADO = "desconectado"
    ESTADOS_QUE_OCUPAM_SLOT = (
        ESTADO_DISPONIVEL,
        ESTADO_AGUARDANDO_CONFIGURACAO,
        ESTADO_CONECTADO,
        ESTADO_REQUER_ATENCAO,
        ESTADO_DESABILITADO,
        ESTADO_BLOQUEADO,
    )
    ESTADOS = ESTADOS_QUE_OCUPAM_SLOT + (ESTADO_DESCONECTADO,)
    _SQL_ESTADOS = "estado IN ({})".format(", ".join(f"'{v}'" for v in ESTADOS))
    _SQL_OCUPA_SLOT = "estado IN ({})".format(
        ", ".join(f"'{v}'" for v in ESTADOS_QUE_OCUPAM_SLOT)
    )
    _SQL_TITULARIDADE = "titularidade IN ({})".format(
        ", ".join(f"'{v}'" for v in TITULARIDADES)
    )

    __table_args__ = (
        db.CheckConstraint(_SQL_ESTADOS, name="ck_plugin_conexao_estado"),
        db.CheckConstraint(_SQL_TITULARIDADE, name="ck_plugin_conexao_titularidade"),
        db.CheckConstraint(
            "("
            "(titularidade = 'pessoal' AND conta_id IS NULL) "
            "OR (titularidade = 'corporativa' AND conta_id IS NOT NULL)"
            ")",
            name="ck_plugin_conexao_titularidade_conta",
        ),
        db.CheckConstraint(
            "(revogado_em IS NULL OR estado = 'desconectado')",
            name="ck_plugin_conexao_revogacao",
        ),
        db.Index(
            "uq_plugin_conexao_user_plugin_ativa",
            "user_id",
            "plugin_id",
            unique=True,
            postgresql_where=db.text(_SQL_OCUPA_SLOT),
            sqlite_where=db.text(_SQL_OCUPA_SLOT),
        ),
        db.Index("ix_plugin_conexao_user_estado", "user_id", "estado"),
    )

    id = db.Column(db.Integer, primary_key=True)
    plugin_id = db.Column(db.Integer, db.ForeignKey("plugin.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=True, index=True)
    # Criador da conexão. Não é o originador das ações posteriores.
    usuario_originador_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    titularidade = db.Column(db.String(20), nullable=False)
    # Identificador externo não secreto (conta no provider, resource id opaco).
    identificador_externo = db.Column(db.String(120), nullable=True)
    estado = db.Column(
        db.String(40),
        nullable=False,
        default=ESTADO_AGUARDANDO_CONFIGURACAO,
        index=True,
    )
    # Código técnico curto, validado pelo serviço no limite desta coluna.
    diagnostico_codigo = db.Column(db.String(80), nullable=True)
    # Texto de apresentação. O serviço recusa payload, JSON e segredo.
    diagnostico_resumo = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)
    estado_alterado_em = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    desconectado_em = db.Column(db.DateTime, nullable=True)
    revogado_em = db.Column(db.DateTime, nullable=True)

    plugin = db.relationship(
        "Plugin",
        backref=db.backref("conexoes", lazy="dynamic"),
    )
    user = db.relationship(
        "User",
        foreign_keys=[user_id],
        backref=db.backref("plugin_conexoes", lazy="dynamic"),
    )
    conta = db.relationship(
        "Conta",
        foreign_keys=[conta_id],
        backref=db.backref("plugin_conexoes", lazy="dynamic"),
    )
    usuario_originador = db.relationship(
        "User",
        foreign_keys=[usuario_originador_id],
        backref=db.backref("plugin_conexoes_originadas", lazy="dynamic"),
    )

    def ocupa_slot(self) -> bool:
        return self.estado in self.ESTADOS_QUE_OCUPAM_SLOT


class PluginCredencialReferencia(db.Model):
    """
    Separação entre a conexão e o material secreto.

    Esta tabela não tem coluna de token, API key ou segredo. cofre_referencia,
    quando existir, é só um identificador opaco de cofre futuro — nunca o segredo.
    Estado invalida não desconecta PluginConexao.
    """

    __tablename__ = "plugin_credencial_referencia"

    ESTADO_NAO_PROVISIONADA = "nao_provisionada"
    ESTADO_REFERENCIADA = "referenciada"
    ESTADO_INVALIDA = "invalida"
    ESTADO_REVOGADA = "revogada"
    ESTADOS = (
        ESTADO_NAO_PROVISIONADA,
        ESTADO_REFERENCIADA,
        ESTADO_INVALIDA,
        ESTADO_REVOGADA,
    )
    _SQL_ESTADOS = "estado IN ({})".format(", ".join(f"'{v}'" for v in ESTADOS))

    __table_args__ = (
        db.UniqueConstraint("conexao_id", name="uq_plugin_credencial_conexao"),
        db.CheckConstraint(_SQL_ESTADOS, name="ck_plugin_credencial_estado"),
        db.CheckConstraint(
            "("
            "(estado = 'referenciada' AND cofre_referencia IS NOT NULL) "
            "OR (estado <> 'referenciada')"
            ")",
            name="ck_plugin_credencial_referencia_presente",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    conexao_id = db.Column(
        db.Integer, db.ForeignKey("plugin_conexao.id"), nullable=False, index=True
    )
    estado = db.Column(db.String(30), nullable=False, default=ESTADO_NAO_PROVISIONADA)
    cofre_referencia = db.Column(db.String(80), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)

    conexao = db.relationship(
        "PluginConexao",
        backref=db.backref("credencial_referencia", uselist=False),
    )


class PluginRestricaoUsuario(db.Model):
    """
    Restrição adicional explícita do usuário sobre uma capability.

    A ausência de linha não é permissão administrativa nem concessão do provedor.
    Só restringe. A autorização efetiva está em avaliar_autorizacao_plugin.
    """

    __tablename__ = "plugin_restricao_usuario"

    EFEITO_BLOQUEADA_PELO_USUARIO = "bloqueada_pelo_usuario"
    EFEITOS = (EFEITO_BLOQUEADA_PELO_USUARIO,)
    _SQL_EFEITO = "efeito IN ({})".format(", ".join(f"'{v}'" for v in EFEITOS))

    __table_args__ = (
        db.UniqueConstraint(
            "conexao_id",
            "capability_id",
            name="uq_plugin_restricao_conexao_capability",
        ),
        db.CheckConstraint(_SQL_EFEITO, name="ck_plugin_restricao_efeito"),
    )

    id = db.Column(db.Integer, primary_key=True)
    conexao_id = db.Column(
        db.Integer, db.ForeignKey("plugin_conexao.id"), nullable=False, index=True
    )
    capability_id = db.Column(
        db.Integer, db.ForeignKey("plugin_capability.id"), nullable=False, index=True
    )
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    efeito = db.Column(db.String(40), nullable=False, default=EFEITO_BLOQUEADA_PELO_USUARIO)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)

    conexao = db.relationship(
        "PluginConexao",
        backref=db.backref("restricoes_usuario", lazy="dynamic"),
    )
    capability = db.relationship(
        "PluginCapability",
        backref=db.backref("restricoes_usuario", lazy="dynamic"),
    )
    user = db.relationship(
        "User",
        foreign_keys=[user_id],
        backref=db.backref("plugin_restricoes", lazy="dynamic"),
    )


class PluginConcessaoProvedor(db.Model):
    """
    Resultado já conhecido da capability concedida pelo provider.

    Linha só existe quando o resultado já é conhecido. Ausência não é concessão.
    Este modelo não consulta o provider. A autorização efetiva trata ausência
    como negação.
    """

    __tablename__ = "plugin_concessao_provedor"

    RESULTADO_CONCEDIDA = "concedida"
    RESULTADO_NEGADA = "negada"
    RESULTADOS = (RESULTADO_CONCEDIDA, RESULTADO_NEGADA)
    _SQL_RESULTADO = "resultado IN ({})".format(", ".join(f"'{v}'" for v in RESULTADOS))

    __table_args__ = (
        db.UniqueConstraint(
            "conexao_id",
            "capability_id",
            name="uq_plugin_concessao_conexao_capability",
        ),
        db.CheckConstraint(_SQL_RESULTADO, name="ck_plugin_concessao_resultado"),
    )

    id = db.Column(db.Integer, primary_key=True)
    conexao_id = db.Column(
        db.Integer, db.ForeignKey("plugin_conexao.id"), nullable=False, index=True
    )
    capability_id = db.Column(
        db.Integer, db.ForeignKey("plugin_capability.id"), nullable=False, index=True
    )
    resultado = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow_naive, onupdate=utcnow_naive, nullable=False)

    conexao = db.relationship(
        "PluginConexao",
        backref=db.backref("concessoes_provedor", lazy="dynamic"),
    )
    capability = db.relationship(
        "PluginCapability",
        backref=db.backref("concessoes_provedor", lazy="dynamic"),
    )


class PluginEventoCentral(db.Model):
    """
    Trilha append-only da Central de Plugins pelo domínio.

    O serviço da Central só insere. Não há operação de edição nem de exclusão.
    Isso não é garantia física de banco: não há trigger, e o ORM ainda aceita
    UPDATE e DELETE se alguém escrever fora do serviço.

    usuario_originador_id neste registro é quem originou este evento. Não
    copia, sozinho, o criador gravado em PluginConexao.

    Não reutiliza AuditoriaGerencial: aquela tabela é decisão do Cleiton e
    aceita contexto_json livre. Aqui o tipo é fechado e não há payload.

    Equivalência com o card:
      conexao_criada = connection_created
      conexao_estado_alterado = connection_state_changed
      restricao_usuario_alterada = user_restriction_changed
      bloqueado = blocked
      desabilitado = disabled
      desconectado = disconnected
      revogado = revoked
      autorizacao_negada = authorization_denied

    detalhe_codigo da negação é um motivo fechado, sem texto livre nem segredo.
    """

    __tablename__ = "plugin_evento_central"

    TIPO_CONEXAO_CRIADA = "conexao_criada"
    TIPO_CONEXAO_ESTADO_ALTERADO = "conexao_estado_alterado"
    TIPO_RESTRICAO_USUARIO_ALTERADA = "restricao_usuario_alterada"
    TIPO_BLOQUEADO = "bloqueado"
    TIPO_DESABILITADO = "desabilitado"
    TIPO_DESCONECTADO = "desconectado"
    TIPO_REVOGADO = "revogado"
    TIPO_AUTORIZACAO_NEGADA = "autorizacao_negada"
    TIPOS = (
        TIPO_CONEXAO_CRIADA,
        TIPO_CONEXAO_ESTADO_ALTERADO,
        TIPO_RESTRICAO_USUARIO_ALTERADA,
        TIPO_BLOQUEADO,
        TIPO_DESABILITADO,
        TIPO_DESCONECTADO,
        TIPO_REVOGADO,
        TIPO_AUTORIZACAO_NEGADA,
    )

    DETALHE_CRIADA = "criada"
    DETALHE_ESTADO_ALTERADO = "estado_alterado"
    DETALHE_RESTRICAO_REGISTRADA = "restricao_registrada"
    DETALHE_RESTRICAO_REMOVIDA = "restricao_removida"

    # Motivos seguros da autorização efetiva. Não são mensagem livre.
    MOTIVO_PLUGIN_INEXISTENTE = "plugin_inexistente"
    MOTIVO_PLUGIN_INDISPONIVEL = "plugin_indisponivel"
    MOTIVO_CONEXAO_INEXISTENTE = "conexao_inexistente"
    MOTIVO_CONEXAO_NAO_PERTENCE = "conexao_nao_pertence"
    MOTIVO_CONEXAO_NAO_OPERACIONAL = "conexao_nao_operacional"
    MOTIVO_CAPABILITY_INEXISTENTE = "capability_inexistente"
    MOTIVO_CAPABILITY_INDISPONIVEL = "capability_indisponivel"
    MOTIVO_BLOQUEADA_PELA_PLATAFORMA = "bloqueada_pela_plataforma"
    MOTIVO_BLOQUEADA_PELO_USUARIO = "bloqueada_pelo_usuario"
    MOTIVO_CONCESSAO_PROVEDOR_NEGADA = "concessao_provedor_negada"
    MOTIVO_CONCESSAO_PROVEDOR_AUSENTE = "concessao_provedor_ausente"
    MOTIVO_CONTA_INCOMPATIVEL = "conta_incompativel"
    MOTIVO_CONTEXTO_INVALIDO = "contexto_invalido"
    MOTIVO_AUTORIZADA = "autorizada"
    MOTIVOS_NEGACAO = (
        MOTIVO_PLUGIN_INEXISTENTE,
        MOTIVO_PLUGIN_INDISPONIVEL,
        MOTIVO_CONEXAO_INEXISTENTE,
        MOTIVO_CONEXAO_NAO_PERTENCE,
        MOTIVO_CONEXAO_NAO_OPERACIONAL,
        MOTIVO_CAPABILITY_INEXISTENTE,
        MOTIVO_CAPABILITY_INDISPONIVEL,
        MOTIVO_BLOQUEADA_PELA_PLATAFORMA,
        MOTIVO_BLOQUEADA_PELO_USUARIO,
        MOTIVO_CONCESSAO_PROVEDOR_NEGADA,
        MOTIVO_CONCESSAO_PROVEDOR_AUSENTE,
        MOTIVO_CONTA_INCOMPATIVEL,
        MOTIVO_CONTEXTO_INVALIDO,
    )
    MOTIVOS = MOTIVOS_NEGACAO + (MOTIVO_AUTORIZADA,)

    DETALHES = (
        DETALHE_CRIADA,
        DETALHE_ESTADO_ALTERADO,
        DETALHE_RESTRICAO_REGISTRADA,
        DETALHE_RESTRICAO_REMOVIDA,
    ) + MOTIVOS_NEGACAO

    _SQL_TIPOS = "tipo_evento IN ({})".format(", ".join(f"'{v}'" for v in TIPOS))
    _SQL_DETALHES = "detalhe_codigo IS NULL OR detalhe_codigo IN ({})".format(
        ", ".join(f"'{v}'" for v in DETALHES)
    )

    __table_args__ = (
        db.CheckConstraint(_SQL_TIPOS, name="ck_plugin_evento_tipo"),
        db.CheckConstraint(_SQL_DETALHES, name="ck_plugin_evento_detalhe"),
        db.Index("ix_plugin_evento_conexao_created", "conexao_id", "created_at"),
        db.Index("ix_plugin_evento_user_created", "user_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    tipo_evento = db.Column(db.String(40), nullable=False, index=True)
    detalhe_codigo = db.Column(db.String(40), nullable=True)
    conexao_id = db.Column(
        db.Integer, db.ForeignKey("plugin_conexao.id"), nullable=False, index=True
    )
    plugin_id = db.Column(db.Integer, db.ForeignKey("plugin.id"), nullable=False, index=True)
    capability_id = db.Column(
        db.Integer, db.ForeignKey("plugin_capability.id"), nullable=True, index=True
    )
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    # Originador deste evento. Execuções futuras gravam o seu próprio.
    usuario_originador_id = db.Column(
        db.Integer, db.ForeignKey("user.id"), nullable=False, index=True
    )
    conta_id = db.Column(db.Integer, db.ForeignKey("conta.id"), nullable=True, index=True)
    estado_anterior = db.Column(db.String(40), nullable=True)
    estado_novo = db.Column(db.String(40), nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False, index=True)

    conexao = db.relationship(
        "PluginConexao",
        backref=db.backref("eventos", lazy="dynamic"),
    )
    plugin = db.relationship("Plugin", foreign_keys=[plugin_id])
    capability = db.relationship("PluginCapability", foreign_keys=[capability_id])
    user = db.relationship("User", foreign_keys=[user_id])
    usuario_originador = db.relationship("User", foreign_keys=[usuario_originador_id])
    conta = db.relationship("Conta", foreign_keys=[conta_id])


class PluginAuditoriaAdministrativa(db.Model):
    """Trilha append-only da governança do catálogo pelo ADM da LogCompleta.

    PluginEventoCentral exige conexao_id e descreve a vida operacional da
    conexão (criação, estado, restrição, bloqueio da conexão, negação).
    Mudar metadado, status do catálogo, titularidade ou política de
    capability não é evento de conexão, então não entra naquela trilha.

    AuditoriaGerencial permanece decisão do Cleiton e aceita contexto_json
    livre. Aqui entidade, alteração e campo são fechados. valor_anterior e
    valor_novo são o par já validado pelo domínio. Não há JSON, segredo
    nem payload de provider.

    O serviço administrativo só insere. Não há edição nem exclusão nesta
    API. Isso não é trava física de banco.
    """

    __tablename__ = "plugin_auditoria_administrativa"

    ENTIDADE_PLUGIN = "plugin"
    ENTIDADE_CAPABILITY = "capability"
    ENTIDADES = (ENTIDADE_PLUGIN, ENTIDADE_CAPABILITY)

    ALTERACAO_PLUGIN_CRIADO = "plugin_criado"
    ALTERACAO_PLUGIN_METADADOS = "plugin_metadados"
    ALTERACAO_PLUGIN_STATUS = "plugin_status"
    ALTERACAO_PLUGIN_TITULARIDADE = "plugin_titularidade"
    ALTERACAO_CAPABILITY_CRIADA = "capability_criada"
    ALTERACAO_CAPABILITY_POLITICA = "capability_politica"
    ALTERACAO_CAPABILITY_STATUS = "capability_status"
    ALTERACOES = (
        ALTERACAO_PLUGIN_CRIADO,
        ALTERACAO_PLUGIN_METADADOS,
        ALTERACAO_PLUGIN_STATUS,
        ALTERACAO_PLUGIN_TITULARIDADE,
        ALTERACAO_CAPABILITY_CRIADA,
        ALTERACAO_CAPABILITY_POLITICA,
        ALTERACAO_CAPABILITY_STATUS,
    )

    CAMPOS = (
        "slug",
        "nome",
        "descricao",
        "adapter_key",
        "status",
        "titularidade",
        "chave",
        "natureza",
        "politica_maxima",
    )

    _SQL_ENTIDADE = "entidade IN ({})".format(", ".join(f"'{v}'" for v in ENTIDADES))
    _SQL_ALTERACAO = "alteracao IN ({})".format(", ".join(f"'{v}'" for v in ALTERACOES))
    _SQL_CAMPO = "campo IN ({})".format(", ".join(f"'{v}'" for v in CAMPOS))

    __table_args__ = (
        db.CheckConstraint(_SQL_ENTIDADE, name="ck_plugin_auditoria_admin_entidade"),
        db.CheckConstraint(_SQL_ALTERACAO, name="ck_plugin_auditoria_admin_alteracao"),
        db.CheckConstraint(_SQL_CAMPO, name="ck_plugin_auditoria_admin_campo"),
        db.CheckConstraint(
            "("
            "(entidade = 'plugin' AND capability_id IS NULL) "
            "OR (entidade = 'capability' AND capability_id IS NOT NULL)"
            ")",
            name="ck_plugin_auditoria_admin_capability",
        ),
        db.Index("ix_plugin_auditoria_admin_plugin_created", "plugin_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    entidade = db.Column(db.String(20), nullable=False)
    alteracao = db.Column(db.String(40), nullable=False)
    campo = db.Column(db.String(40), nullable=False)
    valor_anterior = db.Column(db.Text, nullable=True)
    valor_novo = db.Column(db.Text, nullable=True)
    plugin_id = db.Column(db.Integer, db.ForeignKey("plugin.id"), nullable=False, index=True)
    capability_id = db.Column(
        db.Integer, db.ForeignKey("plugin_capability.id"), nullable=True, index=True
    )
    ator_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False, index=True)

    plugin = db.relationship(
        "Plugin",
        backref=db.backref("auditorias_administrativas", lazy="dynamic"),
    )
    capability = db.relationship("PluginCapability", foreign_keys=[capability_id])
    ator = db.relationship(
        "User",
        foreign_keys=[ator_user_id],
        backref=db.backref("plugin_auditorias_administrativas", lazy="dynamic"),
    )


class IdentidadeCanalExterna(db.Model):
    """
    Pessoa externa em aquisição, antes e depois de existir User.

    Não é sessão Flask, não é PluginConexao e não é fingerprint de Growth.
    O mesmo provedor + sujeito externo só pode ter uma linha não revogada.
    Essa linha tem no máximo um user_id, então a identidade ativa não fica
    vinculada a dois Users ao mesmo tempo.

    contexto_destino é dado técnico opaco do provider. Não entra na unicidade
    deste lote e não é telefone, token nem payload.

    interacoes_uteis é a quota guest desta identidade. Não vem de Growth.
    """

    __tablename__ = "identidade_canal_externa"

    ESTADO_GUEST = "guest"
    ESTADO_CADASTRO_EM_ANDAMENTO = "cadastro_em_andamento"
    ESTADO_VINCULADA = "vinculada"
    ESTADO_REVOGADA = "revogada"
    ESTADO_BLOQUEADA = "bloqueada"
    ESTADOS = (
        ESTADO_GUEST,
        ESTADO_CADASTRO_EM_ANDAMENTO,
        ESTADO_VINCULADA,
        ESTADO_REVOGADA,
        ESTADO_BLOQUEADA,
    )
    ESTADOS_OPERAVEIS_GUEST = (
        ESTADO_GUEST,
        ESTADO_CADASTRO_EM_ANDAMENTO,
    )

    _SQL_ESTADO = "estado IN ({})".format(", ".join(f"'{v}'" for v in ESTADOS))
    _SQL_IDENTIDADE_ATIVA = "estado != 'revogada'"
    _SQL_COERENCIA = (
        "("
        "(estado = 'guest' AND user_id IS NULL AND vinculada_em IS NULL AND revogada_em IS NULL)"
        " OR (estado = 'cadastro_em_andamento' AND user_id IS NULL "
        "AND vinculada_em IS NULL AND revogada_em IS NULL)"
        " OR (estado = 'bloqueada' AND revogada_em IS NULL AND ("
        "(user_id IS NULL AND vinculada_em IS NULL)"
        " OR (user_id IS NOT NULL AND vinculada_em IS NOT NULL)"
        "))"
        " OR (estado = 'vinculada' AND user_id IS NOT NULL "
        "AND vinculada_em IS NOT NULL AND revogada_em IS NULL)"
        " OR (estado = 'revogada' AND revogada_em IS NOT NULL AND ("
        "(user_id IS NULL AND vinculada_em IS NULL)"
        " OR (user_id IS NOT NULL AND vinculada_em IS NOT NULL)"
        "))"
        ")"
    )
    _SQL_INTERACOES = "interacoes_uteis >= 0"
    _SQL_PROVEDOR = "length(provedor) > 0"
    _SQL_SUJEITO = "length(sujeito_externo) > 0"

    __table_args__ = (
        db.CheckConstraint(_SQL_ESTADO, name="ck_identidade_canal_estado"),
        db.CheckConstraint(_SQL_COERENCIA, name="ck_identidade_canal_coerencia"),
        db.CheckConstraint(_SQL_INTERACOES, name="ck_identidade_canal_interacoes_uteis"),
        db.CheckConstraint(_SQL_PROVEDOR, name="ck_identidade_canal_provedor"),
        db.CheckConstraint(_SQL_SUJEITO, name="ck_identidade_canal_sujeito"),
        db.Index(
            "uq_identidade_canal_provider_subject_ativa",
            "provedor",
            "sujeito_externo",
            unique=True,
            postgresql_where=db.text(_SQL_IDENTIDADE_ATIVA),
            sqlite_where=db.text(_SQL_IDENTIDADE_ATIVA),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    provedor = db.Column(db.String(32), nullable=False)
    sujeito_externo = db.Column(db.String(120), nullable=False)
    contexto_destino = db.Column(db.String(120), nullable=True)
    estado = db.Column(db.String(40), nullable=False, default=ESTADO_GUEST, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    interacoes_uteis = db.Column(db.Integer, nullable=False, default=0)
    criada_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    atualizada_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    vinculada_em = db.Column(db.DateTime, nullable=True)
    revogada_em = db.Column(db.DateTime, nullable=True)

    user = db.relationship(
        "User",
        foreign_keys=[user_id],
        backref=db.backref("identidades_canal_externas", lazy="dynamic"),
    )


class InteracaoGuestCanal(db.Model):
    """
    Ledger de idempotência da quota guest.

    A mesma identidade não registra o mesmo evento_externo_id duas vezes.
    Só o resultado interacao_guest_concluida_com_sucesso incrementa a quota,
    e esse incremento acontece na mesma unidade que insere esta linha.
    """

    __tablename__ = "interacao_guest_canal"

    RESULTADO_SUCESSO = "interacao_guest_concluida_com_sucesso"
    RESULTADOS_SEM_CONSUMO = (
        "mensagem_cadastro",
        "coleta_nome",
        "coleta_email",
        "coleta_cargo",
        "resposta_entrevista",
        "aceite_termos",
        "cta",
        "mensagem_duplicada",
        "falha_tecnica",
        "solicitacao_rejeitada",
    )
    RESULTADOS = (RESULTADO_SUCESSO,) + RESULTADOS_SEM_CONSUMO
    _SQL_RESULTADO = "resultado IN ({})".format(", ".join(f"'{v}'" for v in RESULTADOS))
    _SQL_EVENTO = "length(evento_externo_id) > 0"

    __table_args__ = (
        db.UniqueConstraint(
            "identidade_id",
            "evento_externo_id",
            name="uq_interacao_guest_identidade_evento",
        ),
        db.CheckConstraint(_SQL_RESULTADO, name="ck_interacao_guest_resultado"),
        db.CheckConstraint(_SQL_EVENTO, name="ck_interacao_guest_evento"),
    )

    id = db.Column(db.Integer, primary_key=True)
    identidade_id = db.Column(
        db.Integer,
        db.ForeignKey("identidade_canal_externa.id"),
        nullable=False,
        index=True,
    )
    evento_externo_id = db.Column(db.String(120), nullable=False)
    resultado = db.Column(db.String(64), nullable=False)
    registrada_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)

    identidade = db.relationship(
        "IdentidadeCanalExterna",
        backref=db.backref("interacoes_guest", lazy="dynamic"),
    )


class OnboardingCanal(db.Model):
    """
    Jornada conversacional durável de uma identidade externa.

    Os dados pendentes cabem na allowlist das colunas. Não há senha, token,
    telefone nem payload bruto. As respostas da entrevista são só chaves já
    validadas pela definição compartilhada; OnboardingRespostaDeclarada
    continua exigindo User e não é preenchida aqui.

    Uma identidade tem no máximo uma jornada aberta. Expirar ou cancelar
    não apaga a identidade guest.
    """

    __tablename__ = "onboarding_canal"

    ETAPA_CONVITE = "convite_cadastro"
    ETAPA_NOME = "coletando_nome"
    ETAPA_EMAIL = "coletando_email"
    ETAPA_CARGO = "coletando_cargo"
    ETAPA_ENTREVISTA = "coletando_entrevista"
    ETAPA_TERMOS = "aguardando_termos"
    ETAPA_SENHA = "aguardando_senha"
    ETAPA_CONCLUIDO = "concluido"
    ETAPA_CANCELADO = "cancelado"
    ETAPA_EXPIRADO = "expirado"
    ETAPAS_ABERTAS = (
        ETAPA_CONVITE,
        ETAPA_NOME,
        ETAPA_EMAIL,
        ETAPA_CARGO,
        ETAPA_ENTREVISTA,
        ETAPA_TERMOS,
        ETAPA_SENHA,
    )
    ETAPAS_TERMINAIS = (
        ETAPA_CONCLUIDO,
        ETAPA_CANCELADO,
        ETAPA_EXPIRADO,
    )
    ETAPAS = ETAPAS_ABERTAS + ETAPAS_TERMINAIS

    _SQL_ETAPA = "etapa IN ({})".format(", ".join(f"'{v}'" for v in ETAPAS))
    _SQL_JORNADA_ABERTA = "etapa IN ({})".format(
        ", ".join(f"'{v}'" for v in ETAPAS_ABERTAS)
    )
    _SQL_TERMINAIS = (
        "("
        "(etapa = 'expirado' AND expirada_em IS NOT NULL "
        "AND cancelada_em IS NULL AND concluida_em IS NULL)"
        " OR (etapa = 'cancelado' AND cancelada_em IS NOT NULL "
        "AND expirada_em IS NULL AND concluida_em IS NULL)"
        " OR (etapa = 'concluido' AND concluida_em IS NOT NULL "
        "AND expirada_em IS NULL AND cancelada_em IS NULL)"
        " OR (etapa IN ("
        "'convite_cadastro', 'coletando_nome', 'coletando_email', 'coletando_cargo', "
        "'coletando_entrevista', 'aguardando_termos', 'aguardando_senha'"
        ") AND expirada_em IS NULL AND cancelada_em IS NULL AND concluida_em IS NULL)"
        ")"
    )
    _SQL_TERMOS = (
        "("
        "(termos_apresentados_em IS NULL AND termos_referencia IS NULL "
        "AND termos_aceitos_em IS NULL)"
        " OR (termos_apresentados_em IS NOT NULL AND termos_referencia IS NOT NULL "
        "AND termos_aceitos_em IS NULL)"
        " OR (termos_apresentados_em IS NOT NULL AND termos_referencia IS NOT NULL "
        "AND termos_aceitos_em IS NOT NULL AND termos_aceitos_em >= termos_apresentados_em)"
        ")"
    )
    _SQL_EMAIL = (
        "email_normalizado IS NULL OR ("
        "length(email_normalizado) > 0 "
        "AND email_normalizado = lower(email_normalizado)"
        ")"
    )
    _SQL_PAUSAS = "pausas_interacao_guest >= 0"
    _SQL_TEXTOS = (
        "(nome IS NULL OR length(nome) > 0)"
        " AND (job_role IS NULL OR length(job_role) > 0)"
        " AND (termos_referencia IS NULL OR length(termos_referencia) > 0)"
        " AND (origem_aquisicao IS NULL OR length(origem_aquisicao) > 0)"
        " AND (correlation_id IS NULL OR length(correlation_id) > 0)"
        " AND length(taxonomia_versao) > 0"
    )

    __table_args__ = (
        db.CheckConstraint(_SQL_ETAPA, name="ck_onboarding_canal_etapa"),
        db.CheckConstraint(_SQL_TERMINAIS, name="ck_onboarding_canal_terminais"),
        db.CheckConstraint(_SQL_TERMOS, name="ck_onboarding_canal_termos"),
        db.CheckConstraint(_SQL_EMAIL, name="ck_onboarding_canal_email"),
        db.CheckConstraint(_SQL_PAUSAS, name="ck_onboarding_canal_pausas"),
        db.CheckConstraint(_SQL_TEXTOS, name="ck_onboarding_canal_textos"),
        db.Index(
            "uq_onboarding_canal_jornada_aberta",
            "identidade_id",
            unique=True,
            postgresql_where=db.text(_SQL_JORNADA_ABERTA),
            sqlite_where=db.text(_SQL_JORNADA_ABERTA),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    identidade_id = db.Column(
        db.Integer,
        db.ForeignKey("identidade_canal_externa.id"),
        nullable=False,
        index=True,
    )
    etapa = db.Column(db.String(40), nullable=False)
    nome = db.Column(db.String(150), nullable=True)
    email_normalizado = db.Column(db.String(150), nullable=True)
    job_role = db.Column(db.String(100), nullable=True)
    respostas_entrevista_json = db.Column(db.Text, nullable=True)
    termos_apresentados_em = db.Column(db.DateTime, nullable=True)
    termos_referencia = db.Column(db.String(80), nullable=True)
    termos_aceitos_em = db.Column(db.DateTime, nullable=True)
    pausas_interacao_guest = db.Column(db.Integer, nullable=False, default=0)
    taxonomia_versao = db.Column(db.String(40), nullable=False)
    iniciada_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    atualizada_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    expira_em = db.Column(db.DateTime, nullable=False)
    expirada_em = db.Column(db.DateTime, nullable=True)
    cancelada_em = db.Column(db.DateTime, nullable=True)
    concluida_em = db.Column(db.DateTime, nullable=True)
    origem_aquisicao = db.Column(db.String(40), nullable=True)
    correlation_id = db.Column(db.String(64), nullable=True)

    identidade = db.relationship(
        "IdentidadeCanalExterna",
        backref=db.backref("onboardings_canal", lazy="dynamic"),
    )


class OnboardingCanalConclusao(db.Model):
    """
    Prova de uso único do link que conclui uma jornada de canal.

    O segredo fica só na URL. Aqui persiste o hash do token assinado e, nas
    emissões novas, o hash do alias curto. Não há senha, e-mail, nome nem telefone.
    """

    __tablename__ = "onboarding_canal_conclusao"

    FINALIDADE_DEFINIR_SENHA = "definir_senha"
    FINALIDADE_VINCULAR_CONTA = "vincular_conta"
    FINALIDADES = (FINALIDADE_DEFINIR_SENHA, FINALIDADE_VINCULAR_CONTA)

    ESTADO_EMITIDO = "emitido"
    ESTADO_CONSUMIDO = "consumido"
    ESTADO_REVOGADO = "revogado"
    ESTADOS = (ESTADO_EMITIDO, ESTADO_CONSUMIDO, ESTADO_REVOGADO)

    _SQL_FINALIDADE = "finalidade IN ({})".format(
        ", ".join(f"'{v}'" for v in FINALIDADES)
    )
    _SQL_ESTADO = "estado IN ({})".format(", ".join(f"'{v}'" for v in ESTADOS))
    _SQL_COERENCIA = (
        "("
        "(estado = 'emitido' AND consumido_em IS NULL)"
        " OR (estado = 'consumido' AND consumido_em IS NOT NULL)"
        " OR (estado = 'revogado' AND consumido_em IS NULL)"
        ")"
    )
    _SQL_HASH = "length(token_hash) = 64"
    _SQL_ALIAS = "alias_hash IS NULL OR length(alias_hash) = 64"
    _SQL_EMITIDO_ATIVO = "estado = 'emitido'"

    __table_args__ = (
        db.CheckConstraint(_SQL_FINALIDADE, name="ck_onboarding_canal_conclusao_finalidade"),
        db.CheckConstraint(_SQL_ESTADO, name="ck_onboarding_canal_conclusao_estado"),
        db.CheckConstraint(_SQL_COERENCIA, name="ck_onboarding_canal_conclusao_coerencia"),
        db.CheckConstraint(_SQL_HASH, name="ck_onboarding_canal_conclusao_hash"),
        db.CheckConstraint(_SQL_ALIAS, name="ck_onboarding_canal_conclusao_alias"),
        db.UniqueConstraint("alias_hash", name="uq_onboarding_canal_conclusao_alias_hash"),
        db.Index(
            "uq_onboarding_canal_conclusao_emitida",
            "onboarding_id",
            unique=True,
            postgresql_where=db.text(_SQL_EMITIDO_ATIVO),
            sqlite_where=db.text(_SQL_EMITIDO_ATIVO),
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    onboarding_id = db.Column(
        db.Integer,
        db.ForeignKey("onboarding_canal.id"),
        nullable=False,
        index=True,
    )
    finalidade = db.Column(db.String(40), nullable=False)
    token_hash = db.Column(db.String(64), nullable=False, unique=True)
    alias_hash = db.Column(db.String(64), nullable=True)
    estado = db.Column(db.String(20), nullable=False, default=ESTADO_EMITIDO, index=True)
    emitido_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    expira_em = db.Column(db.DateTime, nullable=False)
    consumido_em = db.Column(db.DateTime, nullable=True)
    atualizada_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)

    onboarding = db.relationship(
        "OnboardingCanal",
        backref=db.backref("conclusoes", lazy="dynamic"),
    )


class EventoCanalRecebido(db.Model):
    """
    Evento de entrada já autenticado e reduzido à allowlist.

    A unicidade é provider + evento_externo_id. Não há payload bruto, header,
    assinatura, segredo nem vínculo com User, onboarding ou identidade.
    O texto operacional, quando existe, fica em ConteudoTextualCanal.
    status_processamento sai de recebido só no processamento do canal.
    """

    __tablename__ = "evento_canal_recebido"

    PROVIDER_META_WHATSAPP = "meta_whatsapp"
    PROVIDERS = (PROVIDER_META_WHATSAPP,)

    TIPO_MENSAGEM_TEXTUAL = "mensagem_textual"
    TIPO_MIDIA = "midia"
    TIPO_STATUS_ENTREGA = "status_entrega"
    TIPO_DESCONHECIDO = "desconhecido"
    TIPOS = (
        TIPO_MENSAGEM_TEXTUAL,
        TIPO_MIDIA,
        TIPO_STATUS_ENTREGA,
        TIPO_DESCONHECIDO,
    )

    STATUS_RECEBIDO = "recebido"
    STATUS_PROCESSANDO = "processando"
    STATUS_ROTEADO = "roteado"
    STATUS_IGNORADO = "ignorado"
    STATUS_ERRO_SEGURO = "erro_seguro"
    STATUS_AGUARDANDO_SUPORTE_MIDIA = "aguardando_suporte_midia"
    STATUS_PROCESSAMENTO = (
        STATUS_RECEBIDO,
        STATUS_PROCESSANDO,
        STATUS_ROTEADO,
        STATUS_IGNORADO,
        STATUS_ERRO_SEGURO,
        STATUS_AGUARDANDO_SUPORTE_MIDIA,
    )

    DIAGNOSTICOS = (
        "mensagem_textual",
        "midia",
        "midia:audio",
        "midia:document",
        "midia:image",
        "midia:sticker",
        "midia:video",
        "status_entrega",
        "status_entrega:delivered",
        "status_entrega:failed",
        "status_entrega:played",
        "status_entrega:read",
        "status_entrega:sent",
        "desconhecido",
    )

    _SQL_PROVIDER = "provider = 'meta_whatsapp'"
    _SQL_TIPO = "tipo_evento IN ({})".format(", ".join(f"'{v}'" for v in TIPOS))
    _SQL_STATUS = "status_processamento IN ({})".format(
        ", ".join(f"'{valor}'" for valor in STATUS_PROCESSAMENTO)
    )
    _SQL_DIAGNOSTICO = "diagnostico_seguro IN ({})".format(
        ", ".join(f"'{v}'" for v in DIAGNOSTICOS)
    )
    _SQL_EVENTO = "length(evento_externo_id) > 0"
    _SQL_OPCIONAIS = (
        "(sujeito_externo IS NULL OR length(sujeito_externo) > 0)"
        " AND (contexto_destino IS NULL OR length(contexto_destino) > 0)"
        " AND (correlation_id IS NULL OR length(correlation_id) > 0)"
    )

    __table_args__ = (
        db.UniqueConstraint(
            "provider",
            "evento_externo_id",
            name="uq_evento_canal_recebido_provider_evento",
        ),
        db.CheckConstraint(_SQL_PROVIDER, name="ck_evento_canal_recebido_provider"),
        db.CheckConstraint(_SQL_TIPO, name="ck_evento_canal_recebido_tipo"),
        db.CheckConstraint(_SQL_STATUS, name="ck_evento_canal_recebido_status"),
        db.CheckConstraint(_SQL_DIAGNOSTICO, name="ck_evento_canal_recebido_diagnostico"),
        db.CheckConstraint(_SQL_EVENTO, name="ck_evento_canal_recebido_evento"),
        db.CheckConstraint(_SQL_OPCIONAIS, name="ck_evento_canal_recebido_opcionais"),
    )

    id = db.Column(db.Integer, primary_key=True)
    provider = db.Column(db.String(32), nullable=False)
    evento_externo_id = db.Column(db.String(200), nullable=False)
    tipo_evento = db.Column(db.String(32), nullable=False)
    sujeito_externo = db.Column(db.String(32), nullable=True)
    contexto_destino = db.Column(db.String(32), nullable=True)
    recebido_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive, index=True)
    status_processamento = db.Column(
        db.String(32),
        nullable=False,
        default=STATUS_RECEBIDO,
        index=True,
    )
    correlation_id = db.Column(db.String(32), nullable=True)
    diagnostico_seguro = db.Column(db.String(40), nullable=False)


class ConteudoTextualCanal(db.Model):
    """
    Texto operacional mínimo de uma mensagem textual já recebida.

    Uma linha por evento. Só a string recebida, com tamanho máximo explícito.
    Não há payload bruto, mídia, cabeçalho, assinatura nem segredo.
    """

    __tablename__ = "conteudo_textual_canal"

    TEXTO_MAXIMO = 4096
    _SQL_TEXTO = "length(texto) > 0 AND length(texto) <= 4096"

    __table_args__ = (
        db.UniqueConstraint("evento_id", name="uq_conteudo_textual_canal_evento"),
        db.CheckConstraint(_SQL_TEXTO, name="ck_conteudo_textual_canal_texto"),
    )

    id = db.Column(db.Integer, primary_key=True)
    evento_id = db.Column(
        db.Integer,
        db.ForeignKey("evento_canal_recebido.id"),
        nullable=False,
    )
    texto = db.Column(db.String(TEXTO_MAXIMO), nullable=False)

    evento = db.relationship(
        "EventoCanalRecebido",
        backref=db.backref("conteudo_textual", uselist=False),
    )


class InterpretacaoConversacionalCanal(db.Model):
    """
    Resultado interno do tratamento conversacional de um evento já roteado.

    Uma linha por evento, só para replay. Não há payload do provedor, token
    bruto, URL com token, senha, e-mail nem o texto integral do usuário.
    texto_resposta é a resposta interna já produzida, sem segredo.
    conclusao_id aponta a conclusão já persistida pelo Lote 2. O token bruto
    não cabe nesta linha e não é reconstruível a partir dela.
    """

    __tablename__ = "interpretacao_conversacional_canal"

    TEXTO_MAXIMO = 1024
    CODIGO_MAXIMO = 64
    ACAO_MAXIMA = 64

    _SQL_CODIGO = "length(codigo) > 0 AND length(codigo) <= 64"
    _SQL_ACAO = "length(acao) > 0 AND length(acao) <= 64"
    _SQL_ETAPA = "etapa_final IS NULL OR etapa_final IN ({})".format(
        ", ".join(f"'{etapa}'" for etapa in OnboardingCanal.ETAPAS)
    )
    _SQL_TEXTO = (
        "texto_resposta IS NULL OR "
        "(length(texto_resposta) > 0 AND length(texto_resposta) <= 1024)"
    )
    _SQL_SEM_LINK = (
        "texto_resposta IS NULL OR "
        "lower(texto_resposta) NOT LIKE '%/onboarding/canal/concluir/%'"
    )

    __table_args__ = (
        db.UniqueConstraint("evento_id", name="uq_interpretacao_conversacional_evento"),
        db.CheckConstraint(_SQL_CODIGO, name="ck_interpretacao_conversacional_codigo"),
        db.CheckConstraint(_SQL_ACAO, name="ck_interpretacao_conversacional_acao"),
        db.CheckConstraint(_SQL_ETAPA, name="ck_interpretacao_conversacional_etapa"),
        db.CheckConstraint(_SQL_TEXTO, name="ck_interpretacao_conversacional_texto"),
        db.CheckConstraint(_SQL_SEM_LINK, name="ck_interpretacao_conversacional_sem_link"),
    )

    id = db.Column(db.Integer, primary_key=True)
    evento_id = db.Column(
        db.Integer,
        db.ForeignKey("evento_canal_recebido.id"),
        nullable=False,
    )
    codigo = db.Column(db.String(CODIGO_MAXIMO), nullable=False)
    acao = db.Column(db.String(ACAO_MAXIMA), nullable=False)
    onboarding_id = db.Column(
        db.Integer,
        db.ForeignKey("onboarding_canal.id"),
        nullable=True,
    )
    etapa_final = db.Column(db.String(40), nullable=True)
    texto_resposta = db.Column(db.String(TEXTO_MAXIMO), nullable=True)
    conclusao_id = db.Column(
        db.Integer,
        db.ForeignKey("onboarding_canal_conclusao.id"),
        nullable=True,
    )

    evento = db.relationship(
        "EventoCanalRecebido",
        backref=db.backref("interpretacao_conversacional", uselist=False),
    )
    onboarding = db.relationship(
        "OnboardingCanal",
        backref=db.backref("interpretacoes_conversacionais", lazy="dynamic"),
    )
    conclusao = db.relationship(
        "OnboardingCanalConclusao",
        foreign_keys=[conclusao_id],
    )


class ExecucaoOperacionalCanal(db.Model):
    """
    Resultado da inteligência operacional já existente, disparada pelo canal.

    Uma linha por evento interno. Não é interpretação de onboarding, não
    guarda a pergunta, telefone, e-mail, token nem payload do provedor.
    texto_resposta só existe quando a resposta textual é utilizável.
    execution_id é o identificador que o fluxo da Júlia já aceita.
    """

    __tablename__ = "execucao_operacional_canal"

    TEXTO_MAXIMO = 4096
    ESTADO_RESERVADA = "reservada"
    ESTADO_CHAMADA_INICIADA = "chamada_iniciada"
    ESTADO_CONCLUIDA = "concluida"
    ESTADO_FALHA = "falha"
    ESTADOS = (
        ESTADO_RESERVADA,
        ESTADO_CHAMADA_INICIADA,
        ESTADO_CONCLUIDA,
        ESTADO_FALHA,
    )
    CODIGO_EM_TRATAMENTO = "em_tratamento"
    CODIGO_RESPOSTA = "resposta_operacional"
    CODIGO_GOVERNANCA_NEGADA = "governanca_negada"
    CODIGO_IDENTIDADE_INVALIDA = "identidade_invalida"
    CODIGO_CONTEXTO_INDISPONIVEL = "contexto_indisponivel"
    CODIGO_MENSAGEM_INVALIDA = "mensagem_invalida"
    CODIGO_FALHA_PROVEDOR = "falha_provedor"
    CODIGO_PROVEDOR_INDISPONIVEL = "provedor_indisponivel"
    CODIGO_RESPOSTA_INUTILIZAVEL = "resposta_inutilizavel"
    CODIGO_RESULTADO_INCERTO = "resultado_incerto"
    CODIGO_ERRO_TECNICO = "erro_tecnico"
    CODIGOS_FALHA = (
        CODIGO_GOVERNANCA_NEGADA,
        CODIGO_IDENTIDADE_INVALIDA,
        CODIGO_CONTEXTO_INDISPONIVEL,
        CODIGO_MENSAGEM_INVALIDA,
        CODIGO_FALHA_PROVEDOR,
        CODIGO_PROVEDOR_INDISPONIVEL,
        CODIGO_RESPOSTA_INUTILIZAVEL,
        CODIGO_RESULTADO_INCERTO,
        CODIGO_ERRO_TECNICO,
    )

    _SQL_ESTADO = "estado IN ({})".format(", ".join(f"'{v}'" for v in ESTADOS))
    _SQL_TEXTO = (
        "texto_resposta IS NULL OR "
        "(length(texto_resposta) > 0 AND length(texto_resposta) <= 4096)"
    )
    _SQL_EXECUTION = "execution_id IS NULL OR length(execution_id) = 36"
    _SQL_CORRELATION = (
        "correlation_id IS NULL OR "
        "(length(correlation_id) > 0 AND length(correlation_id) <= 64)"
    )
    _SQL_UTIL = "conclusao_util IN (0, 1)"
    _SQL_COERENCIA = (
        "("
        "(estado = 'reservada' AND codigo = 'em_tratamento' "
        "AND texto_resposta IS NULL AND conclusao_util = 0 "
        "AND user_id IS NOT NULL AND execution_id IS NOT NULL)"
        " OR (estado = 'chamada_iniciada' AND codigo = 'em_tratamento' "
        "AND texto_resposta IS NULL AND conclusao_util = 0 "
        "AND user_id IS NOT NULL AND execution_id IS NOT NULL)"
        " OR (estado = 'concluida' AND codigo = 'resposta_operacional' "
        "AND texto_resposta IS NOT NULL AND conclusao_util = 1 "
        "AND user_id IS NOT NULL AND execution_id IS NOT NULL)"
        " OR (estado = 'falha' AND conclusao_util = 0 AND texto_resposta IS NULL "
        "AND codigo IN ("
        "'governanca_negada', 'identidade_invalida', 'contexto_indisponivel', "
        "'mensagem_invalida', 'falha_provedor', 'provedor_indisponivel', "
        "'resposta_inutilizavel', 'resultado_incerto', 'erro_tecnico'"
        "))"
        ")"
    )

    __table_args__ = (
        db.UniqueConstraint("evento_id", name="uq_execucao_operacional_canal_evento"),
        db.CheckConstraint(_SQL_ESTADO, name="ck_execucao_operacional_canal_estado"),
        db.CheckConstraint(_SQL_TEXTO, name="ck_execucao_operacional_canal_texto"),
        db.CheckConstraint(_SQL_EXECUTION, name="ck_execucao_operacional_canal_execution"),
        db.CheckConstraint(
            _SQL_CORRELATION, name="ck_execucao_operacional_canal_correlation"
        ),
        db.CheckConstraint(_SQL_UTIL, name="ck_execucao_operacional_canal_util"),
        db.CheckConstraint(_SQL_COERENCIA, name="ck_execucao_operacional_canal_coerencia"),
    )

    id = db.Column(db.Integer, primary_key=True)
    evento_id = db.Column(
        db.Integer,
        db.ForeignKey("evento_canal_recebido.id"),
        nullable=False,
    )
    identidade_id = db.Column(
        db.Integer,
        db.ForeignKey("identidade_canal_externa.id"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id"),
        nullable=True,
        index=True,
    )
    codigo = db.Column(db.String(64), nullable=False)
    estado = db.Column(db.String(32), nullable=False)
    texto_resposta = db.Column(db.String(TEXTO_MAXIMO), nullable=True)
    execution_id = db.Column(db.String(36), nullable=True)
    conclusao_util = db.Column(db.Integer, nullable=False, default=0)
    correlation_id = db.Column(db.String(64), nullable=True)
    criada_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    atualizada_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)

    evento = db.relationship(
        "EventoCanalRecebido",
        backref=db.backref("execucao_operacional", uselist=False),
    )
    identidade = db.relationship("IdentidadeCanalExterna")
    user = db.relationship("User")


class ConsumoInteracaoCanal(db.Model):
    """
    Interação comercial elegível do canal.

    Uma execução operacional útil gera no máximo uma linha. A chave
    whatsapp:operacao:<execucao_id> é única. Não guarda pergunta, resposta,
    telefone, e-mail nem payload. A quantidade é a unidade consumida;
    a taxa fica só na régua administrativa. creditos_apropriados guarda
    o lançamento já feito e não é recalculado se a taxa mudar depois.
    """

    __tablename__ = "consumo_interacao_canal"

    TIPO_WHATSAPP_OPERACIONAL = "whatsapp_operacional"
    UNIDADE_INTERACAO = "interacao_whatsapp_util"
    CANAL_WHATSAPP = "whatsapp"
    QUANTIDADE_INTERACAO = 1
    ESTADO_PRONTA = "pronta_para_apropriacao"
    ESTADO_APROPRIADA = "apropriada"
    ESTADO_ERRO = "erro_apropriacao"
    ESTADO_FALHA = "falha_tecnica"
    ESTADOS = (ESTADO_PRONTA, ESTADO_APROPRIADA, ESTADO_ERRO, ESTADO_FALHA)
    MOTIVO_TAXA_PENDENTE = "taxa_comercial_whatsapp_pendente"
    MOTIVO_APROPRIADA = "apropriada"
    MOTIVO_ERRO = "erro_apropriacao"
    MOTIVO_FALHA_REGISTRO = "falha_registro"
    MOTIVO_CONTEXTO_INDISPONIVEL = "contexto_indisponivel"
    MOTIVOS = (
        MOTIVO_TAXA_PENDENTE,
        MOTIVO_APROPRIADA,
        MOTIVO_ERRO,
        MOTIVO_FALHA_REGISTRO,
        MOTIVO_CONTEXTO_INDISPONIVEL,
    )
    CHAVE_PREFIXO = "whatsapp:operacao:"

    _SQL_ESTADO = (
        "estado IN ("
        "'pronta_para_apropriacao', 'apropriada', 'erro_apropriacao', 'falha_tecnica'"
        ")"
    )
    _SQL_MOTIVO = (
        "motivo IN ("
        "'taxa_comercial_whatsapp_pendente', 'apropriada', 'erro_apropriacao', "
        "'falha_registro', 'contexto_indisponivel'"
        ")"
    )
    _SQL_TIPO = "tipo_consumo = 'whatsapp_operacional'"
    _SQL_UNIDADE = "unidade = 'interacao_whatsapp_util'"
    _SQL_CANAL = "canal = 'whatsapp'"
    _SQL_QUANTIDADE = "quantidade = 1"
    _SQL_CHAVE = (
        "chave_idempotente = ("
        "'whatsapp:operacao:' || CAST(execucao_operacional_canal_id AS TEXT)"
        ")"
    )
    _SQL_CORRELATION = (
        "correlation_id IS NULL OR "
        "(length(correlation_id) > 0 AND length(correlation_id) <= 64)"
    )
    _SQL_CREDITOS = (
        "("
        "(estado = 'apropriada' AND creditos_apropriados IS NOT NULL "
        "AND creditos_apropriados > 0)"
        " OR (estado <> 'apropriada' AND creditos_apropriados IS NULL)"
        ")"
    )
    _SQL_COERENCIA = (
        "("
        "(estado = 'pronta_para_apropriacao' "
        "AND motivo = 'taxa_comercial_whatsapp_pendente' "
        "AND creditos_apropriados IS NULL "
        "AND user_id IS NOT NULL AND conta_id IS NOT NULL AND franquia_id IS NOT NULL)"
        " OR (estado = 'apropriada' "
        "AND motivo = 'apropriada' "
        "AND creditos_apropriados IS NOT NULL AND creditos_apropriados > 0 "
        "AND user_id IS NOT NULL AND conta_id IS NOT NULL AND franquia_id IS NOT NULL)"
        " OR (estado = 'erro_apropriacao' "
        "AND motivo = 'erro_apropriacao' "
        "AND creditos_apropriados IS NULL "
        "AND user_id IS NOT NULL AND conta_id IS NOT NULL AND franquia_id IS NOT NULL)"
        " OR (estado = 'falha_tecnica' AND user_id IS NOT NULL "
        "AND creditos_apropriados IS NULL "
        "AND motivo IN ('falha_registro', 'contexto_indisponivel'))"
        ")"
    )

    __table_args__ = (
        db.UniqueConstraint(
            "execucao_operacional_canal_id",
            name="uq_consumo_interacao_canal_execucao",
        ),
        db.UniqueConstraint(
            "chave_idempotente",
            name="uq_consumo_interacao_canal_chave",
        ),
        db.CheckConstraint(_SQL_ESTADO, name="ck_consumo_interacao_canal_estado"),
        db.CheckConstraint(_SQL_MOTIVO, name="ck_consumo_interacao_canal_motivo"),
        db.CheckConstraint(_SQL_TIPO, name="ck_consumo_interacao_canal_tipo"),
        db.CheckConstraint(_SQL_UNIDADE, name="ck_consumo_interacao_canal_unidade"),
        db.CheckConstraint(_SQL_CANAL, name="ck_consumo_interacao_canal_canal"),
        db.CheckConstraint(_SQL_QUANTIDADE, name="ck_consumo_interacao_canal_quantidade"),
        db.CheckConstraint(_SQL_CHAVE, name="ck_consumo_interacao_canal_chave"),
        db.CheckConstraint(
            _SQL_CORRELATION, name="ck_consumo_interacao_canal_correlation"
        ),
        db.CheckConstraint(_SQL_CREDITOS, name="ck_consumo_interacao_canal_creditos"),
        db.CheckConstraint(_SQL_COERENCIA, name="ck_consumo_interacao_canal_coerencia"),
    )

    id = db.Column(db.Integer, primary_key=True)
    execucao_operacional_canal_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "execucao_operacional_canal.id",
            name="fk_consumo_interacao_canal_execucao",
        ),
        nullable=False,
    )
    evento_canal_recebido_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "evento_canal_recebido.id",
            name="fk_consumo_interacao_canal_evento",
        ),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("user.id", name="fk_consumo_interacao_canal_user"),
        nullable=False,
        index=True,
    )
    conta_id = db.Column(
        db.Integer,
        db.ForeignKey("conta.id", name="fk_consumo_interacao_canal_conta"),
        nullable=True,
        index=True,
    )
    franquia_id = db.Column(
        db.Integer,
        db.ForeignKey("franquia.id", name="fk_consumo_interacao_canal_franquia"),
        nullable=True,
        index=True,
    )
    tipo_consumo = db.Column(db.String(40), nullable=False)
    quantidade = db.Column(db.Integer, nullable=False)
    unidade = db.Column(db.String(40), nullable=False)
    canal = db.Column(db.String(32), nullable=False)
    chave_idempotente = db.Column(db.String(80), nullable=False)
    estado = db.Column(db.String(40), nullable=False)
    motivo = db.Column(db.String(80), nullable=False)
    correlation_id = db.Column(db.String(64), nullable=True)
    creditos_apropriados = db.Column(db.Numeric(18, 6), nullable=True)
    criada_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    atualizada_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)

    execucao = db.relationship(
        "ExecucaoOperacionalCanal",
        backref=db.backref("consumo_interacao", uselist=False),
    )
    evento = db.relationship("EventoCanalRecebido")
    user = db.relationship("User")
    conta = db.relationship("Conta")
    franquia = db.relationship("Franquia")

    @staticmethod
    def chave_da_execucao(execucao_id: int) -> str:
        return f"{ConsumoInteracaoCanal.CHAVE_PREFIXO}{int(execucao_id)}"


class EventoCanalSaida(db.Model):
    """
    Resultado técnico de uma saída textual para o provider.

    Uma interpretação ou uma execução operacional produz no máximo uma
    resposta principal. A chave meta_whatsapp:<evento_entrada_id>:resposta_principal
    é única. A orientação de franquia usa outra chave do mesmo evento,
    meta_whatsapp:<evento_entrada_id>:orientacao_franquia:resposta_principal,
    sem interpretação e sem execução. A chave tem de ser a desse
    evento_entrada_id; outra chave com a mesma marca não entra.
    Nunca há duas origens na mesma linha. A linha nasce antes do HTTP e,
    uma vez gravada, uma nova chamada não envia de novo. Não há header,
    access token, telefone, texto nem corpo bruto.

    conclusao_id aponta a conclusão cujo link foi usado na entrega.
    O segredo e a URL não cabem nesta linha. preparando_link significa
    que essa conclusão já foi persistida e ainda não há confirmação
    local de que a Meta aceitou o envio.

    A execução técnica de cada envio fica em TentativaEnvioCanal.
    Esta linha continua sendo a saída lógica. provider_message_id
    aqui é a projeção da tentativa mais recente, não o histórico.
    """

    __tablename__ = "evento_canal_saida"

    PROVIDER_META_WHATSAPP = "meta_whatsapp"
    STATUS_RESERVADO = "reservado"
    STATUS_ACEITO = "aceito_provider"
    STATUS_ERRO = "erro"
    STATUS_AGUARDANDO_LINK = "aguardando_link_seguro"
    STATUS_PREPARANDO_LINK = "preparando_link"
    STATUS_ENVIO = (
        STATUS_RESERVADO,
        STATUS_ACEITO,
        STATUS_ERRO,
        STATUS_AGUARDANDO_LINK,
        STATUS_PREPARANDO_LINK,
    )
    CODIGO_TIMEOUT = "timeout"
    CODIGO_HTTP_4XX = "http_4xx"
    CODIGO_HTTP_5XX = "http_5xx"
    CODIGO_RESPOSTA_INVALIDA = "resposta_invalida"
    CODIGO_CONFIGURACAO_AUSENTE = "configuracao_ausente"
    CODIGO_FALHA_TRANSPORTE = "falha_transporte"
    CODIGOS_ERRO = (
        CODIGO_TIMEOUT,
        CODIGO_HTTP_4XX,
        CODIGO_HTTP_5XX,
        CODIGO_RESPOSTA_INVALIDA,
        CODIGO_CONFIGURACAO_AUSENTE,
        CODIGO_FALHA_TRANSPORTE,
    )
    CHAVE_MAXIMA = 80
    MENSAGEM_MAXIMA = 200
    CORRELATION_MAXIMA = 64

    _SQL_PROVIDER = "provider = 'meta_whatsapp'"
    _SQL_STATUS = (
        "status_envio IN ("
        "'reservado', 'aceito_provider', 'erro', "
        "'aguardando_link_seguro', 'preparando_link'"
        ")"
    )
    # Sem ':nome' no SQL: o compilador trata isso como bind e anula o predicado.
    _SQL_CHAVE = (
        "length(chave_idempotencia) BETWEEN 34 AND 80 "
        "AND substr(chave_idempotencia, 1, 14) = 'meta_whatsapp:' "
        "AND substr(chave_idempotencia, length(chave_idempotencia) - 17, 18) "
        "= 'resposta_principal' "
        "AND substr(chave_idempotencia, length(chave_idempotencia) - 18, 1) = ':'"
    )
    _SQL_ERRO = (
        "codigo_erro IS NULL OR codigo_erro IN ("
        "'timeout', 'http_4xx', 'http_5xx', 'resposta_invalida', "
        "'configuracao_ausente', 'falha_transporte'"
        ")"
    )
    _SQL_MENSAGEM = (
        "provider_message_id IS NULL OR "
        "(length(provider_message_id) BETWEEN 1 AND 200)"
    )
    _SQL_CORRELATION = "length(correlation_id) > 0 AND length(correlation_id) <= 64"
    # A orientação de franquia não nasce de interpretação nem de execução.
    # A chave tem de ser exatamente a do próprio evento. Substring não basta.
    # length < 39 evita índice negativo: a marca mais resposta_principal tem 39.
    _SQL_CHAVE_ORIENTACAO = (
        "('meta_whatsapp:' || CAST(evento_entrada_id AS TEXT)"
        " || ':orientacao_franquia:resposta_principal')"
    )
    _SQL_SEM_MARCA_ORIENTACAO = (
        "(length(chave_idempotencia) < 39 OR "
        "substr(chave_idempotencia, length(chave_idempotencia) - 38, 21)"
        " <> ':orientacao_franquia:')"
    )
    _SQL_ORIGEM = (
        "(interpretacao_id IS NOT NULL AND execucao_operacional_id IS NULL"
        f" AND {_SQL_SEM_MARCA_ORIENTACAO})"
        " OR (interpretacao_id IS NULL AND execucao_operacional_id IS NOT NULL"
        f" AND {_SQL_SEM_MARCA_ORIENTACAO})"
        " OR (interpretacao_id IS NULL AND execucao_operacional_id IS NULL"
        f" AND chave_idempotencia = {_SQL_CHAVE_ORIENTACAO})"
    )
    _SQL_COERENCIA = (
        "("
        "(status_envio = 'aceito_provider' AND provider_message_id IS NOT NULL "
        "AND codigo_erro IS NULL AND enviado_em IS NOT NULL)"
        " OR (status_envio = 'erro' AND provider_message_id IS NULL "
        "AND codigo_erro IS NOT NULL AND enviado_em IS NULL)"
        " OR (status_envio = 'reservado' AND provider_message_id IS NULL "
        "AND codigo_erro IS NULL AND enviado_em IS NULL)"
        " OR (status_envio = 'aguardando_link_seguro' AND provider_message_id IS NULL "
        "AND codigo_erro IS NULL AND enviado_em IS NULL)"
        " OR (status_envio = 'preparando_link' AND provider_message_id IS NULL "
        "AND codigo_erro IS NULL AND enviado_em IS NULL)"
        ")"
    )

    __table_args__ = (
        db.UniqueConstraint("chave_idempotencia", name="uq_evento_canal_saida_chave"),
        db.CheckConstraint(_SQL_PROVIDER, name="ck_evento_canal_saida_provider"),
        db.CheckConstraint(_SQL_STATUS, name="ck_evento_canal_saida_status"),
        db.CheckConstraint(_SQL_CHAVE, name="ck_evento_canal_saida_chave"),
        db.CheckConstraint(_SQL_ERRO, name="ck_evento_canal_saida_erro"),
        db.CheckConstraint(_SQL_MENSAGEM, name="ck_evento_canal_saida_mensagem"),
        db.CheckConstraint(_SQL_CORRELATION, name="ck_evento_canal_saida_correlation"),
        db.CheckConstraint(_SQL_ORIGEM, name="ck_evento_canal_saida_origem"),
        db.CheckConstraint(_SQL_COERENCIA, name="ck_evento_canal_saida_coerencia"),
    )

    id = db.Column(db.Integer, primary_key=True)
    evento_entrada_id = db.Column(
        db.Integer,
        db.ForeignKey("evento_canal_recebido.id"),
        nullable=False,
        index=True,
    )
    interpretacao_id = db.Column(
        db.Integer,
        db.ForeignKey("interpretacao_conversacional_canal.id"),
        nullable=True,
        index=True,
    )
    execucao_operacional_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "execucao_operacional_canal.id",
            name="fk_evento_canal_saida_execucao",
        ),
        nullable=True,
        index=True,
    )
    provider = db.Column(db.String(32), nullable=False)
    chave_idempotencia = db.Column(db.String(CHAVE_MAXIMA), nullable=False)
    status_envio = db.Column(db.String(32), nullable=False)
    provider_message_id = db.Column(
        db.String(MENSAGEM_MAXIMA),
        nullable=True,
        index=True,
    )
    codigo_erro = db.Column(db.String(32), nullable=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    enviado_em = db.Column(db.DateTime, nullable=True)
    correlation_id = db.Column(db.String(CORRELATION_MAXIMA), nullable=False)
    conclusao_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "onboarding_canal_conclusao.id",
            name="fk_evento_canal_saida_conclusao",
        ),
        nullable=True,
        index=True,
    )

    evento_entrada = db.relationship(
        "EventoCanalRecebido",
        backref=db.backref("saidas_canal", lazy="dynamic"),
    )
    interpretacao = db.relationship(
        "InterpretacaoConversacionalCanal",
        backref=db.backref("saidas_canal", lazy="dynamic"),
    )
    execucao_operacional = db.relationship(
        "ExecucaoOperacionalCanal",
        foreign_keys=[execucao_operacional_id],
    )
    conclusao = db.relationship(
        "OnboardingCanalConclusao",
        foreign_keys=[conclusao_id],
    )

    MARCA_ORIENTACAO_FRANQUIA = ":orientacao_franquia:"

    @staticmethod
    def chave_resposta_principal(evento_entrada_id: int) -> str:
        return f"meta_whatsapp:{evento_entrada_id}:resposta_principal"

    @staticmethod
    def chave_orientacao_franquia(evento_entrada_id: int) -> str:
        return (
            f"meta_whatsapp:{int(evento_entrada_id)}"
            f"{EventoCanalSaida.MARCA_ORIENTACAO_FRANQUIA}resposta_principal"
        )


class TentativaEnvioCanal(db.Model):
    """
    Execução técnica de uma saída lógica.

    A saída responde qual resposta nasceu da interpretação. A tentativa
    responde o que aconteceu ao materializar essa saída. A tentativa 1
    nasce com origem envio_inicial e sem chave de recuperação. As
    seguintes, no máximo até 3, nascem só por recuperação manual de
    texto comum e guardam a chave opaca daquela ação. A mesma chave não
    abre outra tentativa. Não há segredo, endereço, frase, destinatário,
    corpo HTTP nem cabeçalho de autorização.

    classificacao_resultado repete a evidência resultado_incerto já
    usada na entrega. Não autoriza outra tentativa. motivo_recuperacao
    é um código fechado, sem comentário livre.
    """

    __tablename__ = "tentativa_envio_canal"

    NUMERO_INICIAL = 1
    NUMERO_MAXIMO = 3
    TIPO_TEXTO_COMUM = "texto_comum"
    TIPO_LINK_SEGURO = "link_seguro"
    TIPOS = (TIPO_TEXTO_COMUM, TIPO_LINK_SEGURO)
    ORIGEM_ENVIO_INICIAL = "envio_inicial"
    ORIGEM_RECUPERACAO_MANUAL = "recuperacao_manual"
    ORIGENS = (ORIGEM_ENVIO_INICIAL, ORIGEM_RECUPERACAO_MANUAL)
    MOTIVO_CONFIGURACAO_CORRIGIDA = "configuracao_corrigida"
    MOTIVO_FALHA_HTTP = "falha_http"
    MOTIVO_RESULTADO_INCERTO = "resultado_incerto"
    MOTIVO_FALHA_ENTREGA_PROVIDER = "falha_entrega_provider"
    MOTIVO_OPERACAO_MANUAL = "operacao_manual"
    MOTIVOS = (
        MOTIVO_CONFIGURACAO_CORRIGIDA,
        MOTIVO_FALHA_HTTP,
        MOTIVO_RESULTADO_INCERTO,
        MOTIVO_FALHA_ENTREGA_PROVIDER,
        MOTIVO_OPERACAO_MANUAL,
    )
    STATUS_RESERVADA = "reservada"
    STATUS_AGUARDANDO_LINK = "aguardando_link_seguro"
    STATUS_PREPARANDO = "preparando"
    STATUS_ACEITA = "aceita_provider"
    STATUS_ERRO = "erro"
    STATUS_TENTATIVA = (
        STATUS_RESERVADA,
        STATUS_AGUARDANDO_LINK,
        STATUS_PREPARANDO,
        STATUS_ACEITA,
        STATUS_ERRO,
    )
    CLASSIFICACAO_RESULTADO_INCERTO = "resultado_incerto"
    CODIGOS_ERRO = EventoCanalSaida.CODIGOS_ERRO

    _SQL_NUMERO = "numero_tentativa >= 1 AND numero_tentativa <= 3"
    _SQL_TIPO = "tipo_tentativa IN ('texto_comum', 'link_seguro')"
    _SQL_ORIGEM = "origem_tentativa IN ('envio_inicial', 'recuperacao_manual')"
    _SQL_MOTIVO = (
        "("
        "(origem_tentativa = 'envio_inicial' AND motivo_recuperacao IS NULL)"
        " OR (origem_tentativa = 'recuperacao_manual' AND motivo_recuperacao IN ("
        "'configuracao_corrigida', 'falha_http', 'resultado_incerto', "
        "'falha_entrega_provider', 'operacao_manual'))"
        ")"
    )
    _SQL_IDENTIDADE = (
        "("
        "(numero_tentativa = 1 AND origem_tentativa = 'envio_inicial' "
        "AND chave_recuperacao IS NULL)"
        " OR (numero_tentativa > 1 AND origem_tentativa = 'recuperacao_manual' "
        "AND chave_recuperacao IS NOT NULL "
        "AND length(chave_recuperacao) BETWEEN 1 AND 64)"
        ")"
    )
    _SQL_STATUS = (
        "status_tentativa IN ("
        "'reservada', 'aguardando_link_seguro', 'preparando', "
        "'aceita_provider', 'erro'"
        ")"
    )
    _SQL_PROVIDER = "provider = 'meta_whatsapp'"
    _SQL_ERRO = (
        "codigo_erro IS NULL OR codigo_erro IN ("
        "'timeout', 'http_4xx', 'http_5xx', 'resposta_invalida', "
        "'configuracao_ausente', 'falha_transporte'"
        ")"
    )
    _SQL_MENSAGEM = (
        "provider_message_id IS NULL OR "
        "(length(provider_message_id) BETWEEN 1 AND 200)"
    )
    _SQL_CORRELATION = "length(correlation_id) > 0 AND length(correlation_id) <= 64"
    _SQL_CONCLUSAO = (
        "("
        "(tipo_tentativa = 'texto_comum' AND conclusao_id IS NULL)"
        " OR ("
        "tipo_tentativa = 'link_seguro' AND ("
        "conclusao_id IS NULL"
        " OR status_tentativa IN ('preparando', 'aceita_provider', 'erro')"
        ")"
        ")"
        ")"
    )
    _SQL_CLASSIFICACAO = (
        "classificacao_resultado IS NULL OR ("
        "classificacao_resultado = 'resultado_incerto' "
        "AND status_tentativa IN ('reservada', 'preparando') "
        "AND provider_message_id IS NULL "
        "AND codigo_erro IS NULL "
        "AND enviado_em IS NULL"
        ")"
    )
    _SQL_COERENCIA = (
        "("
        "(status_tentativa = 'aceita_provider' AND provider_message_id IS NOT NULL "
        "AND codigo_erro IS NULL AND enviado_em IS NOT NULL "
        "AND finalizado_em IS NOT NULL)"
        " OR (status_tentativa = 'erro' AND provider_message_id IS NULL "
        "AND codigo_erro IS NOT NULL AND enviado_em IS NULL)"
        " OR (status_tentativa = 'reservada' AND provider_message_id IS NULL "
        "AND codigo_erro IS NULL AND enviado_em IS NULL "
        "AND finalizado_em IS NULL AND preparado_em IS NULL)"
        " OR (status_tentativa = 'aguardando_link_seguro' "
        "AND provider_message_id IS NULL AND codigo_erro IS NULL "
        "AND enviado_em IS NULL AND finalizado_em IS NULL "
        "AND preparado_em IS NULL AND conclusao_id IS NULL)"
        " OR (status_tentativa = 'preparando' AND provider_message_id IS NULL "
        "AND codigo_erro IS NULL AND enviado_em IS NULL "
        "AND finalizado_em IS NULL)"
        ")"
    )

    __table_args__ = (
        db.UniqueConstraint(
            "saida_id",
            "numero_tentativa",
            name="uq_tentativa_envio_canal_saida_numero",
        ),
        db.UniqueConstraint(
            "saida_id",
            "chave_recuperacao",
            name="uq_tentativa_envio_canal_saida_chave",
        ),
        db.CheckConstraint(_SQL_NUMERO, name="ck_tentativa_envio_canal_numero"),
        db.CheckConstraint(_SQL_TIPO, name="ck_tentativa_envio_canal_tipo"),
        db.CheckConstraint(_SQL_ORIGEM, name="ck_tentativa_envio_canal_origem"),
        db.CheckConstraint(_SQL_MOTIVO, name="ck_tentativa_envio_canal_motivo"),
        db.CheckConstraint(
            _SQL_IDENTIDADE,
            name="ck_tentativa_envio_canal_identidade",
        ),
        db.CheckConstraint(_SQL_STATUS, name="ck_tentativa_envio_canal_status"),
        db.CheckConstraint(_SQL_PROVIDER, name="ck_tentativa_envio_canal_provider"),
        db.CheckConstraint(_SQL_ERRO, name="ck_tentativa_envio_canal_erro"),
        db.CheckConstraint(_SQL_MENSAGEM, name="ck_tentativa_envio_canal_mensagem"),
        db.CheckConstraint(
            _SQL_CORRELATION,
            name="ck_tentativa_envio_canal_correlation",
        ),
        db.CheckConstraint(_SQL_CONCLUSAO, name="ck_tentativa_envio_canal_conclusao"),
        db.CheckConstraint(
            _SQL_CLASSIFICACAO,
            name="ck_tentativa_envio_canal_classificacao",
        ),
        db.CheckConstraint(_SQL_COERENCIA, name="ck_tentativa_envio_canal_coerencia"),
    )

    id = db.Column(db.Integer, primary_key=True)
    saida_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "evento_canal_saida.id",
            name="fk_tentativa_envio_canal_saida",
        ),
        nullable=False,
    )
    numero_tentativa = db.Column(db.Integer, nullable=False)
    tipo_tentativa = db.Column(db.String(32), nullable=False)
    origem_tentativa = db.Column(db.String(32), nullable=False)
    status_tentativa = db.Column(db.String(32), nullable=False)
    codigo_erro = db.Column(db.String(32), nullable=True)
    provider = db.Column(db.String(32), nullable=False)
    provider_message_id = db.Column(
        db.String(EventoCanalSaida.MENSAGEM_MAXIMA),
        nullable=True,
        index=True,
    )
    conclusao_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "onboarding_canal_conclusao.id",
            name="fk_tentativa_envio_canal_conclusao",
        ),
        nullable=True,
    )
    criado_em = db.Column(db.DateTime, nullable=False)
    preparado_em = db.Column(db.DateTime, nullable=True)
    enviado_em = db.Column(db.DateTime, nullable=True)
    finalizado_em = db.Column(db.DateTime, nullable=True)
    correlation_id = db.Column(
        db.String(EventoCanalSaida.CORRELATION_MAXIMA),
        nullable=False,
    )
    classificacao_resultado = db.Column(db.String(32), nullable=True)
    motivo_recuperacao = db.Column(db.String(32), nullable=True)
    chave_recuperacao = db.Column(db.String(64), nullable=True)

    saida = db.relationship(
        "EventoCanalSaida",
        backref=db.backref("tentativas_envio", lazy="dynamic"),
    )
    conclusao = db.relationship(
        "OnboardingCanalConclusao",
        foreign_keys=[conclusao_id],
    )


class EstadoEntregaCanalSaida(db.Model):
    """
    Entrega confirmada pelo provider, separada da aceitação HTTP.

    status_envio da saída continua sendo o resultado da chamada.
    Aqui ficam sent, delivered, read e a falha técnica de entrega.
    O horário de cada estágio é o informado pelo provider.
    resultado_incerto marca saída reservada ou em preparo de link
    que segue sem provider_message_id. Não há corpo nem segredo.
    """

    __tablename__ = "estado_entrega_canal_saida"

    STATUS_SENT = "sent"
    STATUS_DELIVERED = "delivered"
    STATUS_READ = "read"
    STATUS_FAILED = "failed"
    STATUS_ENTREGA = (
        STATUS_SENT,
        STATUS_DELIVERED,
        STATUS_READ,
        STATUS_FAILED,
    )
    CODIGO_FALHA_ENTREGA = "falha_entrega"
    CLASSIFICACAO_RESULTADO_INCERTO = "resultado_incerto"

    _SQL_VERSAO = "versao >= 0"
    _SQL_STATUS = (
        "status_entrega IS NULL OR status_entrega IN ("
        "'sent', 'delivered', 'read', 'failed')"
    )
    _SQL_FALHA = (
        "codigo_falha_entrega IS NULL OR codigo_falha_entrega = 'falha_entrega'"
    )
    _SQL_CLASSIFICACAO = (
        "classificacao_resultado IS NULL "
        "OR classificacao_resultado = 'resultado_incerto'"
    )
    _SQL_FORMA = (
        "("
        "(classificacao_resultado = 'resultado_incerto' "
        "AND status_entrega IS NULL AND provider_status_em IS NULL "
        "AND sent_em IS NULL AND delivered_em IS NULL AND read_em IS NULL "
        "AND failed_em IS NULL AND codigo_falha_entrega IS NULL)"
        " OR (classificacao_resultado IS NULL AND provider_status_em IS NOT NULL "
        "AND ("
        "(status_entrega = 'sent' AND sent_em IS NOT NULL "
        "AND delivered_em IS NULL AND read_em IS NULL "
        "AND failed_em IS NULL AND codigo_falha_entrega IS NULL)"
        " OR (status_entrega = 'delivered' AND delivered_em IS NOT NULL "
        "AND read_em IS NULL AND ("
        "(failed_em IS NULL AND codigo_falha_entrega IS NULL)"
        " OR (failed_em IS NOT NULL AND codigo_falha_entrega = 'falha_entrega')"
        "))"
        " OR (status_entrega = 'read' AND read_em IS NOT NULL AND ("
        "(failed_em IS NULL AND codigo_falha_entrega IS NULL)"
        " OR (failed_em IS NOT NULL AND codigo_falha_entrega = 'falha_entrega')"
        "))"
        " OR (status_entrega = 'failed' AND failed_em IS NOT NULL "
        "AND codigo_falha_entrega = 'falha_entrega' "
        "AND delivered_em IS NULL AND read_em IS NULL)"
        "))"
        ")"
    )

    __table_args__ = (
        db.UniqueConstraint("saida_id", name="uq_estado_entrega_canal_saida"),
        db.CheckConstraint(_SQL_VERSAO, name="ck_estado_entrega_canal_versao"),
        db.CheckConstraint(_SQL_STATUS, name="ck_estado_entrega_canal_status"),
        db.CheckConstraint(_SQL_FALHA, name="ck_estado_entrega_canal_falha"),
        db.CheckConstraint(
            _SQL_CLASSIFICACAO,
            name="ck_estado_entrega_canal_classificacao",
        ),
        db.CheckConstraint(_SQL_FORMA, name="ck_estado_entrega_canal_forma"),
    )

    id = db.Column(db.Integer, primary_key=True)
    saida_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "evento_canal_saida.id",
            name="fk_estado_entrega_canal_saida",
        ),
        nullable=False,
    )
    versao = db.Column(db.Integer, nullable=False, default=0)
    status_entrega = db.Column(db.String(16), nullable=True)
    provider_status_em = db.Column(db.DateTime, nullable=True)
    sent_em = db.Column(db.DateTime, nullable=True)
    delivered_em = db.Column(db.DateTime, nullable=True)
    read_em = db.Column(db.DateTime, nullable=True)
    failed_em = db.Column(db.DateTime, nullable=True)
    codigo_falha_entrega = db.Column(db.String(32), nullable=True)
    classificacao_resultado = db.Column(db.String(32), nullable=True)

    saida = db.relationship(
        "EventoCanalSaida",
        backref=db.backref("estado_entrega", uselist=False),
    )


class EstadoEntregaTentativaCanal(db.Model):
    """
    Entrega confirmada de uma tentativa, separada da projeção da saída.

    sent, delivered e read de uma tentativa não se misturam com os de
    outra. A linha da saída continua sendo uma projeção operacional.
    Não há corpo nem segredo.
    """

    __tablename__ = "estado_entrega_tentativa_canal"

    _SQL_VERSAO = EstadoEntregaCanalSaida._SQL_VERSAO
    _SQL_STATUS = EstadoEntregaCanalSaida._SQL_STATUS
    _SQL_FALHA = EstadoEntregaCanalSaida._SQL_FALHA
    _SQL_CLASSIFICACAO = EstadoEntregaCanalSaida._SQL_CLASSIFICACAO
    _SQL_FORMA = EstadoEntregaCanalSaida._SQL_FORMA

    __table_args__ = (
        db.UniqueConstraint(
            "tentativa_envio_id",
            name="uq_estado_entrega_tentativa_canal",
        ),
        db.CheckConstraint(_SQL_VERSAO, name="ck_estado_entrega_tentativa_versao"),
        db.CheckConstraint(_SQL_STATUS, name="ck_estado_entrega_tentativa_status"),
        db.CheckConstraint(_SQL_FALHA, name="ck_estado_entrega_tentativa_falha"),
        db.CheckConstraint(
            _SQL_CLASSIFICACAO,
            name="ck_estado_entrega_tentativa_classificacao",
        ),
        db.CheckConstraint(_SQL_FORMA, name="ck_estado_entrega_tentativa_forma"),
    )

    id = db.Column(db.Integer, primary_key=True)
    tentativa_envio_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "tentativa_envio_canal.id",
            name="fk_estado_entrega_tentativa_envio",
        ),
        nullable=False,
    )
    saida_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "evento_canal_saida.id",
            name="fk_estado_entrega_tentativa_saida",
        ),
        nullable=False,
        index=True,
    )
    versao = db.Column(db.Integer, nullable=False, default=0)
    status_entrega = db.Column(db.String(16), nullable=True)
    provider_status_em = db.Column(db.DateTime, nullable=True)
    sent_em = db.Column(db.DateTime, nullable=True)
    delivered_em = db.Column(db.DateTime, nullable=True)
    read_em = db.Column(db.DateTime, nullable=True)
    failed_em = db.Column(db.DateTime, nullable=True)
    codigo_falha_entrega = db.Column(db.String(32), nullable=True)
    classificacao_resultado = db.Column(db.String(32), nullable=True)

    tentativa = db.relationship(
        "TentativaEnvioCanal",
        foreign_keys=[tentativa_envio_id],
    )
    saida = db.relationship(
        "EventoCanalSaida",
        foreign_keys=[saida_id],
    )


class AplicacaoStatusCanal(db.Model):
    """
    Tentativa de tratamento de um evento status_entrega já persistido.

    A primeira linha nasce no processamento. sem_saida pode ganhar uma
    tentativa posterior, numerada, quando a saída aparecer. Os demais
    resultados encerram o evento. A linha antiga não é apagada.
    Não há corpo nem segredo.
    """

    __tablename__ = "aplicacao_status_canal"

    RESULTADO_APLICADO = "aplicado"
    RESULTADO_SEM_EFEITO = "sem_efeito"
    RESULTADO_SEM_AVANCO = "registrado_sem_avanco"
    RESULTADO_SEM_SAIDA = "sem_saida"
    RESULTADO_STATUS_IGNORADO = "status_ignorado"
    RESULTADO_SAIDA_AMBIGUA = "saida_ambigua"
    RESULTADO_ILEGIVEL = "ilegivel"
    RESULTADOS = (
        RESULTADO_APLICADO,
        RESULTADO_SEM_EFEITO,
        RESULTADO_SEM_AVANCO,
        RESULTADO_SEM_SAIDA,
        RESULTADO_STATUS_IGNORADO,
        RESULTADO_SAIDA_AMBIGUA,
        RESULTADO_ILEGIVEL,
    )
    RESULTADOS_COM_SAIDA = (
        RESULTADO_APLICADO,
        RESULTADO_SEM_EFEITO,
        RESULTADO_SEM_AVANCO,
    )
    RESULTADOS_SEM_SAIDA = (
        RESULTADO_SEM_SAIDA,
        RESULTADO_STATUS_IGNORADO,
        RESULTADO_SAIDA_AMBIGUA,
    )

    _SQL_RESULTADO = "resultado IN ({})".format(
        ", ".join(f"'{valor}'" for valor in RESULTADOS)
    )
    _SQL_CORRELATION = (
        "correlation_id IS NULL OR "
        "(length(correlation_id) > 0 AND length(correlation_id) <= 32)"
    )
    _SQL_TENTATIVA = "numero_tentativa >= 1"
    _SQL_COERENCIA = (
        "("
        "(resultado IN ('aplicado', 'sem_efeito', 'registrado_sem_avanco') "
        "AND saida_id IS NOT NULL AND provider_status_em IS NOT NULL)"
        " OR (resultado IN ('sem_saida', 'status_ignorado', 'saida_ambigua') "
        "AND saida_id IS NULL AND provider_status_em IS NOT NULL)"
        " OR (resultado = 'ilegivel' AND saida_id IS NULL "
        "AND provider_status_em IS NULL)"
        ")"
    )

    __table_args__ = (
        db.UniqueConstraint(
            "evento_recebido_id",
            "numero_tentativa",
            name="uq_aplicacao_status_canal_evento_tentativa",
        ),
        db.CheckConstraint(_SQL_RESULTADO, name="ck_aplicacao_status_canal_resultado"),
        db.CheckConstraint(
            _SQL_CORRELATION,
            name="ck_aplicacao_status_canal_correlation",
        ),
        db.CheckConstraint(_SQL_TENTATIVA, name="ck_aplicacao_status_canal_tentativa"),
        db.CheckConstraint(_SQL_COERENCIA, name="ck_aplicacao_status_canal_coerencia"),
    )

    id = db.Column(db.Integer, primary_key=True)
    evento_recebido_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "evento_canal_recebido.id",
            name="fk_aplicacao_status_canal_evento",
        ),
        nullable=False,
    )
    numero_tentativa = db.Column(db.Integer, nullable=False, default=1)
    saida_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "evento_canal_saida.id",
            name="fk_aplicacao_status_canal_saida",
        ),
        nullable=True,
    )
    resultado = db.Column(db.String(32), nullable=False)
    provider_status_em = db.Column(db.DateTime, nullable=True)
    correlation_id = db.Column(db.String(32), nullable=True)
    criado_em = db.Column(db.DateTime, nullable=False, default=utcnow_naive)
    tentativa_envio_id = db.Column(
        db.Integer,
        db.ForeignKey(
            "tentativa_envio_canal.id",
            name="fk_aplicacao_status_canal_tentativa_envio",
        ),
        nullable=True,
        index=True,
    )

    evento_recebido = db.relationship(
        "EventoCanalRecebido",
        backref=db.backref("aplicacoes_status", lazy="dynamic"),
    )
    saida = db.relationship(
        "EventoCanalSaida",
        foreign_keys=[saida_id],
    )
