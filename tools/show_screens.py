"""
tools/show_screens.py — Percorre todas as telas do display por 3s cada.
Caso a tela seja especificada na chamada, exibe apenas uma
Requer acesso ao /dev/fb1 (rode com sudo ou grupo video).
"""

import sys, os, time, json

screen = sys.argv[1].lower() if len(sys.argv) > 1 else None

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from display import Display
from controller import ControllerState, ControllerMode

CURVES_PATH = os.path.join(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."), "curves.json")
with open(CURVES_PATH) as f:
    curves = json.load(f)

DELAY = 3

with Display() as disp:

    if screen in (None, "idle"):
        print("IDLE")
        disp.show_idle(temp=1047.3, rtc_time="23:45:10")
        time.sleep(DELAY)

    if screen in (None, "menu"):
        print("MENU")
        disp.show_menu(["Curvas de Queima", "Pré-aquecimento", "Adicionar Curva", "Editar Curva"], selected=0)
        time.sleep(DELAY)

    if screen in (None, "select"):
        print("SELEÇÃO DE CURVA")
        disp.show_selecting(curves, selected=0)
        time.sleep(DELAY)

    if screen in (None, "confirm"):
        print("CONFIRMAÇÃO")
        disp.show_confirming(curves[0])
        time.sleep(DELAY)

    if screen in (None, "running"):
        print("RUNNING")
        state = ControllerState()
        state.mode = ControllerMode.RAMPING
        state.segment_index = 1
        state.setpoint = 851.0
        state.bank_a = True
        state.bank_b = True
        state.current_rate = 148.0
        state.bank_b_suppressed = False
        state.curve = curves[0]
        disp.show_running(state, curves[0], elapsed_s=4*3600+32*60+10, progress=0.68, measured_temp=847.5)
        time.sleep(DELAY)

    if screen in (None, "soaking"):
        print("SOAKING")
        state = ControllerState()
        state.mode = ControllerMode.SOAKING
        state.setpoint = 1220.0
        state.bank_a = True
        state.bank_b = True
        state.curve  = curves[0]
        disp.show_soaking(state, elapsed_s=5*3600+45*60, remaining_s=18*60, measured_temp=1218.4)
        time.sleep(DELAY)

    if screen in (None, "preheat"):
        print("PRÉ-AQUECIMENTO")
        disp.show_preheat(temp=187.0, target=350.0, bank_a=True)
        time.sleep(DELAY)

    if screen in (None, "finished"):
        print("FINISHED")
        disp.show_finished(elapsed_s=6*3600+32*60+45, max_temp=1223.0, start_time_str="19:12")
        time.sleep(DELAY)

    if screen in (None, "error"):
        print("ERROR")
        disp.show_error("Termopar desconectado")
        time.sleep(DELAY)


    segs_exemplo = [
        {"rate_per_hour": 100, "target_temp": 200},
        {"rate_per_hour": 150, "target_temp": 1100},
    ]

    if screen in (None, "edit_list"):
        print("EDITAR: lista de curvas")
        disp.show_curve_list_edit(curves, selected=0)
        time.sleep(DELAY)

    if screen in (None, "seg_rate"):
        print("SEGMENTO: campo rate ativo")
        disp.show_curve_segment(seg_index=0, seg_total=3, field=0, rate=150, target=1100, is_new=False)
        time.sleep(DELAY)

    if screen in (None, "seg_target"):
        print("SEGMENTO: campo target ativo")
        disp.show_curve_segment(seg_index=0, seg_total=3, field=1, rate=150, target=1100, is_new=True)
        time.sleep(DELAY)

    if screen in (None, "add_seg"):
        print("ENTRE SEGMENTOS")
        disp.show_curve_add_segment(segs_exemplo)
        time.sleep(DELAY)

    if screen in (None, "curve_soak"):
        print("PATAMAR")
        disp.show_curve_soak(minutes=30, is_new=True)
        time.sleep(DELAY)

    if screen in (None, "curve_save"):
        print("SALVAR CURVA")
        disp.show_curve_save("Curva 3", segs_exemplo + [{"rate_per_hour": 60, "target_temp": 1220}], soak_min=30)
        time.sleep(DELAY)

    if screen in (None, "curve_discard"):
        print("DESCARTAR EDIÇÃO")
        disp.show_discard_confirm(is_new=False)
        time.sleep(DELAY)


print("Teste concluído.")