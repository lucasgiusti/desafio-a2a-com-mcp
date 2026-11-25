# A Ponte: um agente A2A com MCP por dentro

Dois processos Python, sem LLM:

```
servidor-mcp/servidor.py   servidor MCP (SDK mcp 2.3.0), Streamable HTTP em http://localhost:7301/mcp
agente/agente.py           agente A2A v1.0 (a2a-sdk 1.3.1), JSON-RPC em http://localhost:7300/a2a,
                           card em /.well-known/agent-card.json; cliente MCP do servidor acima
```

O agente fala com o servidor MCP só por HTTP, como cliente MCP (`mcp.Client`). Conflito, política e alternativas são decididos no servidor; o agente traduz protocolo.

## Como rodar

Pré-requisitos: [uv](https://docs.astral.sh/uv/) e Python 3.12+ (o uv instala, se faltar). As versões estão travadas em `servidor-mcp/pyproject.toml`, `agente/pyproject.toml` e nos `uv.lock`.

```bash
git clone https://github.com/lucasgiusti/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
```

Terminal 1, servidor MCP (o stderr mostra o log de cada request). A chave de integridade do `requestState` vem de `REQUEST_STATE_SECRET`, com no mínimo 32 bytes de aleatoriedade; gere a sua e não a versione:

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
cd servidor-mcp && uv run servidor.py
```

Para testar o retry depois de reiniciar o servidor, reinicie-o no mesmo terminal (mesma `REQUEST_STATE_SECRET`).

Terminal 2, agente:

```bash
cd agente && uv run agente.py
```

Terminal 3, validador (com os dois processos recém-iniciados, porque as reservas ficam em memória):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Portas e URLs têm padrão no código e podem ser trocadas por variável de ambiente: `MCP_PORT` (7301), `AGENTE_PORT` (7300), `MCP_URL` (`http://127.0.0.1:7301/mcp`) e `AGENTE_URL` (`http://127.0.0.1:7300`, usada no card).

Cada linha do stderr do servidor MCP traz `method`, `id`, `client` (o `clientInfo` do `_meta`), `name` (tool ou URI), `traceparent` e `capabilities`. As linhas com `client=agente-central-de-salas` são as do agente: o `tools/list` vem antes do primeiro `tools/call`, o trace-id é o que o validador imprime na primeira linha, e o retry de uma pausa aparece como um segundo `tools/call` com outro `id`.

## Onde a ponte acontece

A ida está em `agente/agente.py`, `ExecutorDeReserva._tratar_resultado`: o agente chama `client.session.call_tool(..., allow_input_required=True)`, então recebe o `InputRequiredResult` cru do servidor MCP, sem callback de elicitation que responda sozinho. Ao recebê-lo, guarda em `EstadoDoAgente.pausas[task_id]` uma `Pausa` com os argumentos do pedido, a chave do `inputRequests`, o `requestState` (opaco, sem abrir), as alternativas do `enum` e a versão da política, e chama `updater.requires_input(...)` com a linha `alternativas: <ids>`, o que leva a Task a `TASK_STATE_INPUT_REQUIRED`. A volta está em `ExecutorDeReserva._continuar`: o `SendMessage` com `taskId` e `escolha=<valor>` recupera a `Pausa` da Task, valida a escolha contra o `enum` (fora dele, a Task continua pausada e a lista é repetida) e repete o mesmo `tools/call` com `inputResponses` na mesma chave (`accept` com a sala, ou `decline` para `escolha=recusar`) e o `requestState` ecoado como veio. O SDK gera um id JSON-RPC novo por request, então o retry nunca reaproveita o id do request inicial. No servidor, o outro lado está em `servidor-mcp/servidor.py`, `reservar_sala`: sem `requestState`, ou reserva, ou devolve `pedir_escolha(...)` (`InputRequiredResult`); com `requestState`, reconstrói o pedido a partir dele e conclui.

## Decisões técnicas

**Proteção do `requestState`.** Usei o utilitário do SDK: `MCPServer(..., request_state_security=RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600))`. O `RequestStateBoundary` do SDK sela todo `requestState` de saída com AES-256-GCM (chave derivada por HKDF-SHA256 do segredo) dentro de um envelope com `iat`, `exp`, método, nome da tool, digest dos argumentos e audiência (`central-de-salas`), e verifica todo `requestState` de entrada antes de a tool rodar. Um caractere trocado falha a tag GCM, e argumentos reenviados diferentes dos selados falham o vínculo; os dois casos e o estado expirado são recusados com `-32602` (`Invalid or expired requestState`), e o motivo real vai só para o stderr. O servidor não sobe sem `REQUEST_STATE_SECRET` de 32 bytes ou mais, e não há segredo no código.

**Validade.** 10 minutos (`TTL_REQUEST_STATE = 600.0`).

**O que o `requestState` carrega.** O texto em claro, antes de selado, é `{"pedido": {sala, inicio, fim, responsavel}, "alternativas": [...]}`. No retry, a tool usa o pedido e as alternativas que vêm do estado selado, não os argumentos reenviados, e o servidor não guarda nada em memória entre o `input_required` e o retry. Como a chave vem do ambiente, um retry apresentado depois de reiniciar o servidor continua válido. Se a sala escolhida tiver sido ocupada entre as rodadas, o servidor pergunta de novo com as alternativas do momento.

**Por que não o `Resolve(...)` do SDK.** O primeiro desenho usou `Resolve`/`Elicit`. Nele, o `requestState` guarda só a resposta e o digest da pergunta, e o resolver recalcula a pergunta a cada rodada. Depois de um restart, as reservas em memória voltam ao estado inicial, as alternativas recalculadas mudam e o SDK descarta a resposta e pergunta de novo (log: `Discarding the answer for resolver ...: the question changed since it was asked`). Por isso a tool devolve o `InputRequiredResult` diretamente.

**Estado das Tasks.** As Tasks ficam no `InMemoryTaskStore` do a2a-sdk. As pausas (o `requestState` e o que é preciso para o retry) ficam em `EstadoDoAgente.pausas`, um dicionário em memória do agente indexado pelo id da Task. Cada pausa é removida quando a Task é retomada, então duas Tasks pausadas ao mesmo tempo nunca trocam de `requestState`. O `requestState` nunca entra em mensagem, artifact ou card.

**Trace context.** O agente lê o header `traceparent` do request A2A e põe no `_meta` de cada request MCP daquela Task um `traceparent` com o mesmo trace-id e um span-id novo. Na continuação, usa o header dela, ou o guardado na pausa se ela não trouxer um.

**Adaptações aos SDKs**, cada uma com a evidência:

- Capability de elicitation. O `ClientSession` do `mcp` 2.3.0 só declara elicitation quando recebe `elicitation_callback`, e então declara form e url (`mcp/client/session.py`, `_build_capabilities`: `types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability()) if self._elicitation_callback is not _default_elicitation_callback else None`). O agente não usa callback, porque quem responde é o cliente A2A. Por isso `ClienteMCP` sobrescreve só a capability anunciada para `{"elicitation": {"form": {}}}`. Os headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name` e o resto do `_meta` continuam por conta do SDK.
- Versão do A2A. O a2a-sdk 1.3.1 trata request sem header `A2A-Version` como 0.3 e recusa `SendMessage`/`GetTask` (`a2a/utils/version_validator.py`: `if not actual_version: return constants.PROTOCOL_VERSION_0_3`; log `Version mismatch: actual='0.3', expected='1.0'`). O validador e os exemplos de `exemplos/wire/` não mandam esse header. `ContextoA2AVersao1` assume 1.0 quando ele falta, que é a única versão declarada no card.
- Mensagem final da Task. No a2a-sdk, a mensagem do status corrente fica em `status.message` e só vai para `history` quando o status muda de novo (`a2a/server/tasks/task_manager.py`: `task.history.append(task.status.message)`). Numa Task `FAILED`, a mensagem exata da tool (`Error executing tool reservar_sala: Sala inexistente: ...`) aparece em `status.message` no `SendMessage` e no `GetTask`, e as anteriores (o pedido e as alternativas) ficam em `history`.
- `GetTask`. O a2a-sdk devolve a Task direto em `result`, e não em `result.task` como em `exemplos/wire/09`. O validador aceita as duas formas.

**Determinismo.** O agente interpreta o pedido com duas expressões regulares fixas (`reservar sala=... inicio=... fim=... responsavel=...` e `escolha=...`) e decide por regra. Não há SDK de LLM nas dependências.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: 953b41997d2e4809800dbb0cb1521f10
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
