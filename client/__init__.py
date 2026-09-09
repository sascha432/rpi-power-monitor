"""Power-monitor web dashboard client package.

The client is a **stdlib-only Python web server**. It connects to the Raspberry
Pi over TCP (raw binary stream, ``shared.binary``), keeps a rolling in-memory
history, and serves an HTML/JS dashboard over HTTP + WebSocket. Run with
``python -m client`` and open http://<host>:<port>/ in a browser.
"""
