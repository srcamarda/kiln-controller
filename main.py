"""
main.py — Loop principal e máquina de estados do controlador de forno.

Estados da UI:
  IDLE            → tela inicial, mostra temperatura e hora
  MENU            → opções: Curvas / Pré-aquecimento / Nova curva / Editar curva
  SELECTING       → escolhe curva para queimar
  CONFIRMING      → confirma início da queima
  RUNNING         → queima em andamento (RAMPING ou SOAKING)
  PREHEAT_SET     → define temperatura do pré-aquecimento
  PREHEAT_RUN     → pré-aquecimento em andamento
  FINISHED        → resumo pós-queima
  ERROR           → falha crítica
  CURVE_NEW_SEG   → adicionando segmento a nova curva
  CURVE_ADD_MORE  → pergunta se adiciona mais segmento
  CURVE_SOAK      → define patamar da nova/editada curva
  CURVE_SAVE      → confirma salvar curva
  CURVE_EDIT_LIST → seleciona curva para editar
  CURVE_EDIT_SEG  → edita segmento existente
"""

import time
import signal
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from notifier      import TelegramNotifier
from sensor        import create_sensor
from controller    import KilnController, ControllerMode
from display       import Display
from encoder       import create_encoder, EncoderEvent
from relays        import create_relays
from rtc           import create_rtc
from curves_manager import (load_curves, save_curves,
                             next_curve_name, new_curve_template,
                             validate_curve)

# ──────────────────────────────────────────────
# Constantes
# ──────────────────────────────────────────────

LOOP_HZ         = 0.2    # ciclos por segundo do loop de controle
DISPLAY_HZ      = 2      # atualizações de display por segundo
PREHEAT_MIN     = 100
PREHEAT_MAX     = 600
PREHEAT_STEP    = 10
RATE_STEP       = 10     # °C/h por tick do encoder
TARGET_STEP     = 10     # °C por tick do encoder
SOAK_STEP       = 5      # minutos por tick do encoder
RATE_MIN        = 10
RATE_MAX        = 300
TARGET_MIN      = 100
TARGET_MAX      = 1270

MENU_OPTIONS = [
    "Curvas de Queima",
    "Pré-aquecimento",
    "Nova Curva",
    "Editar Curva",
]


# ──────────────────────────────────────────────
# Estado da UI
# ──────────────────────────────────────────────

class UIState:
    IDLE           = "idle"
    MENU           = "menu"
    SELECTING      = "selecting"
    CONFIRMING     = "confirming"
    RUNNING        = "running"
    PREHEAT_SET    = "preheat_set"
    PREHEAT_RUN    = "preheat_run"
    FINISHED       = "finished"
    ERROR          = "error"
    CURVE_NEW_SEG  = "curve_new_seg"
    CURVE_ADD_MORE = "curve_add_more"
    CURVE_SOAK     = "curve_soak"
    CURVE_SAVE     = "curve_save"
    CURVE_EDIT_LIST= "curve_edit_list"
    CURVE_EDIT_SEG = "curve_edit_seg"


# ──────────────────────────────────────────────
# Aplicação principal
# ──────────────────────────────────────────────

class KilnApp:

    def __init__(self):
        self.sensor     = create_sensor()
        self.controller = KilnController()
        self.display    = Display()
        self.encoder    = create_encoder()
        self.relays     = create_relays()
        self.rtc        = create_rtc()
        self.curves     = load_curves()
        
        # Controle de notificações de hora em hora
        self.last_hourly_announcement = 0
        
        # Configuração do Telegram        
        TOKEN = "SEU_TOKEN_AQUI"
        CHAT_IDS = ["SEU_CHAT_ID"]
        self.notifier = TelegramNotifier(TOKEN, CHAT_IDS, status_callback=self._generate_status)

        # Estado da UI
        self.ui         = UIState.IDLE
        self.menu_sel   = 0
        self.curve_sel  = 0
        self.edit_sel   = 0

        # Pré-aquecimento
        self.preheat_target = 200

        # Edição de curva
        self.edit_curve     = None   # curva sendo criada/editada
        self.edit_seg_idx   = 0      # índice do segmento em edição
        self.edit_field     = 0      # 0=rate, 1=target
        self.edit_rate      = 100
        self.edit_target    = 500
        self.edit_soak      = 30
        self.editing_existing = False  # True = edição, False = nova

        # Resumo da última queima
        self.run_max_temp   = 0.0
        self.run_start_str  = ""

        # Controle de tempo
        self._last_control  = 0.0
        self._last_display  = 0.0

        # Shutdown limpo
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT,  self._shutdown)

    def run(self):
        self.display.open()
        print("\r[[main] Iniciando kiln controller.")
        try:
            while True:
                now = time.monotonic()

                # ── Leitura do encoder ─────────────────────────
                for event in self.encoder.poll():
                    self._handle_event(event)

                # ── Ciclo de controle ──────────────────────────
                if now - self._last_control >= 1.0 / LOOP_HZ:
                    self._control_tick()
                    self._last_control = now

                # ── Atualização do display ─────────────────────
                if now - self._last_display >= 1.0 / DISPLAY_HZ:
                    self._update_display()
                    self._last_display = now

                time.sleep(0.02)  # 50Hz de polling do encoder

        except KeyboardInterrupt:
            pass
        finally:
            self._emergency_stop()
            self.display.close()
            self.encoder.cleanup()
            self.relays.cleanup()
            print("\r\n[main] Encerrado.")

    # ──────────────────────────────────────────────
    # Ciclo de controle
    # ──────────────────────────────────────────────

    def _generate_status(self, is_hourly=False) -> str:
        """Gera o texto de status atual para o Telegram."""
        temp = self.sensor.temperature
        state = self.controller.state
        
        prefix = "⏱ *Atualização de Hora em Hora*\n" if is_hourly else "📊 *Status do Forno*\n"
            
        if self.ui == UIState.RUNNING:
            if state.mode == ControllerMode.SOAKING:
                rem_m = int(self.controller.soak_remaining_seconds() // 60)
                return f"{prefix}Modo: Patamar (Soak)\nTemp: {temp:.1f}°C (Alvo: {state.setpoint:.0f}°C)\nRestante: {rem_m} min"
            else:
                seg = state.curve["segments"][state.segment_index]
                return f"{prefix}Curva: {state.curve['name']} (Seg {state.segment_index+1}/{len(state.curve['segments'])})\nTemp: {temp:.1f}°C (Alvo: {seg['target_temp']}°C)\nTaxa: {state.display_rate:+.0f}°C/h"
                
        if self.ui == UIState.PREHEAT_RUN:
            return f"{prefix}Modo: Pré-aquecimento\nTemp: {temp:.1f}°C (Alvo: {self.preheat_target:.0f}°C)"
            
        if self.ui == UIState.FINISHED:
            return f"{prefix}A última queima foi concluída.\nTemp. Atual (Resfriando): {temp:.1f}°C"
            
        return f"{prefix}Estado da UI: {self.ui.upper()}\nTemp: {temp:.1f}°C"

    def _control_tick(self):
        temp  = self.sensor.temperature
        fault = self.sensor.fault

        # Registra temperatura máxima durante queima
        if self.ui == UIState.RUNNING:
            if temp > self.run_max_temp:
                self.run_max_temp = temp

        state = self.controller.update(temp, fault)
        self.sensor.update(state.bank_a, state.bank_b)
        self.relays.set(state.bank_a, state.bank_b)

        # Transições automáticas do controlador
        if state.mode == ControllerMode.FINISHED and self.ui == UIState.RUNNING:
            self.ui = UIState.FINISHED
            self.relays.off()
            # Alerta de conclusão
            self.notifier.send(f"✅ *Queima Concluída!*\nCurva finalizada com sucesso.\nTemp. Máx atingida: {self.run_max_temp:.1f}°C")

        if state.mode == ControllerMode.ERROR and self.ui not in (UIState.ERROR, UIState.IDLE):
            self.ui = UIState.ERROR
            self.relays.off()
            # Alerta de erro
            self.notifier.send(f"🚨 *ERRO CRÍTICO!*\nFalha no termopar detectada.\nOs relés foram desligados por segurança na temperatura de {temp:.1f}°C.")

        # Aviso automático de hora em hora
        if self.ui in (UIState.RUNNING, UIState.PREHEAT_RUN):
            elapsed_s = self.controller.run_elapsed_seconds()
            hours_elapsed = int(elapsed_s // 3600)
            
            if hours_elapsed > self.last_hourly_announcement and hours_elapsed > 0:
                self.last_hourly_announcement = hours_elapsed
                status_msg = self._generate_status(is_hourly=True)
                self.notifier.send(status_msg)                
                
    # ──────────────────────────────────────────────
    # Atualização do display
    # ──────────────────────────────────────────────

    def _update_display(self):
        temp  = self.sensor.temperature
        state = self.controller.state
        now_str = self.rtc.now().strftime("%H:%M:%S")

        ui = self.ui

        if ui == UIState.IDLE:
            self.display.show_idle(temp, now_str)

        elif ui == UIState.MENU:
            self.display.show_menu(MENU_OPTIONS, self.menu_sel)

        elif ui == UIState.SELECTING:
            self.display.show_selecting(self.curves, self.curve_sel)

        elif ui == UIState.CONFIRMING:
            self.display.show_confirming(self.curves[self.curve_sel])

        elif ui == UIState.RUNNING:
            ctrl_mode = state.mode
            if ctrl_mode == ControllerMode.SOAKING:
                self.display.show_soaking(
                    state,
                    elapsed_s   = self.controller.run_elapsed_seconds(),
                    remaining_s = self.controller.soak_remaining_seconds(),
                    measured_temp = temp,
                )
            else:
                self.display.show_running(
                    state,
                    curve       = state.curve,
                    elapsed_s   = self.controller.run_elapsed_seconds(),
                    progress    = self.controller.segment_progress(),
                    measured_temp = temp,
                )

        elif ui == UIState.PREHEAT_SET:
            self.display.show_preheat(temp, self.preheat_target, bank_a=False)

        elif ui == UIState.PREHEAT_RUN:
            self.display.show_preheat(temp, self.preheat_target, bank_a=state.bank_a)

        elif ui == UIState.FINISHED:
            self.display.show_finished(
                elapsed_s     = self.controller.run_elapsed_seconds(),
                max_temp      = self.run_max_temp,
                start_time_str= self.run_start_str,
            )

        elif ui == UIState.ERROR:
            self.display.show_error()

        elif ui == UIState.CURVE_NEW_SEG:
            total = len(self.edit_curve["segments"]) + 1
            self.display.show_curve_segment(
                seg_index = self.edit_seg_idx,
                seg_total = total,
                field     = self.edit_field,
                rate      = self.edit_rate,
                target    = self.edit_target,
                is_new    = not self.editing_existing,
            )

        elif ui == UIState.CURVE_EDIT_SEG:
            total = len(self.edit_curve["segments"])
            self.display.show_curve_segment(
                seg_index = self.edit_seg_idx,
                seg_total = total,
                field     = self.edit_field,
                rate      = self.edit_rate,
                target    = self.edit_target,
                is_new    = False,
            )

        elif ui == UIState.CURVE_ADD_MORE:
            self.display.show_curve_add_segment(self.edit_curve["segments"])

        elif ui == UIState.CURVE_SOAK:
            self.display.show_curve_soak(self.edit_soak,
                                         is_new=not self.editing_existing)

        elif ui == UIState.CURVE_SAVE:
            self.display.show_curve_save(
                self.edit_curve["name"],
                self.edit_curve["segments"],
                self.edit_curve["soak_minutes"],
            )

        elif ui == UIState.CURVE_EDIT_LIST:
            self.display.show_curve_list_edit(self.curves, self.edit_sel)

    # ──────────────────────────────────────────────
    # Eventos do encoder
    # ──────────────────────────────────────────────

    def _handle_event(self, event: EncoderEvent):
        ui = self.ui
        cw = (event == EncoderEvent.ROTATE_CW)
        ccw = (event == EncoderEvent.ROTATE_CCW)
        click = (event == EncoderEvent.CLICK)
        long_ = (event == EncoderEvent.LONG_PRESS)
        delta = 1 if cw else (-1 if ccw else 0)

        # ── IDLE ──────────────────────────────────
        if ui == UIState.IDLE:
            if click:
                self.ui = UIState.MENU
                self.menu_sel = 0

        # ── MENU ──────────────────────────────────
        elif ui == UIState.MENU:
            if delta:
                self.menu_sel = (self.menu_sel + delta) % len(MENU_OPTIONS)
            elif click:
                self._menu_confirm()
            elif long_:
                self.ui = UIState.IDLE

        # ── SELECTING ─────────────────────────────
        elif ui == UIState.SELECTING:
            if delta:
                self.curve_sel = (self.curve_sel + delta) % len(self.curves)
            elif click:
                self.ui = UIState.CONFIRMING
            elif long_:
                self.ui = UIState.MENU

        # ── CONFIRMING ────────────────────────────
        elif ui == UIState.CONFIRMING:
            if click:
                self._start_curve()
            elif long_:
                self.ui = UIState.SELECTING

        # ── RUNNING ───────────────────────────────
        elif ui == UIState.RUNNING:
            if long_:
                self._stop_run()

        # ── PREHEAT SET ───────────────────────────
        elif ui == UIState.PREHEAT_SET:
            if delta:
                self.preheat_target = max(PREHEAT_MIN,
                    min(PREHEAT_MAX, self.preheat_target + delta * PREHEAT_STEP))
            elif click:
                self._start_preheat()
            elif long_:
                self.ui = UIState.MENU

        # ── PREHEAT RUN ───────────────────────────
        elif ui == UIState.PREHEAT_RUN:
            if delta:
                # Permite ajustar alvo durante o aquecimento
                self.preheat_target = max(PREHEAT_MIN,
                    min(PREHEAT_MAX, self.preheat_target + delta * PREHEAT_STEP))
                self.controller.set_preheat_target(self.preheat_target)
            elif long_:
                self._stop_run()

        # ── FINISHED ──────────────────────────────
        elif ui == UIState.FINISHED:
            if click or long_:
                self.ui = UIState.IDLE

        # ── ERROR ─────────────────────────────────
        elif ui == UIState.ERROR:
            if click or long_:
                self.controller.stop()
                self.ui = UIState.IDLE

        # ── NOVA CURVA: SEGMENTO ──────────────────
        elif ui in (UIState.CURVE_NEW_SEG, UIState.CURVE_EDIT_SEG):
            if delta:
                if self.edit_field == 0:
                    self.edit_rate = max(RATE_MIN,
                        min(RATE_MAX, self.edit_rate + delta * RATE_STEP))
                else:
                    # Target deve ser maior que o segmento anterior
                    # Em edição, exclui o segmento atual do cálculo
                    min_target = TARGET_MIN
                    segs = self.edit_curve["segments"]
                    if self.ui == UIState.CURVE_EDIT_SEG:
                        prev = segs[:self.edit_seg_idx]
                    else:
                        prev = segs
                    if prev:
                        min_target = prev[-1]["target_temp"] + TARGET_STEP
                    self.edit_target = max(min_target,
                        min(TARGET_MAX, self.edit_target + delta * TARGET_STEP))
            elif click:
                if self.edit_field == 0:
                    # Avança para campo target
                    self.edit_field = 1
                else:
                    # Confirma segmento
                    self._confirm_segment()
            elif long_:
                self._cancel_edit()

        # ── NOVA CURVA: ADICIONAR MAIS ────────────
        elif ui == UIState.CURVE_ADD_MORE:
            if click:
                self._begin_new_segment()
            elif long_:
                self.ui = UIState.CURVE_SOAK

        # ── NOVA CURVA: PATAMAR ───────────────────
        elif ui == UIState.CURVE_SOAK:
            if delta:
                self.edit_soak = max(1,
                    min(240, self.edit_soak + delta * SOAK_STEP))
            elif click:
                self.edit_curve["soak_minutes"] = self.edit_soak
                self.ui = UIState.CURVE_SAVE
            elif long_:
                self._cancel_edit()

        # ── NOVA CURVA: SALVAR ────────────────────
        elif ui == UIState.CURVE_SAVE:
            if click:
                self._save_curve()
            elif long_:
                self._cancel_edit()

        # ── EDITAR: LISTA ─────────────────────────
        elif ui == UIState.CURVE_EDIT_LIST:
            if delta:
                self.edit_sel = (self.edit_sel + delta) % len(self.curves)
            elif click:
                self._begin_edit_curve()
            elif long_:
                self.ui = UIState.MENU

    # ──────────────────────────────────────────────
    # Ações
    # ──────────────────────────────────────────────

    def _menu_confirm(self):
        sel = self.menu_sel
        if sel == 0:   # Curvas de queima
            self.curve_sel = 0
            self.ui = UIState.SELECTING
        elif sel == 1: # Pré-aquecimento
            self.ui = UIState.PREHEAT_SET
        elif sel == 2: # Nova curva
            self._begin_new_curve()
        elif sel == 3: # Editar curva
            self.edit_sel = 0
            self.ui = UIState.CURVE_EDIT_LIST

    def _start_curve(self):
        curve = self.curves[self.curve_sel]
        self.run_max_temp  = self.sensor.temperature
        self.run_start_str = self.rtc.now().strftime("%H:%M")
        self.controller.start_curve(curve, self.sensor.temperature)
        self.ui = UIState.RUNNING
        self.last_hourly_announcement = 0
        
        # Envia alerta do Telegram
        self.notifier.send(f"🔥 *Queima Iniciada*\nCurva: {curve['name']}\nTemp. Atual: {self.sensor.temperature:.1f}°C")

    def _start_preheat(self):
        self.controller.start_preheat(self.preheat_target, self.sensor.temperature)
        self.ui = UIState.PREHEAT_RUN
        self.last_hourly_announcement = 0

    def _stop_run(self):
        self.controller.stop()
        self.relays.off()
        self.ui = UIState.IDLE

    def _begin_new_curve(self):
        name = next_curve_name(self.curves)
        self.edit_curve       = new_curve_template(name)
        self.edit_seg_idx     = 0
        self.edit_field       = 0
        self.edit_rate        = 100
        self.edit_target      = 500
        self.edit_soak        = 30
        self.editing_existing = False
        self.ui = UIState.CURVE_NEW_SEG

    def _begin_edit_curve(self):
        import copy
        self.edit_curve       = copy.deepcopy(self.curves[self.edit_sel])
        self.edit_seg_idx     = 0
        self.edit_field       = 0
        self.editing_existing = True
        # Pré-carrega valores do primeiro segmento
        seg = self.edit_curve["segments"][0]
        self.edit_rate   = seg["rate_per_hour"]
        self.edit_target = seg["target_temp"]
        self.edit_soak   = self.edit_curve["soak_minutes"]
        self.ui = UIState.CURVE_EDIT_SEG

    def _begin_new_segment(self):
        self.edit_seg_idx = len(self.edit_curve["segments"])
        self.edit_field   = 0
        self.edit_rate    = 100
        # Sugere temperatura acima do último segmento
        if self.edit_curve["segments"]:
            last = self.edit_curve["segments"][-1]["target_temp"]
            self.edit_target = min(TARGET_MAX, last + 100)
        else:
            self.edit_target = 500
        self.ui = UIState.CURVE_NEW_SEG

    def _confirm_segment(self):
        seg = {
            "rate_per_hour": self.edit_rate,
            "target_temp":   self.edit_target,
        }
        if self.ui == UIState.CURVE_EDIT_SEG:
            # Atualiza segmento existente
            self.edit_curve["segments"][self.edit_seg_idx] = seg
            # Avança para próximo segmento ou patamar
            next_idx = self.edit_seg_idx + 1
            if next_idx < len(self.edit_curve["segments"]):
                self.edit_seg_idx = next_idx
                next_seg = self.edit_curve["segments"][next_idx]
                self.edit_rate   = next_seg["rate_per_hour"]
                self.edit_target = next_seg["target_temp"]
                self.edit_field  = 0
                # Permanece em CURVE_EDIT_SEG
            else:
                self.ui = UIState.CURVE_SOAK
        else:
            # Nova curva — adiciona segmento e pergunta se adiciona mais
            self.edit_curve["segments"].append(seg)
            self.ui = UIState.CURVE_ADD_MORE

    def _save_curve(self):
        err = validate_curve(self.edit_curve)
        if err:
            print(f"\r[[main] Curva inválida: {err}")
            return

        if self.editing_existing:
            # Substitui curva original
            for i, c in enumerate(self.curves):
                if c["id"] == self.edit_curve["id"]:
                    self.curves[i] = self.edit_curve
                    break
        else:
            self.curves.append(self.edit_curve)

        save_curves(self.curves)
        print(f"\r[main] Curva '{self.edit_curve['name']}' salva.")
        self.edit_curve = None
        self.ui = UIState.IDLE

    def _cancel_edit(self):
        self.edit_curve = None
        self.ui = UIState.MENU

    def _emergency_stop(self):
        self.relays.off()
        self.controller.stop()
        print("\r[main] Emergency stop — relés desligados.")

    def _shutdown(self, *_):
        print("\r[main] Sinal de shutdown recebido.")
        raise KeyboardInterrupt


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    app = KilnApp()
    app.run()
