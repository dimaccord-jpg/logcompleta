# Log Completa

Aplicacao Flask com tres frentes principais:

- `Julia`: editorial, chat logistico, insights e artigos.
- `Cleiton`: governanca operacional, consumo por franquia, billing tecnico e validacao administrativa.
- `Roberto`: upload, BI e previsao de frete.

Este `README.md` e a fonte principal de contexto funcional e operacional do projeto.
Os demais guias devem existir apenas como anexos curtos de apoio, sem competir com este documento.

## Estado Atual Consolidado

Escopo atual confirmado:

- governanca operacional por franquia aplicada a fluxos reais;
- identidade de consumo por conta/franquia/usuario;
- billing tecnico com reconciliacao;
- painel admin operacional;
- pipeline editorial da Julia;
- chat da Julia com markdown seguro, sugestoes clicaveis e busca web contextual restrita;
- mensageria operacional centralizada do dominio Cleiton;
- CTA de upgrade parametrizado por ambiente via `PLANOS_UPGRADE_URL`;
- jornada de contratacao de plano iniciada pelo card `Pagamento` em `/perfil`;
- Roberto com upload oficial governado, persistencia temporaria dedicada, painel admin de controles e limites operacionais parametrizaveis.

## Regras Criticas do Sistema

Estas regras nao devem ser violadas em ajustes futuros:

- o endpoint `/api/chat_julia` continua sendo a rota oficial do chat;
- autorizacao operacional passa por `avaliar_autorizacao_operacao_por_franquia`;
- consumo e observabilidade de IA continuam passando pelo trilho oficial do Cleiton;
- nao criar fluxo paralelo para a Julia;
- nao criar fluxo paralelo para upload do Roberto;
- nao separar artificialmente frontend, autorizacao e governanca, porque o comportamento real depende do conjunto.

## Roberto Atual

Este bloco e a referencia canonica do estado atual do Roberto.

### Entrada oficial e governanca

- o upload de fretes continua entrando exclusivamente por `/api/roberto/upload`;
- a rota continua protegida por `avaliar_autorizacao_operacao_por_franquia`;
- nao existe rota paralela de recepcao de upload;
- billing e apropriacao continuam no trilho oficial do Cleiton;
- `rows_processed` continua refletindo o volume real validado do upload, antes do corte operacional de consumo analitico;
- a limpeza manual do payload continua passando por `/api/roberto/clear_upload`.

### Persistencia temporaria do upload

- o payload do upload nao fica mais inteiro na sessao Flask;
- a sessao guarda apenas uma referencia leve do upload temporario;
- os dados do upload ficam em persistencia temporaria dedicada em arquivo;
- o TTL do upload e parametrizavel por controle operacional;
- a validacao do TTL do upload ativo continua ocorrendo na leitura;
- a limpeza pesada de expirados nao roda em todo request comum do BI; ela e controlada por sweep com intervalo minimo.

### Estrategia operacional de limitacao

- o Roberto nao usa mais o hardcode antigo de `20` registros por mes como regra estrutural de entrada;
- o upload respeita `upload_total_max` como teto operacional global;
- quando o upload excede o teto, o corte busca preservar representatividade temporal por mes, em vez de manter apenas linhas mais recentes;
- a estrategia atual prioriza seguranca operacional e reducao de vies grosseiro, nao um "otimo estatistico final";
- o objetivo pratico atual e melhorar qualidade analitica sem abrir risco real de timeout.

### Controles operacionais do Roberto

A tela `/admin/agentes/roberto` agora e o centro oficial de calibracao manual do Roberto.

Parametros atualmente suportados:

- `upload_total_max`
- `previsao_meses`
- `min_linhas_mes_modelo`
- `min_linhas_uf_heatmap_ranking`
- `max_pontos_dispersao`
- `max_linhas_mes_modelo`
- `max_linhas_uf_heatmap`
- `max_linhas_uf_ranking`
- `upload_ttl_minutes`

Valores iniciais atuais do projeto:

- `upload_total_max = 10000`
- `previsao_meses = 18`
- `min_linhas_mes_modelo = 10`
- `min_linhas_uf_heatmap_ranking = 10`
- `max_pontos_dispersao = 500`
- `max_linhas_mes_modelo = 300`
- `max_linhas_uf_heatmap = 300`
- `max_linhas_uf_ranking = 300`
- `upload_ttl_minutes = 30`

Regras atuais de validacao de configuracao:

- `min_linhas_mes_modelo` nao pode ser maior que `max_linhas_mes_modelo`;
- `min_linhas_uf_heatmap_ranking` nao pode ser maior que `max_linhas_uf_heatmap`;
- `min_linhas_uf_heatmap_ranking` nao pode ser maior que `max_linhas_uf_ranking`;
- `upload_total_max` deve ser suficiente para `previsao_meses * min_linhas_mes_modelo`.

### Leitura analitica do BI

O BI do Roberto continua consumindo uma base unica por request, mas os controles operacionais agora diferenciam finalidades.

#### Previsao e serie temporal

- a previsao continua usando regressao linear sobre serie mensal suavizada por EMA;
- a janela historica usada no BI e na previsao e parametrizavel por `previsao_meses`;
- o historico do modelo pode ser limitado por `max_linhas_mes_modelo`;
- a qualidade do modelo usa `min_linhas_mes_modelo` como piso operacional relevante;
- o motor de previsao continua projetando 6 meses a frente.

#### Heatmap territorial

- o `heatmap` continua operando por UF de destino;
- a elegibilidade territorial usa `min_linhas_uf_heatmap_ranking`;
- o volume por UF no heatmap pode ser limitado por `max_linhas_uf_heatmap`;
- o heatmap nao compartilha mais o mesmo teto do ranking.

#### Ranking territorial

- o ranking por UF de destino continua calculando custo robusto por grupo;
- a elegibilidade por UF usa o mesmo piso territorial de `min_linhas_uf_heatmap_ranking`;
- o volume por UF do ranking e controlado separadamente por `max_linhas_uf_ranking`.

#### Dispersao

- o grafico de dispersao continua limitando pontos de exibicao;
- esse limite agora e parametrizavel via `max_pontos_dispersao`.

### Regras criticas do Roberto

Estas regras nao devem ser violadas em ajustes futuros:

- nao criar rota paralela para upload do Roberto;
- nao remover o Roberto do trilho oficial de billing e governanca do Cleiton;
- nao reintroduzir hardcode de amostragem fixa por mes como solucao estrutural;
- nao acoplar novamente ranking e heatmap ao mesmo limite sem justificativa explicita;
- nao guardar o payload completo do upload apenas em sessao HTTP;
- nao mover limpeza pesada de expirados para o caminho comum de leitura do BI;
- nao aumentar chamadas de IA para resolver problema analitico do upload.

## Experiencia Atual de Telas

Este bloco documenta o comportamento vigente de frontend relevante.
Nao houve mudanca de regra de governanca, autorizacao operacional por franquia, consumo, billing tecnico ou calculo do motor fora do que esta descrito neste README.

### Tela `/fretes`

- a rota `/fretes` esta acessivel para qualquer usuario autenticado;
- a consulta por rota (`UF + Cidade`, baseada em `frete_real`) continua visivel apenas para administradores;
- para usuario comum, a experiencia e focada no modulo de upload/BI;
- no usuario comum, continuam ocultos os blocos:
  - `Qualidade da base analisada`
  - `Recomendacoes`
  - `Custo medio (periodo)`
- no usuario comum, `Proporcao por modal` ocupa o slot lateral e o mapa aparece no final da pagina;
- com upload bem-sucedido, a UI informa:
  - quantidade recebida;
  - quantidade utilizada;
  - quantidade descartada por limite operacional;
- o titulo visual da serie do Roberto passou a ser:
  - `Evolucao do custo medio e previsao`

### Tela `/admin/agentes/roberto`

- a tela deixou de ser placeholder;
- ela agora e o painel manual de controles operacionais do Roberto;
- o objetivo da tela e calibrar limites operacionais do upload e da leitura analitica;
- mudancas nessa tela devem continuar respeitando:
  - o trilho oficial de upload;
  - o billing do Cleiton;
  - a observabilidade operacional;
  - a ausencia de rotas paralelas.

### Tela `/perfil`

- o card `Pagamento` foi transformado em bloco clicavel;
- o clique redireciona para `/contrate-um-plano`;
- a pagina `Contrate um Plano` continua com conteudo provisoria:
  - `Estamos construindo essa funcionalidade.`

## Governanca de Creditos e Planos

Este bloco consolida a regra oficial atual de creditos, franquia e onboarding.

### Fonte de verdade operacional

- o consumo real do sistema e governado por `Franquia`;
- os campos operacionais oficiais sao `Franquia.limite_total`, `Franquia.consumo_acumulado` e `Franquia.status`;
- a identidade de consumo continua baseada em `conta_id`, `franquia_id` e `usuario_id`;
- bloqueio, degradacao e autorizacao operacional sao decididos a partir da franquia vinculada ao usuario.

### Campo legado

- `User.creditos` e legado;
- ele nao governa saldo, abatimento, bloqueio ou autorizacao operacional;
- ele nao deve ser usado como fonte de verdade para consumo, UI de saldo, relatorio operacional ou regra nova.

### Regra oficial do plano Free

- todo novo usuario comercial `free` deve nascer com `Franquia.limite_total` numerico;
- esse limite nao deve ser hardcoded no codigo;
- a fonte canonica da referencia do plano e `plano_service.obter_limite_referencia_plano_admin(...)`;
- se a referencia administrativa do plano Free nao estiver configurada, o onboarding deve falhar explicitamente.

### Semantica de ilimitado

- `Franquia.limite_total = None` nao e comportamento permitido para novo usuario comercial;
- uso ilimitado e excecao legitima apenas para a estrutura interna/sistema;
- a estrutura interna reservada continua sendo:
  - `Conta.SLUG_SISTEMA = "sistema-interno"`
  - `Franquia.SLUG_SISTEMA_OPERACIONAL = "operacional-interno"`

### Observabilidade e validacao

- a leitura operacional da franquia continua sendo a base para UI e decisao de uso;
- o endpoint admin de inspecao operacional continua sendo `/admin/api/cleiton-franquia/<franquia_id>/validacao`;
- em incidente de saldo, bloqueio ou limite, revisar primeiro a `Franquia`, nao o `User`;
- em incidente de onboarding Free, validar nesta ordem:
  - configuracao administrativa do plano Free;
  - `franquia_id` do usuario criado;
  - `Franquia.limite_total`;
  - `Franquia.status`.

## Chat da Julia

Arquivos centrais:

- backend: `app/run_julia_chat.py`
- frontend: `app/templates/chat_julia.html`
- comportamento do chat: `app/static/js/chat_behavior.js`
- busca web contextual: `app/services/julia_web_search_service.py`
- prompts: `app/prompts.py`

Comportamento esperado hoje:

- `Enter` envia;
- `Shift+Enter` quebra linha;
- respostas da Julia renderizam markdown basico com seguranca;
- mensagens do usuario continuam em texto puro;
- sugestoes clicaveis podem disparar execucao direta da intencao sugerida;
- links uteis so devem ser exibidos quando houver aderencia clara ao tema da consulta;
- a mensagem visual de bloqueio operacional aceita markdown basico para exibir link de upgrade com clique;
- se a busca web falhar ou nao houver resultado confiavel, a resposta continua sem quebrar.

## Noticias, Insights e Artigos

Template principal de detalhe:

- `app/templates/noticia_interna.html`

Comportamento esperado:

- artigos continuam renderizando conteudo rico com `| safe` nas superficies legiveis;
- o botao inferior aponta para a home;
- o texto do botao e `Voltar Para Home`.

## Operacao Local

Pre-requisitos minimos:

- dependencias instaladas com `pip install -r requirements.txt`;
- `DATABASE_URL` valido;
- `APP_ENV` definido explicitamente;
- arquivo de ambiente baseado em `app/.env.example`.

Subida local:

```powershell
$env:APP_ENV="dev"
python -m app.web
```

Validacao minima recomendada:

1. autenticar no sistema;
2. validar `/api/chat_julia` com usuario permitido;
3. validar sugestao clicavel no chat;
4. validar pergunta tecnica com e sem links uteis;
5. validar upload Roberto:
   - enviar arquivo pequeno e confirmar sucesso;
   - confirmar mensagem de `recebidos`, `utilizados` e `descartados por limite operacional`;
   - validar limpeza do upload;
6. validar telas admin:
   - `/admin/dashboard`
   - `/admin/agentes/julia`
   - `/admin/agentes/cleiton`
   - `/admin/agentes/roberto`
7. em `/admin/agentes/roberto`, validar:
   - carregamento dos controles atuais;
   - salvamento de combinacoes validas;
   - mensagem clara ao tentar combinacao invalida;
8. validar `/fretes` em dois perfis:
   - admin: consulta `UF + Cidade` e bloco completo;
   - usuario comum: fluxo visual de upload/BI com blocos analiticos ocultos;
9. validar `/perfil`:
   - card `Pagamento` clicavel;
   - redirecionamento para `/contrate-um-plano`;
10. validar mensagens operacionais da franquia para status `degraded`, `blocked` e `expired`:
   - presenca do nome amigavel do plano;
   - presenca do link markdown de upgrade;
11. validar `PLANOS_UPGRADE_URL` por ambiente;
12. rodar testes principais:
   - `tests/test_roberto_controles.py`
   - `tests/test_cleiton_upload_billing_service.py`
   - `tests/test_franquia_operacao_autorizacao_service.py`

## Homologacao e Deploy

Status atual conhecido:

- a homologacao nao deve ser considerada concluida sem validar migrations, schema e fluxos reais;
- o Roberto faz parte desse checklist de homologacao com upload, BI, ranking e heatmap.

Bloqueios operacionais historicos relevantes:

- necessidade de confirmar `upgrade head` e `current` no ambiente alvo antes de qualquer go-live;
- nao considerar deploy valido sem persistencia adequada e variaveis corretas por ambiente.

## Guias Complementares

Use este `README.md` como fonte principal.
Os demais arquivos devem existir apenas como anexos curtos de apoio operacional:

- `app/README_RUN.md`: lembrete curto de subida local e comandos;
- `app/README_DEPLOY.md`: lembrete curto de sequencia de deploy;
- `app/GUIA_TEMPLATES_HTML.md`: padroes de templates/frontend;
- `migrations/README`: cadeia de migrations.

## Diretriz de Documentacao

Para evitar perda de conhecimento por excesso de fonte:

- este `README.md` deve permanecer como documento principal;
- os demais guias devem ser mantidos curtos, especializados e coerentes com ele;
- nenhuma regra funcional central deve existir apenas em anexo operacional;
- qualquer mudanca relevante no Roberto, na Julia, no frontend editorial ou na governanca operacional deve ser refletida primeiro aqui.
