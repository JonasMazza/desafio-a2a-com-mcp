"""Tasks A2A em memoria: identidade, estado e produto.

Cada Task tem duas partes bem separadas:

- a parte PUBLICA (id, contextId, status, history, artifacts), que e o que
  `para_wire()` serializa e o cliente A2A ve;
- a parte INTERNA (`pausa`, `trace_id`, `politica`), que nunca sai do processo. E aqui que o
  `requestState` do MCP fica guardado, ligado aquela Task e a nenhuma outra.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
REJECTED = "TASK_STATE_REJECTED"

TERMINAIS = {COMPLETED, CANCELED, FAILED, REJECTED}


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Pausa:
    """O que o agente guarda enquanto a Task espera o cliente A2A.

    `request_state` e opaco: o agente guarda e ecoa, nunca abre nem interpreta.
    """

    tool: str
    argumentos: dict[str, Any]
    chave: str  # chave do inputRequests, devolvida igual no inputResponses
    campo: str  # propriedade do requestedSchema (ex.: "sala")
    opcoes: list[str]  # enum da elicitation, na ordem em que veio
    request_state: str


@dataclass
class Task:
    id: str
    context_id: str
    trace_id: str
    estado: str = SUBMITTED
    mensagem_status: dict | None = None
    history: list[dict] = field(default_factory=list)
    artifacts: list[dict] = field(default_factory=list)
    momento: str = field(default_factory=agora)
    politica: str | None = None  # versao lida do resource politica://uso
    pausa: Pausa | None = None
    trava: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def registrar_usuario(self, mensagem: dict) -> None:
        self.history.append({**mensagem, "taskId": self.id, "contextId": self.context_id})

    def mudar(self, estado: str, texto: str | None = None) -> None:
        """Transicao de estado. Estado terminal e definitivo."""
        if self.terminal:
            raise RuntimeError(f"Task {self.id} ja terminou em {self.estado}")
        print(f"[a2a] {self.id} {self.estado} -> {estado}", file=sys.stderr, flush=True)
        self.estado = estado
        self.mensagem_status = None
        if texto is not None:
            self.mensagem_status = {
                "messageId": novo_id("msg"),
                "role": "ROLE_AGENT",
                "parts": [{"text": texto}],
                "taskId": self.id,
                "contextId": self.context_id,
            }
            self.history.append(self.mensagem_status)
        self.momento = agora()

    def para_wire(self) -> dict:
        status: dict[str, Any] = {"state": self.estado, "timestamp": self.momento}
        if self.mensagem_status is not None:
            status["message"] = self.mensagem_status
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": self.history,
            "artifacts": self.artifacts,
        }


class Tarefas:
    """Store em memoria. As Tasks nao sobrevivem a restart do agente (fora de escopo)."""

    def __init__(self) -> None:
        self._por_id: dict[str, Task] = {}

    def criar(self, context_id: str | None, trace_id: str) -> Task:
        task = Task(id=novo_id("task"), context_id=context_id or novo_id("ctx"), trace_id=trace_id)
        self._por_id[task.id] = task
        print(f"[a2a] {task.id} criada em {SUBMITTED}", file=sys.stderr, flush=True)
        return task

    def obter(self, task_id: str) -> Task | None:
        return self._por_id.get(task_id)
