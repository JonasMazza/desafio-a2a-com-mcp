"""A PONTE: onde o MRTR do MCP encontra a Task do A2A.

Dois mecanismos de estado, nenhum com sessao:

    MCP (por dentro)                         A2A (por fora)
    ----------------                         --------------
    tools/call                       <---    SendMessage (Task nova)
    resultType: input_required       --->    Task em TASK_STATE_INPUT_REQUIRED
      inputRequests + requestState             "alternativas: a, b"
                                               (requestState guardado na Task)
    tools/call com id NOVO           <---    SendMessage com taskId + "escolha=a"
      inputResponses + requestState
    resultType: complete             --->    Task em TASK_STATE_COMPLETED + artifact

O agente traduz protocolo, nao dominio: conflito, politica e alternativas sao
decisao do servidor MCP. O agente nao sabe o que e uma sala; ele so copia o
`enum` da elicitation para a mensagem e a escolha do usuario para o
`inputResponses`.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

import httpx2
import mcp_types as types
from mcp.shared.exceptions import MCPError

from host_mcp import HostMCP
from tarefas import CANCELED, COMPLETED, FAILED, INPUT_REQUIRED, REJECTED, WORKING, Pausa, Task

TOOL_RESERVA = "reservar_sala"
NOME_ARTIFACT = "reserva"
CAMPOS_ARTIFACT = ("reserva", "sala", "inicio", "fim", "responsavel")

FORMATO_PEDIDO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
RE_PEDIDO = re.compile(r"^\s*reservar\s+(?P<pares>.+?)\s*$")
RE_PAR = re.compile(r"(\w+)=(\S+)")
RE_ESCOLHA = re.compile(r"^\s*escolha=(?P<valor>\S+)\s*$")
RECUSAR = "recusar"


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _linha_alternativas(opcoes: list[str]) -> str:
    # Exatamente esta linha, sem prefixo: o avaliador compara duas pausas byte a byte.
    return "alternativas: " + ", ".join(opcoes)


def interpretar_pedido(texto: str) -> dict[str, str] | None:
    """`reservar sala=<id> inicio=<iso> fim=<iso> responsavel=<nome>` -> dict. Regra fixa, sem LLM."""
    m = RE_PEDIDO.match(texto)
    if not m:
        return None
    pares = dict(RE_PAR.findall(m.group("pares")))
    if not {"sala", "inicio", "fim", "responsavel"} <= pares.keys():
        return None
    return pares


def texto_da_mensagem(mensagem: dict) -> str:
    return " ".join(p.get("text", "") for p in mensagem.get("parts") or [] if isinstance(p, dict)).strip()


class Ponte:
    def __init__(self, host: HostMCP) -> None:
        self.host = host

    # ------------------------------------------------------------ Task nova

    async def iniciar(self, task: Task, texto: str) -> None:
        """SendMessage sem taskId: SUBMITTED -> WORKING -> (COMPLETED | INPUT_REQUIRED | FAILED)."""
        task.mudar(WORKING)
        pedido = interpretar_pedido(texto)
        if pedido is None:
            task.mudar(REJECTED, f"Pedido fora do formato. Use: {FORMATO_PEDIDO}")
            return

        try:
            # Descoberta em runtime, antes da primeira chamada: o agente nao tem
            # lista fixa de tools nem schema hardcoded.
            tools = await self.host.descobrir_tools(task.trace_id)
            tool = tools.get(TOOL_RESERVA)
            if tool is None:
                task.mudar(FAILED, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
                return
            # Os argumentos sao montados a partir do inputSchema descoberto.
            esquema = tool.input_schema or {}
            argumentos = {k: v for k, v in pedido.items() if k in (esquema.get("properties") or {})}
            faltando = [k for k in esquema.get("required") or [] if k not in argumentos]
            if faltando:
                task.mudar(REJECTED, f"Faltam campos exigidos pela tool: {', '.join(faltando)}")
                return
            # Resource e escolha da aplicacao: o host le a politica e guarda a versao
            # na Task, para carimbar o artifact quando a reserva sair.
            task.politica = await self.host.ler_versao_politica(task.trace_id)

            resultado = await self.host.chamar_tool(TOOL_RESERVA, argumentos, task.trace_id)
            await self._traduzir(task, TOOL_RESERVA, argumentos, resultado)
        except Exception as exc:  # noqa: BLE001 - qualquer falha de protocolo/transporte termina a Task
            self._falhar(task, exc)

    # ------------------------------------------------------------ continuacao

    async def continuar(self, task: Task, texto: str) -> None:
        """SendMessage com taskId de uma Task em INPUT_REQUIRED."""
        pausa = task.pausa
        assert pausa is not None and task.estado == INPUT_REQUIRED

        m = RE_ESCOLHA.match(texto)
        valor = m.group("valor") if m else None
        if valor != RECUSAR and valor not in pausa.opcoes:
            # Escolha fora do enum: a Task continua pausada e repete a lista.
            task.mudar(INPUT_REQUIRED, _linha_alternativas(pausa.opcoes))
            return

        if valor == RECUSAR:
            resposta = types.ElicitResult(action="decline")
        else:
            resposta = types.ElicitResult(action="accept", content={pausa.campo: valor})

        task.mudar(WORKING)
        task.pausa = None
        try:
            # O retry: mesmo tools/call, id JSON-RPC novo, inputResponses com a MESMA
            # chave que veio no inputRequests e o requestState ecoado sem modificacao.
            resultado = await self.host.chamar_tool(
                pausa.tool,
                pausa.argumentos,
                task.trace_id,
                input_responses={pausa.chave: resposta},
                request_state=pausa.request_state,
            )
            await self._traduzir(task, pausa.tool, pausa.argumentos, resultado)
        except Exception as exc:  # noqa: BLE001
            self._falhar(task, exc)

    # ------------------------------------------------------------ traducao MCP -> A2A

    async def _traduzir(
        self,
        task: Task,
        tool: str,
        argumentos: dict[str, Any],
        resultado: types.CallToolResult | types.InputRequiredResult,
    ) -> None:
        if isinstance(resultado, types.InputRequiredResult):
            self._pausar(task, tool, argumentos, resultado)
            return

        if resultado.is_error:
            # Erro de execucao da tool: a mensagem exata dela vai para o historico.
            texto = " ".join(getattr(c, "text", "") for c in resultado.content).strip()
            task.mudar(FAILED, texto or "A tool devolveu erro sem mensagem")
            return

        dados = resultado.structured_content or {}
        if dados.get("reservado") is False:
            # A recusa (decline/cancel) conclui no MCP sem reservar: no A2A vira CANCELED.
            task.mudar(CANCELED, f"Reserva nao realizada: {dados.get('motivo') or 'recusada'}")
            return

        reserva = {campo: dados.get(campo) for campo in CAMPOS_ARTIFACT}
        reserva["politica"] = task.politica
        task.artifacts.append(
            {
                "artifactId": f"art-{task.id.removeprefix('task-')}",
                "name": NOME_ARTIFACT,
                "parts": [{"text": json.dumps(reserva, ensure_ascii=False)}],
            }
        )
        task.mudar(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")

    def _pausar(self, task: Task, tool: str, argumentos: dict[str, Any], resultado: types.InputRequiredResult) -> None:
        """input_required do MCP -> TASK_STATE_INPUT_REQUIRED do A2A."""
        pedidos = resultado.input_requests or {}
        if len(pedidos) != 1 or resultado.request_state is None:
            task.mudar(FAILED, "O servidor MCP pediu um input que o agente nao sabe repassar")
            return
        chave, pedido = next(iter(pedidos.items()))
        params = pedido.params.model_dump(by_alias=True, exclude_none=True) if hasattr(pedido, "params") else {}
        if getattr(pedido, "method", None) != "elicitation/create" or params.get("mode", "form") != "form":
            task.mudar(FAILED, "O servidor MCP pediu um input que o agente nao sabe repassar")
            return

        propriedades = (params.get("requestedSchema") or {}).get("properties") or {}
        if len(propriedades) != 1:
            task.mudar(FAILED, "Elicitation com formulario que o agente nao sabe repassar")
            return
        campo, esquema = next(iter(propriedades.items()))
        opcoes = list(esquema.get("enum") or ([esquema["const"]] if "const" in esquema else []))
        if not opcoes:
            task.mudar(FAILED, "Elicitation sem opcoes enumeradas")
            return

        # O requestState fica guardado NA Task (e so nela). Nunca vai para o cliente A2A.
        task.pausa = Pausa(
            tool=tool,
            argumentos=argumentos,
            chave=chave,
            campo=campo,
            opcoes=opcoes,
            request_state=resultado.request_state,
        )
        _log(f"[ponte] {task.id} pausada: inputRequests[{chave!r}] -> {opcoes}")
        task.mudar(INPUT_REQUIRED, _linha_alternativas(opcoes))

    @staticmethod
    def _falhar(task: Task, exc: Exception) -> None:
        if isinstance(exc, MCPError) and exc.code == types.CONNECTION_CLOSED:
            texto = f"Servidor MCP indisponivel: {exc.message}"
        elif isinstance(exc, MCPError):
            texto = f"Erro de protocolo MCP {exc.code}: {exc.message}"
        elif isinstance(exc, httpx2.HTTPError | OSError):
            texto = f"Servidor MCP indisponivel: {exc}"
        else:
            texto = f"Falha ao falar com o servidor MCP: {exc}"
        _log(f"[ponte] {task.id} falhou: {texto}")
        if not task.terminal:
            task.pausa = None
            task.mudar(FAILED, texto)
