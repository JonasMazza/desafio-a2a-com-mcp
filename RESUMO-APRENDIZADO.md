# Resumo & Aprendizado: Desafio A Ponte (MCP + A2A)

## 1. O que o desafio pedia (em 1 frase)
Construir um **servidor MCP** de reserva de salas e um **agente** que é cliente MCP por dentro e servidor A2A por fora, e costurar os dois mecanismos de estado sem sessão: o `requestState` do MCP e a **Task** do A2A.

## 2. O que foi feito (fluxo)
1. **`servidor-mcp/`**: `MCPServer` do SDK `mcp==2.3.0`, Streamable HTTP **stateless** na porta 7301.
   - Tools: `listar_salas`, `consultar_disponibilidade`, `reservar_sala`.
   - Resource: `politica://uso`.
   - As regras de sala ficam isoladas em `dominio.py`.
2. **MRTR em `reservar_sala`**: no conflito, a tool **termina a resposta** com `InputRequiredResult`, que leva uma elicitation em form mode com o `enum` de alternativas e o `requestState` com o pedido selado. No retry, ela lê `ctx.input_responses` e `ctx.request_state` e conclui.
3. **`agente/host_mcp.py`**: `mcp.Client` oficial com `allow_input_required=True`, para receber o `input_required` **cru** em vez de o SDK responder sozinho.
4. **`agente/ponte.py`**: traduz `input_required` → `TASK_STATE_INPUT_REQUIRED`, guarda o `requestState` na Task e, na continuação, refaz o `tools/call` com id novo.
5. **`agente/agente.py`**: Agent Card + JSON-RPC (`SendMessage`, `GetTask`).
6. Validador: **36/36**, conferido a partir de um clone limpo. Os passos manuais 7 a 13 do avaliador também foram testados, incluindo o restart do servidor entre o `input_required` e o retry.

## 3. Conceitos-chave (o "porquê")
| Conceito | Em uma frase | Onde ver no código |
|---|---|---|
| **Sem sessão** | Cada request MCP carrega no `_meta` a versão do protocolo e as capabilities. O servidor não lembra de nada entre requests. | `_declarou_elicitation_form` em `servidor.py` |
| **MRTR ≠ callback** | O servidor não "pergunta" no meio da execução. Ele **devolve** a pergunta, e o cliente volta com um **request novo** (id novo). | `_pedir_escolha` / `_retomar` |
| **`requestState` é entrada hostil** | Ele volta pelas mãos do cliente, então precisa de integridade (AES-GCM + expiração + binding com tool e argumentos). Se for adulterado, a resposta é `-32602`. | `RequestStateSecurity` em `servidor.py` |
| **Estado viaja, não fica** | O pedido inteiro vai selado no `requestState`, por isso o retry funciona depois de um restart do servidor. | `pedido = json.loads(ctx.request_state)` |
| **Capability por request** | Sem `elicitation.form` no `_meta` deste request, a resposta é `-32021` com `requiredCapabilities` e HTTP 400. | `_pedir_escolha` |
| **Task A2A** | Tem identidade (`id`, `contextId`), estado (`SUBMITTED → WORKING → INPUT_REQUIRED → COMPLETED/CANCELED/FAILED`) e produto (artifact). Estado terminal é definitivo. | `agente/tarefas.py` |
| **A ponte** | O `requestState` (MCP) fica guardado **dentro** da Task (A2A), opaco: o agente guarda e ecoa, sem nunca abrir. | `_pausar` / `continuar` em `ponte.py` |
| **Opacidade A2A** | Quem chama o agente não sabe se do outro lado há LLM, grafo ou `if`. Aqui é um `if`. | `interpretar_pedido` |
| **Trace context** | O trace-id do header `traceparent` (A2A) vai para o `_meta.traceparent` de todo request MCP, com um span-id novo a cada um. | `traceparent()` em `host_mcp.py` |

## 4. As 3 lições mais valiosas (que só aparecem fazendo)
1. **O SDK já tinha quase tudo.** `RequestStateSecurity` sela e verifica o `requestState` antes de a tool rodar, e ainda amarra o estado aos argumentos originais. Escrever uma assinatura própria seria pior e mais longo. A pista do enunciado ("os SDKs oferecem um utilitário pronto") era literal.
2. **`allow_input_required=True` é o divisor de águas no cliente.** Sem esse parâmetro, o `Client` resolve o MRTR sozinho (com o callback de elicitation) e a Task nunca pausa. Ele é a diferença entre "o agente responde por conta própria" e "o agente devolve a pergunta ao cliente A2A".
3. **Cliente MCP de longa duração precisa se recuperar.** Quando o servidor MCP caiu, o `Client` do SDK ficou morto ("Connection closed") mesmo depois de o servidor voltar. A solução foi um supervisor que recria o cliente. Manter o objeto vivo não é sessão de protocolo, mas exige cuidado com o transporte.

## 5. O que você teria aprendido fazendo manualmente
- Ler `exemplos/wire/` antes de codar e perceber que a chave `__main__:escolha_de_sala` e o prefixo `v1.` do `requestState` denunciam a implementação de referência: Python, com `Resolve`/`Elicit` e `RequestStateBoundary`.
- Tentar `ctx.elicit()` no transporte stateless e tomar o erro de "sem canal de volta". É a primeira fricção plantada no desafio.
- Como o SDK traduz códigos JSON-RPC em status HTTP: `-32602`, `-32021` e `-32020` viram 400 no Streamable HTTP.
- A diferença entre **erro de execução** (`isError: true`, que vira `TASK_STATE_FAILED` com a mensagem no histórico) e **erro de protocolo** (`error` JSON-RPC).

## 6. Resultado final
- `python3 validador/validar.py` → **36 passaram, 0 falharam**, exit 0.
- Repositório: https://github.com/JonasMazza/desafio-a2a-com-mcp (branch `main`).
