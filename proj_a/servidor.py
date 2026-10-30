import json
import os
import secrets
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
MODE = os.getenv("PLC_MODE", "simulation").lower()
PLC_IP = os.getenv("PLC_IP", "xx.xx.xx.xx")
PORT = int(os.getenv("PORT", "8000"))
APP_PASSWORD = os.getenv("APP_PASSWORD", "21010")
SESSION_TTL = 8 * 60 * 60
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = 60
sessions = {}
sessions_lock = threading.Lock()
login_failures = {}
login_failures_lock = threading.Lock()

TAGS = {
    "level": os.getenv("TANK_LEVEL_TAG", "Tank_Level"),
    "pump1_command": os.getenv("PUMP1_COMMAND_TAG", "Pump1_Command"),
    "pump1_running": os.getenv("PUMP1_RUNNING_TAG", "Pump1_Running"),
    "pump2_command": os.getenv("PUMP2_COMMAND_TAG", "Pump2_Command"),
    "pump2_running": os.getenv("PUMP2_RUNNING_TAG", "Pump2_Running"),
}


class Simulation:
    def __init__(self):
        self.lock = threading.RLock()
        self.level = 54.0
        self.pumps = {1: False, 2: False}
        self.last_update = time.monotonic()

    def _advance(self):
        now = time.monotonic()
        elapsed = min(now - self.last_update, 5.0)
        self.last_update = now
        self.level += (int(self.pumps[1]) * 1.1 - int(self.pumps[2]) * 1.4) * elapsed
        self.level = max(0.0, min(100.0, self.level))
        if self.level >= 100:
            self.pumps[1] = False
        if self.level <= 0:
            self.pumps[2] = False

    def status(self):
        with self.lock:
            self._advance()
            return {
                "mode": "simulation",
                "connected": True,
                "tankLevel": round(self.level, 1),
                "pumps": [
                    {"id": 1, "name": "Bomba 01", "running": self.pumps[1], "direction": "Enchimento"},
                    {"id": 2, "name": "Bomba 02", "running": self.pumps[2], "direction": "Descarga"},
                ],
                "updatedAt": time.strftime("%H:%M:%S"),
            }

    def set_pump(self, pump_id, running):
        with self.lock:
            self._advance()
            if running and pump_id == 1 and self.level >= 100:
                raise ValueError("Tanque cheio; a bomba de enchimento não pode ser ligada.")
            if running and pump_id == 2 and self.level <= 0:
                raise ValueError("Tanque vazio; a bomba de recirculação não pode ser ligada.")
            self.pumps[pump_id] = running
            return self.status()


class RockwellPLC:
    def __init__(self):
        self.lock = threading.Lock()

    def _open(self):
        if "x" in PLC_IP.lower():
            raise RuntimeError("Configure PLC_IP com o endereço do controlador antes de conectar.")
        from pycomm3 import LogixDriver

        return LogixDriver(PLC_IP)

    @staticmethod
    def _check_results(results):
        for result in results:
            if result.error:
                raise RuntimeError(str(result.error))

    def status(self):
        with self.lock, self._open() as plc:
            results = plc.read(TAGS["level"], TAGS["pump1_running"], TAGS["pump2_running"])
            self._check_results(results)
            level, pump1, pump2 = (result.value for result in results)
            return {
                "mode": "rockwell",
                "connected": True,
                "tankLevel": round(float(level), 1),
                "pumps": [
                    {"id": 1, "name": "Bomba 01", "running": bool(pump1), "direction": "Enchimento"},
                    {"id": 2, "name": "Bomba 02", "running": bool(pump2), "direction": "Descarga"},
                ],
                "updatedAt": time.strftime("%H:%M:%S"),
            }

    def set_pump(self, pump_id, running):
        command_tag = TAGS[f"pump{pump_id}_command"]
        with self.lock, self._open() as plc:
            result = plc.write((command_tag, running))
            self._check_results([result])
        return self.status()


simulation = Simulation()
controller = RockwellPLC() if MODE == "rockwell" else simulation


class RequestHandler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        morsel = cookie.get("plc_session")
        return morsel.value if morsel else None

    def _is_authenticated(self):
        token = self._session_token()
        if not token:
            return False
        with sessions_lock:
            expires_at = sessions.get(token)
            if expires_at is None:
                return False
            if expires_at <= time.time():
                sessions.pop(token, None)
                return False
            return True

    def _require_authentication(self):
        if self._is_authenticated():
            return True
        self._send_json({"error": "Autenticação necessária."}, 401)
        return False

    def _login_attempts_blocked(self, client_ip):
        now = time.monotonic()
        with login_failures_lock:
            attempt = login_failures.get(client_ip)
            if attempt is None:
                return False
            started_at, failures = attempt
            if now - started_at >= LOGIN_WINDOW:
                login_failures.pop(client_ip, None)
                return False
            return failures >= LOGIN_MAX_FAILURES

    def _record_login_failure(self, client_ip):
        now = time.monotonic()
        with login_failures_lock:
            attempt = login_failures.get(client_ip)
            if attempt is None or now - attempt[0] >= LOGIN_WINDOW:
                login_failures[client_ip] = (now, 1)
            else:
                login_failures[client_ip] = (attempt[0], attempt[1] + 1)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/session":
            self._send_json({"authenticated": self._is_authenticated()})
            return
        if path == "/api/status":
            if not self._require_authentication():
                return
            try:
                self._send_json(controller.status())
            except Exception as error:
                self._send_json({"connected": False, "mode": MODE, "error": str(error)}, 502)
            return

        if path in ("/", "/index.html"):
            body = (BASE_DIR / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self._send_json({"error": "Rota não encontrada."}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/login":
            client_ip = self.client_address[0]
            if self._login_attempts_blocked(client_ip):
                self._send_json({"error": "Muitas tentativas. Aguarde um minuto."}, 429)
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length > 1024:
                    raise ValueError("Requisição muito grande.")
                payload = json.loads(self.rfile.read(content_length))
                password = payload.get("password")
                if not isinstance(password, str) or not secrets.compare_digest(password, APP_PASSWORD):
                    self._record_login_failure(client_ip)
                    self._send_json({"error": "Senha incorreta."}, 401)
                    return
            except (ValueError, json.JSONDecodeError):
                self._send_json({"error": "Requisição de login inválida."}, 400)
                return

            token = secrets.token_urlsafe(32)
            now = time.time()
            with login_failures_lock:
                login_failures.pop(client_ip, None)
            with sessions_lock:
                expired = [key for key, expires_at in sessions.items() if expires_at <= now]
                for key in expired:
                    sessions.pop(key, None)
                sessions[token] = now + SESSION_TTL
            self._send_json(
                {"authenticated": True},
                headers={"Set-Cookie": f"plc_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={SESSION_TTL}"},
            )
            return

        if path == "/api/logout":
            token = self._session_token()
            if token:
                with sessions_lock:
                    sessions.pop(token, None)
            self._send_json(
                {"authenticated": False},
                headers={"Set-Cookie": "plc_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"},
            )
            return

        if not self._require_authentication():
            return

        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[:2] != ["api", "pump"]:
            self._send_json({"error": "Rota não encontrada."}, 404)
            return

        try:
            pump_id = int(parts[2])
            if pump_id not in (1, 2):
                raise ValueError("Bomba inválida.")
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > 1024:
                raise ValueError("Requisição muito grande.")
            payload = json.loads(self.rfile.read(content_length))
            running = payload.get("running")
            if not isinstance(running, bool):
                raise ValueError("O campo 'running' deve ser verdadeiro ou falso.")
            self._send_json(controller.set_pump(pump_id, running))
        except ValueError as error:
            self._send_json({"error": str(error)}, 400)
        except Exception as error:
            self._send_json({"connected": False, "mode": MODE, "error": str(error)}, 502)

    def log_message(self, format_string, *args):
        print(f"[{self.log_date_time_string()}] {format_string % args}")


if MODE not in ("simulation", "rockwell"):
    raise SystemExit("PLC_MODE deve ser 'simulation' ou 'rockwell'.")

server = ThreadingHTTPServer(("0.0.0.0", PORT), RequestHandler)
server.daemon_threads = True
print(f"Dashboard disponível em http://localhost:{PORT} (modo: {MODE})")
try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\nServidor encerrado.")
    server.server_close()