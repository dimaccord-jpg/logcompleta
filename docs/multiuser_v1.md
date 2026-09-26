# Multiuser V1 em produção

Guia funcional do Multiuser V1, vigente na produção `fa98317` (Sprint 12; baseline Multiuser da Sprint 11). Configurações externas informadas pela operação são referências datadas, não constantes do código nem resultado de consulta ao banco ou Stripe nesta revisão.

## Organização, papéis e acesso

`Conta` é a raiz organizacional e comercial. `ContaVinculoOrganizacional` mantém o vínculo persistente e seu histórico, com os papéis `contratante` e `membro`. Existe exatamente um contratante ativo por Conta Multiuser regular. `User.is_admin` é administração global da plataforma; `User.categoria` é plano, não papel organizacional. Não existe terceiro papel org-admin no V1.

Cada usuário ativo, inclusive o contratante, ocupa uma Franquia individual. A Conta controla `quantidade_assentos_contratados`; as franquias compartilham o ciclo contratual, mas o consumo é individual. Capacidade comprometida considera vínculos ativos e reservas válidas de convites. Revogar um membro libera ocupação sem reduzir a quantidade contratada.

A gestão do contratante está em `/gestao-multiuser`. As rotas `/api/multiuser/*` são endpoints internos da aplicação, autenticados e autorizados conforme a operação, com proteção CSRF nas mutações. Não constituem API pública de integração.

## Privacidade entre membros

Pertencer à mesma Conta não compartilha documentos, uploads, auditorias, chats, tabelas, resultados, memória ou artefatos privados. `conta_id` identifica contexto organizacional/comercial e não pode servir genericamente de autorização para esses artefatos. O ownership do usuário e o escopo de sessão/domínio continuam obrigatórios.

Cleide/AgenteAudita, Júlia e AgenteCompara preservam seus namespaces e contextos próprios. O trilho documental e a governança do Cleiton são infraestrutura comum, não memória coletiva da organização. O contratante gerencia o contrato e os membros; esse papel não concede leitura dos arquivos privados dos demais. Ver [agentes](AGENTS.md) e [privacidade técnica](lgpd_governanca_tecnica.md).

## Contratação e dados empresariais

O fluxo começa em `/contrate-um-plano`, valida os dados empresariais, cria a intenção persistente e abre o Checkout Stripe. Nova contratação exige razão social, nome fantasia, CNPJ validado/normalizado, e-mail empresarial e endereço estruturado: logradouro, número, cidade, UF e CEP.

A página apresenta cards de planos e formulário Multiuser; o Checkout Stripe é incorporado quando iniciado. O endereço pode ser auxiliado por consulta online ao ViaCEP, por botão “Buscar CEP” ou após completar o CEP. Logradouro, bairro, cidade e UF são preenchidos quando disponíveis e continuam editáveis; número e complemento são manuais. Falha de consulta permite preenchimento manual. Um retorno incompleto sem `erro: true` pode exibir “CEP localizado. Complete os campos restantes.”, inclusive em caso de CEP inexistente; não use essa mensagem como validação definitiva de endereço.

A quantity mínima é configurável. A configuração administrativa dos planos usa `ConfigRegras`; o benefício pago só é concedido após confirmação de pagamento. A Conta usa um Stripe Customer, uma Subscription e um Subscription Item cuja `quantity` representa os assentos contratados.

### Configuração comercial

Preço, catálogo Stripe, quantidade mínima, franquia individual e limite de aumento automático são parâmetros administrativos. Consulte `ConfigRegras` e o catálogo Stripe do ambiente antes de citar valores vigentes; referências de uma revisão anterior não comprovam a configuração atual. IDs de produto/preço não são credenciais. Segredos ficam no provedor; ver [segurança](../SECURITY_SECRETS.md).

### Webhook e renovação

Endpoint de produção: `https://www.agentefrete.com.br/api/webhook/stripe`.

Eventos relevantes habilitados, conforme a operação e suportados no código:

- `checkout.session.completed`
- `invoice.paid`
- `invoice.payment_failed`
- `customer.subscription.updated`
- `customer.subscription.deleted`

`invoice.paid` é a autoridade para ativação/renovação paga. O retorno do Checkout isoladamente não concede benefício. A renovação propaga o ciclo contratual às franquias e aplica o reset do novo período de forma idempotente; replay do mesmo invoice não pode resetar o consumo novamente. Eventos de falha e alteração contratual são tratados pelo trilho de monetização, sem substituir essa autoridade.

## Convites e reentrada

O convite é persistente, com estado, token assinado, TTL de 3600 segundos, reserva de assento, envio por e-mail, aceite e reenvio. Locks, constraints e verificação de capacidade protegem operações concorrentes. Convites expirados deixam de comprometer capacidade conforme a liberação de reservas.

O usuário existente só é transferido por aceite explícito. Contrato/benefício pago incompatível bloqueia a transferência; não existe troca livre entre Contas. O aceite associa o usuário a uma Franquia disponível e cria o vínculo operacional, preservando os registros históricos e o consumo da Franquia. Reentrada segue esse mesmo fluxo de convite/aceite; não reinicia o ciclo nem apaga consumo anterior e não promete reassociação à mesma Franquia organizacional usada antes.

## Revogação

O contratante pode encerrar o vínculo de um membro. O vínculo permanece como `encerrado`, o User e a Franquia permanecem existentes, e o consumo já realizado é preservado. A operação não cancela a Subscription nem altera sua quantity.

O usuário retorna a contexto individual elegível; quando não há contexto restaurável, o serviço cria Conta e Franquia individuais, define `User.categoria=free` e obtém `Franquia.limite_total` da referência administrativa vigente do Free (`exigir_configurado=True`). A nova franquia não nasce com limite nulo; seu consumo inicial segue a criação operacional da franquia. Se houver contexto individual reutilizável, a categoria só volta a Free quando não existe benefício pago vigente nesse contexto; o serviço não normaliza indiscriminadamente todas as franquias históricas. A geração do contexto de sessão é incrementada e as referências operacionais antigas são invalidadas. O login permanece disponível; e-mail e notificação orientam a contratação de plano próprio. Isso encerra o acesso organizacional, sem desidentificar a pessoa nem apagar seu histórico.

Repetir a revogação é idempotente. O contratante não pode revogar a si próprio nem liberar seu assento por esse fluxo.

O e-mail “Seu acesso Multiuser foi encerrado — continue no Agente Frete” e a notificação interna orientam o membro a contratar um plano próprio. O CTA útil conduz à contratação, sem prometer manutenção do acesso aos documentos da Conta anterior.

## Aumento de assentos

Dentro do limite automático acumulado configurado para o ciclo, o contratante pode ampliar a capacidade imediatamente. A atualização usa o mesmo Subscription Item, sem nova Subscription e sem prorata do ciclo atual (`proration_behavior=none`). A cobrança recorrente futura acompanha a nova quantity; ciclo e consumo permanecem.

Acima do limite, o aumento excepcional entra em análise administrativa. Pode ser aprovado gratuitamente, aprovado com cobrança extraordinária ou rejeitado. A cobrança extraordinária não cria nova Subscription. Correlação e idempotência vinculam pagamento e solicitação: a confirmação libera a capacidade exatamente uma vez, sem reiniciar ciclo ou consumo. Falhas parciais exigem observar os estados persistidos antes de repetir uma operação.

## Redução para o próximo ciclo

`quantity_futura` é alvo para o próximo ciclo e não reduz a capacidade operacional do ciclo atual. O contratante deve adequar a ocupação antes do corte. Se a ocupação comprometida exceder a quantidade futura na efetivação, o fluxo falha fechado: não revoga usuários automaticamente nem reduz indevidamente a cobrança.

O serviço protege divergências entre Stripe e estado local. A recuperação causal da SCRUM-190 existe para falhas parciais com evidência durável correlacionada. Encontrar a quantity desejada no Stripe, isoladamente, não autoriza concluir que a redução local foi aplicada. O diagnóstico é somente leitura; recuperação é operação distinta e governada, não efeito da consulta.

## Transferência de titularidade

Existe transferência administrativa/de suporte, com solicitação e decisão autorizadas. Ela preserva Conta, Customer, Subscription, quantity, ciclo e franquias, mantendo exatamente um contratante ativo. O novo titular deve ser elegível. Não é self-service de troca de Conta e não cria um papel org-admin.

## Diagnóstico, BI e reconciliação

O diagnóstico Multiuser é read-only. Os nomes conceituais e valores serializados são:

| Estado | Valor no código |
|---|---|
| OK | `ok` |
| DIVERGENTE | `divergente` |
| PENDENTE_ESPERADO | `pendente_esperado` |
| RECONCILIACAO_NECESSARIA | `reconciliacao_necessaria` |
| INCONCLUSIVO | `inconclusivo` |

O CSV administrativo incorpora `multiuser_status_diagnostico`, `multiuser_achado_codigo` e `multiuser_achado_detalhe`. `RECONCILIACAO_NECESSARIA` implica risco pelo menos `atenção` e revisão manual = true; severidade maior é preservada. Pendência esperada não deve ser confundida automaticamente com erro. Ver [troubleshooting](troubleshooting_operacional.md).

## Futuro e pendências

Fora do V1: API pública Multiuser, OAuth de integração, API keys, scopes e rate limit dessa API; org-admin; troca livre entre Contas; auto-revogação para cumprir redução; liberação do próprio assento pelo contratante. O login OAuth já existente na plataforma é outra funcionalidade.

SCRUM-186 é evolução futura para permitir que o contratante libere seu próprio assento mantendo o papel de contratante/gestão. O comportamento atual continua contando esse assento.

As notificações atuais e as limitações de validação financeira estão em [estado de produção](estado_producao.md#pendências-conhecidas-e-pós-release).

## Referências de implementação

- Organização e capacidade: `app/models.py`, `app/services/conta_organizacional_rules.py`, `conta_multiuser_capacidade_service.py`.
- Contratação e ciclo: `conta_multiuser_contratacao_service.py`, `conta_multiuser_checkout_intencao_service.py`, `conta_multiuser_ciclo_service.py`, `cleiton_monetizacao_service.py` em `app/services/`.
- Convites, aumentos, redução, revogação e titularidade: serviços `app/services/conta_multiuser_*_service.py` e rotas `app/conta_multiuser_*_routes.py`.
- Diagnóstico/CSV: `app/services/conta_multiuser_diagnostico_service.py`, `app/services/admin_auditoria_clientes_csv_service.py`.
- Schema: [banco e migrations](DATABASE_AND_MIGRATIONS.md).

Os testes existentes de F1–F8, `tests/test_fase7_multiuser_lifecycle.py` e `tests/test_uat_8_5_csv_auditoria_multiuser.py` são referências de cobertura, não prova de execução aprovada ou cobrança real. Os limites de evidência do UAT e a verificação financeira pós-release estão no [estado de produção](estado_producao.md#pendências-conhecidas-e-pós-release).
