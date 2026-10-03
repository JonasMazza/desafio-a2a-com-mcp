"""O agente como HOST MCP: fala com o servidor de salas por HTTP, como cliente MCP de verdade.

Usa o cliente oficial do SDK (`mcp.Client`) em modo de protocolo fixo 2026-07-28.
Pontos que importam para o desafio:

- Nada de sessao: cada request leva no `_meta` a versao do protocolo e as
  clientCapabilities (o SDK carimba isso em todo request), e o SDK espelha
  `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` nos headers HTTP.
- O `traceparent` vai no `_meta` de cada request com o trace-id da Task e um
  span-id novo por request.
- `allow_input_required=True`: o SDK NAO resolve o MRTR sozinho. Ele devolve o
  `InputRequiredResult` cru para o agente, que transforma isso em pausa da Task.
  O `elicitation_callback` existe apenas para o SDK declarar a capability de
  elicitation; ele nunca e chamado (se fosse, a Task nunca pausaria).
"""

from __future__ import annotations

import os
import secrets
import sys
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import anyio
import anyio.abc
import mcp_types as types
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.client.session import ClientRequestContext

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
PROTOCOLO = "2026-07-28"
T = TypeVar("T")
URI_POLITICA = "politica://uso"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def traceparent(trace_id: str) -> str:
    """W3C trace context: mesmo trace-id da Task, span-id novo a cada request MCP."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


async def _nunca_responder_sozinho(ctx: ClientRequestContext, params: types.ElicitRequestParams) -> types.ErrorData:
    # Com allow_input_required=True este callback nao e acionado: o input_required
    # volta cru para o agente. Ele so existe para o SDK anunciar a capability.
    return types.ErrorData(code=types.INTERNAL_ERROR, message="o agente nao responde elicitation sozinho")


def _conexao_caiu(exc: BaseException) -> bool:
    """Erro de protocolo devolvido pelo servidor (-32602, -32021...) nao derruba o cliente;
    queda de transporte (conexao fechada, servidor fora do ar) derruba."""
    return not isinstance(exc, MCPError) or exc.code == types.CONNECTION_CLOSED


class HostMCP:
    """Um objeto cliente vivo durante todo o processo (recomendado pelo enunciado).

    Manter o objeto vivo nao e sessao de protocolo: o servidor e stateless e
    nenhum request depende de outro anterior. Se o transporte cair (servidor MCP
    reiniciado ou fora do ar), o cliente do SDK fica inutilizavel; um supervisor
    descarta e recria o objeto, e o proximo request segue normalmente.
    """

    def __init__(self, url: str = MCP_URL) -> None:
        self._url = url
        self._client: Client | None = None
        self._pronto = anyio.Event()
        self._reiniciar = anyio.Event()
        self._grupo: anyio.abc.TaskGroup | None = None

    async def __aenter__(self) -> HostMCP:
        self._grupo = anyio.create_task_group()
        await self._grupo.__aenter__()
        self._grupo.start_soon(self._supervisionar)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        assert self._grupo is not None
        self._grupo.cancel_scope.cancel()
        await self._grupo.__aexit__(*exc)

    async def _supervisionar(self) -> None:
        # O Client e aberto e fechado sempre nesta mesma task (exigencia do anyio).
        while True:
            self._reiniciar = anyio.Event()
            try:
                async with Client(
                    self._url,
                    mode=PROTOCOLO,
                    cache=None,
                    elicitation_callback=_nunca_responder_sozinho,
                    client_info=types.Implementation(name="agente-central-de-salas", version="1.0.0"),
                ) as client:
                    self._client = client
                    self._pronto.set()
                    await self._reiniciar.wait()
            except Exception as exc:  # noqa: BLE001 - o cliente morto encerra com erro; recria
                _log(f"[agente] cliente MCP encerrado: {exc!r}")
            self._client = None
            self._pronto = anyio.Event()
            _log("[agente] recriando o cliente MCP")
            await anyio.sleep(0.2)

    async def _executar(self, operacao: Callable[[Client], Awaitable[T]]) -> T:
        while self._client is None:
            await self._pronto.wait()
        client = self._client
        try:
            return await operacao(client)
        except Exception as exc:
            if _conexao_caiu(exc) and self._client is client:
                self._reiniciar.set()
            raise

    async def descobrir_tools(self, trace_id: str) -> dict[str, types.Tool]:
        """tools/list em runtime: o agente nao carrega lista fixa de tools."""
        params = types.PaginatedRequestParams.model_validate({"_meta": {"traceparent": traceparent(trace_id)}})
        resultado = await self._executar(lambda c: c.session.list_tools(params=params))
        return {t.name: t for t in resultado.tools}

    async def ler_versao_politica(self, trace_id: str) -> str:
        """Le o resource politica://uso e extrai a versao da primeira linha (`versao: X`)."""
        resultado = await self._executar(
            lambda c: c.session.read_resource(URI_POLITICA, meta={"traceparent": traceparent(trace_id)})
        )
        texto = getattr(resultado.contents[0], "text", "") if resultado.contents else ""
        primeira = texto.splitlines()[0] if texto else ""
        chave, _, valor = primeira.partition(":")
        if chave.strip() != "versao" or not valor.strip():
            raise ValueError(f"{URI_POLITICA} sem a linha 'versao: <valor>'")
        return valor.strip()

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace_id: str,
        *,
        input_responses: types.InputResponses | None = None,
        request_state: str | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        """tools/call. Cada chamada sai com id JSON-RPC novo (o SDK numera os requests),
        entao o retry do MRTR nunca reaproveita o id do request inicial."""
        etapa = "retry MRTR" if request_state is not None else "chamada inicial"
        _log(f"[agente] tools/call {nome} ({etapa}) trace-id={trace_id}")
        return await self._executar(
            lambda c: c.session.call_tool(
                nome,
                argumentos,
                meta={"traceparent": traceparent(trace_id)},
                input_responses=input_responses,
                request_state=request_state,
                allow_input_required=True,
            )
        )
