"""
test_display.py — Percorre todas as telas do display por 3s cada.
Caso a tela seja especificada na chamada, exibe apenas uma
Requer acesso ao /dev/fb1 (rode com sudo ou grupo video).
"""

import sys, os, time, json

screen = sys.argv[1].lower() if len(sys.argv) > 1 else None

sys.path.insert(0, os.path.dirname(__file__))

from display import Display
from controller import ControllerState, ControllerMode

CURVES_PATH = os.path.join(os.path.dirname(__file__), "curves.json")
with open(CURVES_PATH) as f:
    curves = json.load(f)

DELAY = 3  # segundos por tela

with Display() as disp:

    if screen in (None, "idle"):
        print("1/8 — IDLE")
        disp.show_idle(temp=247.3, rtc_time="23:45:10")
        time.sleep(DELAY)

    if screen in (None, "menu"):
        print("2/8 — MENU")
        disp.show_menu(["Curvas de Queima", "Pré-aquecimento"], selected=0)
        time.sleep(DELAY)

    if screen in (None, "select"):
        print("3/8 — SELEÇÃO DE CURVA")
        disp.show_selecting(curves, selected=0)
        time.sleep(DELAY)

    if screen in (None, "confirm"):
        print("4/8 — CONFIRMAÇÃO")
        disp.show_confirming(curves[0]["name"])
        time.sleep(DELAY)

    if screen in (None, "running"):
        print("5/8 — RUNNING (RAMPING)")
        state = ControllerState()
        state.mode          = ControllerMode.RAMPING
        state.segment_index = 1
        state.setpoint      = 851.0
        state.bank_a        = True
        state.bank_b        = True
        state.current_rate  = 148.0
        state.bank_b_suppressed = False
        state.curve         = curves[0]
        disp.show_running(state, curves[0], elapsed_s=4*3600+32*60+10, progress=0.68, measured_temp=847.5)
        time.sleep(DELAY)

    if screen in (None, "soaking"):
        print("6/8 — SOAKING")
        state.mode = ControllerMode.SOAKING
        state.setpoint = 1220.0
        state.bank_a = True
        state.bank_b = False
        disp.show_soaking(state, elapsed_s=5*3600+45*60, remaining_s=18*60, measured_temp=1218.4)
        time.sleep(DELAY)

    if screen in (None, "preheat"):
        print("7/8 — PRÉ-AQUECIMENTO")
        disp.show_preheat(temp=187.0, target=350.0, bank_a=True)
        time.sleep(DELAY)

    if screen in (None, "finished"):
        print("8/8 — FINISHED")
        disp.show_finished(elapsed_s=6*3600+32*60+45, max_temp=1223.0, start_time_str="19:12")
        time.sleep(DELAY)

    if screen in (None, "error"):
        print("Extra — ERROR")
        disp.show_error("Termopar desconectado")
        time.sleep(DELAY)

print("Teste concluído.")