# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio do MBA Engenharia de Software com IA (curso de MCP e A2A).

Há dois processos separados que conversam só por HTTP:

| Processo | Porta | Papel | Código |
|---|---|---|---|
| **Servidor MCP** `central-de-salas` | `7301` (`/mcp`) | Streamable HTTP stateless, 3 tools, 1 resource, MRTR na reserva | [`servidor-mcp/`](servidor-mcp/) |
| **Agente** `Central de Salas` | `7300` (`/a2a`, `/.well-known/agent-card.json`) | Por fora é servidor A2A v1.0 (JSON-RPC). Por dentro é host MCP | [`agente/`](agente/) |

```
cliente A2A ──SendMessage──▶ agente (A2A) ──tools/call──▶ servidor MCP
            ◀─INPUT_REQUIRED─  [requestState guardado ◀─input_required──
                                 na Task, nunca exposto]  + requestState
            ──escolha=X──────▶               ──tools/call (id novo)──▶
                                             inputResponses + requestState
            ◀─COMPLETED+artifact─            ◀──── complete ─────────
```

Stack: Python 3.12, SDK oficial `mcp==2.3.0` (alinhado à revisão `2026-07-28`) no servidor e no cliente MCP do agente, Starlette e uvicorn para o lado A2A. Não há LLM nem SDK de provedor de LLM: o agente interpreta o pedido por regra fixa.

## Como rodar

Pré-requisitos: Python 3.10 ou superior e [uv](https://docs.astral.sh/uv/). O arquivo `.python-version` fixa o 3.12, e o `uv` baixa essa versão sozinho se ela não estiver instalada.

```bash
git clone https://github.com/JonasMazza/desafio-a2a-com-mcp.git
```

```bash
cd desafio-a2a-com-mcp && uv sync
```

**Terminal 1, servidor MCP.** A chave de integridade do `requestState` vem da variável `REQUEST_STATE_SECRET` e precisa ter no mínimo 32 bytes de aleatoriedade. Gere a sua (nunca a coloque no repositório):

```bash
export REQUEST_STATE_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
```

```bash
uv run python servidor-mcp/servidor.py
```

Para testar o retry depois de um restart (passo 12 do avaliador), reinicie o servidor **no mesmo terminal**, com a mesma `REQUEST_STATE_SECRET` exportada. Uma chave nova invalida, como deve, os `requestState` emitidos com a chave anterior.

**Terminal 2, agente** (não precisa do segredo, porque nunca abre o `requestState`):

```bash
uv run python agente/agente.py
```

**Terminal 3, validador** (com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

<details>
<summary>Sem uv (pip + venv)</summary>

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Depois troque `uv run python` por `.venv/bin/python` nos comandos acima.
</details>

Variáveis opcionais: `MCP_PORT`/`MCP_HOST` (servidor), `AGENT_PORT`/`AGENT_HOST`/`AGENT_PUBLIC_URL`/`MCP_URL` (agente). Os padrões são as portas do enunciado.

### O que olhar no stderr do servidor MCP

Cada request recebido gera uma linha com método, id, `traceparent`, capabilities e os headers espelhados:

```
[mcp] method=tools/list id=4 traceparent=00-da64b0c9718adc9f20a372d91803d4a0-485659f5d31e4f1f-01 caps={"elicitation":{"form":{},"url":{}}} hdr[Mcp-Method=tools/list Mcp-Name=-]
[mcp] method=resources/read id=5 traceparent=00-da64b0c9718adc9f20a372d91803d4a0-... hdr[Mcp-Method=resources/read Mcp-Name=politica://uso]
[mcp] method=tools/call id=6 traceparent=00-da64b0c9718adc9f20a372d91803d4a0-783600be5b882428-01 name=reservar_sala ... hdr[Mcp-Method=tools/call Mcp-Name=reservar_sala]
[mcp] method=tools/call id=7 traceparent=00-da64b0c9718adc9f20a372d91803d4a0-26aded4cc08fbcaa-01 name=reservar_sala (retry MRTR) ...
```

Nesse trecho dá para ver o `tools/list` antes do primeiro `tools/call`, o trace-id do validador propagado com um span-id novo a cada request, e o retry com id diferente do request inicial (6 → 7). O stderr do agente mostra as transições de cada Task (`SUBMITTED -> WORKING -> INPUT_REQUIRED ...`).

## Onde a ponte acontece

**MCP `input_required` → A2A `TASK_STATE_INPUT_REQUIRED`.** O host MCP chama a tool com `allow_input_required=True` ([`agente/host_mcp.py:157`](agente/host_mcp.py:157)), então o SDK devolve o `InputRequiredResult` cru e não tenta responder a elicitation sozinho. Em [`agente/ponte.py:152`](agente/ponte.py:152), `_traduzir` reconhece o `InputRequiredResult` e chama `_pausar` ([`agente/ponte.py:179`](agente/ponte.py:179)). Essa função extrai a chave do `inputRequests`, o nome do campo e o `enum` do `requestedSchema`, guarda tudo junto com o `requestState` opaco no objeto `Pausa` daquela Task ([`agente/ponte.py:202`](agente/ponte.py:202)) e move a Task para `TASK_STATE_INPUT_REQUIRED` com a linha `alternativas: ...` ([`agente/ponte.py:211`](agente/ponte.py:211)). A `Pausa` mora na parte interna da Task ([`agente/tarefas.py`](agente/tarefas.py)), que `para_wire()` nunca serializa. Por isso o `requestState` não aparece no card, no artifact nem em mensagem alguma.

**O `requestState` voltando ao servidor.** O `SendMessage` com `taskId` cai em `Ponte.continuar` ([`agente/ponte.py:110`](agente/ponte.py:110)). Uma escolha fora do `enum` mantém a Task pausada e repete a lista. Uma escolha válida vira `ElicitResult(action="accept")`, e `escolha=recusar` vira `action="decline"`. O agente então repete o `tools/call` original com os mesmos argumentos, `inputResponses` com **a mesma chave** recebida e o `requestState` **ecoado sem modificação** ([`agente/ponte.py:136-137`](agente/ponte.py:136)). O id JSON-RPC é novo porque o cliente do SDK numera cada request. Do lado do servidor, o `RequestStateBoundary` do SDK verifica e abre o estado antes de a tool rodar, e `_retomar` ([`servidor-mcp/servidor.py:197`](servidor-mcp/servidor.py:197)) reconstrói o pedido **a partir do próprio `requestState`** ([`servidor-mcp/servidor.py:205`](servidor-mcp/servidor.py:205)), não dos `arguments` reenviados.

Do lado do servidor, a primeira perna do MRTR está em `_pedir_escolha` ([`servidor-mcp/servidor.py:161`](servidor-mcp/servidor.py:161)). Ela checa a capability de elicitation em form mode no `_meta` **deste** request (sem ela, responde `-32021` com `data.requiredCapabilities` e HTTP 400) e **termina a resposta** com `InputRequiredResult`. Não há callback nem canal de volta: quem volta é o cliente, com um request novo.

## Decisões técnicas

**Proteção do `requestState`.** Usei o utilitário do próprio SDK: `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600, audience="central-de-salas")` ([`servidor-mcp/servidor.py:110`](servidor-mcp/servidor.py:110)), que instala o middleware `RequestStateBoundary`.
- **AES-256-GCM** (AEAD) com chave derivada do segredo por HKDF-SHA256. O estado fica cifrado e autenticado, e trocar um caractere quebra a tag GCM, o que vira `-32602 "Invalid or expired requestState"`. O motivo real vai só para o log (`requestState rejected on tools/call: seal`).
- O envelope selado carrega `iat`/`exp` e um **binding** com o método, o nome da tool e um digest SHA-256 dos `arguments`. Um retry com argumentos adulterados é rejeitado com `-32602` (`request binding`). Mesmo que passasse, `_retomar` só usa os valores selados.
- A chave nunca está no código. O servidor se recusa a subir sem `REQUEST_STATE_SECRET` ou com menos de 32 bytes.

**Validade.** São 10 minutos (`ttl=600`), dentro da faixa de 5 a 30 minutos exigida. Como o pedido inteiro (sala, intervalo, responsável e as alternativas oferecidas) viaja dentro do `requestState` e o servidor não guarda nada entre as duas pernas, um retry válido funciona depois de um restart do servidor MCP, desde que o segredo seja o mesmo. No retry o servidor confere se a escolha está entre as alternativas seladas e se a sala continua livre.

**MRTR escrito à mão em vez de `Resolve`/`Elicit`.** O SDK oferece resolvers automáticos (`Annotated[T, Resolve(fn)]`), mas preferi a tool devolver `InputRequiredResult` explicitamente e ler `ctx.input_responses`/`ctx.request_state`. Assim o ciclo fica visível no código: o caminho de ida, o de volta e o que vai selado. A chave do `inputRequests` é `escolha_de_sala`. O agente não fixa esse texto: ele copia a chave que veio.

**Estado das Tasks.** As Tasks ficam em memória no processo do agente ([`agente/tarefas.py`](agente/tarefas.py)), num dicionário `task_id → Task`. Cada Task tem uma parte pública (id, contextId, status, history, artifacts) e uma parte interna (a `Pausa` com o `requestState`, o trace-id e a versão da política). Cada Task também tem o próprio `asyncio.Lock`, então duas Tasks pausadas ao mesmo tempo nunca trocam de `requestState`. Estado terminal é definitivo: `SendMessage` numa Task `COMPLETED`/`CANCELED`/`FAILED`/`REJECTED` recebe o erro A2A `-32004` (UnsupportedOperation), e um `taskId` desconhecido recebe `-32001` (TaskNotFound).

**Agente como host MCP.** Uso o `mcp.Client` oficial com protocolo fixo em `2026-07-28`. Ele carimba `protocolVersion` e `clientCapabilities` no `_meta` de todo request e espelha `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` nos headers. Cada Task nova faz `tools/list` (o agente não tem lista fixa de tools; os argumentos são filtrados pelo `inputSchema` descoberto), depois `resources/read politica://uso` (de onde sai a versão carimbada no artifact) e por fim o `tools/call`. O `traceparent` do header A2A é propagado no `_meta` de todos os requests MCP da Task com o mesmo trace-id e um span-id novo. Sem header, a Task ganha um trace-id próprio. Mantenho um único objeto cliente vivo, e um supervisor o recria se o transporte cair (servidor MCP reiniciado ou fora do ar). Nesse caso a Task em andamento termina em `FAILED` com "Servidor MCP indisponivel", e a próxima segue normalmente.

**Lado A2A escrito à mão.** O binding JSON-RPC precisa de dois métodos, `SendMessage` e `GetTask`. Implementei direto em Starlette para que o formato (`result.task`, `TASK_STATE_*`, `ROLE_AGENT`, card com `supportedInterfaces[]`) bata com `exemplos/wire/`. Um pedido fora do formato termina em `TASK_STATE_REJECTED`. Um erro de execução da tool termina em `TASK_STATE_FAILED` com o texto exato da tool no histórico. Uma recusa termina em `TASK_STATE_CANCELED`.

**Domínio separado do protocolo.** As regras de sala (janela 08:00–20:00 em -03:00, no máximo 2 h, sobreposição, alternativas ordenadas por capacidade e depois por `id`, no máximo 3) ficam em [`servidor-mcp/dominio.py`](servidor-mcp/dominio.py). O agente nunca importa esse módulo. As reservas criadas ficam em memória.

### Limitação documentada do SDK

O enunciado pede que o agente declare `{"elicitation": {"form": {}}}`. O cliente do SDK 2.3.0 declara a capability de elicitation automaticamente quando recebe um `elicitation_callback`, e sempre com os dois modos. Isso está fixo no código, em `mcp/client/session.py:635`:

```python
elicitation = (
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
    if self._elicitation_callback is not _default_elicitation_callback
    else None
)
```

Por isso o log mostra `caps={"elicitation":{"form":{},"url":{}}}`. Form mode está declarado, que é o que o servidor exige. O `url` a mais vem do SDK. Segui a orientação do enunciado e não reescrevi o cliente para contornar isso. O callback passado ao SDK nunca é acionado: com `allow_input_required=True`, o `input_required` volta cru para o agente.

## Saída do validador

Última execução, com os dois processos recém-iniciados a partir de um clone limpo:

```
SAIDA_DO_VALIDADOR
```
