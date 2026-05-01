# Diagnostico de Homologacao e Publicacao

Este documento complementa o `README.md` principal com foco exclusivo em go/no-go de homologacao.
Use o `README.md` da raiz como fonte unica do estado funcional e visual atual.

## Estado Atual Confirmado

- pacote funcional da fase atual integrado localmente;
- chat da Julia com renderer markdown seguro, sugestoes clicaveis e busca web contextual filtrada;
- chat do Roberto na `/fretes` com UX visual atualizada no frontend, incluindo orientacao inicial de upload e copia local de respostas sem impacto em governanca, consumo ou observabilidade;
- detalhe de noticia/artigo com botao `Voltar Para Home`;
- monetizacao Stripe estabilizada com contratacao inicial, upgrade na assinatura existente, downgrade pago agendado e cancelamento para `free` por `cancel_at_period_end`;
- CSV administrativo de auditoria local consolidado em `/admin/dashboard/auditoria-clientes.csv`, sem Stripe online e sem mutacao;
- publicacao final em homolog ainda depende da validacao completa de migrations no ambiente alvo.

## Escopo Operacional Sensivel

Nao tratar como opcional:

- governanca operacional por franquia;
- autorizacao operacional usada pelo chat da Julia e pelo upload/chat Roberto;
- identidade de consumo por conta, franquia e usuario;
- coerencia entre Stripe, `ContaMonetizacaoVinculo` e `MonetizacaoFato`;
- coerencia do CSV administrativo entre `User.categoria` legado, `ContaMonetizacaoVinculo`, `Franquia`, `MonetizacaoFato` e `ConfigRegras`;
- execucao real da virada de ciclo em `/cron/executar-cleiton`;
- migrations da cadeia ativa;
- telas admin alinhadas com o backend.

## Bloqueio Historico de Homolog

O ponto critico conhecido continua sendo a estrategia de migrations no runtime de homolog.
Sem confirmar `upgrade head` e `current` no ambiente alvo, nao ha homolog concluida.

## Go / No-Go

### No-Go

- migrations nao executadas ou nao confirmadas;
- schema nao validado;
- health checks ou fluxos reais nao validados.

### Go

Somente quando todos forem verdadeiros:

1. migrations aplicadas sem erro;
2. schema validado no banco alvo;
3. health checks ok;
4. cron protegido validado;
5. telas admin ok;
6. chat Julia validado com autorizacao por franquia, sugestoes, busca web contextual e markdown seguro;
7. upload Roberto e chat Roberto validados no fluxo real da `/fretes`;
8. webhook Stripe validado no ambiente correto com assinatura valida;
9. retorno do checkout conciliado com `session_id` real;
10. downgrade `pro -> starter` ou `starter/pro -> free` com pendencia interna auditavel;
11. virada de ciclo confirmada em cron;
12. `/admin/dashboard/auditoria-clientes.csv` validado com protecao admin, filtros, flags principais, severidade e ausencia de efeito colateral.

## Referencia Principal

Detalhes funcionais, fluxos, regras criticas e experiencia visual vigente ficam consolidados no `README.md` da raiz.
