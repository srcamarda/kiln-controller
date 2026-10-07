"""
tools/simulate_curves.py — Simula o controlador passo a passo com tempo mockado.
Cada passo representa 5 segundos de forno (o mesmo período do loop real).
"""

import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# ── Mock de tempo ──────────────────────────────────────────────────────────────
_sim_time = 0.0

def _mock_monotonic():
    return _sim_time

def _mock_time():
    return 1_700_000_000 + _sim_time

time.monotonic = _mock_monotonic
time.time      = _mock_time

# ── Imports após mock ──────────────────────────────────────────────────────────
from controller import KilnController, ControllerMode

# ── Simulador de temperatura simples ──────────────────────────────────────────

class StepSensor:
    # Ganhos realistas (~3,6 °C/min só A, ~7,2 °C/min A+B). Com os ganhos
    # antigos (150 °C por passo de 60 s) a sobretemperatura disparava.
    GAIN_A_ONLY  = 0.06  # °C/s
    GAIN_A_AND_B = 0.12  # °C/s
    LOSS         = 0.02  # °C/s resfriamento passivo
    AMBIENT      = 25.0

    def __init__(self):
        self._temp = self.AMBIENT
        self.fault = False

    def step(self, bank_a: bool, bank_b: bool, dt: float):
        if bank_a and bank_b:
            self._temp += self.GAIN_A_AND_B * dt
        elif bank_a:
            self._temp += self.GAIN_A_ONLY * dt
        else:
            self._temp = max(self.AMBIENT, self._temp - self.LOSS * dt)

    @property
    def temperature(self):
        return round(self._temp, 1)


# ── Carrega curvas ─────────────────────────────────────────────────────────────
with open(os.path.join(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."), "curves.json")) as f:
    curves = json.load(f)

# ── Executa simulação ──────────────────────────────────────────────────────────

def run_simulation(curve, step_seconds=5, max_steps=30 * 720):
    global _sim_time
    _sim_time = 0.0

    sensor     = StepSensor()
    controller = KilnController()
    controller.start_curve(curve, start_temp=sensor.temperature)

    print(f"\n{'='*75}")
    print(f"Curva: {curve['name']}  |  Passo: {step_seconds}s  |  Máx: {max_steps} passos")
    print(f"{'='*75}")
    print(f"{'Tempo':>8}  {'Temp':>8}  {'Setpoint':>9}  "
          f"{'A':>4}  {'B':>4}  {'Rate/h':>8}  {'Modo'}")
    print("-" * 75)

    prev_mode = None

    for step in range(max_steps):
        state = controller.update(sensor.temperature, sensor.fault)
        sensor.step(state.bank_a, state.bank_b, dt=step_seconds)
        _sim_time += step_seconds

        if step % max(1, 900 // step_seconds) == 0 or state.mode != prev_mode:
            elapsed_h = _sim_time / 3600
            soak_r    = controller.soak_remaining_seconds()
            mode_str  = state.mode.name
            if state.mode == ControllerMode.SOAKING:
                mode_str += f" {soak_r/60:.0f}min restam"
            if state.bank_b_suppressed:
                mode_str += " [B suprimido]"

            print(f"{elapsed_h:>7.2f}h  {sensor.temperature:>7.1f}°C  "
                  f"{state.setpoint:>8.1f}°C  "
                  f"{'ON' if state.bank_a else '--':>4}  "
                  f"{'ON' if state.bank_b else '--':>4}  "
                  f"{state.current_rate:>7.0f}  "
                  f"{mode_str}")

        prev_mode = state.mode

        if state.mode in (ControllerMode.FINISHED, ControllerMode.ERROR):
            print(f"\n>>> Encerrado: {state.mode.name}  "
                  f"Tempo total: {_sim_time/3600:.2f}h  "
                  f"Temp final: {sensor.temperature:.1f}°C")
            break
    else:
        print(f"\n>>> Simulação atingiu {max_steps} passos sem encerrar.")


for curve in curves:
    run_simulation(curve)
    print()
