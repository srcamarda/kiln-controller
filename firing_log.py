"""
firing_log.py — Registro completo de cada queima para análise posterior.

Para cada queima (ou pré-aquecimento) são criados dois arquivos em
$LOGS_DIRECTORY (systemd: /var/log/kiln-controller) ou ./logs:

  AAAAMMDD-HHMMSS_<curva>.csv   uma linha por ciclo de controle (5 s) e uma
                                linha por evento (início, troca de segmento,
                                patamar, retomada, erro, fim...)
  AAAAMMDD-HHMMSS_<curva>.json  metadados: curva completa, parâmetros de
                                controle e segurança, e um resumo ao final

Depois do fim (concluída, cancelada ou erro) o registro continua durante o
resfriamento, uma linha por minuto, até a temperatura cair abaixo de
COOLDOWN_END_C ou passar COOLDOWN_MAX_S — útil para ver a curva de
resfriamento real do forno.

O log nunca derruba o controle: qualquer erro de E/S é avisado no console
uma vez e ignorado.
"""

import csv
import json
import os
import re
import threading
import time
import unicodedata
from datetime import datetime

_HERE   = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.environ.get("LOGS_DIRECTORY") or os.path.join(_HERE, "logs")

FSYNC_INTERVAL_S = 60       # grava em disco (fsync) no máximo a cada N s
COOLDOWN_LOG_S   = 60       # intervalo de registro durante o resfriamento
COOLDOWN_END_C   = 100.0    # para de registrar o resfriamento abaixo disso
COOLDOWN_MAX_S   = 24 * 3600
MAX_FIRINGS      = 100      # mantém os N registros mais recentes

COLUMNS = [
    "timestamp",        # data/hora local (RTC)
    "elapsed_s",        # segundos desde o início da queima (inclui tempo fora do ar)
    "mode",             # PREHEAT / RAMPING / SOAKING / FINISHED / ERROR / OFF / COOLDOWN
    "segment",          # segmento atual (1..N), vazio fora de curva
    "setpoint_c",
    "temp_c",           # temperatura filtrada usada pelo controle
    "temp_raw_c",       # última leitura bruta do termopar (vazio se inválida)
    "bank_a",           # 1 = ligado
    "bank_b",
    "b_suppressed",     # 1 = banco B cortado por subida rápida
    "rate_c_h",         # taxa bruta usada no controle (°C/h)
    "rate_valid",       # 1 = janela suficiente para o controle usar a taxa
    "rate_display_c_h", # taxa suavizada mostrada no display
    "sensor_fail",      # leituras inválidas seguidas no momento
    "event",            # texto do evento (linhas de evento)
]


def _slug(text: str) -> str:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    t = re.sub(r"[^A-Za-z0-9]+", "-", t).strip("-").lower()
    return t[:40] or "queima"


def _fmt(v, nd=1):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


class FiringLog:

    def __init__(self, log_dir: str = None):
        self.log_dir   = log_dir or LOG_DIR
        self.path      = None        # CSV atual
        self._f        = None
        self._w        = None
        self._meta     = None
        self._lock     = threading.Lock()
        self._last_sync = 0.0
        self._warned   = False
        self._cooldown_since = None
        self._last_cooldown_row = 0.0
        self._prev     = None        # (mode, segment) do último registro
        self._sum      = None        # resumo incremental

    # ── Ciclo de vida ─────────────────────────

    def start(self, kind: str, name: str, curve, params: dict, wall: datetime,
              start_temp: float):
        """Abre um novo registro (fecha o anterior, inclusive resfriamento)."""
        with self._lock:
            self._close()
            base = f"{wall.strftime('%Y%m%d-%H%M%S')}_{_slug(name)}"
            path = os.path.join(self.log_dir, base + ".csv")
            self._meta = {
                "kind":       kind,            # "curve" ou "preheat"
                "name":       name,
                "curve":      curve,
                "started_at": wall.isoformat(timespec="seconds"),
                "start_temp": start_temp,
                "params":     params,
                "resumes":    [],
                "summary":    None,
            }
            self._prev_banks = None
            self._sum = {"max_temp": start_temp, "switches_a": 0, "switches_b": 0,
                         "max_over_setpoint": 0.0, "segments": {}, "soak_max_over": None,
                         "soak_min_under": None, "sensor_fail_events": 0}
            if self._open(path, new=True):
                self._write_meta()

    def attach(self, path: str, note: str, downtime_s: float = 0.0):
        """Reabre o registro de uma queima retomada após reinício."""
        with self._lock:
            self._close()
            if not path or not os.path.exists(path):
                return False
            if not self._open(path, new=False):
                return False
            try:
                with open(self._meta_path(), encoding="utf-8") as f:
                    self._meta = json.load(f)
            except (OSError, ValueError):
                self._meta = {"resumes": []}
            self._meta.setdefault("resumes", []).append(
                {"at": datetime.now().isoformat(timespec="seconds"),
                 "downtime_s": round(downtime_s), "note": note})
            self._prev_banks = None
            self._sum = self._meta.get("partial_summary") or {
                "max_temp": 0.0, "switches_a": 0, "switches_b": 0,
                "max_over_setpoint": 0.0, "segments": {}, "soak_max_over": None,
                "soak_min_under": None, "sensor_fail_events": 0}
            self._write_meta()
            return True

    def end(self, reason: str, wall: datetime = None, row: dict = None):
        """Marca o fim da queima e passa a registrar o resfriamento."""
        with self._lock:
            if self._f is None:
                return
            self._event_row(row or {}, f"FIM: {reason}", wall)
            if self._meta is not None:
                self._meta["ended_at"] = (wall or datetime.now()).isoformat(timespec="seconds")
                self._meta["end_reason"] = reason
                self._meta["summary"] = self._summary()
                self._write_meta()
            self._cooldown_since = time.monotonic()
            self._last_cooldown_row = 0.0
            self._sync(force=True)

    def close(self):
        with self._lock:
            self._close()

    # ── Registro ──────────────────────────────

    def record(self, row: dict, wall: datetime = None, event: str = ""):
        """
        Uma linha por ciclo. `row` traz os campos de COLUMNS (sem timestamp).
        Eventos de troca de modo/segmento são detectados aqui.
        """
        with self._lock:
            if self._f is None:
                return
            now = time.monotonic()

            if self._cooldown_since is not None:
                temp = row.get("temp_c")
                if (now - self._cooldown_since > COOLDOWN_MAX_S
                        or (temp is not None and temp < COOLDOWN_END_C)):
                    self._event_row(row, "Fim do registro de resfriamento", wall)
                    self._close()
                    return
                if now - self._last_cooldown_row < COOLDOWN_LOG_S:
                    return
                self._last_cooldown_row = now
                row = dict(row, mode="COOLDOWN")
                self._write(row, wall, event)
                self._sync()
                return

            key = (row.get("mode"), row.get("segment"))
            if self._prev is not None and key != self._prev:
                if key[0] != self._prev[0]:
                    event = event or f"Modo: {self._prev[0]} → {key[0]}"
                else:
                    event = event or f"Segmento {self._prev[1]} → {key[1]}"
            self._update_summary(row)
            self._prev = key
            self._write(row, wall, event)
            self._sync()

    def event(self, text: str, row: dict = None, wall: datetime = None):
        with self._lock:
            if self._f is not None:
                self._event_row(row or {}, text, wall)
                self._sync(force=True)

    @property
    def active(self) -> bool:
        """True durante a queima (não conta o resfriamento)."""
        return self._f is not None and self._cooldown_since is None

    # ── Internos ──────────────────────────────

    def _open(self, path, new):
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            exists = os.path.exists(path)
            self._f = open(path, "a", newline="", encoding="utf-8")
            self._w = csv.writer(self._f)
            if new or not exists:
                self._w.writerow(COLUMNS)
            self.path = path
            self._prev = None
            self._cooldown_since = None
            self._last_sync = 0.0
            if new:
                self._prune()
            return True
        except OSError as e:
            self._warn(e)
            self._f = self._w = None
            self.path = None
            return False

    def _close(self):
        if self._f is not None:
            try:
                if self._meta is not None and self._meta.get("summary") is None:
                    self._meta["partial_summary"] = self._sum
                    self._write_meta()
                self._sync(force=True)
                self._f.close()
            except OSError as e:
                self._warn(e)
        self._f = self._w = None
        self._cooldown_since = None

    def _write(self, row, wall, event):
        wall = wall or datetime.now()
        values = [wall.isoformat(timespec="seconds")]
        for col in COLUMNS[1:-1]:
            v = row.get(col)
            values.append(_fmt(v, 0 if col == "elapsed_s" else 1))
        values.append(event or "")
        try:
            self._w.writerow(values)
        except (OSError, ValueError) as e:
            self._warn(e)

    def _event_row(self, row, text, wall):
        self._write(row, wall, text)

    def _sync(self, force=False):
        now = time.monotonic()
        if not force and now - self._last_sync < FSYNC_INTERVAL_S:
            return
        self._last_sync = now
        try:
            self._f.flush()
            os.fsync(self._f.fileno())
        except (OSError, ValueError) as e:
            self._warn(e)
        if self._meta is not None and self._meta.get("summary") is None:
            self._meta["partial_summary"] = self._sum
            self._write_meta()

    def _meta_path(self):
        return os.path.splitext(self.path)[0] + ".json"

    def _write_meta(self):
        if self.path is None or self._meta is None:
            return
        tmp = self._meta_path() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._meta, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self._meta_path())
        except OSError as e:
            self._warn(e)

    def _update_summary(self, row):
        s = self._sum
        if s is None:
            return
        temp, sp = row.get("temp_c"), row.get("setpoint_c")
        if temp is not None:
            temp = round(temp, 1)
            s["max_temp"] = max(s["max_temp"], temp)
        if self._prev_banks is not None:
            if row.get("bank_a") != self._prev_banks[0]:
                s["switches_a"] += 1
            if row.get("bank_b") != self._prev_banks[1]:
                s["switches_b"] += 1
        self._prev_banks = (row.get("bank_a"), row.get("bank_b"))
        if temp is not None and sp:
            s["max_over_setpoint"] = max(s["max_over_setpoint"], round(temp - sp, 1))
            if row.get("mode") == "SOAKING":
                d = round(temp - sp, 1)
                s["soak_max_over"]  = d if s["soak_max_over"] is None else max(s["soak_max_over"], d)
                s["soak_min_under"] = d if s["soak_min_under"] is None else min(s["soak_min_under"], d)
        seg = row.get("segment")
        if seg and row.get("mode") == "RAMPING":
            seg_s = s["segments"].setdefault(str(seg), {"first_elapsed_s": row.get("elapsed_s"),
                                                       "start_temp": temp})
            seg_s["last_elapsed_s"] = row.get("elapsed_s")
            seg_s["end_temp"] = temp
        if row.get("sensor_fail") == 1:
            s["sensor_fail_events"] += 1

    _prev_banks = None

    def _summary(self):
        s = dict(self._sum or {})
        for seg in s.get("segments", {}).values():
            a, b = seg.get("first_elapsed_s"), seg.get("last_elapsed_s")
            t0, t1 = seg.get("start_temp"), seg.get("end_temp")
            if a is not None and b is not None and b > a and t0 is not None and t1 is not None:
                seg["duration_min"] = round((b - a) / 60, 1)
                seg["actual_rate_c_h"] = round((t1 - t0) / (b - a) * 3600, 1)
        return s

    def _prune(self):
        try:
            csvs = sorted(f for f in os.listdir(self.log_dir) if f.endswith(".csv"))
        except OSError:
            return
        for old in csvs[:-MAX_FIRINGS]:
            for ext in (".csv", ".json"):
                try:
                    os.remove(os.path.join(self.log_dir, os.path.splitext(old)[0] + ext))
                except OSError:
                    pass

    def _warn(self, e):
        if not self._warned:
            print(f"\r[log] Falha ao gravar registro da queima: {e}")
            self._warned = True
