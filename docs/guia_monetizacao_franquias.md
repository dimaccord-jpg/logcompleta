# Guia de Monetizacao, Franquias e Planos

Este documento consolida a parte de monetizacao do Agentefrete / LogCompleta com foco em:

- planos comerciais e operacionais
- franquia de consumo
- contagem, calculo e conversao de uso em creditos
- upload do Roberto e apropriacao de consumo
- integracao com Stripe
- regras de governanca e observabilidade

Ele complementa o `README.md`, mas nao substitui o documento principal do projeto.

## 1. Visao Geral

A monetizacao do projeto e separada em duas camadas:

- camada comercial/contratual: define plano, cobranca, vinculo externo e fatos de monetizacao
- camada operacional: define se o uso pode acontecer, quanto ja foi consumido e qual o estado real da franquia

Regra central:

- a fonte de verdade operacional e `Franquia`
- Stripe nao substitui `Franquia`
- `User.creditos` e legado e nao deve ser usado como fonte oficial de saldo

Na pratica:

- o contrato pode nascer ou mudar via admin e Stripe
- o consumo tecnico vira credito consumido no motor Cleiton
- o efeito real de limite, degradacao, bloqueio e expiracao converge para `Franquia`

## 2. Fonte de Verdade Operacional

Entidade principal:

- `Franquia`

Campos operacionais oficiais:

- `Franquia.limite_total`
- `Franquia.consumo_acumulado`
- `Franquia.status`

Leitura operacional:

- autorizacao em tempo de uso deve olhar `Franquia`
- incidentes de saldo, limite ou bloqueio devem ser investigados primeiro em `Franquia`

Campos e estruturas auxiliares:

- `Conta`: raiz contratual/comercial
- `ContaMonetizacaoVinculo`: vinculo comercial externo da conta
- `MonetizacaoFato`: trilha append-only de fatos externos e internos de monetizacao
- `ProcessingEvent`: eventos tecnicos de processamento
- `IaConsumoEvento`: eventos tecnicos de IA
- `CleitonBillingApropriacao`: apropriacao idempotente do billing tecnico do Roberto
- `CleitonCostConfig`: parametros de custo e regua de creditos

## 3. Catalogo Completo de Planos

Planos catalogados no codigo administrativo:

- `free`
- `starter`
- `pro`
- `multiuser`
- `avulso`

Plano operacional adicional suportado pelo controle interno:

- `uso_adm`

### 3.1 Estado comercial atual por tipo

Planos preparados para gateway de monetizacao nesta fase:

- `starter`
- `pro`

Planos catalogados, mas fora do fluxo principal de gateway/assinatura nesta fase:

- `free`
- `multiuser`
- `avulso`

Plano de uso interno/administrativo:

- `uso_adm`

Observacoes importantes:

- `free` nao tem `price_id` de Stripe no fluxo comercial atual
- `starter` e `pro` sao os planos pagos preparados para Stripe
- `multiuser` existe no catalogo e no controle operacional, inclusive com criacao de franquias adicionais e codigos de acesso
- `avulso` existe no motor operacional e na administracao de planos
- `uso_adm` existe no controle de usuario e no fluxo operacional, mas nao faz parte do catalogo SaaS exibido como produto comercial padrao

### 3.2 Onde o projeto guarda os parametros de cada plano

Os planos nao dependem de valores hardcoded para monetizacao do dia a dia. A configuracao administrativa usa `ConfigRegras`.

Chaves administrativas por plano:

- `plano_valor_admin_<codigo>`
- `plano_franquia_ref_admin_<codigo>`

Chaves adicionais para planos com gateway:

- `plano_gateway_provider_admin_<codigo>`
- `plano_gateway_product_id_admin_<codigo>`
- `plano_gateway_price_id_admin_<codigo>`
- `plano_gateway_currency_admin_<codigo>`
- `plano_gateway_interval_admin_<codigo>`
- `plano_gateway_ready_admin_<codigo>`

Configuracao especial do free:

- `freemium_trial_dias`

Interpretacao oficial:

- valor do plano = parametro comercial/admin
- franquia do plano = limite operacional real em creditos

## 4. Regra da Franquia

A franquia e a unidade operacional real de consumo.

Cada franquia tem:

- conta vinculada
- limite total em creditos
- consumo acumulado em creditos
- status persistido
- ciclo operacional

Regras oficiais:

- novo usuario comercial `free` deve nascer com `Franquia.limite_total` numerico
- `Franquia.limite_total = None` nao e comportamento permitido para novo usuario comercial
- nao atualizar saldo operacional fora de `Franquia`
- nao usar `User.creditos` para decisao de autorizacao, cobranca ou bloqueio

## 5. Regua de Conversao para Creditos

A conversao de consumo tecnico em credito depende de `CleitonCostConfig`.

Parametros principais:

- `credit_tokens_per_credit`
- `credit_lines_per_credit`
- `credit_ms_per_credit`

Interpretacao:

- quantos tokens de IA 1 credito compra
- quantas linhas processadas 1 credito compra
- quantos milissegundos processados 1 credito compra

### 5.1 Formula de conversao por tokens

Formula:

- `creditos = tokens / credit_tokens_per_credit`

Comportamento:

- converte com precisao decimal
- arredonda para 6 casas
- se a regua estiver ausente ou invalida, a conversao falha e o motor registra erro de configuracao

### 5.2 Formula de conversao por linhas

Formula:

- `creditos = linhas / credit_lines_per_credit`

Uso:

- aplicada a `ProcessingEvent.rows_processed`

### 5.3 Formula de conversao por tempo de processamento

Formula:

- `creditos = processing_time_ms / credit_ms_per_credit`

Uso:

- aplicada a `ProcessingEvent.processing_time_ms`

### 5.4 Soma de creditos de um evento

Eventos de IA:

- usam `total_tokens`
- fallback para `input_tokens + output_tokens`

Eventos de processamento:

- somam creditos por linhas processadas
- somam creditos por milissegundos processados

Formula conceitual do processamento:

- `creditos_evento_processing = creditos_linhas + creditos_ms`

## 6. Custo Tecnico e Referencia de Runtime

O projeto tambem guarda parametros de custo para leitura operacional e dashboard.

Campos principais em `CleitonCostConfig`:

- `runtime_monthly_cost`
- `month_seconds`
- `allocation_percent`
- `overhead_factor`
- `cost_per_million_tokens`

Formula derivada de custo por segundo:

- `custo_por_segundo = (runtime_monthly_cost * allocation_percent * overhead_factor) / month_seconds`

Importante:

- esse calculo serve como referencia de custo tecnico
- `cost_per_million_tokens` e apenas referencia simples
- essa referencia nao entra no calculo do upload Roberto

## 7. Regras de Abatimento de Consumo

Nem todo evento tecnico deve abater da franquia do cliente.

O motor nao abate quando:

- a origem e sistema interno
- nao existe `usuario_id`
- nao existe `franquia_id`
- a origem e HTTP anonima
- a franquia e a franquia reservada do sistema interno

O motor so deve refletir consumo quando houver identidade operacional valida de cliente.

## 8. Regras de Status da Franquia

O status operacional e recalculado com base em:

- bloqueio manual
- plano operacional
- vigencia do ciclo
- limite total
- consumo acumulado

Classificacao oficial:

- `active`
  - franquia interna sem bloqueio manual
  - ou limite ainda nao atingido
  - ou limite efetivo inexistente
- `blocked`
  - bloqueio manual
  - plano `free` no limite
  - plano indefinido no limite
  - fallback de seguranca
- `degraded`
  - planos `starter`, `pro` e `multiuser` quando atingem o limite
- `expired`
  - vigencia expirada
  - ou plano `avulso` ao atingir limite/vigencia

Leitura resumida por plano:

- `free`: bateu o limite, bloqueia
- `starter`: bateu o limite, degrada
- `pro`: bateu o limite, degrada
- `multiuser`: bateu o limite, degrada
- `avulso`: bateu o limite ou vigencia, expira
- `uso_adm` / interna: ignora regra comercial padrao e permanece operacional, salvo bloqueio manual

## 9. Upload do Roberto e Consumo da Franquia

O upload de Excel do Roberto participa diretamente da monetizacao operacional.

Fluxo resumido:

1. o upload valida arquivo e colunas obrigatorias
2. o sistema normaliza e processa as linhas validas
3. o sistema aplica limite operacional de volume para uso do BI
4. os dados uteis sao gravados no storage temporario do Roberto
5. o billing operacional do upload e apropriado de forma idempotente

### 9.1 O que conta para consumo no upload

No fluxo atual:

- `rows_processed` usa a quantidade de `linhas_processadas` validas antes do corte operacional de `linhas_utilizadas`
- `processing_time_ms` mede o tempo real de processamento da request

Isso significa:

- o consumo do upload e calculado pelo que foi efetivamente processado para preparar o upload
- o teto operacional de exibicao/uso posterior nao altera retroativamente a contagem tecnica ja feita para billing

### 9.2 Idempotencia do upload

O upload usa chave:

- `roberto-upload:<execution_id>`

Garantias:

- uma mesma chave nao deve apropriar consumo duas vezes
- `CleitonBillingApropriacao` guarda o marcador append-only da apropriacao
- em caso de repeticao, o fluxo retorna como duplicado em vez de debitar novamente

### 9.3 Contrato da apropriacao

O resultado da apropriacao do upload expõe:

- se foi duplicado
- se apropriou consumo
- `processing_event_id`
- `creditos_apropriados`
- motivo
- novo status da franquia
- consumo acumulado atual

## 10. IA, Tokens e Consumo

Para eventos de IA:

- o consumo tecnico vem de `IaConsumoEvento`
- o motor converte tokens em creditos via `credit_tokens_per_credit`

Regra operacional:

- IA bem sucedida pode abater da franquia quando houver identidade valida de cliente
- se a configuracao de creditos estiver ausente ou invalida, a apropriacao falha com erro de configuracao e deve ser tratada como incidente de parametrizacao, nao como saldo zero

## 11. Multiuser

O plano `multiuser` exige atencao especial porque pode gerar mais de uma franquia operacional para a mesma conta.

Comportamentos relevantes:

- cria franquias adicionais vinculadas a mesma conta
- gera codigos em `MultiuserFranquiaCodigo`
- mantem governanca central via `Conta` e `Franquia`
- quando atinge o limite operacional, entra em `degraded`

## 12. Free, Trial e Usuarios Legados

O plano `free` tem duas regras importantes:

- onboarding novo deve nascer com limite operacional numerico
- trial administrativo e parametrizado por `freemium_trial_dias`

Existe tambem rotina de saneamento para legado:

- `corrigir_franquias_free_sem_limite()`

Objetivo:

- corrigir usuarios `free` antigos ou inconsistentes que ficaram sem `limite_total`

## 13. Stripe, Cobranca e Contrato

Stripe entra como fonte de fatos externos, nao como estado operacional final.

Entidades principais:

- `ContaMonetizacaoVinculo`
- `MonetizacaoFato`

Regra de arquitetura:

- fatos Stripe relevantes sao persistidos
- a correlacao comercial precisa ser auditavel
- o efeito operacional em `Franquia` continua mediado pela camada central do Cleiton

### 13.1 Fluxos comerciais mapeados

Fluxo atual documentado:

- `free -> starter`: inicia novo checkout embutido
- `starter -> pro`: atualiza subscription existente
- `pro -> starter`: downgrade pendente para virar no proximo ciclo
- `starter/pro -> free`: `cancel_at_period_end = true` e troca interna posterior

Guardrail de UX atual:

- downgrade para `free` ou `starter` deve abrir modal de confirmacao antes de qualquer chamada ao backend;
- o endpoint oficial permanece `/api/contratacao/stripe/iniciar`;
- o payload oficial permanece `{ plano_codigo, confirmar_downgrade }`;
- ausencia do modal deve ser tratada como erro visivel no frontend, nao como falha silenciosa.

Importante:

- nenhum efeito operacional de plano deve ignorar o Cleiton
- status contratual externo nao escreve diretamente o status da franquia sem passar pela camada central

## 14. O que cada plano representa na monetizacao

### `free`

- entrada freemium
- sem price Stripe no fluxo atual
- usa trial administrativo
- deve nascer com franquia numerica
- ao atingir limite, bloqueia

### `starter`

- plano pago com suporte de gateway
- usa configuracao administrativa de valor e franquia
- possui chaves de provider/product/price/currency/interval/ready
- ao atingir limite, degrada

### `pro`

- plano pago com suporte de gateway
- mesma governanca central de `starter`
- upgrade e downgrade tratados pelo fluxo de monetizacao
- ao atingir limite, degrada

### `multiuser`

- plano catalogado e suportado operacionalmente
- pode gerar varias franquias/codigos na mesma conta
- tratado como plano degradavel ao atingir limite
- nao faz parte do gateway comercial padrao desta fase

### `avulso`

- plano catalogado e suportado operacionalmente
- ao atingir limite ou vigencia, expira
- nao faz parte do gateway comercial padrao desta fase

### `uso_adm`

- plano interno/administrativo
- suportado no controle de usuarios
- nao e produto comercial publico
- nao deve ser usado como referencia de monetizacao de cliente final

## 15. Observabilidade e Auditoria

Objetos e trilhas que sustentam auditoria:

- `Franquia`
- `IaConsumoEvento`
- `ProcessingEvent`
- `CleitonBillingApropriacao`
- `ContaMonetizacaoVinculo`
- `MonetizacaoFato`
- `CleitonCostConfig`

Checagens importantes:

- reconciliar `Franquia.consumo_acumulado` com soma recalculada dos eventos abataveis
- validar coerencia entre `ContaMonetizacaoVinculo`, `MonetizacaoFato` e `Franquia`
- revisar status persistido e status recalculado da franquia

Endpoint administrativo citado no projeto:

- `/admin/api/cleiton-franquia/<franquia_id>/validacao`

## 16. Regras que nao podem ser quebradas

Estas regras devem ser preservadas em qualquer manutencao futura:

- nao mover a fonte de verdade operacional para `User.creditos`
- nao escrever consumo, limite ou status fora de `Franquia`
- nao aplicar efeito contratual direto de Stripe sem passar pela camada central do Cleiton
- nao debitar upload do Roberto sem idempotencia
- nao tratar consumo anonimo/sistema como consumo faturavel do cliente
- nao criar bypass de governanca para planos pagos, trial ou franquias multiuser
- nao assumir que valor comercial do plano e o mesmo que a franquia operacional

## 17. Checklist Rapido de Validacao

Antes de alterar monetizacao, planos ou franquias, confirmar:

- o plano existe no catalogo correto
- a franquia de referencia do plano esta configurada
- a leitura operacional continua baseada em `Franquia`
- a regua de creditos esta configurada e valida
- o upload do Roberto continua idempotente
- eventos de IA e processamento continuam conciliaveis
- Stripe continua como fonte de fatos, nao como fonte de verdade operacional
- observabilidade e trilhas append-only continuam preservadas

## 18. Resumo Executivo

Em uma frase:

- monetizacao comercial define contrato; franquia define operacao.

Em termos práticos:

- planos definem embalagem comercial e referencia operacional
- Cleiton converte uso tecnico em creditos
- Roberto consome franquia via upload/processamento
- Stripe registra fatos externos
- `Franquia` decide o estado real do cliente no uso do sistema
