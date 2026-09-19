"""Provas independentes F4. Falhas expressam contratos esperados; não corrigem produto."""
from datetime import timedelta
import json
import pytest
from flask import session
from flask_login import login_user
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex
from app.extensions import db
from app.models import Conta, Franquia, User, ContaMultiuserConvite, ContaVinculoOrganizacional, utcnow_naive
from app.auth_services import register_user
from app.services import conta_multiuser_convite_service as svc
from app.services.conta_multiuser_capacidade_service import ocupar_assento
from app.services.conta_multiuser_errors import ConviteJaVinculadoError, ConviteContratanteProprioError, ConviteContratoIncompativelError
from tests.test_fase4_multiuser_convites import _preparar_conta_multiuser, _monetizacao_paga, _build_client, _login
from tests.conftest import seed_usuario

def setup_case():
    conta, owner = _preparar_conta_multiuser('audit-target', qtd=5, email='owner@audit.test')
    user, error = register_user('Audit Free', 'free@audit.test', 'Test-password-123!', accept_terms=True)
    assert error is None
    invite = svc.criar_convite(ator=owner, email_destino=user.email, enviar=False)
    return conta, owner, user, invite

def accept(user, invite):
    return svc.aceitar_convite(user=user, token=invite.token, csrf_token=svc.gerar_csrf_token_aceite(user.id, invite.convite_id))

def test_aud_f4_01_membro_sem_reserva(ctx):
    conta, owner, user, invite = setup_case()
    accept(user, invite)
    with pytest.raises(ConviteJaVinculadoError):
        svc.criar_convite(ator=owner, email_destino=user.email.upper(), enviar=False)
    assert ContaMultiuserConvite.query.filter_by(estado='pendente').count() == 0

def test_aud_f4_02_contratante_sem_reserva(ctx):
    conta, owner, user, invite = setup_case()
    count = ContaMultiuserConvite.query.count()
    with pytest.raises(ConviteContratanteProprioError):
        svc.criar_convite(ator=owner, email_destino=' '+owner.email.upper()+' ', enviar=False)
    assert ContaMultiuserConvite.query.count() == count

def test_aud_f4_03_signup_real(ctx):
    conta, owner, user, invite = setup_case()
    old_conta, old_fr = user.conta_id, user.franquia_id
    assert ContaVinculoOrganizacional.query.filter_by(user_id=user.id).count() == 0
    accept(user, invite)
    assert user.conta_id == conta.id and user.franquia_id == invite.franquia_id
    assert ContaVinculoOrganizacional.query.filter_by(user_id=user.id, estado='ativo').count() == 1
    assert User.query.filter_by(franquia_id=old_fr).count() == 0
    assert db.session.get(Conta, old_conta) and db.session.get(Franquia, old_fr)

@pytest.mark.parametrize('plano,futuro', [('starter','free'), ('pro','starter')])
def test_aud_f4_04_05_cutoff_futuro(ctx, plano, futuro):
    conta, owner, user, invite = setup_case()
    row = _monetizacao_paga(user.conta_id, plano)
    row.vigencia_externa_fim = utcnow_naive()+timedelta(days=10)
    row.snapshot_normalizado_json = json.dumps({'mudanca_pendente':True,'tipo_mudanca':'downgrade','plano_futuro':futuro,'efetivar_em':row.vigencia_externa_fim.isoformat()})
    db.session.commit()
    assert user.categoria == 'free'
    with pytest.raises(ConviteContratoIncompativelError):
        accept(user, invite)

@pytest.mark.parametrize('plano', ['starter','pro'])
def test_aud_f4_06_payment_failed_real(ctx, plano):
    from app.services.cleiton_monetizacao_service import processar_evento_stripe
    conta, owner, user, invite = setup_case()
    old_conta = user.conta_id
    row = _monetizacao_paga(old_conta, plano)
    user.categoria = plano
    db.session.commit()
    processar_evento_stripe({'id':'evt_audit_fail','type':'invoice.payment_failed','data':{'object':{
        'id':'in_audit_fail','customer':row.customer_id,'subscription':row.subscription_id,
        'billing_reason':'subscription_cycle','metadata':{'conta_id':str(old_conta),'franquia_id':str(user.franquia_id),'usuario_id':str(user.id),'plano_interno':plano}}}})
    db.session.refresh(row)
    assert row.ativo and row.status_contratual_externo == 'payment_failed'
    assert user.categoria == plano
    with pytest.raises(ConviteContratoIncompativelError):
        accept(user, invite)

def test_aud_f4_07_ordem_locks_e_sql(ctx, monkeypatch):
    conta, owner, user, invite = setup_case()
    ids=[]
    original=svc.bloquear_conta_para_capacidade
    def record(cid):
        ids.append(cid)
        return original(cid)
    monkeypatch.setattr(svc,'bloquear_conta_para_capacidade',record)
    expected=sorted([conta.id,user.conta_id])
    accept(user, invite)
    assert ids == expected
    for query in [svc._query_convite_lock(invite.convite_id),svc._query_user_lock(user.id),svc._query_franquia_lock(invite.franquia_id)]:
        assert 'FOR UPDATE' in str(query.statement.compile(dialect=postgresql.dialect()))

def test_aud_f4_08_reserva_propria(ctx):
    conta, owner, user, invite = setup_case()
    for i in range(3):
        svc.criar_convite(ator=owner,email_destino=f'other{i}@audit.test',enviar=False)
    accept(user,invite)
    assert ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id,estado='ativo').count()==2
    assert ContaMultiuserConvite.query.filter_by(conta_id=conta.id,estado='pendente').count()==3

def test_aud_f4_09_documento_julia_pos_transferencia(app, ctx, monkeypatch, tmp_path):
    import app.cleiton_doc_store as store
    from app.cleiton_doc_service import register_document_placeholder
    from app.julia_documents_routes import julia_documents_bp
    monkeypatch.setattr(store,'get_cleiton_doc_tmp_dir',lambda:str(tmp_path))
    conta, owner, user, invite = setup_case()
    client=_build_client(app)
    app.register_blueprint(julia_documents_bp)
    @app.route('/', endpoint='index')
    def index(): return 'index'
    with app.test_request_context('/'):
        login_user(user)
        record=register_document_placeholder(display_name='old-account.txt',extension='.txt',mime_type='text/plain',size_bytes=10,prepared_context='OLD ACCOUNT CONTENT')
        saved=dict(session)
    with client.session_transaction() as sess:
        sess.update(saved)
    response=client.post(f'/convite/{invite.token}/aceitar',data={'csrf_token':svc.gerar_csrf_token_aceite(user.id,invite.convite_id)})
    assert response.status_code==302
    assert user.conta_id==conta.id
    response=client.get('/api/julia/documents')
    assert response.status_code==200
    assert record['doc_id'] not in [r['doc_id'] for r in response.json['documents']]

def test_aud_f4_10_idor_http(app,ctx,monkeypatch):
    conta, owner, user, invite=setup_case()
    conta_b, owner_b=_preparar_conta_multiuser('audit-b',qtd=5,email='ownerb@audit.test')
    client=_build_client(app)
    _login(client,owner_b)
    sent=[]
    monkeypatch.setattr(svc,'send_email',lambda **kw:sent.append(kw))
    response=client.post(f'/api/multiuser/convites/{invite.convite_id}/reenviar',json={'csrf_token':svc.gerar_csrf_token_gestao_convite(owner_b.id),'conta_id':conta.id})
    assert response.status_code==404 and not sent

def test_aud_capacity_f2_reserva(ctx):
    conta, owner, user, invite=setup_case()
    for i in range(3): svc.criar_convite(ator=owner,email_destino=f'reserved{i}@audit.test',enviar=False)
    extra=Franquia(conta_id=conta.id,nome='Extra legada',slug='extra-audit',status='active')
    db.session.add(extra); db.session.commit()
    member=seed_usuario(extra.id,conta.id,email='legacy@audit.test',categoria='free')
    try:
        ocupar_assento(conta_id=conta.id,user_id=member.id,franquia_id=extra.id,papel='membro')
    except Exception:
        db.session.rollback()
    occupied=ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id,estado='ativo').count()
    reserved=ContaMultiuserConvite.query.filter_by(conta_id=conta.id,estado='pendente').count()
    assert occupied+reserved <= conta.quantidade_assentos_contratados

def test_aud_unknown_integrity_not_masked(ctx,monkeypatch):
    conta, owner, user, invite=setup_case()
    original=db.session.flush
    def flush(*args,**kwargs):
        if any(isinstance(o,ContaMultiuserConvite) for o in db.session.new):
            raise IntegrityError('INSERT',{},Exception('unknown constraint'))
        return original(*args,**kwargs)
    monkeypatch.setattr(db.session,'flush',flush)
    with pytest.raises(IntegrityError):
        svc.criar_convite(ator=owner,email_destino='unknown@audit.test',enviar=False)

def test_aud_email_failure_commit_retry(app,ctx,monkeypatch):
    conta, owner, user, invite=setup_case()
    client=_build_client(app); _login(client,owner)
    def fail(**kwargs): raise RuntimeError('provider unavailable')
    monkeypatch.setattr(svc,'send_email',fail)
    payload={'csrf_token':svc.gerar_csrf_token_gestao_convite(owner.id),'email':'delivery@audit.test'}
    response=client.post('/api/multiuser/convites',json=payload)
    assert response.status_code==500 and response.json['ok'] is False
    row=ContaMultiuserConvite.query.filter_by(email_destino=payload['email']).one()
    identity=(row.id,row.franquia_id,row.expires_at)
    sent=[]; monkeypatch.setattr(svc,'send_email',lambda **kwargs:sent.append(kwargs))
    response=client.post(f'/api/multiuser/convites/{row.id}/reenviar',json=payload)
    assert response.status_code==200 and len(sent)==1
    assert (row.id,row.franquia_id,row.expires_at)==identity

def test_aud_pg_partial_indexes(ctx):
    ddl=[str(CreateIndex(i).compile(dialect=postgresql.dialect())) for i in ContaMultiuserConvite.__table__.indexes if i.unique]
    assert len(ddl)==2 and all("WHERE estado = 'pendente'" in sql for sql in ddl)

def test_aud_capacity_admin_real(ctx):
    from app.services.user_plan_control_service import atribuir_plano_para_usuario
    conta, owner, user, invite=setup_case()
    for i in range(3): svc.criar_convite(ator=owner,email_destino=f'adminreserved{i}@audit.test',enviar=False)
    extra=Franquia(conta_id=conta.id,nome='Extra legada',slug='extra-admin-audit',status='active')
    db.session.add(extra); db.session.commit()
    member=seed_usuario(extra.id,conta.id,email='adminlegacy@audit.test',categoria='free')
    atribuir_plano_para_usuario(email=member.email,plano_raw='multiuser',quantidade_franquias_raw='5')
    occupied=ContaVinculoOrganizacional.query.filter_by(conta_id=conta.id,estado='ativo').count()
    reserved=ContaMultiuserConvite.query.filter_by(conta_id=conta.id,estado='pendente').count()
    assert occupied+reserved <= conta.quantidade_assentos_contratados

def test_aud_future_deleted_real(ctx):
    from app.services import cleiton_monetizacao_service as money
    conta, owner, user, invite=setup_case()
    row=_monetizacao_paga(user.conta_id,'starter')
    user.categoria='starter'
    future=utcnow_naive()+timedelta(days=10)
    fr=db.session.get(Franquia,user.franquia_id)
    fr.inicio_ciclo=utcnow_naive()-timedelta(days=20); fr.fim_ciclo=future
    money._registrar_mudanca_pendente_vinculo(conta_id=user.conta_id,plano_futuro='free',efetivar_em=future,origem=money.MUDANCA_PENDENTE_ORIGEM_USUARIO)
    db.session.commit()
    money.processar_evento_stripe({'id':'evt_future_deleted','type':'customer.subscription.deleted','data':{'object':{
        'id':row.subscription_id,'customer':row.customer_id,'status':'canceled',
        'metadata':{'conta_id':str(user.conta_id),'franquia_id':str(fr.id),'usuario_id':str(user.id)}}}})
    db.session.refresh(row); db.session.refresh(user)
    assert user.categoria=='starter' and fr.fim_ciclo==future
    with pytest.raises(ConviteContratoIncompativelError): accept(user,invite)

def test_aud_csrf_bound(ctx):
    conta,owner,user,invite=setup_case()
    csrf=svc.gerar_csrf_token_aceite(user.id,invite.convite_id)
    assert svc.validar_csrf_token_aceite(csrf,user.id,invite.convite_id)
    assert not svc.validar_csrf_token_aceite(csrf,owner.id,invite.convite_id)
    assert not svc.validar_csrf_token_aceite(csrf,user.id,invite.convite_id+1)

def test_aud_global_admin_no_authority(app,ctx):
    from app.services.conta_multiuser_errors import ConviteNaoAutorizadoError
    conta,owner,user,invite=setup_case()
    user.is_admin=True; user.categoria='multiuser'; db.session.commit()
    with pytest.raises(ConviteNaoAutorizadoError):
        svc.criar_convite(ator=user,email_destino='admin-target@audit.test',enviar=False)
    client=_build_client(app); _login(client,user)
    response=client.post('/api/multiuser/convites',json={'email':'admin-target@audit.test','csrf_token':svc.gerar_csrf_token_gestao_convite(user.id)})
    assert response.status_code==403

def test_aud_member_resend_denied_http(app,ctx):
    from app.services.conta_multiuser_errors import ConviteNaoAutorizadoError
    conta,owner,user,invite=setup_case(); accept(user,invite)
    other=svc.criar_convite(ator=owner,email_destino='resend@audit.test',enviar=False)
    with pytest.raises(ConviteNaoAutorizadoError): svc.reenviar_convite(ator=user,convite_id=other.convite_id,enviar=False)
    client=_build_client(app); _login(client,user)
    response=client.post(f'/api/multiuser/convites/{other.convite_id}/reenviar',json={'csrf_token':svc.gerar_csrf_token_gestao_convite(user.id)})
    assert response.status_code==403

def test_aud_email_pii_log(ctx,monkeypatch,caplog):
    import logging
    from types import SimpleNamespace
    import app.auth_services as auth
    monkeypatch.setenv('RESEND_API_KEY','audit-fake')
    monkeypatch.setattr(auth.requests,'post',lambda *a,**kw:SimpleNamespace(status_code=200))
    with caplog.at_level(logging.INFO):
        auth.send_email('private-recipient@audit.test','Convite','<p>invite</p>')
    assert 'private-recipient@audit.test' not in caplog.text

def test_aud_migration_fk_check_unique_and_pg():
    import importlib.util
    import io
    from pathlib import Path
    from sqlalchemy import create_engine
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext
    path=Path('migrations/versions/c4d5e6f7a8b9_fase4_multiuser_convites.py')
    spec=importlib.util.spec_from_file_location('audit_migration_f4',path)
    migration=importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
    engine=create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.exec_driver_sql('PRAGMA foreign_keys=ON')
        for table in ('conta','franquia','user'):
            conn.exec_driver_sql(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY)')
            conn.exec_driver_sql(f'INSERT INTO {table} VALUES (1), (2)')
        migration.op=Operations(MigrationContext.configure(conn))
        migration.upgrade()
        sql="INSERT INTO conta_multiuser_convite (id,conta_id,franquia_id,criado_por_user_id,email_destino,estado,expires_at) VALUES (?,?,?,?,?,?,?)"
        conn.exec_driver_sql(sql,(1,1,1,1,'one@audit.test','pendente','2030-01-01'))
        for args in [(2,1,1,1,'two@audit.test','pendente','2030-01-01'),(2,1,2,1,'one@audit.test','pendente','2030-01-01'),(2,99,2,1,'two@audit.test','pendente','2030-01-01'),(2,1,2,1,'two@audit.test','bad','2030-01-01')]:
            with pytest.raises(IntegrityError): conn.exec_driver_sql(sql,args)
        conn.exec_driver_sql("UPDATE conta_multiuser_convite SET estado='expirado' WHERE id=1")
        conn.exec_driver_sql(sql,(2,1,1,1,'one@audit.test','pendente','2030-01-01'))
        migration.downgrade(); migration.upgrade()
    output=io.StringIO()
    migration.op=Operations(MigrationContext.configure(dialect_name='postgresql',opts={'as_sql':True,'output_buffer':output}))
    migration.upgrade()
    ddl=output.getvalue()
    assert ddl.count("WHERE estado = 'pendente'")==2
    assert ddl.count('FOREIGN KEY')==4
