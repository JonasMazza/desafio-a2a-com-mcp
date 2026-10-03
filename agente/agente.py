"""Agente "Central de Salas": servidor A2A v1.0 (JSON-RPC 2.0 sobre HTTP) na porta 7300.

- GET  /.well-known/agent-card.json  -> Agent Card (identidade publica, descoberta)
- POST /a2a                          -> SendMessage e GetTask

Por fora ele e um agente A2A; por dentro e um host MCP (host_mcp.py). A costura
entre os dois fica em ponte.py. Nao ha LLM: o pedido chega em formato fixo e e
interpretado por regra, entao o mesmo pedido produz sempre o mesmo resultado.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import sys
from collections.abc import AsyncIterator
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from host_mcp import HostMCP
from ponte import Ponte, texto_da_mensagem
from tarefas import INPUT_REQUIRED, Tarefas

HOST = os.environ.get("AGENT_HOST", "127.0.0.1")
PORTA = int(os.environ.get("AGENT_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENT_PUBLIC_URL", f"http://{HOST}:{PORTA}")

# Codigos de erro JSON-RPC: os padrao e os especificos do A2A v1.0.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004

RE_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-(?P<trace>[0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")


def agent_card() -> dict:
    """Agent Card na forma da v1.0: url, binding e versao vivem em supportedInterfaces[]."""
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": "1.0.0",
        "supportedInterfaces": [
            {"url": f"{URL_PUBLICA}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [
                    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                    "fim=2026-11-03T15:00:00-03:00 responsavel=Marty",
                    "escolha=sala-fusca",
                    "escolha=recusar",
                ],
            }
        ],
    }


class ErroRPC(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def trace_id_do_header(request: Request) -> str | None:
    m = RE_TRACEPARENT.match(request.headers.get("traceparent", "").strip().lower())
    return m.group("trace") if m else None


class AgenteA2A:
    def __init__(self) -> None:
        self.tarefas = Tarefas()
        self.host = HostMCP()
        self.ponte = Ponte(self.host)

    # ------------------------------------------------------------ HTTP

    async def card(self, request: Request) -> JSONResponse:
        return JSONResponse(agent_card())

    async def rpc(self, request: Request) -> JSONResponse:
        try:
            corpo = json.loads(await request.body())
        except ValueError:
            return self._erro(None, PARSE_ERROR, "Parse error")
        if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or "method" not in corpo:
            return self._erro(corpo.get("id") if isinstance(corpo, dict) else None, INVALID_REQUEST, "Invalid Request")

        rid, metodo, params = corpo.get("id"), corpo["method"], corpo.get("params") or {}
        print(f"[a2a] {metodo} id={rid} traceparent={request.headers.get('traceparent', '-')}", file=sys.stderr, flush=True)
        try:
            if metodo == "SendMessage":
                resultado = await self.send_message(params, trace_id_do_header(request))
            elif metodo == "GetTask":
                resultado = self.get_task(params)
            else:
                raise ErroRPC(METHOD_NOT_FOUND, f"Metodo nao suportado: {metodo}")
        except ErroRPC as e:
            return self._erro(rid, e.code, e.message)
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": resultado})

    @staticmethod
    def _erro(rid: Any, code: int, message: str) -> JSONResponse:
        return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    # ------------------------------------------------------------ metodos A2A

    async def send_message(self, params: dict, trace_id: str | None) -> dict:
        mensagem = params.get("message")
        if not isinstance(mensagem, dict) or not mensagem.get("messageId"):
            raise ErroRPC(INVALID_PARAMS, "params.message com messageId e obrigatorio")
        texto = texto_da_mensagem(mensagem)
        task_id = mensagem.get("taskId")

        if not task_id:
            # Task nova, com id e contextId proprios. Sem traceparent no header,
            # a Task ganha um trace-id proprio, usado em todos os requests MCP dela.
            task = self.tarefas.criar(mensagem.get("contextId"), trace_id or secrets.token_hex(16))
            async with task.trava:
                task.registrar_usuario(mensagem)
                await self.ponte.iniciar(task, texto)
            return {"task": task.para_wire()}

        task = self.tarefas.obter(task_id)
        if task is None:
            raise ErroRPC(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
        async with task.trava:
            # Estado terminal e definitivo: nada volta a WORKING.
            if task.terminal:
                raise ErroRPC(UNSUPPORTED_OPERATION, f"Task {task_id} ja terminou em {task.estado}")
            if task.estado != INPUT_REQUIRED or task.pausa is None:
                raise ErroRPC(UNSUPPORTED_OPERATION, f"Task {task_id} nao esta aguardando input")
            if trace_id:
                task.trace_id = trace_id
            task.registrar_usuario(mensagem)
            await self.ponte.continuar(task, texto)
        return {"task": task.para_wire()}

    def get_task(self, params: dict) -> dict:
        task = self.tarefas.obter(str(params.get("id", "")))
        if task is None:
            raise ErroRPC(TASK_NOT_FOUND, f"Task nao encontrada: {params.get('id')}")
        return {"task": task.para_wire()}


def criar_app() -> Starlette:
    agente = AgenteA2A()

    @contextlib.asynccontextmanager
    async def ciclo_de_vida(app: Starlette) -> AsyncIterator[None]:
        async with agente.host:
            print(f"[a2a] Central de Salas em {URL_PUBLICA}/a2a, MCP em {os.environ.get('MCP_URL', 'http://127.0.0.1:7301/mcp')}",
                  file=sys.stderr, flush=True)
            yield

    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", agente.card, methods=["GET"]),
            Route("/a2a", agente.rpc, methods=["POST"]),
        ],
        lifespan=ciclo_de_vida,
    )


if __name__ == "__main__":
    uvicorn.run(criar_app(), host=HOST, port=PORTA, log_level="warning")
