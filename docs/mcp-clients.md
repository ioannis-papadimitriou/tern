# Using tern from an MCP client

`tern mcp` serves tern's tools to a local MCP client (LM Studio, your own agents) over **stdio only**: the client starts `tern mcp` as a child process and talks to it on stdin/stdout. There is no network transport and no option to add one (decisions #9).

## What it is

- `tern mcp` is a thin proxy. It connects to the running `ternd` over its Unix socket with bridge role `mcp-proxy` (`tern-bridge/1.1`, decisions #37) and forwards every tool call to ternd's `tools.call`.
- ternd runs every call through its one tool registry: the permission engine and the audit log (`Registry.call`). The audit caller is `mcp:<connection id>`. ternd assigns a new id to each `tern mcp` connection, so restarting the client gives it a new id. The client's own name (`clientInfo.name`) is kept as a display label only.
- `tern mcp` never starts a ternd. Without one it exits 2 with `mcp: unavailable: no ternd listening at …` on stderr. ternd normally runs because Thunderbird started it, or because of the systemd user unit.
- MCP clients cannot approve anything, undo anything, or read the audit list. A write that needs your approval (W2/W3, from plan 12) shows its card in Thunderbird, and only your decision there lets it run.

## Setup (LM Studio)

1. Find the absolute path of the `tern` entry point in tern's virtual environment: from the repository, `uv run which tern` (for example `/home/you/code/tern/.venv/bin/tern`).
2. In LM Studio, open the MCP settings (Program → Install → Edit `mcp.json`) and add a `tern` server:

   ```json
   {
     "mcpServers": {
       "tern": {
         "command": "/home/you/code/tern/.venv/bin/tern",
         "args": ["mcp"]
       }
     }
   }
   ```

   This uses tern's normal data locations, the same ternd that Thunderbird talks to.

   To use a separate tern home instead (the test profile, decisions #22/#24), add its path:

   ```json
   {
     "mcpServers": {
       "tern": {
         "command": "/home/you/code/tern/.venv/bin/tern",
         "args": ["mcp"],
         "env": {"TERN_HOME": "/home/you/tern-private/tern-home-test"}
       }
     }
   }
   ```

3. Enable the server in LM Studio. Its tool list shows tern's tools (read-only tools until plans 10 and 12 add writes). Tools with a forbidden effect are never listed.

## What the client sees

- **Read-only hints.** R0/R1 tools are marked `readOnlyHint`, and W2/W3 tools `destructiveHint`. No tool is open-world.
- **Third-party mail text.** Results that hold third-party mail text (`search_messages`, `get_message`, ...) are text only. The JSON comes inside one `<untrusted_email>` envelope, and the result's `_meta` carries `"tern/untrusted_output": true`. The server's instructions tell the client's model to treat that text as data, never as instructions.
- **Refusals.** A refused or failed call is an MCP error result whose text is `{"error": {"code", "reason", "message"}}`. Examples: invalid arguments (`invalid_params`), an unknown tool (`unknown_tool`), a rate limit (`rate_limited`).
- **ternd going away.** If ternd stops or restarts while the client is connected, `tern mcp` exits 1 at once (`mcp: ternd closed the connection` on stderr). The client shows the server as stopped. Turn it off and on again once ternd runs.
- **Long waits.** A W2/W3 call keeps its MCP request open until you decide the card or it expires (10 minutes; from plan 12). `tern mcp` gives up waiting after the expiry plus a minute and answers `unavailable`. Some clients time out earlier; your decision still governs either way.

## Cancelling a request

MCP lets a client cancel one request. `tern mcp` does **not** forward that to ternd: `tern-bridge/1.1` has no cancel method (#37).

- The call keeps running in ternd and is audited as usual.
- A W2/W3 call stays pending and runs if you later approve its card. Deny the card if you no longer want it.
- Closing the client, or turning the tern server off in it, ends `tern mcp`. ternd then cancels every call still running on that connection, and each cancel is recorded on its audit row.

## Limits

- **A-9: tern cannot see where the client sends tool results.** "Local MCP clients only" is a promise about tern's transport, not about the client. If LM Studio (or any client) is set to use a remote model, the mail text tern returns goes to that remote model, and tern has no way to notice. Use only local models in a client you connect to tern. The audit log records each call with the client's name (`tern audit`), so you can see afterwards what was asked.
- **Same-user processes.** Any process running as your user can start `tern mcp` (A-21). Its calls get their own `mcp:<id>` caller in the audit log and the same permission rules as any other client.
- **Tool list at start.** `tern mcp` reads the tool list when it starts. Restart the server in the client after ternd loads new plugins.
