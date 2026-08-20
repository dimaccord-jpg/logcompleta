# LGPD e Governança Técnica

Referência auditada em 2026-08-19.

## Princípio

O código atual implementa desidentificação e encerramento controlado. Ele não faz hard-delete indiscriminado de conta, empresa e histórico relacionado.

## Distinções importantes

- encerramento operacional de usuário não é cancelamento de assinatura Stripe
- encerramento operacional não é exclusão física
- exercício de privacidade/LGPD não é self-service de perfil
- encerrar jornada de ativação não é automaticamente opt-out ou suppression

## Encerramento operacional de usuário

O serviço `user_lifecycle_service.py` trata o encerramento do vínculo operacional.

Estado esperado de usuário encerrado:

- e-mail substituído por marcador anônimo operacional
- nome substituído por `Conta encerrada`
- `password_hash` removido
- `oauth_provider` e `oauth_sub` removidos
- `subscribes_to_newsletter` desligado
- `job_role` e `usage_purpose` minimizados

Preservações explícitas:

- row do `User`
- relações com `Conta` e `Franquia`
- billing, eventos e histórico
- `accepted_terms_at`

## Revogação de acesso

- usuário encerrado não deve continuar autenticado
- o carregamento com Flask-Login rejeita usuários encerrados
- sessões antigas não devem continuar autorizando esse usuário
- isso vale também para usuário admin encerrado

## Exercício de privacidade/LGPD

O serviço `user_privacy_rights_service.py` implementa exercício administrativo controlado.

Características:

- default em dry-run
- apply explícito
- não aceita e-mail como autoridade; usa `user_id`
- desidentifica o perfil
- encerra jornadas de ativação associadas ao `converted_user_id`

Preserva explicitamente categorias como:

- conta
- franquia
- billing
- `funnel_event`
- `processing_event`
- `ia_consumo_evento`
- `lead`
- `communication_suppression`

Limites atuais declarados pelo próprio serviço:

- cleanup global de sessão não é suportado
- purge global de storage temporário por usuário não é suportado como garantia geral

## Activation journey

`Lead` possui estado persistido de encerramento da jornada de ativação:

- `activation_ended_at`
- `activation_ended_for_user_id`

Objetivo técnico:

- registrar terminalidade operacional da jornada do `Lead` convertido
- servir de base para lifecycle, suppression e futura minimização conservadora do e-mail do lead

## Minimização de `Lead.email`

### O que existe

- placeholder: `lead_minimized_<id>@anon.invalid`
- persistência opcional de `Lead.email_hmac`
- CLI: `lead-email-minimization`

### Critérios conservadores confirmados

- `converted_user_id` é obrigatório
- a ativação precisa estar em estado terminal inequívoco
- opt-out histórico sem suppression correspondente bloqueia elegibilidade
- conflito entre plaintext e `email_hmac` inválido/divergente falha de modo conservador

Estados terminais aceitos pela implementação atual incluem:

- jornada de ativação encerrada
- usuário convertido operacionalmente encerrado
- opt-out real com suppression já materializada

### O que não faz

- não cria suppression por si só
- não minimiza newsletter
- não opera sem `COMMUNICATION_SUPPRESSION_HMAC_SECRET` no apply

## Retenção técnica confirmada

Somente documentamos TTLs sustentados por código/testes da arquitetura atual:

- sessão da aplicação: aproximadamente `24h`
- documentos/memória da Júlia: aproximadamente `48h`
- storage temporário do Roberto: aproximadamente `30 min`
- memória/artefatos da Cleide: aproximadamente `48h`
- memória/resultado do AgenteCompara: aproximadamente `48h`

Também há compatibilidade de limpeza no AgenteCompara para artefatos legados baseados em `mtime`.

## O que não está provado como TTL automático geral

- billing append-only
- analytics append-only
- auditorias e fatos históricos
- logs externos
- storage controlado por fornecedor externo

Esses pontos dependem de política operacional ou do provedor e não devem ser descritos como TTL automático já implementado.
