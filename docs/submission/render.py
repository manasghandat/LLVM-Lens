"""Render report.html to the submission PDF.

Reuses capture.py's CDP client, since the only browser on this machine is the
one Playwright downloaded -- `chrome` channel is not installed. The page is
loaded over HTTP rather than file:// so the @font-face files resolve without
depending on Chromium's file-origin rules.

    python3 -m http.server 8799 &        # from this directory
    python3 render.py
"""

import base64
import sys
from pathlib import Path

from capture import CDP, launch

HERE = Path(__file__).resolve().parent
SRC = "http://127.0.0.1:8799/report.html"
OUT = HERE / "LLVM-Lens-SEGFAULT-2026.pdf"

# A4, with the margins the CSS is laid out against: 178mm of text width.
MARGIN_MM = {"top": 15, "bottom": 14, "left": 16, "right": 16}
MM = 1 / 25.4

FOOTER = (
    '<div style="width:100%;text-align:center;font-family:monospace;'
    'font-size:7pt;color:#5A6472"><span class="pageNumber"></span></div>'
)


def main():
    proc, _profile, cdp = launch()
    try:
        cdp.call("Emulation.setDeviceMetricsOverride", width=1000, height=1400,
                 deviceScaleFactor=1, mobile=False)
        cdp.call("Page.navigate", url=SRC)
        # However many <figure> blocks the document carries, all of them loaded.
        cdp.wait("""document.readyState === 'complete'
                 && document.images.length >= document.querySelectorAll('figure').length
                 && document.images.length > 0""",
                 "the document and its figures")
        cdp.eval("document.fonts.ready.then(() => true)")

        # A page that silently fell back to a system serif, or dropped a figure,
        # would still print -- so both are checked before the PDF is written.
        report = cdp.eval("""(() => ({
            fonts: [...new Set([...document.fonts]
                     .filter(f => f.status === 'loaded')
                     .map(f => f.family + ' ' + f.weight + ' ' + f.style))],
            figures: document.images.length,
            broken: [...document.images].filter(i => !i.naturalWidth)
                      .map(i => i.getAttribute('src')),
            heightPx: document.documentElement.scrollHeight,
        }))()""")
        print(f"  fonts loaded : {report['fonts']}")
        print(f"  figures      : {report['figures']}, broken: {report['broken']}")
        print(f"  document     : {report['heightPx']}px tall")
        if report["broken"]:
            raise SystemExit(f"broken figures: {report['broken']}")

        cdp.call("Emulation.setEmulatedMedia", media="print")
        res = cdp.call(
            "Page.printToPDF",
            printBackground=True,
            preferCSSPageSize=False,
            paperWidth=210 * MM, paperHeight=297 * MM,
            marginTop=MARGIN_MM["top"] * MM, marginBottom=MARGIN_MM["bottom"] * MM,
            marginLeft=MARGIN_MM["left"] * MM, marginRight=MARGIN_MM["right"] * MM,
            displayHeaderFooter=True, headerTemplate="<div></div>",
            footerTemplate=FOOTER, transferMode="ReturnAsBase64",
        )
        raw = base64.b64decode(res["data"])
        OUT.write_bytes(raw)
        print(f"  wrote        : {OUT.name}  {len(raw) / 1e6:.1f} MB")
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
