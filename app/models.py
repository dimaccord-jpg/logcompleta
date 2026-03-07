from app.extensions import db
from flask_login import UserMixin
from datetime import datetime


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=True)  # None para usuários só OAuth (ex.: Google)
    full_name = db.Column(db.String(150), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    categoria = db.Column(db.String(50), default='free')
    creditos = db.Column(db.Integer, default=10)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login_at = db.Column(db.DateTime, nullable=True)
    subscribes_to_newsletter = db.Column(db.Boolean, default=False)
    usage_purpose = db.Column(db.String(50), nullable=True)
    job_role = db.Column(db.String(100), nullable=True)
    oauth_provider = db.Column(db.String(50), nullable=True)
    oauth_sub = db.Column(db.String(255), nullable=True)

    def set_password(self, password: str) -> None:
        from werkzeug.security import generate_password_hash
        self.password_hash = generate_password_hash(password, method='pbkdf2:sha256')

    def verify_password(self, password: str) -> bool:
        if self.password_hash is None:
            return False
        from werkzeug.security import check_password_hash
        return check_password_hash(self.password_hash, password)

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
    data_emissao = db.Column(db.DateTime(20))
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
    data_publicacao = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    def __repr__(self):
        return f"<{self.tipo.capitalize()}: {self.titulo_julia}>"