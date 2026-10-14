"""
heartbeat.py — Sinal de vida externo (healthchecks.io) durante as queimas.

Se o Pi parar de dar sinal (queda de energia, travamento total, sem rede),
o healthchecks.io avisa — o próprio Pi não consegue avisar que morreu.

Funcionamento (só durante queimas, para não alertar com o Pi desligado
entre queimas):
  start()   início/retomada da queima → ping /start (retoma o check pausado)
  update()  chamado a cada ciclo; a cada PING_INTERVAL_S envia um ping com
            temperatura, setpoint, segmento etc. no corpo (fica no histórico)
  fail()    queima entrou em ERRO → ping /fail (alerta imediato pelo healthchecks)
  finish()  queima concluída/cancelada → ping de sucesso + pausa o check
            (requer KILN_HC_API_KEY; sem ela o check não é pausado)

Toda a comunicação roda numa thread própria: rede lenta ou fora do ar nunca
atrasa o loop de controle.

Configure o check com período 5 min e tolerância 5 min (alerta ~10 min
após o último sinal).
"""

import json
import queue
import re
import threading
import time
import urllib.error
import urllib.request

PING_INTERVAL_S = 60
TIMEOUT_S       = 10
EVENT_RETRIES   = 3
API_BASE        = "https://healthchecks.io/api/v3"

_UUID_RE = re.compile(r"/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?$", re.I)


class Heartbeat:

    def __init__(self, ping_url: str = "", api_key: str = "", api_base: str = API_BASE):
        self.ping_url = (ping_url or "").rstrip("/")
        self.api_key  = api_key or ""
        self.api_base = api_base.rstrip("/")
        m = _UUID_RE.search(self.ping_url)
        self.uuid = m.group(1) if m else None
        self.enabled = bool(self.ping_url)

        self._events  = queue.Queue()
        self._lock    = threading.Lock()
        self._active  = False
        self._info    = ""
        self._last_ping = 0.0
        self._last_err  = None

        if self.enabled:
            if self.api_key and not self.uuid:
                print("\r[heartbeat] A URL de ping não está no formato https://hc-ping.com/<uuid>; "
                      "o check não poderá ser pausado.")
            threading.Thread(target=self._worker, daemon=True).start()
            print("\r[heartbeat] Ativado (healthchecks.io).")

    # ── API usada pelo main (thread do loop) ───

    def start(self, info: str = ""):
        if not self.enabled:
            return
        with self._lock:
            self._active, self._info = True, info
        self._events.put(("start", info))

    def update(self, info: str):
        """Atualiza o texto do próximo ping (chamado a cada ciclo de controle)."""
        if self.enabled:
            with self._lock:
                self._info = info

    def fail(self, info: str):
        if not self.enabled:
            return
        with self._lock:
            self._active = False
        self._events.put(("fail", info))

    def finish(self, info: str):
        if not self.enabled:
            return
        with self._lock:
            self._active = False
        self._events.put(("finish", info))

    @property
    def active(self) -> bool:
        return self._active

    # ── Thread ────────────────────────────────

    def _worker(self):
        while True:
            try:
                self._step()
            except Exception as e:      # nunca deixa a thread morrer em silêncio
                err = f"erro inesperado: {e!r}"
                if err != self._last_err:
                    print(f"\r[heartbeat] {err} — continuando.")
                    self._last_err = err
                time.sleep(5)

    def _step(self):
        try:
            kind, info = self._events.get(timeout=1.0)
        except queue.Empty:
            kind = None
        if kind:
            self._handle_event(kind, info)
        else:
            self._tick()

    def _tick(self):
        """Ping periódico durante a queima."""
        with self._lock:
            active, info = self._active, self._info
        if active and time.monotonic() - self._last_ping >= PING_INTERVAL_S:
            if self._ping("", info):
                self._last_ping = time.monotonic()
            else:
                self._last_ping = time.monotonic() - PING_INTERVAL_S + 15   # tenta em 15 s

    def _handle_event(self, kind, info):
        suffix = {"start": "/start", "fail": "/fail", "finish": ""}[kind]
        for attempt in range(EVENT_RETRIES):
            if self._ping(suffix, info):
                break
            time.sleep(5 * (attempt + 1))
        self._last_ping = time.monotonic()
        if kind == "finish":
            self._pause()

    def _ping(self, suffix, body) -> bool:
        return self._request(self.ping_url + suffix, body.encode("utf-8"), {})

    def _pause(self):
        if not (self.api_key and self.uuid):
            return
        self._request(f"{self.api_base}/checks/{self.uuid}/pause", b"",
                      {"X-Api-Key": self.api_key})

    def _request(self, url, data, headers) -> bool:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S):
                pass
            if self._last_err:
                print("\r[heartbeat] Comunicação com o healthchecks.io normalizada.")
                self._last_err = None
            return True
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if e.code in (401, 403):
                err += " (chave de API inválida ou sem permissão de escrita?)"
            elif e.code == 404:
                err += " (check não encontrado — confira a URL/uuid)"
        except (urllib.error.URLError, OSError, ValueError) as e:
            err = str(e)
        if err != self._last_err:
            print(f"\r[heartbeat] Falha ao contatar o healthchecks.io: {err}")
            self._last_err = err
        return False


def describe(state, temp, curve_name=None) -> str:
    """Texto curto para o corpo do ping (aparece no histórico do check)."""
    d = {"modo": state.mode.name, "temp_c": round(temp, 1),
         "setpoint_c": round(state.setpoint, 1),
         "banco_a": state.bank_a, "banco_b": state.bank_b}
    if curve_name:
        d["curva"] = curve_name
        d["segmento"] = state.segment_index + 1
    return json.dumps(d, ensure_ascii=False)
