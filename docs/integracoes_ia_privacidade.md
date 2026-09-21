# Integrações de IA e Privacidade

Referência atualizada em 2026-09-19 (SCRUM-75).

## Fronteira obrigatória

Antes de Gemini, DuckDuckGo, Imagen ou outro provedor externo, o conteúdo passa pela governança contextual local de Cleiton.

Contrato vivo: [Governança contextual de IA](cleiton_ai_data_governance.md).

Fluxo:

```text
raw_text / documento / histórico
→ Cleiton governance
→ classificação local
→ minimização / alias
→ payload autorizado
→ provider
```

Não se envia o original para outra IA perguntar se ele é sensível.

## Camadas

- `cleiton_ai_data_governance.py`: fachada, finalidade, fail-closed, status para a UI futura
- `cleiton_ai_privacy_classifier.py`: classificador local (sem regex, sem HTTP)
- `cleiton_ai_safe_context.py`: aliases temporários da operação
- `external_ai_masking.py`: mascaramento field-aware de chaves estruturadas conhecidas; permanece como camada inferior, não como contrato definitivo

## O que permanece

- documento original local não é reescrito
- isolamento Multiuser (conta comum não autoriza artefato privado)
- dados logísticos necessários à tarefa
- Roberto não ganha nova memória; o contexto que ele envia também passa pelo wrapper de Cleiton

## O que não deve ser prometido

- OCR de PDF escaneado
- desligar a proteção por configuração
- nova memória persistente de aliases
- anonimização absoluta de qualquer conteúdo visual não extraível

## Contexto Multiuser

A governança de saída não substitui autorização. Antes de montar contexto para Júlia, Cleide/AgenteAudita ou AgenteCompara, os fluxos preservam ownership e escopo da sessão; a Conta comum não autoriza incluir documentos ou memória privada de outro membro. Ver [Multiuser V1](multiuser_v1.md#privacidade-entre-membros).
