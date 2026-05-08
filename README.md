# Log Completa

Aplicacao Flask com tres frentes principais:

- `Julia`: editorial, chat logistico, insights e artigos.
- `Cleiton`: governanca operacional, consumo por franquia, billing tecnico e validacao administrativa.
- `Roberto`: upload, BI, previsao de frete e chat analitico na `/fretes`.

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
- chat do Roberto na `/fretes`, com contexto analitico canônico do BI e memoria propria;
- mensageria operacional centralizada do dominio Cleiton;
- CTA de upgrade parametrizado por ambiente via `PLANOS_UPGRADE_URL`;
- jornada de contratacao de plano iniciada pelo card `Pagamento` em `/perfil`;
- Roberto com upload oficial governado, persistencia temporaria dedicada, painel admin de controles e limites operacionais parametrizaveis.
- ajustes recentes de experiencia visual consolidados sem alterar governanca, billing, autorizacao operacional e observabilidade.

## Regras Criticas do Sistema

Estas regras nao devem ser violadas em ajustes futuros:

- o endpoint `/api/chat_julia` continua sendo a rota oficial do chat;
- o endpoint `/api/chat_roberto` e a rota oficial do chat do Roberto na `/fretes`;
- autorizacao operacional passa por `avaliar_autorizacao_operacao_por_franquia`;
- consumo e observabilidade de IA continuam passando pelo trilho oficial do Cleiton;
- nao criar fluxo paralelo para a Julia;
- nao criar fluxo paralelo para upload do Roberto;
- nao criar fluxo paralelo para chat do Roberto;
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
- `chat_max_history`

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
- `chat_max_history = 10`

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

### Chat Roberto em `/fretes`

- frontend oficial: balao flutuante no canto da tela, com mensagens proativas quando fechado;
- experiencia visual recente do chat:
  - mensagem inicial orientativa antes do upload: `Realize o upload do arquivo para que possamos analisa-lo juntos.`
  - cada resposta do Roberto exibe acao discreta de `Copiar`;
  - a copia atua apenas no frontend, sobre o texto ja renderizado;
  - o feedback visual curto apos copia e `Copiado`;
- backend oficial: `app/run_roberto_chat.py` + endpoint `/api/chat_roberto` em `app/web.py`;
- regra de contexto do chat: uso exclusivo do upload ativo do proprio usuario;
- sem upload ativo: o chat responde de forma controlada e nao usa base ouro;
- autorizacao pre-consumo obrigatoria por `avaliar_autorizacao_operacao_por_franquia(current_user)`;
- toda chamada LLM do Roberto passa por `cleiton_governed_generate_content(...)` com:
  - `agent="roberto"`
  - `flow_type="roberto_chat_fretes"`
  - `api_key_label="GEMINI_API_KEY_ROBERTO"`
- chave exclusiva do chat do Roberto: `GEMINI_API_KEY_ROBERTO` (sem mistura com chaves da Julia);
- memoria propria do Roberto: `chat_max_history` (admin em `/admin/agentes/roberto`);
- janela de contexto aplicada em duas camadas:
  - frontend: recorte de historico enviado ao endpoint;
  - backend: recorte final antes da montagem do prompt;
- fonte de contexto: snapshot compacto derivado do motor oficial de BI (`app/roberto_bi.py`) em modo upload-only, sem fallback para base ouro e sem envio de dataset bruto ao modelo;
- observabilidade de processamento: montagem do snapshot do chat registrada em `ProcessingEvent` (`flow_type="roberto_chat_snapshot"`) sem duplicacao de eventos;
- observabilidade: consumo IA registrado em `IaConsumoEvento` via governanca Cleiton.

Regras adicionais de experiencia visual do chat Roberto:

- ajustes de UX do chat devem permanecer preferencialmente no frontend oficial (`app/templates/chat_roberto_fretes.html` e `app/static/js/chat_roberto_fretes.js`);
- melhorias visuais nao devem criar rota nova, callback de telemetria, evento tecnico adicional nem chamada extra ao modelo;
- acoes puramente visuais, como copiar resposta, nao devem gerar `ProcessingEvent`, `IaConsumoEvento` ou abatimento de franquia;
- qualquer ajuste visual deve preservar a autorizacao por franquia e o trilho oficial do Cleiton sem bifurcacao paralela.

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
- o chat do Roberto aparece na propria `/fretes` com identidade visual separada da Julia;
- quando ainda nao ha upload util para analise, a mensagem visual inicial do chat orienta o usuario a subir o arquivo antes de conversar;
- as respostas do Roberto podem ser copiadas diretamente no frontend, sem nova chamada de rede;
- o chat do Roberto e restrito ao universo analitico de fretes/logistica e pode gerar e-mail executivo sob solicitacao.

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
- a pagina `Contrate um Plano` executa a jornada oficial de contratacao Stripe embedded;
- os planos `starter` e `pro` aparecem com status de prontidao por configuracao admin;
- a inicializacao de checkout ocorre somente pelo endpoint oficial `/api/contratacao/stripe/iniciar`.

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

## Stripe e Governanca Cleiton

Este bloco registra o estado implantado do trilho Stripe integrado a governanca Cleiton.
Stripe fornece fatos externos; a operacao continua decidida por `Franquia`.

### O que o codigo confirma hoje

- a fonte de verdade operacional continua sendo `Franquia`, nao `User.creditos`;
- a autorizacao pre-consumo oficial continua em `avaliar_autorizacao_operacao_por_franquia`;
- o estado operacional lido pela aplicacao continua vindo de `ler_franquia_operacional_cleiton`;
- a auditoria administrativa oficial continua exposta em `/admin/api/cleiton-franquia/<franquia_id>/validacao`;
- existe export oficial somente leitura do CSV de auditoria de clientes em `/admin/dashboard/auditoria-clientes.csv`;
- os limites comerciais continuam sendo parametrizados no admin via `plano_service` e `/admin/planos/saas/salvar`;
- a jornada visual de pagamento agora usa fluxo white label embedded na tela oficial `/contrate-um-plano`;
- existe endpoint autenticado oficial para iniciar checkout Stripe (`/api/contratacao/stripe/iniciar`);
- existe endpoint oficial de webhook Stripe (`/api/webhook/stripe`) com verificacao de assinatura e idempotencia por evento;
- existe preparacao de persistencia canonica em `Conta` para vinculo comercial externo e fatos append-only de monetizacao;
- fatos Stripe relevantes passam a ser correlacionados e persistidos em `MonetizacaoFato`, com pendencia explicita quando faltar correlacao inequívoca;
- eventos pendentes de correlacao podem ser reprocessados apenas por acao administrativa explicita no endpoint oficial de validacao (`acao=reprocessar_pendencias_correlacao`), sem mutacao automatica em leitura;
- o reprocessamento administrativo reutiliza o pipeline oficial de correlacao/processamento do Cleiton, com idempotencia por evento e trilha auditavel no proprio fato interno;
- a auditoria admin destaca divergencias relevantes entre ultimo fato interno com efeito e vinculo comercial externo ativo, mantendo o fato interno como referencia primaria;
- aplicacao contratual sobre `Franquia` continua mediada pela camada central do Cleiton.

### Contrato operacional vigente

- o trilho oficial de contratacao permanece em `/api/contratacao/stripe/iniciar`;
- o trilho oficial de webhook permanece em `/api/webhook/stripe`, com assinatura e idempotencia;
- o trilho oficial de auditoria/reprocessamento admin permanece em `/admin/api/cleiton-franquia/<franquia_id>/validacao`;
- o trilho oficial de exportacao administrativa consolidada para auditoria local permanece em `/admin/dashboard/auditoria-clientes.csv`;
- nenhum efeito operacional de plano ignora o Cleiton: aplicacao contratual converge para `Franquia`;
- `User.creditos` permanece legado e fora da governanca operacional.

### Risco residual conhecido no trilho Stripe

- para `starter` e `pro`, `garantir_ciclo_operacional_franquia` usa renovacao mensal aproximada e marca a pendencia `renovacao_recorrente_aproximada_sem_data_pagamento`;
- ainda podem existir eventos Stripe sem correlacao inequívoca de `conta_id` e `franquia_id`; nesses casos o fato e registrado como pendente sem efeito operacional;
- ciclos contratuais sem dados Stripe confiaveis continuam dependentes de fallback interno como excecao controlada;
- o motor operacional continua sem depender de Stripe para autorizacao pre-consumo e governanca de consumo.

### Restricoes permanentes do dominio

- manter Stripe como origem de fatos, nunca como fonte operacional final;
- manter governanca operacional e autorizacao no Cleiton sem rota paralela;
- manter identidade operacional por `conta_id` + `franquia_id` + `usuario_id`;
- manter parametrizacao de planos sem hardcode de ids/valores externos;
- manter reflexo contratual auditavel em `Franquia` e fatos internos append-only.
 
### Complementos operacionais estabilizados

- existe conciliacao explicita do retorno do checkout por `conciliar_checkout_session_stripe(...)` no fluxo de `/contrate-um-plano`;
- existe rotina oficial de virada por `efetivar_mudancas_pendentes_ciclo(...)`, executada em `/cron/executar-cleiton`;
- o trilho oficial de conciliacao do retorno permanece em `/contrate-um-plano?checkout=success&session_id=...`, sempre com `session_id` real;
- o trilho oficial de virada de ciclo permanece em `/cron/executar-cleiton`, protegido por `CRON_SECRET`;
- o padrao operacional de cron em homolog/prod e `curl -fsS -X POST "$APP_BASE_URL/cron/executar-cleiton" -H "X-Cron-Secret: $CRON_SECRET"` e `curl -fsS -X POST "$APP_BASE_URL/cron/billing-snapshot" -H "X-Cron-Secret: $CRON_SECRET"`;
- todo usuario nasce em `free`;
- os planos pagos no estado atual sao `starter` e `pro`;
- `free` nao existe como `price` no Stripe;
- downgrade para `free` significa `cancel_at_period_end=true` na assinatura atual e troca interna posterior pela virada de ciclo;
- downgrade pago `pro -> starter` altera a assinatura existente e posterga o efeito operacional para a virada;
- checkout novo so e permitido quando a conta nao possui assinatura Stripe ativa canonica.

### Persistencia canonica de monetizacao

Entidade principal:

- `ContaMonetizacaoVinculo`

Campos que a equipe deve auditar primeiro:

- `customer_id`
- `subscription_id`
- `price_id`
- `plano_interno`
- `status_contratual_externo`
- `ativo`
- `snapshot_normalizado_json`

Regras importantes:

- existe no maximo um vinculo `ativo=true` por conta;
- o historico da conta e preservado em vinculos antigos;
- a pendencia de mudanca fica no `snapshot_normalizado_json`;
- os campos de snapshot mais importantes nos downgrades atuais sao `mudanca_pendente`, `tipo_mudanca`, `plano_futuro` e `efetivar_em`;
- a trilha append-only e auditavel fica em `MonetizacaoFato`.

### CSV administrativo de auditoria de clientes

Endpoint oficial:

- `GET /admin/dashboard/auditoria-clientes.csv`

Escopo e governanca:

- rota somente leitura;
- protegida pelo mesmo mecanismo admin do dashboard (`login_required` + verificacao de admin);
- sem chamada Stripe online;
- sem mutacao de plano, franquia, vinculo, consumo ou fatos;
- sem rota paralela fora do admin;
- download filtrado pelos mesmos filtros do dashboard (`categoria`, `franquia_status`, `cancelado`).

Objetivo:

- auditar o estado local consolidado por usuario/conta/franquia;
- detectar divergencias entre legado, contrato e operacao;
- inspecionar coerencia entre `ContaMonetizacaoVinculo`, `MonetizacaoFato`, `Franquia` e configuracao administrativa local;
- apoiar auditoria financeira local e investigacao administrativa sem aplicar correcao automatica.

Fontes de verdade expostas no CSV:

- `plano_usuario_legacy`: espelho de `User.categoria`, apenas legado;
- `plano_contratual_vinculo`: plano local contratado vindo do vinculo comercial exibido;
- `plano_operacional_resolvido`: plano operacional resolvido pelo Cleiton para a franquia;
- `status_operacional_franquia`: status persistido da `Franquia`;
- `status_operacional_recalculado`: status recalculado em leitura por `classificar_estado_operacional_franquia(...)`.

Regras importantes do CSV:

- `User.categoria` nao e fonte de verdade financeira principal;
- `flag_plano_user_vs_vinculo` e mantida apenas como alias/legado para leitura historica;
- a severidade nao deve considerar divergencia legacy isolada como prova financeira conclusiva;
- o CSV distingue `ultimo_fato_geral`, `ultimo_fato_efeito` e `ultimo_fato_relevante`;
- fatos relevantes de auditoria sao explicitamente listados no servico e nao dependem mais de substring ampla generica;
- a validacao de `price_id` do plano contratado usa configuracao administrativa local em `ConfigRegras`;
- a identificacao de plano pago no CSV usa configuracao dinamica (`plano_valor_admin_*` e `plano_gateway_price_id_admin_*`) com fallback defensivo apenas quando a base admin estiver vazia.

Confiabilidade do vinculo exibido:

- `vinculo_exibido` e a linha mostrada no CSV para inspecao humana;
- `vinculo_confiabilidade` classifica o estado do vinculo como `confiavel`, `inconclusivo`, `ambiguo` ou `ausente`;
- `vinculo_confiabilidade_conclusiva=true` exige ativo unico coerente, sem conflito relevante, sem pendencia perdida e sem problema local de `price_id`;
- historico multiplo de `customer_id` ou `subscription_id` impede conclusao automatica forte e tende a rebaixar o vinculo para `inconclusivo`;
- multiplos vinculos ativos na mesma conta sao tratados como ambiguidade critica.

Pendencia perdida e resolucao:

- o CSV pode destacar pendencia historica em vinculo desativado;
- a neutralizacao de `flag_pendencia_perdida_vencida` exige correlacao forte com fato posterior local;
- a correlacao usa janela maxima de resolucao, ids externos compativeis e tipo de fato conhecido de efetivacao;
- fato posterior fora da janela ou sem ids compativeis nao deve limpar a flag.

Severidade:

- `nivel_risco_auditoria` resume a leitura como `ok`, `atenção` ou `crítico`;
- divergencia isolada entre status persistido e status recalculado fica em `atenção`;
- divergencia operacional combinada com evidencia forte de billing/contrato pode subir para `crítico`;
- `flag_requer_revisao_manual` resume se a linha exige triagem administrativa.

### Assinatura canonica e prevencao de multiplas assinaturas

Houve bug anterior de abertura indevida de checkout para conta que ja tinha assinatura. O comportamento correto agora e:

- `_obter_assinatura_stripe_ativa(...)` tenta primeiro o vinculo ativo da conta;
- se o vinculo ativo estiver contaminado, cancelado ou insuficiente, tenta o historico da mesma conta;
- depois disso, ainda existe fallback por `customer_id` na Stripe;
- se ja existe assinatura ativa canonica para a conta, nao abrir novo checkout pago;
- upgrade e downgrade pago devem atualizar a assinatura existente;
- webhook e conciliacao nao devem promover vinculo inconsistente quando `customer_id` ou `subscription_id` divergirem do vinculo ativo.

Na leitura administrativa local via CSV:

- vinculo unico nao deve ser tratado automaticamente como prova absoluta se houver conflito relevante local;
- historico multiplo de ids ou divergencia com fato relevante pode marcar o vinculo como `inconclusivo`;
- a confiabilidade local do vinculo exportado e um sinal administrativo, nao substitui a necessidade de conferir os fatos internos quando houver risco alto.

### Fluxos de monetizacao suportados hoje

#### `free -> starter`

- Stripe:
  - abre checkout embedded de nova assinatura;
  - usa `price_id` do plano pago selecionado.
- Banco:
  - registra `stripe_checkout_session_created`;
  - nao altera a franquia operacional apenas por abrir checkout.
- UI:
  - a tela `/contrate-um-plano` recebe o embedded checkout;
  - no retorno, a conciliacao usa `session_id` valido.

#### `starter -> pro`

- Stripe:
  - reaproveita a assinatura existente da conta;
  - atualiza a subscription sem abrir checkout novo;
  - usa `proration_behavior=none`.
- Banco:
  - sincroniza `ContaMonetizacaoVinculo`;
  - registra `stripe_subscription_plan_modify_requested`.
- UI:
  - nao depende de uma segunda assinatura para fazer upgrade.

#### `pro -> starter`

- Stripe:
  - nao cria nova assinatura;
  - altera a assinatura existente para o `price` de `starter`.
- Banco:
  - registra `mudanca_pendente=true`;
  - registra `plano_futuro=starter`;
  - registra `efetivar_em`;
  - mantem o plano operacional atual ate a virada.
- UI:
  - deve tratar como downgrade agendado, nao imediato.

#### `starter/pro -> free`

- Stripe:
  - nao existe `price` free;
  - encontra a assinatura ativa canonica;
  - envia `cancel_at_period_end=true`.
- Banco:
  - registra `mudanca_pendente=true`;
  - registra `plano_futuro=free`;
  - registra `efetivar_em`;
  - mantem o plano atual operacional ate a virada.
- UI:
  - deve comunicar cancelamento ao fim do periodo, nao remocao imediata do acesso pago.

### Papel do Stripe vs papel do sistema interno

- Stripe:
  - `customer`
  - `subscription`
  - `price`
  - status contratual externo
  - periodo de vigencia externa
  - webhooks e retorno do checkout
- Aplicacao:
  - `Franquia` como estado operacional
  - plano efetivamente vigente no uso
  - limite, consumo e status de acesso
  - pendencias internas de mudanca
  - efetivacao final na virada de ciclo

### Webhook, conciliacao e virada de ciclo

- webhook oficial: `/api/webhook/stripe`
- conciliacao oficial do retorno: `conciliar_checkout_session_stripe(session_id)`
- rotina de efetivacao interna: `efetivar_mudancas_pendentes_ciclo(...)`
- cron oficial: `/cron/executar-cleiton`

Comportamento esperado:

- o webhook recebe fatos externos assinados, aplica idempotencia e registra fatos internos;
- a conciliacao de checkout busca a `checkout session` e, quando existir, a `subscription` correspondente para fechar o fluxo do retorno do usuario;
- `checkout.session.completed` sozinho nao deve ser tratado como prova suficiente de efeito operacional final;
- a virada de ciclo e quem efetiva internamente downgrades pendentes para `starter` e `free`;
- o Stripe informa o encerramento ou a troca contratual, mas a aplicacao continua responsavel pela transicao operacional final.

## Observabilidade e Auditoria do Cleiton

Este bloco consolida o que Cleiton ja rastreia hoje e o que precisa permanecer como trilho oficial em qualquer evolucao.

### Fontes oficiais de observabilidade

- `IaConsumoEvento`: tentativa real de chamada LLM, com `provider`, `operation`, `model`, `agent`, `flow_type`, `api_key_label`, tokens, status e identidade `conta_id`/`franquia_id`/`usuario_id`;
- `ProcessingEvent`: processamento nao-LLM, com `agent`, `flow_type`, `processing_type`, `rows_processed`, `processing_time_ms`, status e identidade completa;
- `CleitonBillingApropriacao`: apropriacao idempotente de billing tecnico, hoje usada no upload Roberto;
- `Franquia`: estado operacional persistido consumido pela autorizacao em tempo de uso;
- `IaBillingCostSnapshot`: snapshot de custo real para dashboard operacional;
- `AuditoriaGerencial`: trilha gerencial de decisoes do Cleiton fora da camada direta de franquia.

### Funcoes operacionais do Cleiton rastreadas no codigo

- autorizacao pre-consumo: `avaliar_autorizacao_operacao_por_franquia`;
- mensageria operacional para UI: `montar_mensagem_operacao`;
- leitura operacional consolidada: `ler_franquia_operacional_cleiton`;
- classificacao de estado por plano, vigencia, limite e bloqueio manual: `classificar_estado_operacional_franquia`;
- inicializacao e leitura de ciclo operacional: `garantir_ciclo_operacional_franquia` e `ler_ciclo_vigente`;
- reconciliacao entre consumo persistido e eventos abataveis: `reconciliar_franquia_cleiton`;
- pacote administrativo de auditoria: `obter_pacote_validacao_franquia_cleiton`;
- apropriacao idempotente de billing tecnico do Roberto: `apropriar_billing_upload_roberto`;
- parametrizacao comercial de planos e franquias de referencia: `plano_service`.

### O que o endpoint administrativo de validacao entrega

Endpoint oficial:

- `/admin/api/cleiton-franquia/<franquia_id>/validacao`

Capacidades atuais:

- leitura operacional da franquia;
- reconciliacao entre `Franquia.consumo_acumulado` e soma recalculada dos eventos abataveis;
- lista de pendencias do plano/ciclo;
- contexto monetario da conta (vinculo comercial externo + fatos recentes de monetizacao);
- pendencias explicitas de configuracao Stripe para `starter` e `pro` (sem bloqueio operacional);
- opcao de sincronizar ciclo na leitura;
- opcao administrativa explicita de aplicar correcao de reconciliacao.

### O que o CSV administrativo de auditoria entrega

- linha por usuario com contexto de `conta` e `franquia`;
- separacao explicita entre legado (`User.categoria`), contrato local (`ContaMonetizacaoVinculo`) e operacao (`Franquia`);
- status persistido e status recalculado da franquia;
- vinculo exibido, nivel de confiabilidade do vinculo e motivo da classificacao;
- ultimo fato geral, ultimo fato com efeito operacional e ultimo fato relevante de auditoria;
- flags de price/config local, pendencia perdida, ids entrelacados, historico multiplo e coerencia operacional;
- severidade final local (`nivel_risco_auditoria`) para triagem administrativa.

### Regra obrigatoria para futuras monetizacoes

- nao criar trilha de observabilidade separada para pagamento fora do dominio Cleiton;
- nao liberar acesso apenas por retorno de frontend;
- nao decidir status operacional com base em campo legado ou sessao;
- nao atualizar consumo, limite ou status fora da entidade `Franquia`;
- nao ocultar do admin os fatos necessarios para auditar ativacao, renovacao, falha ou cancelamento.

## Papel dos Documentos

Este bloco resume o papel de cada documento relevante para a equipe.

- `README.md`: documento principal do produto, da governanca Cleiton e do estado funcional vigente;
- `app/README_RUN.md`: lembrete curto de execucao local;
- `app/README_DEPLOY.md`: lembrete curto de deploy;
- `app/GUIA_TEMPLATES_HTML.md`: referencia curta de comportamento visual e templates;
- `migrations/README`: ordem e criticidade da cadeia de migrations;
- `DIAGNOSTICO_HOMOLOG_PUBLICACAO.md`: fotografia operacional de homologacao/publicacao;
- `SECURITY_SECRETS.md` e `scripts/security/*`: rotinas e guardrails de segredos/rotacao;
- `app/.env.example`: contrato de configuracao por ambiente, incluindo URL de upgrade e variaveis operacionais.

## Estado de Encerramento da Implantacao Stripe/Cleiton

Encerramento funcional confirmado no codigo:

- contratacao Stripe em jornada embedded oficial na tela `/contrate-um-plano`;
- webhook Stripe oficial com validacao de assinatura, idempotencia e correlacao;
- reprocessamento admin explicito para pendencias de correlacao no endpoint de validacao;
- auditoria monetaria consolidada no pacote admin com contexto de vinculo/fatos/divergencias;
- aplicacao contratual sobre `Franquia` preservando Cleiton como camada central.

## Prompt de Auditoria Externa (Opcional)

Se precisar de segunda opiniao em modo somente leitura, usar prompt de auditoria focado no estado implantado:

- confirmar aderencia dos endpoints oficiais `/api/contratacao/stripe/iniciar`, `/api/webhook/stripe` e `/admin/api/cleiton-franquia/<franquia_id>/validacao`;
- confirmar que `Franquia` permanece fonte operacional unica e que `User.creditos` segue legado;
- confirmar observabilidade/auditoria no trilho Cleiton sem rotas paralelas;
- listar apenas riscos residuais e pendencias operacionais objetivas, sem ampliar escopo.

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
   - `/admin/dashboard/auditoria-clientes.csv`
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
   - em ambos: validar Chat Roberto (abrir/fechar, sugestao proativa, mensagem normal e pedido de e-mail executivo);
9. validar `/perfil`:
   - card `Pagamento` clicavel;
   - redirecionamento para `/contrate-um-plano`;
10. validar mensagens operacionais da franquia para status `degraded`, `blocked` e `expired`:
   - presenca do nome amigavel do plano;
   - presenca do link markdown de upgrade;
11. validar `PLANOS_UPGRADE_URL` por ambiente;
12. rodar a suite critica local por dominio:
   - Stripe/monetizacao: contratacao, guardrails, blockers, upgrade/downgrade e vinculo comercial;
   - cron seguro: autenticacao por `X-Cron-Secret`, `/cron/executar-cleiton`, `/cron/finance` e `/cron/billing-snapshot`;
   - franquia/autorizacao: classificacao operacional, bloqueio, degradacao e CTA de upgrade;
   - billing/auditoria: reconciliacao, apropriacao de billing e export administrativo de auditoria;
   - Roberto: controles de upload/TTL/BI e rotas principais;
13. rodar suite completa local: `python -m pytest -q`.

## Politica de Testes

- testes nao fazem parte do deploy de runtime em homologacao ou producao;
- testes rodam apenas localmente, usando dependencias de desenvolvimento;
- a suite critica local deve proteger no minimo:
  - Stripe e monetizacao;
  - cron seguro;
  - franquia e autorizacao operacional;
  - billing, reconciliacao e auditoria;
- remocao de testes so deve ocorrer com evidencia objetiva de obsolescencia, substituicao ou consolidacao;
- quando nomes de arquivos mudarem, a documentacao deve priorizar dominios e categorias em vez de listas fixas de arquivos.

## Homologacao e Deploy

Pre-condicao explicita de fechamento:

- homologacao Stripe so pode ser considerada concluida com evidencias objetivas de credenciais e webhook no ambiente de homolog (`STRIPE_API_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET`, endpoint `/api/webhook/stripe` ativo e assinado).

### Checklist final de homologacao

- migrations aplicadas no alvo com `upgrade head` e `current` coerentes;
- fluxo `/api/contratacao/stripe/iniciar` validado com usuario autenticado;
- fluxo `/api/webhook/stripe` validado com assinatura correta e replay idempotente;
- retorno `/contrate-um-plano?checkout=success&session_id=...` validado com `session_id` real e conciliacao positiva;
- fluxo `/admin/api/cleiton-franquia/<franquia_id>/validacao` validado em leitura e reprocessamento admin;
- fluxo `/admin/dashboard/auditoria-clientes.csv` validado em leitura, protecao admin, filtros, severidade e download sem efeitos colaterais;
- painel `/contrate-um-plano` validado com plano pronto e retorno de erro contratual quando plano estiver pendente;
- trilho Roberto validado (upload, BI, ranking/heatmap e limpeza de upload temporario).

### Comando de start com migration obrigatoria

Para evitar subir aplicacao com schema defasado (ex.: tabela nova ausente), o start do servico web deve executar migration antes do Gunicorn.

- Build Command (Render): `bash ./build.sh`
- Start Command (Render): `bash ./start.sh`
- Infra versionada: `render.yaml` na raiz (homolog + prod)

Fluxo do `start.sh`:

1. `python -m flask --app app.web db upgrade`
2. `gunicorn --config gunicorn_config.py app.web:app`

Regras operacionais:

- `build.sh` nao pode subir servidor web;
- segredos (`DATABASE_URL`, `SECRET_KEY`, Stripe, Resend, OAuth, tokens) nao devem ser versionados;
- no Render, manter essas variaveis no painel com `sync: false` no `render.yaml`.

Validacao minima de startup no deploy:

1. logs mostram `db upgrade` antes do `gunicorn`;
2. `python -m flask --app app.web db current` retorna revisao esperada;
3. `/admin/planos` abre sem erro de tabela;
4. `/politica-de-privacidade` responde (404 sem politica ativa e comportamento esperado).

Checklist operacional para homologacao/producao:

1. validar `render.yaml` versionado;
2. conferir env vars por ambiente no Render (homolog x prod);
3. deploy em homolog;
4. validar logs e schema;
5. validar `/admin/planos` e upload da politica;
6. validar trilho de termo sem regressao;
7. validar cron protegido;
8. promover para producao e repetir validacoes criticas.

### Checklist de prontidao para producao

- variaveis de ambiente obrigatorias revisadas por ambiente (`APP_ENV`, `DATABASE_URL`, segredos Stripe, `APP_DATA_DIR`, `CRON_SECRET`);
- persistencia de dados confirmada para `APP_DATA_DIR` e artefatos temporarios operacionais;
- observabilidade habilitada para `IaConsumoEvento`, `ProcessingEvent`, `MonetizacaoFato` e pacote admin de validacao;
- export administrativo de auditoria de clientes validado com base local coerente (`ContaMonetizacaoVinculo`, `MonetizacaoFato`, `Franquia`, `ConfigRegras`);
- monitoramento de webhook com alarmes para falhas 4xx/5xx e volume anomalo de pendencias de correlacao;
- procedimento de suporte documentado para reconciliacao e reprocessamento sem mutacao manual fora do trilho oficial.

### Riscos residuais aceitaveis

- eventos Stripe sem correlacao inequívoca ficam pendentes sem efeito operacional ate acao admin;
- fallback interno de ciclo pode ser usado como excecao controlada quando evento nao trouxer ciclo confiavel;
- o CSV de auditoria continua sendo auditoria financeira local, nao prova externa online em tempo real;
- vinculo local ainda pode exigir revisao humana quando o proprio historico interno estiver contaminado;
- warning de biblioteca externa (`flask_session` deprecado) sem impacto funcional imediato na implantacao Stripe/Cleiton.

### Riscos residuais nao aceitaveis

- qualquer bypass que altere consumo/status sem passar por `Franquia` e camada central Cleiton;
- qualquer rota paralela para contratacao/webhook/governanca fora dos endpoints oficiais;
- qualquer export ou rotina admin que trate `User.categoria` isoladamente como prova financeira conclusiva;
- homologacao declarada sem evidencia de segredo de webhook e validacao de assinatura;
- deploy sem migrations aplicadas ou sem persistencia operacional configurada.

### Criterio objetivo de encerramento definitivo

- `tests/test_roberto_controles.py` verde sem `PermissionError` de `tmp_path` no ambiente local;
- suite critica local verde nos dominios Stripe/monetizacao, cron seguro, franquia/autorizacao e billing/auditoria;
- suite completa do projeto verde no ambiente local;
- trilho oficial preservado sem novos endpoints paralelos;
- documentacao e contrato de ambiente atualizados para refletir estado implantado real.

## Troubleshooting Stripe e Operacao

Este bloco concentra os incidentes reais ja enfrentados e a forma correta de investigar.

### Webhook local nao chegando

- confirmar que o endpoint publicado e `/api/webhook/stripe`;
- confirmar que o `cloudflared` atual e o mesmo endpoint cadastrado no Stripe;
- sempre que a URL publica do tunel mudar, atualizar o endpoint de webhook no dashboard Stripe;
- validar se o evento foi enviado para o ambiente correto: `test` e `live` sao separados;
- em erro `400`, revisar `Stripe-Signature` e `STRIPE_WEBHOOK_SECRET`;
- em erro `500`, revisar logs do backend e payload recebido.

### Cloudflared com URL antiga

- este ja foi um problema operacional real;
- sintoma comum: checkout fecha, mas o backend local nao recebe evento;
- correcao:
  - subir tunel novo;
  - copiar a URL HTTPS publica atual;
  - atualizar a configuracao do webhook no Stripe;
  - reenviar evento de teste e confirmar `200`.

### Vinculo ativo inconsistente

- o vinculo ativo pode ficar contaminado quando houve checkout novo indevido no passado;
- a recuperacao canonica de assinatura agora segue esta ordem:
  - vinculo ativo;
  - historico da mesma conta;
  - fallback por `customer_id`;
- evento divergente nao deve promover automaticamente um vinculo novo;
- antes de mexer no banco, revisar o pacote admin de validacao da franquia.

### Multiplas assinaturas por checkout novo indevido

- este foi um bug real e ja tratado com guardrails;
- comportamento correto atual:
  - se existe assinatura ativa canonica, nao abrir checkout pago novo;
  - upgrade e downgrade pago devem usar update da assinatura existente;
- ao investigar, conferir:
  - `customer_id`
  - `subscription_id`
  - `price_id`
  - se houve revalidacao antes do `/checkout/sessions`
  - se a UI retornou com `session_id` correto.

### Assinatura antiga ou cancelada presa no banco

- um `subscription_id` historico pode continuar no banco sem ser a assinatura vigente;
- `_obter_assinatura_stripe_ativa(...)` ja faz GET e aceita apenas status usaveis como canonicos;
- ao auditar, revisar origem da recuperacao:
  - vinculo ativo
  - historico da conta
  - fallback por `customer_id`

### Checklist minimo de validacao operacional

1. contratacao inicial:
   validar `customer_id`, `subscription_id`, `price_id`, fatos internos e efeito operacional final.
2. downgrade para `free`:
   validar `cancel_at_period_end=true`, `mudanca_pendente`, `plano_futuro=free` e `efetivar_em`.
3. downgrade pago `pro -> starter`:
   validar alteracao da assinatura existente, ausencia de checkout novo e pendencia interna.
4. virada de ciclo:
   validar execucao do `/cron/executar-cleiton`, efetivacao do plano futuro e limpeza da pendencia.
5. homologacao/local:
   validar ambiente Stripe correto, webhook correto, tunel correto e cron protegido.

## Cron no Render

- Variaveis necessarias no Cron Job:
  - `APP_BASE_URL`
  - `CRON_SECRET`
- Comandos padrao:

```bash
curl -fsS -X POST "$APP_BASE_URL/cron/executar-cleiton" -H "X-Cron-Secret: $CRON_SECRET"
curl -fsS -X POST "$APP_BASE_URL/cron/billing-snapshot" -H "X-Cron-Secret: $CRON_SECRET"
```

- Validacao esperada:
  - sem header retorna `403`
  - header invalido retorna `403`
  - header correto retorna `200` ou executa a rotina
  - `curl -f` torna falhas `4xx/5xx` visiveis no Render
  - `?secret=` permanece apenas como compatibilidade temporaria e sera removido depois da homologacao

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
- alteracoes recentes de experiencia visual tambem devem ser consolidadas primeiro aqui, mesmo quando a implementacao for apenas de frontend;
- qualquer mudanca relevante no Roberto, na Julia, no frontend editorial ou na governanca operacional deve ser refletida primeiro aqui.
