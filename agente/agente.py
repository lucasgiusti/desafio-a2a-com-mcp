"""Agente Central de Salas: host MCP por dentro, servidor A2A por fora (porta 7300).

Sem LLM: o pedido chega em formato fixo e o agente decide por regra. O agente nao
implementa regra de sala; ele traduz protocolo. A ponte esta em `_tratar_resultado`
(input_required do MCP vira TASK_STATE_INPUT_REQUIRED) e em `_continuar`
(o requestState guardado volta ao servidor MCP num tools/call com id novo).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import DefaultServerCallContextBuilder, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.helpers.proto_helpers import new_task
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill, Part, TaskState
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CallToolResult,
    ClientCapabilities,
    ElicitationCapability,
    ElicitResult,
    FormElicitationCapability,
    Implementation,
    InputRequiredResult,
)
from starlette.applications import Starlette

PORTA = int(os.environ.get("AGENTE_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://127.0.0.1:{PORTA}")
URL_MCP = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
VERSAO_MCP = "2026-07-28"
TOOL_RESERVA = "reservar_sala"
RESOURCE_POLITICA = "politica://uso"

PEDIDO = re.compile(r"^reservar sala=(\S+) inicio=(\S+) fim=(\S+) responsavel=(.+)$")
ESCOLHA = re.compile(r"^escolha=(\S+)$")
TRACEPARENT = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def log(mensagem: str) -> None:
    print(mensagem, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- cliente MCP

# A capability declarada em todo request: elicitation em form mode, e so isso.
CAPABILITIES = ClientCapabilities(elicitation=ElicitationCapability(form=FormElicitationCapability()))


class ClienteMCP(Client):
    """`Client` do SDK MCP declarando so `{"elicitation": {"form": {}}}`.

    O SDK so declara elicitation quando recebe um `elicitation_callback`, e nesse
    caso declara form e url. Aqui nao ha callback: quem responde a elicitation e o
    cliente A2A, entao o agente sobrescreve so a capability anunciada.
    """

    async def _build_session(self, exit_stack: AsyncExitStack):
        sessao = await super()._build_session(exit_stack)
        sessao._build_capabilities = lambda versao: CAPABILITIES
        return sessao


def meta_da_task(trace: TraceContext | None) -> dict | None:
    """`_meta` extra do request MCP: traceparent com o trace-id da Task e span-id novo."""
    if trace is None:
        return None
    return {"traceparent": f"00-{trace.trace_id}-{secrets.token_hex(8)}-{trace.flags}"}


@dataclass
class TraceContext:
    trace_id: str
    flags: str


def trace_do_header(headers: dict) -> TraceContext | None:
    m = TRACEPARENT.match((headers.get("traceparent") or "").strip().lower())
    return TraceContext(trace_id=m.group(2), flags=m.group(4)) if m else None


# ---------------------------------------------------------------- estado das Tasks pausadas


@dataclass
class Pausa:
    """O que o agente guarda de uma Task em TASK_STATE_INPUT_REQUIRED.

    `request_state` e opaco: guardado e ecoado sem modificacao, nunca aberto.
    """

    argumentos: dict
    chave: str
    request_state: str
    alternativas: list[str]
    politica: str
    trace: TraceContext | None


@dataclass
class EstadoDoAgente:
    pausas: dict[str, Pausa] = field(default_factory=dict)  # task_id -> Pausa


def linha_alternativas(alternativas: list[str]) -> str:
    return "alternativas: " + ", ".join(alternativas)


def texto(parts) -> str:
    return " ".join(p.text for p in parts if p.text)


# ---------------------------------------------------------------- executor A2A


class ExecutorDeReserva(AgentExecutor):
    def __init__(self, mcp: Client, estado: EstadoDoAgente) -> None:
        self.mcp = mcp
        self.estado = estado

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        pedido = context.get_user_input().strip()
        trace = trace_do_header(context.call_context.state.get("headers", {}))
        if context.current_task is None:
            # Task nova, com id e contextId proprios, aberta em TASK_STATE_SUBMITTED.
            await event_queue.enqueue_event(new_task(
                context.task_id, context.context_id, TaskState.TASK_STATE_SUBMITTED, history=[context.message]))
            await self._iniciar(updater, pedido, trace)
        else:
            await self._continuar(updater, context.task_id, pedido, trace)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        self.estado.pausas.pop(context.task_id, None)
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()

    def _mensagem(self, updater: TaskUpdater, conteudo: str):
        return updater.new_agent_message(parts=[Part(text=conteudo)])

    async def _iniciar(self, updater: TaskUpdater, pedido: str, trace: TraceContext | None) -> None:
        await updater.start_work()
        m = PEDIDO.match(pedido)
        if not m:
            await updater.failed(self._mensagem(
                updater, "Pedido invalido: use reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"))
            return
        argumentos = dict(zip(("sala", "inicio", "fim", "responsavel"), m.groups()))
        try:
            # Descoberta em runtime: tools/list antes da primeira chamada.
            tools = await self.mcp.list_tools(meta=meta_da_task(trace))
            if TOOL_RESERVA not in {t.name for t in tools.tools}:
                await updater.failed(self._mensagem(updater, f"Tool {TOOL_RESERVA} nao encontrada no servidor MCP"))
                return
            # O resource e escolha da aplicacao: o agente le a politica e extrai a versao.
            politica = await self.mcp.session.read_resource(RESOURCE_POLITICA, meta=meta_da_task(trace))
            primeira_linha = politica.contents[0].text.splitlines()[0]
            versao = primeira_linha.split(":", 1)[1].strip()
            resultado = await self.mcp.session.call_tool(
                TOOL_RESERVA, argumentos, meta=meta_da_task(trace), allow_input_required=True)
        except MCPError as e:
            await updater.failed(self._mensagem(updater, e.error.message))
            return
        await self._tratar_resultado(updater, resultado, argumentos, versao, trace)

    async def _continuar(self, updater: TaskUpdater, task_id: str, pedido: str, trace: TraceContext | None) -> None:
        pausa = self.estado.pausas.get(task_id)
        if pausa is None:
            await updater.failed(self._mensagem(updater, "Task sem pausa pendente"))
            return
        m = ESCOLHA.match(pedido)
        escolha = m.group(1) if m else None
        if escolha != "recusar" and escolha not in pausa.alternativas:
            # Fora do enum: a Task continua pausada e a lista e repetida.
            await updater.requires_input(self._mensagem(updater, linha_alternativas(pausa.alternativas)))
            return
        trace = trace or pausa.trace
        await updater.start_work()
        if escolha == "recusar":
            resposta = ElicitResult(action="decline")
        else:
            resposta = ElicitResult(action="accept", content={"sala": escolha})
        del self.estado.pausas[task_id]
        try:
            # A ponte, volta: o mesmo tools/call, com id JSON-RPC novo (o SDK gera um por
            # request), levando inputResponses com a mesma chave e o requestState ecoado.
            resultado = await self.mcp.session.call_tool(
                TOOL_RESERVA,
                pausa.argumentos,
                input_responses={pausa.chave: resposta},
                request_state=pausa.request_state,
                meta=meta_da_task(trace),
                allow_input_required=True,
            )
        except MCPError as e:
            await updater.failed(self._mensagem(updater, e.error.message))
            return
        await self._tratar_resultado(updater, resultado, pausa.argumentos, pausa.politica, trace)

    async def _tratar_resultado(self, updater: TaskUpdater, resultado, argumentos: dict, politica: str,
                                trace: TraceContext | None) -> None:
        if isinstance(resultado, InputRequiredResult):
            # A ponte, ida: input_required do MCP vira TASK_STATE_INPUT_REQUIRED, e o
            # requestState fica guardado ligado a esta Task, fora de qualquer resposta A2A.
            chave, pedido = next(iter(resultado.input_requests.items()))
            campo = pedido.params.requested_schema["properties"]["sala"]
            alternativas = list(campo.get("enum") or [campo["const"]])
            self.estado.pausas[updater.task_id] = Pausa(
                argumentos=argumentos, chave=chave, request_state=resultado.request_state,
                alternativas=alternativas, politica=politica, trace=trace,
            )
            await updater.requires_input(self._mensagem(updater, linha_alternativas(alternativas)))
            return
        assert isinstance(resultado, CallToolResult)
        if resultado.is_error:
            await updater.failed(self._mensagem(updater, texto(resultado.content)))
            return
        dados = resultado.structured_content or {}
        if dados.get("reservado") is False:
            await updater.cancel(self._mensagem(updater, f"Reserva nao realizada: {dados.get('motivo')}"))
            return
        reserva = {
            "reserva": dados["reserva"],
            "sala": dados["sala"],
            "inicio": dados["inicio"],
            "fim": dados["fim"],
            "responsavel": dados["responsavel"],
            "politica": politica,
        }
        await updater.add_artifact([Part(text=json.dumps(reserva))], name="reserva")
        await updater.complete(self._mensagem(updater, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}."))


# ---------------------------------------------------------------- Agent Card e app

CARD = AgentCard(
    name="Central de Salas",
    description="Reserva salas de reuniao da Hill Valley Tech.",
    provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
    version="1.0.0",
    supported_interfaces=[AgentInterface(url=f"{URL_PUBLICA}/a2a", protocol_binding="JSONRPC", protocol_version="1.0")],
    capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    skills=[
        AgentSkill(
            id="reservar-sala",
            name="Reservar sala",
            description="Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            tags=["salas", "agenda"],
            input_modes=["text/plain"],
            output_modes=["text/plain"],
            examples=[
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        )
    ],
)


class ContextoA2AVersao1(DefaultServerCallContextBuilder):
    """Request sem header A2A-Version e tratado como 1.0, a unica versao que o card declara.

    O a2a-sdk interpreta header ausente como 0.3 e recusa SendMessage/GetTask com
    VersionNotSupportedError; os clientes do desafio (validador e exemplos/wire) nao
    enviam esse header.
    """

    def build(self, request):
        contexto = super().build(request)
        contexto.state["headers"].setdefault("a2a-version", "1.0")
        return contexto


def criar_app() -> Starlette:
    mcp = ClienteMCP(
        URL_MCP,
        mode=VERSAO_MCP,
        client_info=Implementation(name="agente-central-de-salas", version="1.0.0"),
        cache=None,
    )
    executor = ExecutorDeReserva(mcp, EstadoDoAgente())
    handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=CARD)

    @asynccontextmanager
    async def ciclo_de_vida(_app):
        async with mcp:
            log(f"[agente] A2A em {URL_PUBLICA}/a2a, cliente MCP de {URL_MCP}")
            yield

    return Starlette(
        routes=[*create_agent_card_routes(CARD), *create_jsonrpc_routes(handler, "/a2a", context_builder=ContextoA2AVersao1())],
        lifespan=ciclo_de_vida,
    )


if __name__ == "__main__":
    uvicorn.run(criar_app(), host=os.environ.get("AGENTE_HOST", "127.0.0.1"), port=PORTA, log_level="warning")
