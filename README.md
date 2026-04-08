# Log Completa

Aplicação Flask com três frentes principais:

- `Júlia`: editorial, chat logístico, insights e artigos.
- `Cleiton`: governança operacional, consumo por franquia, billing técnico e validação administrativa.
- `Roberto`: upload/BI e explicação assistida.

Este `README.md` é a fonte principal de contexto operacional e funcional do projeto.  
Os demais guias existem como anexos especializados e devem complementar este documento, não competir com ele.

## Estado Atual Consolidado

O código local está com o pacote funcional da Fase 2 integrado e com os ajustes recentes da experiência da Júlia já incorporados.

Escopo atual confirmado:

- governança operacional por franquia aplicada a fluxos reais;
- identidade de consumo por conta/franquia/usuário;
- billing técnico com reconciliação;
- painel admin operacional;
- pipeline editorial da Júlia;
- chat da Júlia com:
  - renderização segura de markdown básico no frontend;
  - suporte a `Shift+Enter` para quebra de linha;
  - sugestões clicáveis de continuidade;
  - execução direta de sugestões clicadas com contexto adicional;
  - busca web contextual restrita ao chat;
  - filtro de relevância para links úteis;
- detalhe de notícia/artigo com botão visual de retorno para home alinhado à paleta do site.

## Regras Críticas do Sistema

Estas regras não devem ser violadas em ajustes futuros:

- o endpoint `/api/chat_julia` continua sendo a rota oficial do chat;
- autorização operacional do chat passa por `avaliar_autorizacao_operacao_por_franquia`;
- consumo/observabilidade de IA continua passando por `cleiton_governed_generate_content`;
- não criar fluxo paralelo para o chat da Júlia;
- não separar artificialmente frontend, autorização e governança, porque o comportamento real depende do conjunto.

## Governança de Créditos e Planos

Este bloco consolida a regra oficial atual de créditos, franquia e onboarding.  
Mudanças futuras nesse domínio devem ser refletidas primeiro aqui.

### Fonte de verdade operacional

- o consumo real do sistema é governado por `Franquia`;
- os campos operacionais oficiais são `Franquia.limite_total`, `Franquia.consumo_acumulado` e `Franquia.status`;
- a identidade de consumo continua baseada em `conta_id`, `franquia_id` e `usuario_id`;
- bloqueio, degradação e autorização operacional são decididos a partir da franquia vinculada ao usuário.

### Campo legado

- `User.creditos` é legado;
- ele não governa saldo, abatimento, bloqueio ou autorização operacional;
- ele não deve ser usado como fonte de verdade para consumo, UI de saldo, relatório operacional ou regra nova;
- a remoção de `creditos=10` do onboarding faz parte da correção definitiva do bug do plano Free.

### Regra oficial do plano Free

- todo novo usuário comercial `free` deve nascer com `Franquia.limite_total` numérico;
- esse limite não deve ser hardcoded no código;
- a fonte canônica da referência do plano é `plano_service.obter_limite_referencia_plano_admin(...)`;
- se a referência administrativa do plano Free não estiver configurada, o onboarding deve falhar explicitamente;
- o sistema não deve mais criar usuário `free` com franquia aberta por omissão.

### Semântica de ilimitado

- `Franquia.limite_total = None` não é comportamento permitido para novo usuário comercial;
- uso ilimitado é exceção legítima apenas para a estrutura interna/sistema;
- a estrutura interna reservada continua sendo:
  - `Conta.SLUG_SISTEMA = "sistema-interno"`
  - `Franquia.SLUG_SISTEMA_OPERACIONAL = "operacional-interno"`
- qualquer fluxo novo que crie franquia comercial deve sair com limite explícito.

### Regra de onboarding após a correção

- cadastro local e cadastro Google criam `Conta` + `Franquia`;
- antes de persistir o usuário `free`, o onboarding lê o limite administrativo vigente do plano Free;
- a franquia do novo usuário nasce já com `limite_total` preenchido;
- novos usuários `free` não devem mais aparecer como `Ilimitado` por erro estrutural;
- mudança do limite do plano Free no admin afeta novos cadastros futuros sem hardcode no código.

### Observabilidade e validação

- a leitura operacional da franquia continua sendo a base para UI e decisão de uso;
- o endpoint admin de inspeção operacional continua sendo `/admin/api/cleiton-franquia/<franquia_id>/validacao`;
- em incidente de saldo, bloqueio ou limite, revisar primeiro a `Franquia`, não o `User`;
- em incidente de onboarding Free, validar nesta ordem:
  - configuração administrativa do plano Free;
  - `franquia_id` do usuário criado;
  - `Franquia.limite_total`;
  - `Franquia.status`;
- leitura operacional no endpoint admin.

## Chat da Júlia

Arquivos centrais:

- backend: `app/run_julia_chat.py`
- frontend: `app/templates/chat_julia.html`
- comportamento do chat: `app/static/js/chat_behavior.js`
- busca web contextual: `app/services/julia_web_search_service.py`
- prompts: `app/prompts.py`

Comportamento esperado hoje:

- `Enter` envia;
- `Shift+Enter` quebra linha;
- a dica visual de teclado não aparece na interface;
- respostas da Júlia renderizam markdown básico com segurança:
  - `**negrito**`
  - `*ênfase*`
  - listas simples iniciadas por `* `
  - links markdown;
- mensagens do usuário continuam em texto puro;
- sugestões clicáveis podem disparar execução direta da intenção sugerida;
- links úteis só devem ser exibidos quando houver aderência clara ao tema da consulta;
- se a busca web falhar ou não houver resultado confiável, a resposta continua sem quebrar e sem bloco de links.

Limites e bordas:

- o histórico do chat continua respeitando `get_julia_chat_max_history()`;
- os campos extras do payload do chat continuam opcionais;
- a busca web é exclusiva do chat da Júlia e não deve contaminar insights/artigos.

## Notícias, Insights e Artigos

Template principal de detalhe:

- `app/templates/noticia_interna.html`

Comportamento esperado:

- artigos continuam renderizando conteúdo rico com `| safe` dentro das superfícies legíveis;
- o botão inferior aponta para a home;
- o texto do botão é `Voltar Para Home`;
- o estilo do botão respeita a paleta visual atual do site.

## Operação Local

Pré-requisitos mínimos:

- dependências instaladas com `pip install -r requirements.txt`;
- `DATABASE_URL` válido;
- `APP_ENV` definido explicitamente;
- arquivo de ambiente baseado em `app/.env.example`.

Subida local:

```powershell
$env:APP_ENV="dev"
python -m app.web
```

Validação mínima recomendada:

1. autenticar no sistema;
2. validar `/api/chat_julia` com usuário permitido;
3. validar sugestão clicável no chat;
4. validar pergunta técnica com e sem links úteis;
5. validar upload Roberto;
6. validar telas admin;
7. validar uma página `/noticia/<id>`.

## Homologação e Deploy

Status atual conhecido:

- merge local concluído;
- publicação final de homolog ainda depende da estratégia de migrations no ambiente alvo;
- não considerar homolog concluída sem validar migrations e schema.

Bloqueio operacional histórico já documentado:

- problema com Alembic no runtime de homolog/Render;
- necessidade de confirmar `upgrade head` e `current` no ambiente alvo antes de qualquer go-live.

## Onde Consultar Assuntos Específicos

Use este `README.md` como visão principal.  
Consulte os documentos abaixo apenas quando precisar de profundidade operacional específica:

- `DIAGNOSTICO_HOMOLOG_PUBLICACAO.md`: status de homologação, go/no-go e bloqueios;
- `app/README_RUN.md`: execução local e checklist rápido;
- `app/README_DEPLOY.md`: sequência segura de deploy;
- `app/GUIA_TEMPLATES_HTML.md`: padrões de templates e frontend;
- `RENDER_CRON_HOMOLOG.md`: notas específicas de cron/Render;
- `migrations/README`: cadeia e operação de migrations.

## Diretriz de Documentação

Para evitar perda de conhecimento por excesso de fonte:

- este `README.md` deve permanecer como documento principal;
- os demais guias devem ser mantidos curtos, especializados e coerentes com ele;
- qualquer mudança relevante no chat da Júlia, no frontend editorial ou na governança operacional deve ser refletida primeiro aqui.
