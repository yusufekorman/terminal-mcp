"""Self-check: uv run --with mcp --with pyte python test_server.py"""
import asyncio, sys

from server import kill, list_sessions, mouse, read, screen, send, start

MOUSE_APP = """python3 -c "
import curses
def main(w):
    curses.mousemask(curses.ALL_MOUSE_EVENTS); w.addstr(0, 0, 'ready'); w.refresh()
    while w.getch() != curses.KEY_MOUSE: pass
    _, x, y, _, b = curses.getmouse(); w.addstr(1, 0, f'CLICK {y} {x} {bool(b & curses.BUTTON1_PRESSED)}'); w.refresh(); w.getch()
curses.wrapper(main)"
"""
CURSES = "python3 -c \"import curses; curses.wrapper(lambda w: (w.addstr(5, 10, 'HELLO TUI'), w.refresh(), w.getch()))\""


async def main():
    # stdio REPL: prompt -> input -> output
    out = await start("python3 -i -q")
    assert out.startswith("session 1"), out
    out = await send(1, "print(6*7)", ["enter"])
    assert "42" in out, out

    # long-running command interrupted with ctrl-c, while another session is alive
    await start("sleep 100")
    assert "running" in list_sessions()
    await send(2, keys=["ctrl-c"], wait=0.5)
    assert "exited" in (await read(2, timeout=2)), list_sessions()

    # full-screen TUI rendered via screen
    await start(CURSES)
    scr = await screen(3, wait=1)
    assert "HELLO TUI" in scr.splitlines()[6], scr
    await send(3, "q")
    assert "exited 0" in (await read(3, timeout=2))

    # mouse click lands on the right cell
    await start(MOUSE_APP)
    assert "mouse=on" in (await screen(4, wait=1)).splitlines()[0]
    await mouse(4, row=7, col=21, action="press")
    assert "CLICK 7 21 True" in await screen(4), await screen(4)
    await kill(4)

    # a terminal-mcp server started inside a session refuses to run
    await start(f"{sys.executable} server.py", wait=0)
    out = await read(5, timeout=10, until="disabled")
    assert "disabled inside sessions" in out, out

    # read with until + kill
    await send(1, "import time; time.sleep(0.5); print('DONE')", ["enter"], wait=0)
    assert "DONE" in await read(1, timeout=3, until="DONE")
    assert "exited" in await kill(1)
    assert "removed" in await kill(1)
    print("ok")


asyncio.run(main())
