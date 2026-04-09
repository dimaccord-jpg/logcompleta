# Deploy em Homolog/Produção

Este documento complementa o `README.md` principal com a sequência segura de deploy.

## Premissas

- `APP_ENV` definido explicitamente;
- `DATABASE_URL` apontando para o banco correto;
- segredos no provedor, não em arquivo versionado;
- persistência configurada para runtime;
- migrations disponíveis e estratégia de execução definida.

## Pacote Que Deve Subir Junto

- governança operacional Cleiton;
- identidade de consumo por conta/franquia;
- upload Roberto com billing técnico;
- painel admin;
- pipeline editorial e chat da Júlia;
- mensageria operacional com CTA de upgrade por franquia;
- rota `/contrate-um-plano` e card `Pagamento` clicável em `/perfil`;
- migrations da cadeia ativa;
- testes mínimos da Fase 2.

## Sequência Segura

1. publicar código no serviço alvo;
2. validar variáveis e persistência;
3. executar migrations no banco alvo;
4. confirmar `current` e `head`;
5. validar health checks;
6. validar cron protegido;
7. validar telas admin;
8. validar fluxos reais:
   - chat da Júlia
   - upload Roberto
   - detalhe de notícia/artigo
   - `/fretes` para admin e usuário comum
   - `/perfil` com redirecionamento para `/contrate-um-plano`

## Checklist Pós-Deploy

- `GET /health/liveness` retorna 200;
- `GET /health/readiness` retorna 200;
- `/cron/executar-cleiton` protegido corretamente;
- chat Júlia respeita autorização por franquia;
- chat Júlia mantém markdown seguro, sugestões e busca web contextual;
- chat Júlia exibe mensagem inicial atualizada sem `(BDI, FBX)` no texto de boas-vindas;
- chat Júlia renderiza link markdown em mensagens de bloqueio/limite;
- páginas `/noticia/<id>` exibem botão `Voltar Para Home`;
- upload Roberto continua operacional;
- upload Roberto exibe mensagem de erro de upload com links markdown clicáveis quando enviados pelo backend;
- `/fretes` mantém:
  - consulta por rota para admin;
  - experiência visual de upload/BI para usuário comum;
- `/perfil` mantém card `Pagamento` clicável para `/contrate-um-plano`;
- `PLANOS_UPGRADE_URL` está configurado no ambiente alvo e refletido em runtime;
- o plano Free está configurado no admin com franquia de referência válida;
- novo cadastro `free` nasce com `Franquia.limite_total` numérico;
- novo cadastro `free` não exibe saldo ilimitado por erro estrutural;
- conferência de saldo e bloqueio operacional é feita na `Franquia`, não em `User.creditos`.

## Riscos de Regressão

- publicar sem migrations válidas;
- alterar governança operacional sem revisar chat/upload;
- publicar frontend sem backend correspondente ou vice-versa;
- publicar sem configurar a referência administrativa do plano Free;
- reintroduzir uso funcional de `User.creditos` como se fosse fonte de verdade operacional;
- publicar sem `PLANOS_UPGRADE_URL` consistente entre ambientes;
- quebrar a renderização segura de markdown ao alterar mensagens operacionais no frontend;
- tratar homolog como concluída sem validar schema e fluxos reais.

## Referência Principal

Use o `README.md` da raiz como fonte principal do cenário atual.
