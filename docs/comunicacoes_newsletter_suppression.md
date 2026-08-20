# Comunicações, Newsletter e Suppression

Referência auditada em 2026-08-19.

## Communication suppression

### Modelo e identidade

- modelo: `CommunicationSuppression`
- não armazena e-mail em plaintext
- a identidade é HMAC SHA-256 hex
- `Lead.email_hmac` e `CommunicationSuppression.email_hmac` usam a mesma derivação

### Finalidades atuais

- `PRE_REGISTRATION`
- `ACTIVATION`

### Secret

- chave de ambiente: `COMMUNICATION_SUPPRESSION_HMAC_SECRET`
- deve permanecer estável entre execuções
- a documentação não expõe nem replica o valor real

### Comportamento técnico

- sem secret válido, a autoridade recusa operar
- os gates críticos foram implementados em modo conservador/fail-closed
- não há fallback para plaintext como autoridade oficial

### Backfill histórico

CLI registrada:

- `flask --app app.web communication-suppression-backfill`

Comportamento:

- default: dry-run
- apply só com `--apply`
- não executa migration
- exige `COMMUNICATION_SUPPRESSION_HMAC_SECRET`

Estado histórico conhecido do rollout de produção em 2026:

- nenhum registro histórico elegível exigiu apply

## Newsletter

### Autoridade operacional

- modelo: `NewsletterSubscription`
- é a autoridade operacional própria da newsletter
- é independente de `Lead`
- pode existir para pessoas que não sejam `User`

### Relação com `User`

- `User.subscribes_to_newsletter` permanece por compatibilidade e UX
- essa flag não substitui `NewsletterSubscription`

### Cadastro público

- cadastro público de newsletter não deve criar `Lead`
- newsletter não deve virar aquisição operacional automaticamente
- OAuth/login não faz opt-in automático

### Cancelamento

- rota/fluxo próprio de cancelamento: `/newsletter/cancelar/<token>`
- unsubscribe mantém o row e marca `unsubscribed_at`
- unsubscribe não cria `CommunicationSuppression`
- unsubscribe de newsletter não é campaign opt-out nem activation opt-out

### Backfill histórico

CLI registrada:

- `flask --app app.web newsletter-subscription-backfill`

Comportamento:

- default: dry-run
- apply só com `--apply`
- fonte única: `User.subscribes_to_newsletter == True`
- não reativa subscription cancelada automaticamente

Estado histórico conhecido do rollout de produção em 2026:

- dry-run oficial encontrou `12` inscrições a criar
- apply posterior criou `12`
- reconciliação histórica foi concluída

## Lifecycle de usuário e newsletter

- encerramento operacional do usuário desliga `subscribes_to_newsletter`
- o fluxo de privacidade também desidentifica essa preferência
- isso não transforma o processo em exclusão física de estrutura contratual

## Lead e suppression

- suppression e newsletter são camadas diferentes
- minimização de `Lead.email` não minimiza newsletter
- lead campaign unsubscribe e activation unsubscribe usam suppression, não a autoridade de newsletter

## Limites documentados

- newsletter está operacional, mas isso não significa prioridade comercial atual
- unsubscribe de newsletter não deve ser descrito como exclusão de todos os canais
- `CommunicationSuppression` não substitui a newsletter, e a newsletter não substitui `CommunicationSuppression`
