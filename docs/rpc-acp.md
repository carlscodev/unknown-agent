# Stable JSON-RPC and ACP service

Unknown Agent exposes ACP v1 over newline-delimited JSON-RPC 2.0 on standard input/output. Editors launch `ua acp` as a subprocess. Protocol output is written only to stdout; normal diagnostics remain outside the wire stream.

## Lifecycle

1. `initialize` negotiates `protocolVersion: 1` and returns agent capabilities.
2. `session/new` creates durable project-local session metadata.
3. `session/load` restores a known session after an editor or agent restart.
4. `session/prompt` invokes the existing `core.runner.run_task` composition root.
5. `session/update` notifications stream user text, thoughts, messages, and tool state.
6. `session/set_mode` selects `auto`, `plan`, or `react` and persists it.
7. `session/cancel` propagates cancellation into the running mission and its subprocess tree.

This adapter does not create a second agent runtime. Provider routing, tools, policy, sandboxing, signed extensions, hooks, mission journals, validation, and cancellation all remain owned by the canonical runner.

```bash
ua acp --timeout 300
```

## Wire example

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{},"clientInfo":{"name":"editor","version":"1.0"}}}
{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/workspace/project","mcpServers":[]}}
{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"sess_...","prompt":[{"type":"text","text":"Run the focused tests and repair failures"}]}}
```

The server may emit notifications between responses:

```json
{"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"sess_...","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"..."}}}}
```

## Reliability and security

- Pydantic v2 models reject malformed envelopes, parameters, and unknown fields where ACP does not define extension points.
- Messages are bounded to 1 MiB and concurrent requests are bounded by a semaphore.
- Every request timeout is greater than zero and no more than 300 seconds.
- Session stores are replaced atomically with mode `0600` and contain metadata, not provider credentials.
- Session working directories must exist inside the configured project.
- Cancellation is handled concurrently with a prompt because each JSON-RPC line is dispatched in its own bounded task.
- JSON-RPC parse, request, method, parameter, timeout, cancellation, and internal failures use stable numeric error codes.
- `unknown-agent/status` reports negotiated protocol state, durable session count, active sessions, and the timeout ceiling.

ACP and MCP solve different directions: ACP connects an editor/client to Unknown Agent, while MCP lets Unknown Agent connect to external tools and data. MCP configuration supplied to an ACP session remains governed by Unknown Agent's existing MCP trust model.
