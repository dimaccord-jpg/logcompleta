# Deploy em Homolog/Producao

Este arquivo e um anexo operacional curto.
Use o `README.md` da raiz como fonte principal do cenario atual.

## Sequencia Segura

1. publicar codigo no servico alvo;
2. validar variaveis de ambiente e persistencia;
3. garantir que o start do servico execute migration antes de subir web;
4. confirmar `current` e `head`;
5. validar health checks;
6. validar cron protegido;
7. validar fluxos reais principais:
   - chat da Julia
   - upload Roberto
   - `/admin/agentes/roberto`
   - `/fretes` para admin e usuario comum
   - `/perfil`

## Comando de Deploy (Render)

Padrao recomendado via infraestrutura versionada (`render.yaml`):

- Build Command: `bash ./build.sh`
- Start Command: `bash ./start.sh`

O `start.sh` executa obrigatoriamente:

1. `python -m flask --app app.web db upgrade`
2. `gunicorn --config gunicorn_config.py app.web:app`

Assim o schema sobe para `head` antes de aceitar requisicoes HTTP.

Importante:

- `build.sh` **nao** inicia servidor;
- `start.sh` e o unico ponto de start web;
- segredos nao devem ser versionados no `render.yaml` (usar `sync: false` e preencher no painel Render).

## Ambientes no render.yaml

- homologacao: branch `homolog`, `APP_ENV=homolog`;
- producao: branch `main`, `APP_ENV=prod`.

Se os nomes reais dos servicos forem diferentes no Render, ajuste apenas o campo `name`.

## Checklist operacional (Render)

1. validar sintaxe/logica do `render.yaml`;
2. confirmar env vars obrigatorias no painel (sem valores vazios);
3. fazer deploy de homologacao;
4. validar logs de startup: `db upgrade` antes do `gunicorn`;
5. confirmar revisao atual (`db current`) e ausencia de erro de tabela inexistente;
6. validar `/admin/planos`;
7. validar upload/ativacao de Politica de Privacidade;
8. validar rota publica `/politica-de-privacidade`;
9. validar fluxo de Termo de Uso sem regressao;
10. promover para producao e repetir validacoes criticas.

## Lembretes de Risco

- nao publicar sem migrations validas;
- nao quebrar o trilho oficial de governanca do Cleiton;
- nao tratar upload Roberto como homologado sem validar upload, BI, ranking e heatmap;
- nao usar este arquivo como fonte funcional principal.

## Referencia Principal

Qualquer mudanca funcional relevante deve ser refletida primeiro no `README.md` da raiz.
Mudancas de experiencia visual aprovadas tambem devem constar primeiro la, para evitar divergencia entre deploy e documentacao.
