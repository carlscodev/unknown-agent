"""Protocol tests for the persistent LSP client using a real stdio subprocess."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from unknown_agent.tools.builtin.lsp_tools import LspClient, LspError, detect_server


FAKE_SERVER = r'''
import json, sys

def send(message):
    body=json.dumps(message,separators=(",",":")).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode()+body)
    sys.stdout.buffer.flush()

def read():
    headers={}
    while True:
        line=sys.stdin.buffer.readline()
        if not line: return None
        if line in (b"\n",b"\r\n"): break
        key,_,value=line.decode().partition(":")
        headers[key.lower().strip()]=value.strip()
    body=sys.stdin.buffer.read(int(headers.get("content-length","0")))
    return json.loads(body)

while True:
    message=read()
    if message is None: break
    method=message.get("method","")
    rid=message.get("id")
    params=message.get("params") or {}
    if method=="initialize":
        send({"jsonrpc":"2.0","id":rid,"result":{"capabilities":{"hoverProvider":True,"definitionProvider":True}}})
    elif method=="shutdown":
        send({"jsonrpc":"2.0","id":rid,"result":None})
    elif method=="exit":
        break
    elif method=="textDocument/didOpen":
        uri=params["textDocument"]["uri"]
        send({"jsonrpc":"2.0","method":"textDocument/publishDiagnostics","params":{"uri":uri,"diagnostics":[{"range":{"start":{"line":0,"character":0},"end":{"line":0,"character":5}},"severity":1,"message":"fake error","source":"test"}]}})
    elif rid is not None:
        if method=="textDocument/hover":
            result={"contents":{"kind":"markdown","value":"`int`"},"echoPosition":params["position"]}
        elif method=="textDocument/definition":
            result={"uri":params["textDocument"]["uri"],"range":{"start":{"line":0,"character":0},"end":{"line":0,"character":5}}}
        elif method=="textDocument/documentSymbol":
            result=[{"name":"value","kind":13,"range":{"start":{"line":0,"character":0},"end":{"line":0,"character":5}},"selectionRange":{"start":{"line":0,"character":0},"end":{"line":0,"character":5}}}]
        elif method=="textDocument/references":
            result=[{"uri":params["textDocument"]["uri"],"range":{"start":{"line":0,"character":0},"end":{"line":0,"character":5}}}]
        elif method=="textDocument/rename":
            uri=params["textDocument"]["uri"]
            result={"changes":{uri:[{"range":{"start":{"line":0,"character":0},"end":{"line":0,"character":5}},"newText":params["newName"]}]}}
        elif method=="test/no-response":
            continue
        else:
            result=None
        send({"jsonrpc":"2.0","id":rid,"result":result})
'''


@pytest.fixture()
def client(tmp_path: Path) -> LspClient:
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER, encoding="utf-8")
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    instance = LspClient(
        [sys.executable, str(script)], root=tmp_path, language="python", request_timeout=2.0
    )
    instance.start()
    yield instance
    instance.stop()


def test_initializes_and_keeps_the_server_alive(client: LspClient) -> None:
    assert client.alive
    assert client.pid is not None
    assert client.capabilities["hoverProvider"] is True
    assert client.describe()["opened_documents"] == 0


def test_positions_are_converted_from_human_one_based_to_lsp_zero_based(
    client: LspClient, tmp_path: Path
) -> None:
    params = client.position_params(tmp_path / "sample.py", line=3, column=7)
    result = client.request("textDocument/hover", params)
    assert result["echoPosition"] == {"line": 2, "character": 6}


def test_diagnostics_arrive_asynchronously(client: LspClient, tmp_path: Path) -> None:
    diagnostics = client.diagnostics(tmp_path / "sample.py", settle_ms=1000)
    assert len(diagnostics) == 1
    assert diagnostics[0]["severity"] == 1
    assert diagnostics[0]["message"] == "fake error"


def test_multiple_requests_reuse_the_same_process(client: LspClient, tmp_path: Path) -> None:
    pid = client.pid
    params = client.position_params(tmp_path / "sample.py", 1, 1)
    definition = client.request("textDocument/definition", params)
    references = client.request(
        "textDocument/references", {**params, "context": {"includeDeclaration": True}}
    )
    assert definition["uri"].startswith("file:")
    assert len(references) == 1
    assert client.pid == pid


def test_timeout_does_not_leave_a_pending_request(client: LspClient) -> None:
    with pytest.raises(LspError, match="timed out"):
        client.request("test/no-response", {}, timeout=0.05)
    # A later request still works; the timeout did not desynchronise ids.
    assert client.request("textDocument/documentSymbol", {"textDocument": {"uri": "x"}})


def test_open_document_refuses_workspace_escape(client: LspClient, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.py"
    outside.write_text("x=1\n", encoding="utf-8")
    with pytest.raises(LspError, match="outside LSP workspace"):
        client.open_document(outside)


def test_unknown_suffix_has_an_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(LspError, match="supported"):
        detect_server(tmp_path / "data.xyz")
