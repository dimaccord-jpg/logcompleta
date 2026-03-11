from app.web import app
from app.extensions import db
from app.models import AuditoriaGerencial

with app.app_context():
    auditorias = AuditoriaGerencial.query.all()
    for audit in auditorias:
        print("Decisão:", audit.tipo_decisao, "| Resultado:", audit.resultado, "| Data:", audit.created_at)
