# terminal-mcp

An MCP server that lets AI agents (Claude Code, or any MCP client) run **interactive terminal programs** in the background and drive them like a human would: type, press keys, click, read the screen.

Normal agent shells only handle run-and-exit commands. With terminal-mcp, an agent can work with REPLs, password/confirmation prompts, `ssh`, debuggers, and full-screen TUIs (htop, vim, lazygit, Textual/datatui apps, even another `claude`) while it keeps using its other tools.

- **Real PTY.** Every command gets its own pseudo-terminal, so programs behave exactly as they would in your terminal (colors, line editing, job control, `ctrl-c`).
- **Screen rendering.** A VT100/xterm emulator ([pyte](https://github.com/selectel/pyte)) turns full-screen TUIs into plain text the agent can read.
- **Async.** Sessions keep running between tool calls. Start a dev server, a REPL, and a TUI at the same time, then come back to any of them.
- **Keyboard and mouse.** Named keys (`enter`, `ctrl-c`, `up`, `f5`, `alt-x`, …) and SGR mouse events (click, drag, scroll).
- **No nesting.** Sessions started by terminal-mcp can't start terminal-mcp again, so an agent driving another agent can't spawn more of them.
- **One file.** One Python file, two dependencies, no daemon.

## Install

Requires Python ≥ 3.10 and [uv](https://docs.astral.sh/uv/). Linux / macOS (uses POSIX PTYs).

```bash
git clone https://github.com/yusufekorman/terminal-mcp.git
```

**Claude Code**

```bash
claude mcp add -s user terminal -- uv run --script /path/to/terminal-mcp/server.py
```

**Any other MCP client** (stdio):

```json
{
  "mcpServers": {
    "terminal": {
      "command": "uv",
      "args": ["run", "--script", "/path/to/terminal-mcp/server.py"]
    }
  }
}
```

Dependencies (`mcp`, `pyte`) are declared inline in `server.py`, and `uv` installs them on first run.

## Tools

| Tool | Description |
|---|---|
| `start(command, cwd?, cols=120, rows=40, wait=1)` | Start a command in a new background PTY. Returns the session id and its first output. |
| `send(id, text?, keys?, wait=1)` | Type text, then press keys in order. Returns new output. |
| `read(id, timeout=5, until?)` | New output since the last read (ANSI stripped). Waits until the `until` regex matches, output goes idle, the process exits, or the timeout passes. |
| `screen(id, wait=0.3)` | The current screen as text, plus cursor position and whether the app enabled mouse tracking. |
| `mouse(id, row, col, action="click", button="left")` | Mouse event at a screen cell: `click`, `press`, `release`, `move`, `drag`, `scroll_up`, `scroll_down`. |
| `resize(id, cols, rows)` | Resize the terminal (the app gets `SIGWINCH` and redraws). |
| `list_sessions()` | All sessions with status, pid, idle time, cwd, and command. |
| `kill(id, signal_name="TERM")` | Send a signal to the session's process group. On an already-exited session, removes it. |

**Key names:** `enter tab esc backspace space up down left right home end pgup pgdn insert delete f1`–`f12`, `ctrl-<x>`, `alt-<x>`.

**`read` or `screen`?** Use `read` for line-based programs (shells, REPLs, build logs). Use `screen` for anything full-screen, where the raw stream is just cursor movements.

## Example

An agent asking a Python REPL something:

```
start("python3 -i -q")                       → session 1 [running] >>>
send(1, "print(6*7)", ["enter"])             → [running] 42 >>>
kill(1)                                      → session 1 [exited 143]
```

An agent driving a TUI:

```
start("htop")                                → session 2 [running]
screen(2)                                    → [running] cursor=row 0 col 0 mouse=on
                                               0[|||       12.3%]   Tasks: 231 ...
send(2, keys=["f6"])                         → (sort menu opens)
mouse(2, row=5, col=3)                       → click a row
send(2, "q")                                 → [exited 0]
```

## How it works

Each session is a `subprocess.Popen` attached to the slave side of a `pty.openpty()` pair. The process runs in its own session and gets the PTY as its controlling terminal, so `ctrl-c` sends `SIGINT` the way it does in a normal terminal. A background thread reads the master fd. It appends to a raw buffer (the last 1 MiB, used by `read`) and feeds a `pyte` screen (used by `screen`). The emulator answers terminal queries such as cursor-position requests, so TUIs that ask about the terminal don't hang.

## Notes

- **Not a sandbox.** Commands run as your user with your permissions. Treat this server like a shell.
- **The nesting guard is not a security boundary.** Child processes get `TERMINAL_MCP_NESTED=1` and a nested server refuses to start. A program that unsets the variable on purpose can get around it.
- **Mouse uses SGR (1006) encoding only.** This covers modern TUIs (Textual, crossterm/ratatui, bubbletea, htop, vim). Legacy X10 encoding isn't sent.
- **Sessions end with the server.** When the MCP client restarts the server, its sessions get `SIGHUP`.

## Test

```bash
uv run --with mcp --with pyte python test_server.py
```

The test covers a REPL round-trip, `ctrl-c` interrupt, curses TUI rendering, mouse click position, the nesting guard, and `read(until=…)` with `kill`.
