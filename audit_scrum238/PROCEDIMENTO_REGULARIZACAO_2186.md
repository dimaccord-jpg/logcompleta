# SCRUM-238 — procedimento de regularização da reserva de 21,86 créditos

Este procedimento não foi executado. A reserva de 21,86 créditos no banco de homologação permanece inalterada.

Não altera plano, régua, saldo comercial nem outras reservas. Não é migration. Não cobra a tentativa, nem agora nem de forma retroativa.

## Código corrigido implantado antes de regularizar

Não executar este procedimento enquanto a revisão que corrige os dois bloqueadores não estiver implantada no serviço de homologação que usa este banco.

A revisão precisa estar em execução antes do `BEGIN`. Ela impede que `liquidar_tentativa_ia` encerre falha do chat sem métricas como consumo zero, recupera o encerramento depois de rollback e faz o motor recusar abatimento quando `failure_reason` começa por `falha_assumida_plataforma`.

Sem esse código, uma liquidação genérica ainda pode gravar consumo zero e uma métrica tardia ainda pode abater 0,10 crédito. Se a implantação não estiver confirmada no serviço, parar. Não usar a liquidação nem o motor como atalho desta regularização.

## Identificação da tentativa do incidente

Executar somente no banco de homologação já conferido no Dashboard, por operador autorizado.

Antes de abrir a transação, numa sessão somente leitura, localizar a tentativa pelos dados do incidente. A chave e o id vêm da coleta autorizada. O valor 21,86 sozinho não identifica a linha.

Parar, sem atualizar, se qualquer item abaixo falhar:

- a consulta devolve uma única linha
- `id` e `attempt_key` são exatamente os do incidente
- `reserved_credits` = 21.860000
- `status` = `uncertain`
- `reserva_ativa` = true
- `settled_at` IS NULL
- `actual_credits` IS NULL
- `flow_type` = `agente_compara_comparison_chat`
- `failure_reason` = `provider_sem_usage`
- a franquia da linha está identificada

Se houver mais de uma linha com 21,86 créditos, parar. Não escolher por aproximação. Consumo confirmado segue a liquidação normal, fora deste procedimento.

```sql
SELECT id, attempt_key, franquia_id, ia_consumo_evento_id
FROM ia_chamada_tentativa
WHERE reserved_credits = 21.860000
  AND flow_type = 'agente_compara_comparison_chat'
  AND status = 'uncertain'
  AND reserva_ativa = true
  AND settled_at IS NULL
  AND actual_credits IS NULL
  AND failure_reason = 'provider_sem_usage';
```

## O que a transação precisa reconfirmar

A tentativa só pode ser encerrada quando todas as condições abaixo forem verdadeiras na mesma transação, com as linhas travadas:

- a linha travada é a tentativa identificada (`id` e `attempt_key`)
- os predicados da seção anterior continuam verdadeiros
- o evento vinculado, travado na mesma transação, tem `status` = `failure` e `input_tokens`, `output_tokens` e `total_tokens` nulos
- não existe `ia_consumo_abatimento` para esse evento
- `actual_credits` continua nulo: não há consumo registrado na tentativa
- a franquia travada tem `reserva_pendente` suficiente para devolver exatamente 21.860000
- `consumo_acumulado` é lido antes e permanece igual depois

Se qualquer item falhar, `ROLLBACK`. Não atualizar.

## Travamento, na ordem do código operacional

Uma transação. A ordem é a de `_assumir_falha_tecnica_sem_metricas`: primeiro `ia_chamada_tentativa`, depois `ia_consumo_evento`, por último `franquia`. O abatimento é conferido enquanto a tentativa permanece travada, que é o que impede o motor de inserir débito no meio da conferência.

`SELECT … FOR UPDATE` na tentativa identificada, no evento vinculado e na franquia. Não travar a franquia antes da tentativa. Não decidir com leitura do evento feita fora do lock: outra sessão pode preencher tokens entre a conferência e o `UPDATE`.

O `UPDATE` da tentativa só vale com o predicado do estado incerto. O efeito na franquia só ocorre se esse `UPDATE` alterar uma linha. A segunda execução encontra `failure_reason` iniciado por `falha_assumida_plataforma`, `status` = `released` e `reserva_ativa` = false, altera zero linhas e não mexe na reserva nem no consumo.

## Preservação do histórico e proibição de cobrança

Não apagar tentativa, evento, abatimento ou motivo. Não alterar `attempt_key`, `reserved_credits`, `actual_credits`, tokens, `status` do evento nem `consumo_acumulado`.

O único motivo gravado é `falha_assumida_plataforma:provider_sem_usage`. O motivo original permanece no sufixo. `actual_credits` continua nulo.

Proibido inserir `ia_consumo_abatimento`, aumentar `consumo_acumulado` ou preencher `actual_credits`. Proibido cobrar esta tentativa de forma retroativa, mesmo se métricas do provider aparecerem depois. Com o código corrigido implantado, o marcador impede essa cobrança. Este procedimento não a cria.

## Execução

```sql
BEGIN;

-- 1. Tentativa do incidente. Parar se id ou attempt_key não baterem.
SELECT id, attempt_key, status, reserva_ativa, settled_at, actual_credits,
       reserved_credits, failure_reason, flow_type, franquia_id, ia_consumo_evento_id
FROM ia_chamada_tentativa
WHERE id = :tentativa_id
  AND attempt_key = :attempt_key
FOR UPDATE;

-- Se já estiver released, com reserva_ativa = false e failure_reason
-- iniciado por falha_assumida_plataforma: COMMIT sem alterar franquia.
-- É a reexecução idempotente. Qualquer outro estado fora do predicado: ROLLBACK.

-- 2. Evento, ainda com a tentativa travada. Se algum token não for nulo
-- ou o status não for failure: ROLLBACK.
SELECT id, status, input_tokens, output_tokens, total_tokens
FROM ia_consumo_evento
WHERE id = :evento_id
FOR UPDATE;

-- 3. Abatimento. Se existir linha, há consumo registrado: ROLLBACK.
SELECT id
FROM ia_consumo_abatimento
WHERE ia_consumo_evento_id = :evento_id
FOR UPDATE;

-- 4. Franquia por último. Guardar consumo_acumulado.
-- Se reserva_pendente < 21.860000: ROLLBACK.
SELECT id, reserva_pendente, consumo_acumulado
FROM franquia
WHERE id = :franquia_id
FOR UPDATE;

WITH encerrada AS (
    UPDATE ia_chamada_tentativa AS tentativa
    SET reserva_ativa = false,
        status = 'released',
        failure_reason = 'falha_assumida_plataforma:provider_sem_usage',
        settled_at = (NOW() AT TIME ZONE 'UTC')
    WHERE tentativa.id = :tentativa_id
      AND tentativa.attempt_key = :attempt_key
      AND tentativa.reserved_credits = 21.860000
      AND tentativa.status = 'uncertain'
      AND tentativa.reserva_ativa = true
      AND tentativa.settled_at IS NULL
      AND tentativa.actual_credits IS NULL
      AND tentativa.flow_type = 'agente_compara_comparison_chat'
      AND tentativa.failure_reason = 'provider_sem_usage'
      AND tentativa.franquia_id = :franquia_id
      AND tentativa.ia_consumo_evento_id = :evento_id
      AND NOT EXISTS (
          SELECT 1
          FROM ia_consumo_evento AS evento
          WHERE evento.id = tentativa.ia_consumo_evento_id
            AND (
                evento.status IS DISTINCT FROM 'failure'
                OR evento.input_tokens IS NOT NULL
                OR evento.output_tokens IS NOT NULL
                OR evento.total_tokens IS NOT NULL
            )
      )
      AND NOT EXISTS (
          SELECT 1
          FROM ia_consumo_abatimento AS abatimento
          WHERE abatimento.ia_consumo_evento_id = tentativa.ia_consumo_evento_id
      )
      AND EXISTS (
          SELECT 1
          FROM franquia
          WHERE franquia.id = tentativa.franquia_id
            AND franquia.reserva_pendente >= tentativa.reserved_credits
      )
    RETURNING tentativa.franquia_id, tentativa.reserved_credits
)
UPDATE franquia
SET reserva_pendente = GREATEST(0, franquia.reserva_pendente - encerrada.reserved_credits)
FROM encerrada
WHERE franquia.id = encerrada.franquia_id
  AND franquia.reserva_pendente >= encerrada.reserved_credits;

-- Conferir antes do COMMIT:
-- uma tentativa encerrada com o marcador falha_assumida_plataforma:provider_sem_usage;
-- actual_credits continua nulo;
-- evento intocado, tokens nulos;
-- nenhum ia_consumo_abatimento;
-- consumo_acumulado igual ao valor lido antes do UPDATE;
-- reserva_pendente reduzida uma única vez em 21.860000.
-- Se o UPDATE da tentativa alterou zero linhas e o marcador ainda não estava
-- gravado, ROLLBACK. Se o marcador já estava gravado, COMMIT sem novo efeito.
COMMIT;
```

A franquia só é alterada se a tentativa sair do estado incerto. Na segunda execução o predicado não encontra a linha, a franquia não é debitada de novo e `consumo_acumulado` não entra no comando.
