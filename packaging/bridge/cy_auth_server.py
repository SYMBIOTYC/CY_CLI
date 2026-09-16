#!/usr/bin/env python3
"""CY local auth server.

Starts a temporary HTTP server on 127.0.0.1:1455, opens the browser to
auth.symbiotyc.workers.dev, and waits for the callback with cy_api_key.
Writes the key to ~/.cy/auth.json and exits.
"""
import http.server
import json
import os
import sys
import subprocess
import threading
import time

PORT = 1455
CY_HOME = os.environ.get("CY_HOME", os.path.expanduser("~/.cy"))
AUTH_FILE = os.path.join(CY_HOME, "auth.json")
AUTH_URL = "https://auth.symbiotyc.workers.dev"


class AuthHandler(http.server.BaseHTTPRequestHandler):
    """Handles the callback from auth-worker with cy_api_key."""

    def do_GET(self):
        if self.path.startswith("/callback"):
            # Parse query string
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            api_key = params.get("cy_api_key", [None])[0]
            email = params.get("email", [None])[0]

            if api_key:
                # Write to auth.json
                os.makedirs(CY_HOME, exist_ok=True)
                data = {"auth_mode": "apiKey", "cy_api_key": api_key}
                if email:
                    data["email"] = email
                with open(AUTH_FILE, "w") as f:
                    json.dump(data, f, indent=2)
                os.chmod(AUTH_FILE, 0o600)

                # Send success page
                html = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<title>CY — Авторизация</title>
<style>
  body { background: #050505; color: #fff; font-family: -apple-system, sans-serif;
         display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
  .card { max-width: 400px; padding: 32px; text-align: center; }
  .logo { font-size: 48px; font-weight: 800; letter-spacing: 12px; margin-bottom: 16px; }
  .msg { color: #00ff88; font-size: 14px; margin-bottom: 8px; }
  .sub { color: rgba(255,255,255,0.6); font-size: 12px; }
</style>
</head>
<body>
  <div class="card">
    <div class="logo">CY</div>
    <div class="msg">Авторизация прошла успешно!</div>
    <div class="sub">Можно закрыть эту вкладку и вернуться в терминал.</div>
  </div>
</body>
</html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(html.encode())

                # Signal success
                print(f"\n[OK] API ключ получен и сохранён в {AUTH_FILE}")
                print(f"  Email: {email or 'n/a'}")
                print(f"  Ключ: {api_key[:12]}...{api_key[-4:]}")

                # Shutdown after sending response
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Missing cy_api_key parameter")
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not found")

    def log_message(self, format, *args):
        pass  # Suppress logs


def main():
    # Check if port is already in use
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", PORT))
        sock.close()
    except OSError:
        print(f"Порт {PORT} уже занят. Попробуйте закрыть другие процессы CY.")
        sys.exit(1)

    # Start server
    server = http.server.HTTPServer(("127.0.0.1", PORT), AuthHandler)
    server.timeout = 300  # 5 minutes timeout

    print(f"CY авторизация")
    print(f"Сервер запущен на http://127.0.0.1:{PORT}")
    print(f"Открываю браузер...")

    # Open browser
    auth_url = f"{AUTH_URL}/auth/google?callback=http://127.0.0.1:{PORT}/callback"
    try:
        subprocess.Popen(["open", auth_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        print(f"Не удалось открыть браузер. Перейдите вручную:")
        print(f"  {auth_url}")

    print(f"Ожидаю авторизацию... (таймаут 5 минут)")

    # Wait for callback
    server.serve_forever()

    print("Готово!")
    server.server_close()


if __name__ == "__main__":
    main()
