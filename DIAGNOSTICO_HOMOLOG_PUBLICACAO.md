# Diagnóstico de Homologação e Publicação

Este documento complementa o `README.md` principal com foco exclusivo em go/no-go de homologação.

## Estado Atual Confirmado

- pacote funcional da Fase 2 integrado localmente;
- chat da Júlia atualizado com renderer markdown seguro, sugestões clicáveis e busca web contextual filtrada;
- detalhe de notícia/artigo ajustado com botão `Voltar Para Home`;
- publicação final em homolog ainda depende da validação completa de migrations no ambiente alvo.

## Escopo Operacional Sensível

Não tratar como opcional:

- governança operacional por franquia;
- autorização operacional usada pelo chat da Júlia e pelo upload Roberto;
- identidade de consumo por conta/franquia/usuário;
- migrations da cadeia ativa;
- telas admin alinhadas com o backend.

## Bloqueio Histórico de Homolog

O ponto crítico conhecido continua sendo a estratégia de migrations no runtime de homolog.  
Sem confirmar `upgrade head` e `current` no ambiente alvo, não há homolog concluída.

## Go / No-Go

### No-Go

- migrations não executadas ou não confirmadas;
- schema não validado;
- health checks ou fluxos reais não validados.

### Go

Somente quando todos forem verdadeiros:

1. migrations aplicadas sem erro;
2. schema validado no banco alvo;
3. health checks ok;
4. cron protegido validado;
5. telas admin ok;
6. chat Júlia validado com:
   - autorização por franquia
   - sugestões
   - busca web contextual
   - markdown seguro
7. upload Roberto validado.

## Referência Principal

Detalhes funcionais, fluxos e regras críticas ficam consolidados no `README.md` da raiz.
