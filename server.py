# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=2.3", "pyte>=0.8"]
# ///
"""MCP server: run interactive / TUI commands in background PTYs and drive them."""
import asyncio, atexit, fcntl, itertools, os, pty, re, signal, struct, subprocess, sys, termios, threading, time

import pyte
from mcp.server.mcpserver import MCPServer

mcp = MCPServer("terminal")

ANSI = re.compile(r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)|[PX^_][^\x1b]*\x1b\\|[()][0-9A-B]|[0-~])|[\x00-\x08\x0b-\x0c\x0e-\x1f]")
KEYS = {
    "enter": "\r", "tab": "\t", "esc": "\x1b", "backspace": "\x7f", "space": " ",
    "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
    "home": "\x1b[H", "end": "\x1b[F", "pgup": "\x1b[5~", "pgdn": "\x1b[6~",
    "insert": "\x1b[2~", "delete": "\x1b[3~",
    "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS",
    "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~", "f8": "\x1b[19~",
    "f9": "\x1b[20~", "f10": "\x1b[21~", "f11": "\x1b[23~", "f12": "\x1b[24~",
}
# set in every child; a terminal-mcp server started inside one of our sessions refuses to run
NESTED = "TERMINAL_MCP_NESTED"
MAX_BUF = 1 << 20  # ponytail: keep last 1 MiB of raw output per session; spill to file if longer history matters


def key(name: str) -> str:
    n = name.lower()
    if n in KEYS:
        return KEYS[n]
    if n.startswith(("ctrl-", "c-")) and len(n.split("-", 1)[1]) == 1:
        return chr(ord(n[-1]) & 0x1F)
    if n.startswith(("alt-", "m-")) and len(n.split("-", 1)[1]) == 1:
        return "\x1b" + name[-1]
    raise ValueError(f"unknown key {name!r}; use {sorted(KEYS)} or ctrl-x / alt-x")


class Session:
    def __init__(self, command: str, cwd: str | None, cols: int, rows: int):
        self.command, self.cwd = command, cwd or os.getcwd()
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.buf, self.pos = bytearray(), 0
        self.lock = threading.Lock()
        self.last = self.started = time.time()
        self.fd, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.proc = subprocess.Popen(
            ["/bin/sh", "-c", command], cwd=self.cwd, stdin=slave, stdout=slave, stderr=slave,
            env={**os.environ, NESTED: "1", "TERM": "xterm-256color", "COLUMNS": str(cols), "LINES": str(rows)},
            start_new_session=True, preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
        )
        os.close(slave)
        # answer terminal queries (cursor position, device attrs) so TUIs don't hang waiting
        self.screen.write_process_input = lambda s: os.write(self.fd, s.encode())
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def _read_loop(self):
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                data = b""
            if not data:
                break
            with self.lock:
                self.buf += data
                self.stream.feed(data)
                self.last = time.time()
                if len(self.buf) > MAX_BUF:
                    cut = len(self.buf) - MAX_BUF
                    del self.buf[:cut]
                    self.pos = max(0, self.pos - cut)
        self.proc.wait()
        os.close(self.fd)

    @property
    def done(self) -> bool:
        return not self.reader.is_alive()

    def status(self) -> str:
        return f"exited {self.proc.returncode}" if self.done else "running"

    def new_text(self, consume: bool) -> str:
        with self.lock:
            data = bytes(self.buf[self.pos:])
            if consume:
                self.pos = len(self.buf)
        return ANSI.sub("", data.decode(errors="replace")).replace("\r\n", "\n")

    async def settle(self, timeout: float, until: str | None = None, idle: float = 0.3):
        """Wait until `until` regex appears in unread output, output goes idle, process exits, or timeout."""
        end = time.time() + timeout
        while time.time() < end and not self.done:
            if until:
                if re.search(until, self.new_text(False)):
                    return
            elif time.time() - self.last > idle and self.pos < len(self.buf):
                return
            await asyncio.sleep(0.05)


sessions: dict[int, Session] = {}
ids = itertools.count(1)


def get(id: int) -> Session:
    if id not in sessions:
        raise ValueError(f"no session {id}; running: {list(sessions)}")
    return sessions[id]


@mcp.tool()
async def start(command: str, cwd: str | None = None, cols: int = 120, rows: int = 40, wait: float = 1.0) -> str:
    """Start a command in a background pseudo-terminal (works for REPLs, prompts, and full-screen TUIs).
    Returns the session id and the first output (waits up to `wait` seconds)."""
    sid = next(ids)
    sessions[sid] = s = Session(command, cwd, cols, rows)
    await s.settle(wait)
    return f"session {sid} [{s.status()}]\n{s.new_text(True)}"


@mcp.tool()
async def send(id: int, text: str = "", keys: list[str] | None = None, wait: float = 1.0) -> str:
    """Type `text` into the session, then press each of `keys` in order
    (e.g. ["enter"], ["ctrl-c"], ["down","down","enter"], ["esc", ":", ...]; names: enter tab esc backspace
    space up down left right home end pgup pgdn insert delete f1-f12 ctrl-<x> alt-<x>).
    Returns new output produced within `wait` seconds. For TUIs call `screen` afterwards."""
    s = get(id)
    if s.done:
        return f"[{s.status()}] cannot send\n{s.new_text(True)}"
    os.write(s.fd, (text + "".join(key(k) for k in keys or [])).encode())
    await s.settle(wait)
    return f"[{s.status()}]\n{s.new_text(True)}"


@mcp.tool()
async def read(id: int, timeout: float = 5.0, until: str | None = None) -> str:
    """Return output (ANSI-stripped) produced since the last read/send/start.
    Waits until `until` regex matches, output goes idle, the process exits, or `timeout` seconds pass."""
    s = get(id)
    await s.settle(timeout, until)
    return f"[{s.status()}]\n{s.new_text(True)}"


@mcp.tool()
async def screen(id: int, wait: float = 0.3) -> str:
    """Render the current terminal screen as text (what a human would see). Use this for TUIs
    (htop, vim, lazygit, textual/datatui apps...) where the raw output stream is meaningless."""
    s = get(id)
    await s.settle(wait)
    with s.lock:
        lines = [l.rstrip() for l in s.screen.display]
        c = s.screen.cursor
        mouse_on = any(m << 5 in s.screen.mode for m in (1000, 1002, 1003))
    while lines and not lines[-1]:
        lines.pop()
    return f"[{s.status()}] cursor=row {c.y} col {c.x} mouse={'on' if mouse_on else 'off'}\n" + "\n".join(lines)


@mcp.tool()
async def mouse(id: int, row: int, col: int, action: str = "click", button: str = "left", wait: float = 0.5) -> str:
    """Send a mouse event at a screen cell (0-based row/col, same coordinates as `screen` output).
    action: click | press | release | move | drag | scroll_up | scroll_down. button: left | middle | right.
    Only works if the app enabled mouse tracking (`screen` shows mouse=on)."""
    s = get(id)
    if s.done:
        return f"[{s.status()}] cannot send"
    b = {"left": 0, "middle": 1, "right": 2}[button]
    # ponytail: SGR (1006) encoding only; add legacy X10 fallback if an app without ?1006 needs it
    seq = lambda code, end="M": f"\x1b[<{code};{col + 1};{row + 1}{end}"
    events = {
        "click": seq(b) + seq(b, "m"), "press": seq(b), "release": seq(b, "m"),
        "drag": seq(b + 32), "move": seq(35), "scroll_up": seq(64), "scroll_down": seq(65),
    }[action]
    os.write(s.fd, events.encode())
    await s.settle(wait)
    return f"[{s.status()}]\n{s.new_text(True)}"


@mcp.tool()
def list_sessions() -> str:
    """List all sessions with status, pid, idle time, cwd and command."""
    if not sessions:
        return "no sessions"
    now = time.time()
    return "\n".join(
        f"{i}\t{s.status()}\tpid={s.proc.pid}\tidle={now - s.last:.0f}s\tcwd={s.cwd}\t{s.command}"
        for i, s in sessions.items()
    )


@mcp.tool()
async def kill(id: int, signal_name: str = "TERM") -> str:
    """Send a signal (TERM, INT, KILL, HUP, ...) to the session's whole process group.
    Calling it on an already-exited session removes it from the list."""
    s = get(id)
    if s.done:
        del sessions[id]
        return f"session {id} removed ({s.status()})"
    os.killpg(s.proc.pid, getattr(signal, "SIG" + signal_name.upper().removeprefix("SIG")))
    await s.settle(2.0, until=r"(?!)")  # just wait for exit
    return f"session {id} [{s.status()}]\n{s.new_text(True)}"


@mcp.tool()
def resize(id: int, cols: int, rows: int) -> str:
    """Resize the session's terminal (sends SIGWINCH so TUIs redraw)."""
    s = get(id)
    with s.lock:
        s.screen.resize(rows, cols)
    fcntl.ioctl(s.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    return f"session {id} resized to {cols}x{rows}"


@atexit.register
def _cleanup():
    for s in sessions.values():
        if not s.done:
            try:
                os.killpg(s.proc.pid, signal.SIGHUP)
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    # ponytail: env-var guard stops accidental nesting, not a determined child that unsets it
    if os.environ.get(NESTED):
        sys.exit("terminal-mcp: disabled inside sessions started by terminal-mcp")
    mcp.run()
