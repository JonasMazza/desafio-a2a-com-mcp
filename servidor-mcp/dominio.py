"""Regras de sala: dados do starter, politica de uso e calculo de alternativas.

Nada aqui sabe de MCP. O servidor chama estas funcoes e traduz o resultado para
o protocolo; o agente A2A nunca as importa (ele so fala com o servidor por HTTP).
"""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(__file__).resolve().parent.parent / "dados"

SAO_PAULO = timezone(timedelta(hours=-3))
ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

# Mensagens exatas exigidas pelo enunciado (o validador compara o texto).
ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"
ERRO_DATA = "Data invalida: use ISO 8601 com fuso, por exemplo 2026-11-03T14:00:00-03:00"


class RegraVioladaError(Exception):
    """Violacao de regra de negocio. Vira erro de execucao (isError: true) na tool."""


SALAS: list[dict] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
SALAS_POR_ID: dict[str, dict] = {s["id"]: s for s in SALAS}
POLITICA_TEXTO: str = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO: str = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()

# Persistencia em memoria: reservas criadas valem ate o processo reiniciar.
RESERVAS: list[dict] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))


def _instante(valor: str) -> datetime:
    try:
        dt = datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        raise RegraVioladaError(ERRO_DATA) from None
    if dt.tzinfo is None:
        raise RegraVioladaError(ERRO_DATA)
    return dt


def validar(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Aplica as regras comuns a consulta e a reserva. Levanta RegraVioladaError."""
    if sala not in SALAS_POR_ID:
        raise RegraVioladaError(ERRO_SALA.format(sala=sala))
    ini, fi = _instante(inicio), _instante(fim)
    if fi <= ini:
        raise RegraVioladaError(ERRO_INTERVALO)
    ini_local, fim_local = ini.astimezone(SAO_PAULO), fi.astimezone(SAO_PAULO)
    if (
        ini_local.date() != fim_local.date()
        or ini_local.time() < ABERTURA
        or fim_local.time() > FECHAMENTO
    ):
        raise RegraVioladaError(ERRO_JANELA)
    if fi - ini > DURACAO_MAXIMA:
        raise RegraVioladaError(ERRO_DURACAO)
    return ini, fi


def conflitos(sala: str, ini: datetime, fi: datetime) -> list[dict]:
    """Reservas da sala que se sobrepoem ao intervalo [ini, fi)."""
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala and _instante(r["inicio"]) < fi and ini < _instante(r["fim"])
    ]


def alternativas(sala: str, ini: datetime, fi: datetime) -> list[str]:
    """Salas livres no intervalo, com capacidade >= a pedida, no maximo 3,
    ordenadas por capacidade crescente e, em empate, por id."""
    minima = SALAS_POR_ID[sala]["capacidade"]
    candidatas = [
        s
        for s in SALAS
        if s["id"] != sala and s["capacidade"] >= minima and not conflitos(s["id"], ini, fi)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:MAX_ALTERNATIVAS]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> dict:
    reserva = {
        "id": f"res-{len(RESERVAS) + 1:04d}",
        "sala": sala,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
    }
    RESERVAS.append(reserva)
    return reserva
