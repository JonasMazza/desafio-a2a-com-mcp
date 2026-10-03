"""Servidor MCP "central-de-salas": Streamable HTTP stateless na porta 7301, endpoint /mcp.

Tres tools (listar_salas, consultar_disponibilidade, reservar_sala), um resource
(politica://uso) e o ciclo de MRTR (multi round-trip request) em reservar_sala.

O MRTR aqui NAO e callback: no conflito a tool termina a resposta com
`InputRequiredResult` (resultType "input_required"), levando a pergunta
(elicitation em form mode) e um `requestState`. O cliente volta depois com um
request novo trazendo `inputResponses` + `requestState`. Entre as duas pontas o
servidor nao guarda nada: o pedido original viaja selado dentro do requestState.

Quem sela/verifica o requestState e o `RequestStateBoundary` do SDK (AES-256-GCM
com chave derivada de REQUEST_STATE_SECRET via HKDF). A tool so ve texto puro que
ela mesma produziu; qualquer adulteracao vira -32602 antes de a tool rodar.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp_types import (
    INVALID_PARAMS,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ElicitRequest,
    ElicitRequestFormParams,
    InputRequiredResult,
)
from pydantic import BaseModel

import dominio
from dominio import RegraVioladaError

NOME = "central-de-salas"
PORTA = int(os.environ.get("MCP_PORT", "7301"))
HOST = os.environ.get("MCP_HOST", "127.0.0.1")

# Chave do unico inputRequest que esta tool emite. O cliente devolve a mesma chave
# em inputResponses; o agente nao fixa esse texto, ele copia o que veio.
CHAVE_ESCOLHA = "escolha_de_sala"
# Validade do requestState: 10 minutos (o enunciado exige entre 5 e 30).
TTL_REQUEST_STATE = 600.0


def log(msg: str) -> None:
    # stderr no lugar do logging de protocolo (notifications/message), que esta depreciado.
    print(msg, file=sys.stderr, flush=True)


def carregar_segredo() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou curto (minimo 32 bytes). Gere com:\n"
            '  export REQUEST_STATE_SECRET="$(python3 -c \'import secrets; print(secrets.token_hex(32))\')"'
        )
    return segredo


# ---------------------------------------------------------------- schemas de saida


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# ---------------------------------------------------------------- servidor

mcp = MCPServer(
    NOME,
    version="1.0.0",
    request_state_security=RequestStateSecurity(
        keys=[carregar_segredo()],
        ttl=TTL_REQUEST_STATE,
        audience=NOME,
    ),
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in dominio.SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    try:
        ini, fi = dominio.validar(sala, inicio, fim)
    except RegraVioladaError as e:
        raise ToolError(str(e)) from None
    em_conflito = dominio.conflitos(sala, ini, fi)
    return Disponibilidade(
        sala=sala,
        livre=not em_conflito,
        conflitos=[ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in em_conflito],
    )


def _reserva_concluida(reserva: dict) -> ReservaOut:
    return ReservaOut(
        reserva=reserva["id"],
        reservado=True,
        sala=reserva["sala"],
        inicio=reserva["inicio"],
        fim=reserva["fim"],
        responsavel=reserva["responsavel"],
        politica=dominio.POLITICA_VERSAO,
    )


def _declarou_elicitation_form(ctx: Context) -> bool:
    """Capability negociada POR REQUEST, lida do _meta deste request (nunca de um anterior).

    O que se exige e elicitation em form mode: `{"elicitation": {"form": {}}}`.
    Um `{"elicitation": {}}` puro (forma anterior aos modes) tambem conta como form;
    so `url` sem `form` nao conta.
    """
    caps = ctx.client_capabilities
    eli = caps.elicitation if caps is not None else None
    return eli is not None and (eli.form is not None or eli.url is None)


def _pedir_escolha(ctx: Context, pedido: dict) -> InputRequiredResult:
    """Primeira perna do MRTR: termina a resposta pedindo informacao."""
    if not _declarou_elicitation_form(ctx):
        # O transporte traduz -32021 para HTTP 400.
        raise MCPError(
            code=MISSING_REQUIRED_CLIENT_CAPABILITY,
            message="Client did not declare the form elicitation capability required by reservar_sala",
            data={"requiredCapabilities": {"elicitation": {"form": {}}}},
        )
    pergunta = ElicitRequest(
        params=ElicitRequestFormParams(
            mode="form",
            message="A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
            requested_schema={
                "type": "object",
                "properties": {
                    "sala": {
                        "type": "string",
                        "title": "Sala",
                        "description": "Sala alternativa escolhida",
                        "enum": pedido["alternativas"],
                    }
                },
                "required": ["sala"],
            },
        )
    )
    # O requestState leva o pedido inteiro. Aqui ele sai em texto puro; o
    # RequestStateBoundary do SDK o sela (AES-GCM + exp + binding com a tool e os
    # argumentos) antes de ir para o fio.
    return InputRequiredResult(
        input_requests={CHAVE_ESCOLHA: pergunta},
        request_state=json.dumps(pedido, separators=(",", ":")),
    )


def _retomar(ctx: Context) -> ReservaOut:
    """Segunda perna do MRTR: o request novo traz inputResponses + requestState.

    O requestState ja chegou verificado e aberto pelo boundary (adulterado ou
    expirado nem chega aqui: vira -32602). O pedido e reconstruido a partir DELE,
    e nao dos `arguments` reenviados, que sao entrada nao confiavel.
    """
    assert ctx.request_state is not None
    pedido = json.loads(ctx.request_state)
    resposta = (ctx.input_responses or {}).get(CHAVE_ESCOLHA)
    if resposta is None or getattr(resposta, "action", None) is None:
        raise MCPError(code=INVALID_PARAMS, message=f"inputResponses sem a chave {CHAVE_ESCOLHA!r}")

    if resposta.action in ("decline", "cancel"):
        # Recusa nao e erro: conclui sem reservar.
        return ReservaOut(reservado=False, motivo="recusado")

    escolha = (resposta.content or {}).get("sala")
    if escolha not in pedido["alternativas"]:
        raise ToolError(f"Escolha invalida: {escolha} nao esta entre as alternativas oferecidas")

    ini, fi = dominio.validar(escolha, pedido["inicio"], pedido["fim"])
    if dominio.conflitos(escolha, ini, fi):
        raise ToolError(f"A sala {escolha} deixou de estar livre no intervalo")
    reserva = dominio.criar_reserva(escolha, pedido["inicio"], pedido["fim"], pedido["responsavel"])
    return _reserva_concluida(reserva)


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str, ctx: Context) -> ReservaOut | InputRequiredResult:
    try:
        if ctx.request_state is not None:
            return _retomar(ctx)

        ini, fi = dominio.validar(sala, inicio, fim)
        if not dominio.conflitos(sala, ini, fi):
            return _reserva_concluida(dominio.criar_reserva(sala, inicio, fim, responsavel))

        opcoes = dominio.alternativas(sala, ini, fi)
        if not opcoes:
            raise RegraVioladaError(dominio.ERRO_SEM_ALTERNATIVA)
        pedido = {
            "sala": sala,
            "inicio": inicio,
            "fim": fim,
            "responsavel": responsavel,
            "alternativas": opcoes,
        }
        return _pedir_escolha(ctx, pedido)
    except RegraVioladaError as e:
        raise ToolError(str(e)) from None


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas. A primeira linha declara a versao.")
def politica() -> str:
    return dominio.POLITICA_TEXTO


# ---------------------------------------------------------------- log de cada request


class LogDeRequests:
    """Middleware ASGI: registra no stderr metodo, id e traceparent de TODO request.

    Fica na frente do transporte para pegar inclusive os que o SDK rejeita antes
    de chegar ao handler (por exemplo, _meta sem os campos obrigatorios).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        corpo = b""
        mensagens = []
        while True:
            msg = await receive()
            mensagens.append(msg)
            if msg["type"] == "http.request":
                corpo += msg.get("body", b"")
                if not msg.get("more_body"):
                    break
            else:
                break
        cabecalhos = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        self._registrar(corpo, cabecalhos)

        async def reenviar() -> dict:
            if mensagens:
                return mensagens.pop(0)
            return await receive()

        await self.app(scope, reenviar, send)

    @staticmethod
    def _registrar(corpo: bytes, cabecalhos: dict[str, str]) -> None:
        try:
            req = json.loads(corpo)
        except (ValueError, UnicodeDecodeError):
            log("[mcp] request com corpo nao-JSON")
            return
        for item in req if isinstance(req, list) else [req]:
            if not isinstance(item, dict):
                continue
            params = item.get("params") or {}
            meta = params.get("_meta") or {} if isinstance(params, dict) else {}
            extra = ""
            if item.get("method") == "tools/call":
                extra = f" name={params.get('name')}"
                if "requestState" in params:
                    extra += " (retry MRTR)"
            caps = meta.get("io.modelcontextprotocol/clientCapabilities")
            log(
                f"[mcp] method={item.get('method')} id={item.get('id')}"
                f" traceparent={meta.get('traceparent', '-')}{extra}"
                f" caps={json.dumps(caps, separators=(',', ':')) if caps is not None else '-'}"
                f" hdr[Mcp-Method={cabecalhos.get('mcp-method', '-')} Mcp-Name={cabecalhos.get('mcp-name', '-')}]"
            )


def main() -> None:
    app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host=HOST)
    log(f"[mcp] {NOME} ouvindo em http://{HOST}:{PORTA}/mcp (politica {dominio.POLITICA_VERSAO})")
    uvicorn.run(LogDeRequests(app), host=HOST, port=PORTA, log_level="warning")


if __name__ == "__main__":
    main()
