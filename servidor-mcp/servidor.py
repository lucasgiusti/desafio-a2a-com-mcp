"""Servidor MCP da Central de Salas (Streamable HTTP, porta 7301, endpoint /mcp).

Tres tools (listar_salas, consultar_disponibilidade, reservar_sala), um resource
(politica://uso) e o ciclo de MRTR na reserva: quando a sala pedida esta ocupada,
a tool termina a resposta com `InputRequiredResult` (elicitation em form mode
mais o `requestState`), que o SDK sela antes de devolver ao cliente.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
import uvicorn
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp_types import (
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ClientCapabilities,
    ElicitationCapability,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    FormElicitationCapability,
    InputRequiredResult,
    MissingRequiredClientCapabilityErrorData,
)
from pydantic import BaseModel

DADOS = Path(__file__).resolve().parent.parent / "dados"
SAO_PAULO = timezone(timedelta(hours=-3))
ABERTURA, FECHAMENTO = time(8, 0), time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
TTL_REQUEST_STATE = 600.0  # 10 minutos

SALAS: list[dict] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
RESERVAS: list[dict] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
POLITICA_TEXTO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()


def log(mensagem: str) -> None:
    print(mensagem, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- modelos de saida


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


# ---------------------------------------------------------------- regras de dominio


def sala_por_id(sala: str) -> dict:
    for s in SALAS:
        if s["id"] == sala:
            return s
    raise ToolError(f"Sala inexistente: {sala}")


def validar(sala: str, inicio: str, fim: str) -> tuple[dict, datetime, datetime]:
    """Aplica as validacoes de sala e de politica; levanta ToolError (isError: true)."""
    dados_sala = sala_por_id(sala)
    try:
        ini = datetime.fromisoformat(inicio).astimezone(SAO_PAULO)
        fi = datetime.fromisoformat(fim).astimezone(SAO_PAULO)
    except ValueError:
        raise ToolError("Intervalo invalido: fim deve ser posterior a inicio") from None
    if fi <= ini:
        raise ToolError("Intervalo invalido: fim deve ser posterior a inicio")
    if not (ABERTURA <= ini.time() <= FECHAMENTO and ABERTURA <= fi.time() <= FECHAMENTO and ini.date() == fi.date()):
        raise ToolError("Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00")
    if fi - ini > DURACAO_MAXIMA:
        raise ToolError("Duracao acima do limite: a politica permite no maximo 2 horas")
    return dados_sala, ini, fi


def conflitos(sala: str, ini: datetime, fi: datetime) -> list[dict]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala
        and datetime.fromisoformat(r["inicio"]) < fi
        and ini < datetime.fromisoformat(r["fim"])
    ]


def alternativas(dados_sala: dict, ini: datetime, fi: datetime) -> list[str]:
    """Salas livres no intervalo com capacidade >= a pedida, ate 3, por capacidade e id."""
    candidatas = [
        s
        for s in SALAS
        if s["id"] != dados_sala["id"] and s["capacidade"] >= dados_sala["capacidade"] and not conflitos(s["id"], ini, fi)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:3]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    numero = max(int(r["id"].split("-")[1]) for r in RESERVAS) + 1 if RESERVAS else 1
    reserva = {"id": f"res-{numero:04d}", "sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    RESERVAS.append(reserva)
    return ReservaOut(
        reserva=reserva["id"], reservado=True, sala=sala, inicio=inicio, fim=fim,
        responsavel=responsavel, politica=POLITICA_VERSAO,
    )


# ---------------------------------------------------------------- servidor MCP

segredo = os.environ.get("REQUEST_STATE_SECRET", "")
if len(segredo.encode()) < 32:
    raise SystemExit(
        "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. "
        'Gere com: python3 -c "import secrets; print(secrets.token_hex(32))"'
    )

mcp = MCPServer(
    name="central-de-salas",
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[segredo], ttl=TTL_REQUEST_STATE),
)


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    _, ini, fi = validar(sala, inicio, fim)
    em_conflito = [ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in conflitos(sala, ini, fi)]
    return Disponibilidade(sala=sala, livre=not em_conflito, conflitos=em_conflito)


CHAVE_ESCOLHA = "escolha_de_sala"


def pedir_escolha(ctx: Context, pedido: dict, opcoes: list[str]) -> InputRequiredResult:
    """Termina a resposta pedindo informacao: input_required com a elicitation e o requestState.

    O requestState em claro carrega o pedido original e as alternativas oferecidas;
    o RequestStateBoundary do SDK o sela (AES-256-GCM, expiracao, vinculo ao
    tools/call e aos argumentos) antes de sair, e o verifica quando volta.
    """
    elicitation = (ctx.client_capabilities.elicitation if ctx.client_capabilities else None)
    if elicitation is None or elicitation.form is None:
        requeridas = ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))
        raise MCPError(
            code=MISSING_REQUIRED_CLIENT_CAPABILITY,
            message="Client did not declare the form elicitation capability required by reservar_sala",
            data=MissingRequiredClientCapabilityErrorData(required_capabilities=requeridas).model_dump(
                by_alias=True, mode="json", exclude_none=True),
        )
    sala_schema = {"type": "string", "title": "Sala", "description": "Sala alternativa escolhida", "enum": opcoes}
    elicitacao = ElicitRequest(params=ElicitRequestFormParams(
        message="A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
        requested_schema={"type": "object", "properties": {"sala": sala_schema}, "required": ["sala"]},
    ))
    estado = json.dumps({"pedido": pedido, "alternativas": opcoes}, separators=(",", ":"))
    return InputRequiredResult(input_requests={CHAVE_ESCOLHA: elicitacao}, request_state=estado)


def oferecer_alternativas(ctx: Context, pedido: dict) -> InputRequiredResult:
    dados_sala, ini, fi = validar(pedido["sala"], pedido["inicio"], pedido["fim"])
    opcoes = alternativas(dados_sala, ini, fi)
    if not opcoes:
        raise ToolError("Sem alternativas disponiveis no intervalo")
    return pedir_escolha(ctx, pedido, opcoes)


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str, ctx: Context) -> ReservaOut | InputRequiredResult:
    if ctx.request_state is None:
        # Primeira rodada.
        pedido = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
        _, ini, fi = validar(sala, inicio, fim)
        if not conflitos(sala, ini, fi):
            return criar_reserva(sala, inicio, fim, responsavel)
        return oferecer_alternativas(ctx, pedido)

    # Retry: o pedido original sai do requestState (ja verificado pelo SDK), nunca
    # dos argumentos reenviados pelo cliente. Nada foi guardado em memoria.
    estado = json.loads(ctx.request_state)
    pedido, opcoes = estado["pedido"], estado["alternativas"]
    resposta = (ctx.input_responses or {}).get(CHAVE_ESCOLHA)
    if not isinstance(resposta, ElicitResult):
        return pedir_escolha(ctx, pedido, opcoes)
    if resposta.action == "decline":
        return ReservaOut(reservado=False, motivo="recusado")
    if resposta.action == "cancel":
        return ReservaOut(reservado=False, motivo="cancelado")
    escolhida = (resposta.content or {}).get("sala")
    if escolhida not in opcoes:
        return pedir_escolha(ctx, pedido, opcoes)
    _, ini, fi = validar(escolhida, pedido["inicio"], pedido["fim"])
    if conflitos(escolhida, ini, fi):
        # A alternativa foi ocupada entre as rodadas: pergunta de novo com a lista atual.
        return oferecer_alternativas(ctx, pedido)
    return criar_reserva(escolhida, pedido["inicio"], pedido["fim"], pedido["responsavel"])


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas de reuniao.")
def politica_de_uso() -> str:
    return POLITICA_TEXTO


# ---------------------------------------------------------------- log de requests no stderr


class LogDeRequests:
    """ASGI: registra no stderr metodo, id e traceparent de cada request JSON-RPC recebido."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        corpo = b""
        mensagens = []
        while True:
            mensagem = await receive()
            mensagens.append(mensagem)
            corpo += mensagem.get("body", b"")
            if not mensagem.get("more_body"):
                break
        try:
            dados = json.loads(corpo)
            params = dados.get("params") or {}
            meta = params.get("_meta") or {}
            cliente = (meta.get("io.modelcontextprotocol/clientInfo") or {}).get("name", "-")
            log(f"[mcp] method={dados.get('method')} id={dados.get('id')} client={cliente} "
                f"name={params.get('name') or params.get('uri') or '-'} "
                f"traceparent={meta.get('traceparent', '-')} "
                f"capabilities={json.dumps(meta.get('io.modelcontextprotocol/clientCapabilities'), separators=(',', ':'))}")
        except (ValueError, AttributeError):
            log("[mcp] corpo nao-JSON recebido")

        async def replay():
            return mensagens.pop(0) if mensagens else await receive()

        await self.app(scope, replay, send)


app = LogDeRequests(mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True))

if __name__ == "__main__":
    porta = int(os.environ.get("MCP_PORT", "7301"))
    log(f"[mcp] central-de-salas em http://127.0.0.1:{porta}/mcp")
    uvicorn.run(app, host=os.environ.get("MCP_HOST", "127.0.0.1"), port=porta, log_level="warning")
