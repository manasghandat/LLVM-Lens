"""Drive LLVM-Lens report pages over CDP and write the submission's figures.

Stdlib only: a minimal WebSocket client speaks Chrome DevTools Protocol to a
headless Chromium. The report pages populate asynchronously, so every step here
waits on a real readiness condition and asserts the state it reached before a
pixel is captured.
"""

import base64
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

CHROME = os.path.expanduser(
    "~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome")
BASE = "http://127.0.0.1:8765"
OUT = Path(__file__).resolve().parent / "shots"


# --- a WebSocket, small enough to read --------------------------------------


class WS:
    def __init__(self, url):
        rest = url[len("ws://"):]
        hostport, _, path = rest.partition("/")
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port)), timeout=120)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        self.buf = b""
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.sock.recv(65536)
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        if b"101" not in head.split(b"\r\n")[0]:
            raise RuntimeError(f"handshake failed: {head[:120]!r}")

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise EOFError("socket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, text):
        payload = text.encode()
        n = len(payload)
        header = bytearray([0x81])
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) +
                          bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self):
        data = b""
        while True:
            b0, b1 = self._read(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            ln = b1 & 0x7F
            if ln == 126:
                ln = struct.unpack(">H", self._read(2))[0]
            elif ln == 127:
                ln = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(ln)
            if opcode == 0x8:
                raise EOFError("closed by peer")
            if opcode == 0x9:
                continue
            data += payload
            if fin:
                return data.decode()


class CDP:
    def __init__(self, url):
        self.ws = WS(url)
        self.id = 0
        self.events = []

    def call(self, method, **params):
        self.id += 1
        want = self.id
        self.ws.send(json.dumps({"id": want, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == want:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append(msg)

    def eval(self, expr, timeout=60):
        r = self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=True, timeout=timeout * 1000)
        res = r.get("result", {})
        if r.get("exceptionDetails"):
            raise RuntimeError(f"js: {r['exceptionDetails'].get('text')} "
                               f"{res.get('description', '')[:200]}")
        return res.get("value")

    def wait(self, expr, what, timeout=45, poll=0.15):
        end = time.time() + timeout
        last = None
        while time.time() < end:
            last = self.eval(expr)
            if last:
                return last
            time.sleep(poll)
        raise TimeoutError(f"waited {timeout}s for {what}; last={last!r}")

    def rect(self, selector, scroll=True):
        """A viewport rect for an element. The rail scrolls internally and
        getBoundingClientRect ignores clipping, so the element is scrolled into
        view first -- otherwise the click lands on empty space."""
        return self.eval(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return null;
            if ({json.dumps(scroll)}) el.scrollIntoView({{block: "center"}});
            const r = el.getBoundingClientRect();
            return {{x: r.x + r.width / 2, y: r.y + r.height / 2,
                     w: r.width, h: r.height, top: r.top,
                     inView: r.top >= 0 && r.bottom <= innerHeight}};
        }})()""")

    def click(self, selector, what=None, settle=0.35):
        r = self.rect(selector)
        if not r or r["w"] == 0:
            raise RuntimeError(f"cannot click {selector!r} ({what}): not visible")
        if not r["inView"]:
            time.sleep(0.25)
            r = self.rect(selector, scroll=False)
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=kind, x=r["x"], y=r["y"],
                      button="left", clickCount=1)
        time.sleep(settle)

    def click_at(self, x, y):
        for kind in ("mousePressed", "mouseReleased"):
            self.call("Input.dispatchMouseEvent", type=kind, x=x, y=y,
                      button="left", clickCount=1)
        time.sleep(0.35)

    def clip_of(self, selector):
        """A CSS-pixel clip rect for one element, so a figure can cover just it."""
        r = self.eval(f"""(() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return null;
            const b = el.getBoundingClientRect();
            return {{x: b.x, y: b.y, width: b.width, height: b.height}};
        }})()""")
        if not r or r["width"] < 8 or r["height"] < 8:
            raise RuntimeError(f"cannot clip {selector!r}: {r}")
        return r

    def shot(self, path, dsf=2, full=False, clip=None):
        if full:
            h = self.eval("document.documentElement.scrollHeight")
            self.call("Emulation.setDeviceMetricsOverride", width=2048,
                      height=min(int(h), 6000), deviceScaleFactor=dsf, mobile=False)
            time.sleep(0.6)
        params = {"format": "png"}
        if clip:
            params["clip"] = {**clip, "scale": 1}
        data = self.call("Page.captureScreenshot", **params)["data"]
        raw = base64.b64decode(data)
        Path(path).write_bytes(raw)
        return raw


# --- browser lifecycle -------------------------------------------------------


def launch():
    profile = tempfile.mkdtemp(prefix="lens-cdp-")
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--remote-debugging-port=0",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", "--hide-scrollbars", "--force-color-profile=srgb",
        "--disable-lcd-text", "--window-size=2048,1280", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = Path(profile) / "DevToolsActivePort"
    for _ in range(200):
        if port_file.exists():
            break
        time.sleep(0.05)
    port = port_file.read_text().splitlines()[0].strip()
    for _ in range(200):
        try:
            pages = json.load(urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=2))
            page = next(p for p in pages if p.get("type") == "page")
            break
        except Exception:
            time.sleep(0.1)
    return proc, profile, CDP(page["webSocketDebuggerUrl"])


def open_report(cdp, name, dsf=2, w=2048, h=1280):
    cdp.call("Emulation.setDeviceMetricsOverride", width=w, height=h,
             deviceScaleFactor=dsf, mobile=False)
    cdp.call("Page.navigate", url=f"{BASE}/{name}/index.html")
    cdp.wait("document.querySelectorAll('#passList .row[data-id]').length",
             f"{name} pass list")
    cdp.eval("window.scrollTo(0, 0)")


def pick_pass(cdp, pid, wait=1.0):
    """Select a card. Mode comes after: a chip the current card cannot offer is
    hidden, so the order matters."""
    cdp.click(f'#passList .row[data-id="{pid}"]', f"pass {pid}")
    cdp.wait(f"!!document.querySelector('#passList .row.sel[data-id=\"{pid}\"]')"
             f" && document.querySelectorAll('#split').length > 0",
             f"pass {pid} rendered")
    time.sleep(wait)


def pick_run(cdp, run_index, settle=1.5):
    """Choose which run of a repeated pass the panes read. A card is one pass
    however many times it ran, so a figure that wants one particular run picks
    it here rather than by card id."""
    picked = cdp.eval(f"""(() => {{
        const s = document.getElementById('runPick');
        if (!s) return false;
        s.value = '{run_index}';
        if (s.value !== '{run_index}') return false;
        s.dispatchEvent(new Event('change', {{bubbles: true}}));
        return true;
    }})()""")
    if not picked:
        raise RuntimeError(f"this card does not offer run {run_index}")
    cdp.wait(f"+document.getElementById('runPick').value === {run_index}",
             f"run {run_index} picked")
    time.sleep(settle)


def pick_mode(cdp, mode, settle=1.2):
    """Click a view chip and confirm it actually took: `effectiveMode()` will
    quietly fall back to `ir` when the current card cannot offer the mode, so
    the active chip is the only honest check."""
    cdp.click(f'#modeCtl button[data-mode="{mode}"]', f"{mode} chip")
    cdp.wait(f"!!document.querySelector('#modeCtl button.on[data-mode=\"{mode}\"]')",
             f"{mode} active")
    time.sleep(settle)


def pick_lane(cdp, lane, settle=1.2):
    cdp.click(f'#passPanel .ptab[data-lane="{lane}"]', f"{lane} lane")
    cdp.wait(f"!!document.querySelector('#passPanel .ptab.active[data-lane=\"{lane}\"]')",
             f"{lane} lane active")
    time.sleep(settle)


def pick_function(cdp, name, settle=2.5):
    """Select a function from the rail. The row carries its own mangled name."""
    clicked = cdp.eval(
        "(() => { const r = document.querySelector("
        f"'#fnList .row[data-fn=\"{name}\"]'); if (!r) return false; r.click();"
        " return true; })()")
    if not clicked:
        raise RuntimeError(f"function {name!r} is not in the rail")
    cdp.wait(f"!!document.querySelector('#fnList .row.sel[data-fn=\"{name}\"]')",
             f"{name} selected")
    time.sleep(settle)


def find_pass(cdp, name, lane="ir", changed=None, run_index=None, nth=0):
    """A card id by pass name, so a rebuild that renumbers the pipeline does
    not silently point a figure at the wrong pass. A pass that ran many times
    gets many cards, so `run_index` picks the run -- the id itself shifts."""
    filt = "".join([
        f"p.lane === {json.dumps(lane)}",
        f" && p.name === {json.dumps(name)}",
        "" if changed is None else f" && p.changed === {json.dumps(changed)}",
        "" if run_index is None else f" && p.runIndex === {run_index}",
    ])
    ids = cdp.eval(f"CURRENT_MANIFEST.passes.filter(p => {filt}).map(p => p.id)")
    if len(ids) <= nth:
        raise RuntimeError(f"no card #{nth} named {name!r} in lane {lane} (found {ids})")
    return ids[nth]


def block_counts(cdp, fn):
    """How many basic blocks the diff on screen spans for one function. A CFG
    figure is only worth taking if the two sides differ, and the graph is only
    readable if the bigger side is small enough to fit without zooming out past
    legibility. Reads the run the picker is on, which is what the panes show."""
    return cdp.eval(f"""(() => {{
        const f = CURRENT_PASS && fnChange({json.dumps(fn)});
        if (!f) return null;
        const n = d => d ? (d.match(/^\\s*n\\d+\\s*\\[/gm) || []).length : 0;
        return {{before: n(f.dotBefore), after: n(f.dotAfter)}};
    }})()""")


def pane_stat(cdp):
    """The split pane's own header: the title and the stat line under it, which
    is where the run number and the churn a caption quotes are written."""
    return cdp.eval("""(() => {
        const t = document.querySelector('#split .pane-title');
        const s = document.querySelector('#split .pane-stat');
        return {title: t ? t.textContent.trim() : null,
                stat: s ? s.textContent.trim() : null,
                run: (document.getElementById('runPick') || {}).value || null};
    })()""")


def source_pane(cdp):
    """What the source pane is actually showing: the stat line the reader sees,
    the file chips above it, and how many mapped lines are in the highlighted
    column. A caption that claims a chip row has to see one here first."""
    return cdp.eval("""(() => {
        const p = document.querySelector('#split .pane[data-pane="src"], #split');
        const stat = (document.querySelector('#split .pane-stat') || {}).textContent;
        const chips = [...document.querySelectorAll('#split .ptab[data-srcfile]')]
            .map(b => b.textContent.trim());
        return {stat: stat || null, chips,
                mapped: document.querySelectorAll('#split .urow.cmap').length};
    })()""")


def find_pass_matching(cdp, needle, lane="ir", changed=None, nth=0):
    """A card id by substring, for names this build only partly determines."""
    filt = "".join([
        f"p.lane === {json.dumps(lane)}",
        f" && p.name.toLowerCase().includes({json.dumps(needle.lower())})",
        "" if changed is None else f" && p.changed === {json.dumps(changed)}",
    ])
    ids = cdp.eval(f"CURRENT_MANIFEST.passes.filter(p => {filt}).map(p => p.id)")
    if len(ids) <= nth:
        raise RuntimeError(f"no card #{nth} matching {needle!r} (found {ids})")
    return ids[nth]


def find_function(cdp, needle, nth=0):
    """A function's exact mangled name, matched on a readable substring."""
    fns = cdp.eval("Object.keys((CURRENT_PASS && CURRENT_PASS.functions) || {})")
    hits = [f for f in fns if needle in f]
    if len(hits) <= nth:
        raise RuntimeError(f"no function #{nth} matching {needle!r} (found {hits[:8]})")
    return hits[nth]


def pick_tab(cdp, tab, settle=0.9):
    """Open a bottom-drawer tab. Which tabs exist varies per card, so the tab
    is asserted present before the click rather than assumed."""
    cdp.wait(f"!!document.querySelector('#bottomTabs .vtab[data-tab=\"{tab}\"]')",
             f"{tab} tab exists")
    cdp.click(f'#bottomTabs .vtab[data-tab="{tab}"]', f"{tab} tab")
    cdp.wait(f"!!document.querySelector('#bottomTabs .vtab.active[data-tab=\"{tab}\"]')",
             f"{tab} tab active")
    time.sleep(settle)


def assert_ctx(cdp, must_contain):
    """The context line is the report's own statement of what is on screen."""
    ctx = cdp.eval("document.getElementById('ctx').textContent")
    if must_contain not in ctx:
        raise AssertionError(f"expected {must_contain!r} in context line, got {ctx!r}")
    return ctx
