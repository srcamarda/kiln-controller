"""
controller.py — Lógica de controle do forno.

Responsabilidades:
  - Calcular o setpoint dinâmico conforme o segmento ativo da curva
  - Controlar os bancos A e B via bang-bang com histerese
  - Detectar overshoot de rate e desligar banco B preventivamente
  - Gerenciar progressão entre segmentos e transição para soak
  - Modo pré-aquecimento (banco A, setpoint fixo)
"""

import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# ──────────────────────────────────────────────
# Configurações
# ──────────────────────────────────────────────

HYSTERESIS      = 5.0    # °C — margem bang-bang (liga abaixo, desliga acima)
DUAL_BANK_TEMP  = 600.0  # °C — abaixo disso só banco A
RATE_TOLERANCE  = 1.20   # 20% acima do rate alvo → desliga banco B
RATE_WINDOW     = 30.0   # segundos para calcular o rate atual
PREHEAT_MIN     = 100    # °C — mínimo do modo pré-aquecimento
PREHEAT_MAX     = 600    # °C — máximo do modo pré-aquecimento
PREHEAT_STEP    = 10     # °C — passo do encoder no pré-aquecimento


# ──────────────────────────────────────────────
# Estado do controlador
# ──────────────────────────────────────────────

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
        print(f"[ctrl] Iniciando curva '{curve['name']}' em {start_temp:.1f}°C")

    def start_preheat(self, target: float, current_temp: float):
        """Inicia modo pré-aquecimento com alvo fixo."""
        target = max(PREHEAT_MIN, min(PREHEAT_MAX, target))
        self.state.preheat_target = target
        self.state.mode           = ControllerMode.PREHEAT
        self.state.run_start_time = time.time()
        print(f"[ctrl] Pré-aquecimento → {target:.0f}°C")

    def stop(self):
        """Para tudo e desliga os relés."""
        s = self.state
        s.mode   = ControllerMode.OFF
        s.bank_a = False
        s.bank_b = False
        print("[ctrl] Parado.")

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
            self._fault()
            return s

        self._update_rate(temp)

        if s.mode == ControllerMode.OFF:
            s.bank_a = False
            s.bank_b = False

        elif s.mode == ControllerMode.PREHEAT:
            self._run_preheat(temp)

        elif s.mode == ControllerMode.RAMPING:
            self._run_ramping(temp)

        elif s.mode == ControllerMode.SOAKING:
            self._run_soaking(temp)

        elif s.mode in (ControllerMode.FINISHED, ControllerMode.ERROR):
            s.bank_a = False
            s.bank_b = False

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
                print(f"[ctrl] Segmento {s.segment_index+1} completo → "
                      f"segmento {next_index+1}")
                s.segment_index      = next_index
                s.segment_start_temp = temp
                s.segment_start_time = time.monotonic()
            else:
                # Todos os segmentos concluídos → inicia soak
                print("[ctrl] Segmentos completos → iniciando soak")
                s.mode            = ControllerMode.SOAKING
                s.soak_start_time = time.monotonic()

        self._apply_banks(temp, s.setpoint)

    def _run_soaking(self, temp: float):
        s = self.state
        seg_final = s.curve["segments"][-1]
        s.setpoint = float(seg_final["target_temp"])

        elapsed_min = (time.monotonic() - s.soak_start_time) / 60.0
        remaining   = s.curve["soak_minutes"] - elapsed_min

        if remaining <= 0:
            print("[ctrl] Soak concluído — finalizando queima.")
            s.mode   = ControllerMode.FINISHED
            s.bank_a = False
            s.bank_b = False
            return

        self._apply_banks(temp, s.setpoint)

    # ── Controle de bancos ─────────────────────

    def _apply_banks(self, temp: float, setpoint: float):
        """
        Decide quais bancos ligar considerando:
          - Temperatura atual (abaixo de DUAL_BANK_TEMP → só A)
          - Rate atual vs rate alvo (suprime B se subindo rápido demais)
        """
        s = self.state

        if temp < DUAL_BANK_TEMP:
            # Abaixo de 600°C: só banco A
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

    DISPLAY_RATE_SAMPLES = 10  # média móvel de N amostras para exibição

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

    def _fault(self):
        s = self.state
        s.mode   = ControllerMode.ERROR
        s.bank_a = False
        s.bank_b = False
        print("[ctrl] ERRO: falha no termopar — relés desligados.")

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
