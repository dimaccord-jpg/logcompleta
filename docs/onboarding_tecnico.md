# Onboarding Tecnico

Data de consolidacao: `2026-05-26`

## 1. Objetivo

Acelerar a entrada tecnica no projeto sem criar leituras paralelas da arquitetura.

## 2. Primeiros documentos

Ler nesta ordem:

1. `README.md`
2. `docs/estado_oficial_consolidado.md`
3. `docs/arquitetura_oficial.md`
4. `docs/runtime_ia_e_observabilidade.md`
5. `docs/troubleshooting_operacional.md`
6. `docs/guia_monetizacao_franquias.md`

## 3. Premissas de runtime

- `APP_ENV` e obrigatorio e aceita apenas `dev`, `homolog`, `prod`
- `DATABASE_URL` deve ser PostgreSQL
- `SECRET_KEY` forte e obrigatoria em `homolog` e `prod`
- `PUBLIC_BASE_URL` define a base publica oficial
- `APP_DATA_DIR` governa storage persistente via `settings.data_dir`

## 4. Modulos principais

- `app/web.py`: superficie publica e rotas web
- `app/cleide_routes.py`: namespace oficial da Cleide
- `app/run_julia_agente_pipeline.py`: pipeline editorial da Julia
- `app/run_julia_agente_imagem.py`: runtime de imagem da Julia
- `app/run_cleiton_agente_retencao.py`: retencao oficial
- `app/services/ia_metrics_service.py`: leitura agregada de metricas de IA

## 5. Contratos que nao podem ser quebrados

- nao criar rota paralela
- nao criar blueprint paralelo
- nao criar bypass operacional
- nao mascarar erro com fallback silencioso
- nao tirar consumo do trilho oficial do Cleiton
- nao alterar `NoticiaPortal.url_imagem` por rotina de retencao
- nao fazer share publico consumir IA

## 6. Ambientes

### Dev

- pode usar fallback de `SECRET_KEY` apenas em dev
- ainda exige `APP_ENV=dev`

### Homolog

- usa host homologico oficial configurado
- `PUBLIC_BASE_URL` precisa refletir o host de homolog
- deve validar contratos publicos antes de promover

### Producao

- base publica oficial: `https://www.agentefrete.com.br`
- canonical, `og:url` e share devem refletir essa base

## 7. Testes de entrada

Comando validado nesta consolidacao:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Resultado de referencia:

- `434 passed, 33 warnings`

## 8. Onde investigar cada dominio

- editorial e pipeline: `tests/test_julia_pipeline.py`
- imagem e retencao editorial: `tests/test_julia_imagem_fluxo.py`, `tests/test_web_media_generated.py`
- share e SEO: `tests/test_social_share.py`
- Cleide upload/chat/admin: `tests/test_cleide_*`
- dashboard e despublicacao: `tests/test_admin_dashboard_visual.py`, `tests/test_admin_despublicacao_editorial.py`

## 9. Saida esperada do onboarding

Ao fim do onboarding tecnico, a pessoa precisa conseguir:

- explicar a arquitetura oficial
- localizar as rotas oficiais de Julia, Roberto e Cleide
- descrever o trilho de governanca do Cleiton
- validar o contrato publico de share/SEO
- reconhecer onde fallback e permitido e onde e proibido
