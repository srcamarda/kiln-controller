"""
controller.py — Lógica de controle do forno.

Responsabilidades:
  - Calcular o setpoint dinâmico conforme o segmento ativo da curva
  - Controlar os bancos A e B via bang-bang com histerese
  - Detectar overshoot de rate e desligar banco B preventivamente
  - Gerenciar progressão entre segmentos e transição para soak
  - Modo pré-aquecimento (banco A, setpoint fixo)
  - Limites de segurança (desligam e travam em ERROR):
      · sobretemperatura absoluta e relativa ao alvo
      · aquecimento com relés desligados (contator colado)
      · "não sobe" (stall) com aquecimento comandado
      · tempo máximo por segmento e de pré-aquecimento
"""

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

def _log(msg): print(f"\r{msg}")


# ──────────────────────────────────────────────
# Configurações
# ──────────────────────────────────────────────

HYSTERESIS      = 8.0    # °C — margem bang-bang (liga abaixo, desliga acima)
WARMUP_TEMP     = 60.0   # °C — abaixo disso banco A fica sempre ligado (sem bang-bang)
DUAL_BANK_TEMP  = 600.0  # °C — abaixo disso só banco A
RATE_TOLERANCE  = 1.20   # 20% acima do rate alvo → desliga banco B
RATE_WINDOW     = 300.0  # segundos para calcular o rate atual
PREHEAT_MIN     = 100    # °C — mínimo do modo pré-aquecimento
PREHEAT_MAX     = 600    # °C — máximo do modo pré-aquecimento
PREHEAT_STEP    = 10     # °C — passo do encoder no pré-aquecimento

# ── Limites de segurança ──────────────────────
OVERTEMP_ABS        = 1300.0  # °C — limite absoluto, vale em qualquer modo (inclusive parado)
OVERTEMP_MARGIN     = 50.0    # °C — acima do alvo do modo ativo → erro
RUNAWAY_RISE        = 30.0    # °C — subida com os dois bancos desligados → contator colado?
STALL_LAG           = 10.0    # °C — só avalia stall se estiver ao menos isto abaixo do setpoint
STALL_MIN_RISE      = 10.0    # °C — subida mínima esperada dentro de STALL_TIMEOUT_S
STALL_TIMEOUT_S     = 40 * 60 # s  — tempo sem subir STALL_MIN_RISE com aquecimento ligado
SEGMENT_TIMEOUT_FACTOR = 2.0  # × duração teórica do segmento
SEGMENT_TIMEOUT_EXTRA_S = 60 * 60  # s — folga somada ao tempo teórico
PREHEAT_MAX_S       = 24 * 3600    # s — duração máxima do pré-aquecimento

# ── Retomada após reinício ────────────────────
SOAK_RESUME_BAND    = 30.0    # °C — no soak, se esfriou mais que isso volta a rampar


# ──────────────────────────────────────────────
# Estado do controlador
# ──────────────────────────────────────────────

class ErrorKind(Enum):
    SENSOR   = auto()   # falha no termopar
    OVERTEMP = auto()   # sobretemperatura
    RUNAWAY  = auto()   # aquece com relés desligados
    STALL    = auto()   # não sobe com aquecimento ligado
    TIMEOUT  = auto()   # segmento/pré-aquecimento excedeu o tempo
    INTERRUPTED = auto()  # queima interrompida e não retomada após reinício


class ControllerMode(Enum):
    OFF      = auto()
    PREHEAT  = auto()
    RAMPING  = auto()  # subindo em segmento de curva
    SOAKING  = auto()  # patamar final
    FINISHED = auto()
    ERROR    = auto()


@dataclass
class ControllerState:
    mode:             ControllerMode = ControllerMode.OFF

    # Curva ativa
    curve:            Optional[dict] = None
    segment_index:    int            = 0
    segment_start_temp: float        = 0.0
    segment_start_time: float        = 0.0

    # Soak
    soak_start_time:  Optional[float] = None

    # Pré-aquecimento
    preheat_target:   float          = 200.0
    preheat_start_time: Optional[float] = None   # monotonic

    # Saídas
    bank_a:           bool           = False
    bank_b:           bool           = False

    # Diagnóstico
    setpoint:         float          = 0.0
    current_rate:     float          = 0.0   # °C/h medido (bruto, usado no controle)
    display_rate:     float          = 0.0   # °C/h suavizado (só para exibição)
    bank_b_suppressed: bool          = False  # desligado por overshoot

    # Histórico para suavização do rate exibido
    _rate_history:    list = field(default_factory=list)  # últimas N leituras de rate

    # Horário de início da queima (epoch)
    run_start_time:   Optional[float] = None

    # Histórico para cálculo de rate
    _temp_history:    list = field(default_factory=list)  # [(timestamp, temp)]

    # Erro (preenchido quando mode == ERROR)
    error_kind:       Optional[ErrorKind] = None
    error_title:      str            = ""   # curto, para o display
    error_message:    str            = ""   # detalhado, para log/Telegram

    # Monitoramento de segurança
    segment_deadline: Optional[float] = None   # monotonic
    _stall_ref:       Optional[tuple] = None   # (t, temp) desde que passou a "ficar para trás"
    _runaway_ref:     Optional[float] = None   # menor temp com bancos desligados


# ──────────────────────────────────────────────
# Controlador principal
# ──────────────────────────────────────────────

class KilnController:

    def __init__(self):
        self.state = ControllerState()

    # ── API pública ────────────────────────────

    def start_curve(self, curve: dict, start_temp: float):
        """Inicia uma curva de queima a partir da temperatura atual."""
        s = self.state
        s.curve              = curve
        s.segment_index      = 0
        s.segment_start_temp = start_temp
        s.segment_start_time = time.monotonic()
        s.run_start_time     = time.time()
        s.soak_start_time    = None
        s.mode               = ControllerMode.RAMPING
        s._temp_history      = []
        self._reset_safety()
        self._set_segment_deadline()
        _log(f"[ctrl] Iniciando curva '{curve['name']}' em {start_temp:.1f}°C")

    def start_preheat(self, target: float, current_temp: float):
        """Inicia modo pré-aquecimento com alvo fixo."""
        target = max(PREHEAT_MIN, min(PREHEAT_MAX, target))
        self.state.preheat_target = target
        self.state.mode           = ControllerMode.PREHEAT
        self.state.run_start_time = time.time()
        self._reset_safety()
        self.state.preheat_start_time = time.monotonic()
        self.state.segment_deadline = self.state.preheat_start_time + PREHEAT_MAX_S
        _log(f"[ctrl] Pré-aquecimento → {target:.0f}°C")

    def stop(self):
        """Para tudo e desliga os relés."""
        s = self.state
        s.mode   = ControllerMode.OFF
        s.bank_a = False
        s.bank_b = False
        self._reset_safety()
        _log("[ctrl] Parado.")

    # ── Persistência / retomada ────────────────

    RESUMABLE_MODES = (ControllerMode.PREHEAT, ControllerMode.RAMPING,
                       ControllerMode.SOAKING)

    def is_active(self) -> bool:
        return self.state.mode in self.RESUMABLE_MODES

    def snapshot(self, temp: float) -> Optional[dict]:
        """Estado mínimo para retomar a queima após reinício (sem monotonic)."""
        s = self.state
        if not self.is_active():
            return None
        now = time.monotonic()
        snap = {
            "mode":           s.mode.name,
            "curve":          s.curve,
            "segment_index":  s.segment_index,
            "preheat_target": s.preheat_target,
            "run_elapsed_s":  self.run_elapsed_seconds(),
            "last_temp":      temp,
        }
        if s.mode == ControllerMode.SOAKING and s.soak_start_time is not None:
            snap["soak_elapsed_s"] = now - s.soak_start_time
        if s.mode == ControllerMode.PREHEAT and s.preheat_start_time is not None:
            snap["preheat_elapsed_s"] = now - s.preheat_start_time
        return snap

    def resume(self, snap: dict, temp: float, downtime_s: float = 0.0) -> str:
        """
        Retoma a partir de um snapshot. A rampa recomeça da temperatura atual
        (o setpoint é reconstruído a partir dela); se o forno esfriou abaixo
        de segmentos já concluídos, volta para o segmento correspondente para
        respeitar as taxas programadas. Retorna uma descrição para log/alerta.
        """
        s   = self.state
        now = time.monotonic()
        mode = ControllerMode[snap["mode"]]

        self._reset_safety()
        s._temp_history  = []
        s._rate_history  = []
        s.current_rate   = 0.0
        s.display_rate   = 0.0
        s.bank_b_suppressed = False
        s.run_start_time = time.time() - snap.get("run_elapsed_s", 0.0) - max(0.0, downtime_s)
        s.preheat_target = snap.get("preheat_target", s.preheat_target)
        s.curve          = snap.get("curve")
        s.soak_start_time = None

        if mode == ControllerMode.PREHEAT:
            elapsed = snap.get("preheat_elapsed_s", 0.0)
            s.mode = ControllerMode.PREHEAT
            s.preheat_start_time = now - elapsed
            s.segment_deadline   = s.preheat_start_time + PREHEAT_MAX_S
            desc = f"pré-aquecimento a {s.preheat_target:.0f} °C"
            _log(f"[ctrl] Retomando {desc}")
            return desc

        segs = s.curve["segments"]
        final_target = float(segs[-1]["target_temp"])

        if mode == ControllerMode.SOAKING and temp >= final_target - SOAK_RESUME_BAND:
            s.mode            = ControllerMode.SOAKING
            s.segment_index   = len(segs) - 1
            s.soak_start_time = now - snap.get("soak_elapsed_s", 0.0)
            desc = f"patamar em {final_target:.0f} °C ({self.soak_remaining_seconds() / 60:.0f} min restantes)"
            _log(f"[ctrl] Retomando {desc}")
            return desc

        idx = len(segs) - 1 if mode == ControllerMode.SOAKING else int(snap["segment_index"])
        idx = max(0, min(idx, len(segs) - 1))
        while idx > 0 and temp < float(segs[idx - 1]["target_temp"]) - HYSTERESIS:
            idx -= 1

        s.mode               = ControllerMode.RAMPING
        s.segment_index      = idx
        s.segment_start_temp = temp
        s.segment_start_time = now
        self._set_segment_deadline()
        desc = f"segmento {idx + 1}/{len(segs)} a partir de {temp:.0f} °C"
        _log(f"[ctrl] Retomando {desc}")
        return desc

    def fail(self, kind: "ErrorKind", title: str, message: str):
        """Coloca o controlador em ERROR (travado) por motivo externo."""
        self._fault(kind, title, message)

    def set_preheat_target(self, target: float):
        target = max(PREHEAT_MIN, min(PREHEAT_MAX, target))
        self.state.preheat_target = target

    # ── Tick principal ─────────────────────────

    def update(self, temp: float, fault: bool) -> ControllerState:
        """
        Deve ser chamado a cada ciclo do loop principal (~1s).
        Retorna o estado atualizado com bank_a e bank_b definidos.
        """
        s = self.state

        if fault:
            self._fault(ErrorKind.SENSOR, "Falha no termopar",
                        "Falha no termopar detectada.")
            return s

        if s.mode == ControllerMode.ERROR:
            s.bank_a = False
            s.bank_b = False
            return s

        self._update_rate(temp)

        # Limites que independem do modo (usam os bancos do ciclo anterior,
        # que são os que estão efetivamente nos relés)
        if self._check_limits(temp):
            return s

        if s.mode == ControllerMode.OFF:
            s.bank_a = False
            s.bank_b = False

        elif s.mode == ControllerMode.PREHEAT:
            self._run_preheat(temp)

        elif s.mode == ControllerMode.RAMPING:
            self._run_ramping(temp)

        elif s.mode == ControllerMode.SOAKING:
            self._run_soaking(temp)

        elif s.mode == ControllerMode.FINISHED:
            s.bank_a = False
            s.bank_b = False

        self._check_stall(temp)
        return s

    # ── Modos internos ─────────────────────────

    def _run_preheat(self, temp: float):
        s = self.state
        s.setpoint = s.preheat_target
        s.bank_b   = False
        s.bank_a   = self._bangbang(temp, s.setpoint)

    def _run_ramping(self, temp: float):
        s = self.state
        seg = s.curve["segments"][s.segment_index]

        # Setpoint dinâmico: avança conforme o rate do segmento
        elapsed  = time.monotonic() - s.segment_start_time
        rate_ps  = seg["rate_per_hour"] / 3600.0        # °C/s
        setpoint = s.segment_start_temp + rate_ps * elapsed
        setpoint = min(setpoint, float(seg["target_temp"]))
        s.setpoint = setpoint

        # Verifica fim do segmento
        if temp >= seg["target_temp"] - HYSTERESIS:
            next_index = s.segment_index + 1
            if next_index < len(s.curve["segments"]):
                # Avança para o próximo segmento
                _log(f"[ctrl] Segmento {s.segment_index+1} completo → "
                      f"segmento {next_index+1}")
                s.segment_index      = next_index
                s.segment_start_temp = temp
                s.segment_start_time = time.monotonic()
                self._set_segment_deadline()
            else:
                # Todos os segmentos concluídos → inicia soak
                _log("[ctrl] Segmentos completos → iniciando soak")
                s.mode            = ControllerMode.SOAKING
                s.soak_start_time = time.monotonic()
                s.segment_deadline = None   # o soak termina sozinho

        self._apply_banks(temp, s.setpoint)

    def _run_soaking(self, temp: float):
        s = self.state
        seg_final = s.curve["segments"][-1]
        s.setpoint = float(seg_final["target_temp"])

        elapsed_min = (time.monotonic() - s.soak_start_time) / 60.0
        remaining   = s.curve["soak_minutes"] - elapsed_min

        if remaining <= 0:
            _log("[ctrl] Soak concluído — finalizando queima.")
            s.mode   = ControllerMode.FINISHED
            s.bank_a = False
            s.bank_b = False
            return

        self._apply_banks(temp, s.setpoint)

    # ── Controle de bancos ─────────────────────

    def _apply_banks(self, temp: float, setpoint: float):
        """
        Decide quais bancos ligar considerando:
          - Warmup: abaixo de WARMUP_TEMP banco A fica sempre ligado
          - Temperatura atual (abaixo de DUAL_BANK_TEMP → só A)
          - Rate atual vs rate alvo (suprime B se subindo rápido demais)
        """
        s = self.state

        if temp < WARMUP_TEMP:
            # Fase de aquecimento inicial — banco A sempre ligado, sem bang-bang
            s.bank_a = True
            s.bank_b = False
            s.bank_b_suppressed = False
            return

        if temp < DUAL_BANK_TEMP:
            # Abaixo de 600°C: só banco A com bang-bang
            s.bank_b = False
            s.bank_b_suppressed = False
            s.bank_a = self._bangbang(temp, setpoint)
            return

        # Acima de 600°C: verifica supressão de rate
        seg = s.curve["segments"][s.segment_index]
        target_rate = float(seg["rate_per_hour"])

        if s.current_rate > target_rate * RATE_TOLERANCE:
            # Subindo rápido demais — desliga banco B
            s.bank_b_suppressed = True
            s.bank_b = False
        else:
            s.bank_b_suppressed = False
            s.bank_b = self._bangbang(temp, setpoint)

        s.bank_a = self._bangbang(temp, setpoint)

    def _bangbang(self, temp: float, setpoint: float) -> bool:
        """
        Retorna True (ligar) ou False (desligar).
        Liga quando abaixo do setpoint, desliga quando acima de setpoint + histerese.
        Para fornos (sistema lento), isso é suficiente para evitar chatter nos relés.
        """
        if temp >= setpoint + HYSTERESIS:
            return False
        if temp < setpoint:
            return True
        # Dentro da banda [setpoint, setpoint + histerese]: mantém desligado
        return False

    # ── Utilitários ───────────────────────────

    DISPLAY_RATE_SAMPLES = 30  # média móvel de N amostras para exibição

    def _update_rate(self, temp: float):
        """
        Calcula o rate bruto (janela deslizante) e o rate suavizado (média móvel).
        O rate bruto é usado no controle de bancos.
        O rate suavizado é usado apenas para exibição no display.
        """
        now = time.monotonic()
        s   = self.state
        s._temp_history.append((now, temp))

        # Remove amostras fora da janela
        cutoff = now - RATE_WINDOW
        s._temp_history = [(t, v) for t, v in s._temp_history if t >= cutoff]

        if len(s._temp_history) >= 2:
            t0, v0 = s._temp_history[0]
            t1, v1 = s._temp_history[-1]
            dt = t1 - t0
            if dt > 0:
                s.current_rate = (v1 - v0) / dt * 3600.0  # °C/hora
        else:
            s.current_rate = 0.0

        # Suavização para display — média móvel das últimas N leituras
        s._rate_history.append(s.current_rate)
        if len(s._rate_history) > self.DISPLAY_RATE_SAMPLES:
            s._rate_history = s._rate_history[-self.DISPLAY_RATE_SAMPLES:]
        s.display_rate = sum(s._rate_history) / len(s._rate_history)

    # ── Segurança ─────────────────────────────

    def _reset_safety(self):
        s = self.state
        s.error_kind    = None
        s.error_title   = ""
        s.error_message = ""
        s.segment_deadline = None
        s._stall_ref    = None
        s._runaway_ref  = None

    def _set_segment_deadline(self):
        """Prazo do segmento atual: duração teórica × fator + folga."""
        s = self.state
        seg  = s.curve["segments"][s.segment_index]
        rate = float(seg["rate_per_hour"])
        span = max(0.0, float(seg["target_temp"]) - s.segment_start_temp)
        expected_s = span / rate * 3600.0 if rate > 0 else 0.0
        s.segment_deadline = (time.monotonic()
                              + expected_s * SEGMENT_TIMEOUT_FACTOR
                              + SEGMENT_TIMEOUT_EXTRA_S)

    def _mode_target(self) -> Optional[float]:
        s = self.state
        if s.mode == ControllerMode.PREHEAT:
            return float(s.preheat_target)
        if s.mode == ControllerMode.RAMPING:
            return float(s.curve["segments"][s.segment_index]["target_temp"])
        if s.mode == ControllerMode.SOAKING:
            return float(s.curve["segments"][-1]["target_temp"])
        return None

    def _check_limits(self, temp: float) -> bool:
        """Sobretemperatura, aquecimento com relés desligados e prazos.
        Retorna True se entrou em ERROR."""
        s   = self.state
        now = time.monotonic()

        # 1. Sobretemperatura absoluta — qualquer modo
        if temp > OVERTEMP_ABS:
            self._fault(ErrorKind.OVERTEMP, "Sobretemperatura",
                        f"Sobretemperatura: {temp:.0f} °C acima do limite "
                        f"absoluto de {OVERTEMP_ABS:.0f} °C.")
            return True

        # 2. Sobretemperatura relativa ao alvo do modo ativo
        target = self._mode_target()
        if target is not None and temp > target + OVERTEMP_MARGIN:
            self._fault(ErrorKind.OVERTEMP, "Sobretemperatura",
                        f"Sobretemperatura: {temp:.0f} °C, alvo {target:.0f} °C "
                        f"(margem {OVERTEMP_MARGIN:.0f} °C).")
            return True

        # 3. Subindo com os dois bancos desligados (contator colado?)
        if s.bank_a or s.bank_b:
            s._runaway_ref = None
        else:
            if s._runaway_ref is None or temp < s._runaway_ref:
                s._runaway_ref = temp
            elif temp - s._runaway_ref > RUNAWAY_RISE:
                self._fault(ErrorKind.RUNAWAY, "Aquece desligado",
                            f"Temperatura subiu {temp - s._runaway_ref:.0f} °C "
                            f"com os relés desligados — verificar contatores.")
                return True

        # 4. Prazo do segmento / pré-aquecimento
        if s.segment_deadline is not None and now > s.segment_deadline:
            if s.mode == ControllerMode.PREHEAT:
                msg = f"Pré-aquecimento excedeu {PREHEAT_MAX_S / 3600:.0f} h."
            else:
                msg = (f"Segmento {s.segment_index + 1} excedeu o tempo máximo "
                       f"(temp {temp:.0f} °C).")
            self._fault(ErrorKind.TIMEOUT, "Tempo excedido", msg)
            return True

        return False

    def _check_stall(self, temp: float):
        """Aquecimento comandado, abaixo do setpoint, e a temperatura não sobe."""
        s = self.state
        demand = (s.mode in (ControllerMode.PREHEAT, ControllerMode.RAMPING,
                             ControllerMode.SOAKING)
                  and s.bank_a
                  and temp < s.setpoint - STALL_LAG)
        if not demand:
            s._stall_ref = None
            return

        now = time.monotonic()
        if s._stall_ref is None or temp >= s._stall_ref[1] + STALL_MIN_RISE:
            s._stall_ref = (now, temp)
            return

        if now - s._stall_ref[0] >= STALL_TIMEOUT_S:
            self._fault(ErrorKind.STALL, "Temp. não sobe",
                        f"Temperatura não subiu {STALL_MIN_RISE:.0f} °C em "
                        f"{STALL_TIMEOUT_S // 60:.0f} min com aquecimento ligado "
                        f"(temp {temp:.0f} °C, setpoint {s.setpoint:.0f} °C). "
                        f"Verificar resistências e termopar.")

    def _fault(self, kind: ErrorKind = ErrorKind.SENSOR,
               title: str = "Falha no termopar",
               message: str = "Falha no termopar detectada."):
        s = self.state
        s.mode   = ControllerMode.ERROR
        s.bank_a = False
        s.bank_b = False
        s.error_kind    = kind
        s.error_title   = title
        s.error_message = message
        _log(f"[ctrl] ERRO ({kind.name}): {message} Relés desligados.")

    # ── Helpers de consulta ────────────────────

    def soak_remaining_seconds(self) -> float:
        """Retorna segundos restantes no soak, ou 0 se não estiver em soak."""
        s = self.state
        if s.mode != ControllerMode.SOAKING or s.soak_start_time is None:
            return 0.0
        elapsed = time.monotonic() - s.soak_start_time
        return max(0.0, s.curve["soak_minutes"] * 60.0 - elapsed)

    def run_elapsed_seconds(self) -> float:
        """Retorna segundos desde o início da queima/pré-aquecimento."""
        s = self.state
        if s.run_start_time is None:
            return 0.0
        return time.time() - s.run_start_time

    def segment_progress(self) -> float:
        """Progresso geral da curva de 0.0 a 1.0."""
        s = self.state
        if s.curve is None:
            return 0.0
        total_segs = len(s.curve["segments"])
        seg_done   = s.segment_index / total_segs
        # Progresso dentro do segmento atual
        seg = s.curve["segments"][s.segment_index]
        span = float(seg["target_temp"]) - s.segment_start_temp
        if span > 0:
            inner = min(1.0, (s.setpoint - s.segment_start_temp) / span)
        else:
            inner = 1.0
        return seg_done + inner / total_segs
