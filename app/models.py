from app.extensions import db
from flask_login import UserMixin
from datetime import datetime, timezone


def utcnow_naive() -> datetime:
    """Retorna datetime UTC naive para compatibilidade com colunas DateTime atuais."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=True)  # None para usuários só OAuth (ex.: Google)
    full_name = db.Column(db.String(150), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    categoria = db.Column(db.String(50), default='free')
    creditos = db.Column(db.Integer, default=10)
    created_at = db.Column(db.DateTime, default=utcnow_naive)
    last_login_at = db.Column(db.DateTime, nullable=True)
    subscribes_to_newsletter = db.Column(db.Boolean, default=False)
    accepted_terms_at = db.Column(db.DateTime, nullable=True)
    usage_purpose = db.Column(db.String(50), nullable=True)
    job_role = db.Column(db.String(100), nullable=True)
    oauth_provider = db.Column(db.String(50), nullable=True)
    oauth_sub = db.Column(db.String(255), nullable=True)
    # Freemium: contador diário de interações no chat Júlia e data do último uso (reset por dia)
    chat_consultas_hoje = db.Column(db.Integer, default=0)
    chat_data_ultima_consulta = db.Column(db.DateTime, nullable=True)
    # Início do período de trial (null = sem trial); usado com FREEMIUM_TRIAL_DIAS em ConfigRegras
    trial_start_date = db.Column(db.DateTime, nullable=True)

    def set_password(self, password: str) -> None:
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')

    def verify_password(self, password: str) -> bool:
        if self.password_hash is None:
            return False
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)


class TermsOfUse(db.Model):
    """Termo de Uso vigente: PDF em app/static/terms/, um ativo por vez."""
    __tablename__ = "terms_of_use"
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False)
    upload_date = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    is_active = db.Column(db.Boolean, default=True, index=True)


class DeParaLogistica(db.Model):
    __bind_key__ = 'localidades'
    __tablename__ = 'de_para_logistica'
    id = db.Column(db.Integer, primary_key=True)
    uf_nome = db.Column(db.String(50))
    cidade_nome = db.Column(db.String(100))
    chave_busca = db.Column(db.String(200), unique=True)
    id_uf = db.Column(db.Integer)
    id_cidade = db.Column(db.Integer)

class FreteReal(db.Model):
    # Alterado para 'historico' para separar do banco de cidades
    __bind_key__ = 'historico' 
    __tablename__ = 'frete_real'
    
    id = db.Column(db.Integer, primary_key=True)
    data_emissao = db.Column(db.DateTime)
    id_cidade_origem = db.Column(db.Integer)
    id_cidade_destino = db.Column(db.Integer)
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
    __bind_key__ = 'leads'
    __tablename__ = 'leads'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    data_inscricao = db.Column(db.DateTime, default=db.func.current_timestamp())

class Pauta(db.Model):
    """Pauta para a Júlia. Scout/import preenchem; Verificador define status_verificacao. Só aprovadas vão para Julia."""
    __bind_key__ = 'noticias'
    __tablename__ = 'pautas'
    id = db.Column(db.Integer, primary_key=True)
    titulo_original = db.Column(db.String(500), nullable=False)
    fonte = db.Column(db.String(200))
    link = db.Column(db.String(500), unique=True, nullable=False, index=True)
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
    __bind_key__ = 'noticias'
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
    __bind_key__ = 'gerencial'
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
    __bind_key__ = 'gerencial'
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
    __bind_key__ = 'gerencial'
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
    __bind_key__ = 'gerencial'
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
    __bind_key__ = 'gerencial'
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
    __bind_key__ = 'gerencial'
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
    __bind_key__ = 'gerencial'
    __tablename__ = 'auditoria_gerencial'
    id = db.Column(db.Integer, primary_key=True)
    tipo_decisao = db.Column(db.String(50), nullable=False, index=True)  # orquestracao, dispatch, retry, purge_dados, purge_imagens, insight
    decisao = db.Column(db.String(255))
    contexto_json = db.Column(db.Text)
    resultado = db.Column(db.String(50))  # sucesso, falha, ignorado
    detalhe = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=utcnow_naive, index=True)


# --- Fase 5: Customer Insight (métricas por canal, recomendações estratégicas) ---

class InsightCanal(db.Model):
    """Métricas consolidadas por notícia/canal para retroalimentar estratégia (bind gerencial)."""
    __bind_key__ = 'gerencial'
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
    __bind_key__ = 'gerencial'
    __tablename__ = 'recomendacao_estrategica'
    id = db.Column(db.Integer, primary_key=True)
    contexto_json = db.Column(db.Text)
    recomendacao = db.Column(db.Text, nullable=False)
    prioridade = db.Column(db.Integer, default=5)
    status = db.Column(db.String(30), default='pendente', index=True)  # pendente | aplicada | descartada
    criado_em = db.Column(db.DateTime, default=utcnow_naive, index=True)