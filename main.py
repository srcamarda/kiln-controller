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
  MESSAGE         → aviso (curva inválida, sem curvas...) — click volta
"""

import time
import signal
import queue
import threading
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

# relays PRIMEIRO: registra o handler que força os pinos dos relés em HIGH na
# saída antes de qualquer módulo importar o gpiozero (ver relays.py).
from relays        import create_relays, HardwareRelays

from config        import (load_env_file, telegram_config, telegram_admin_ids,
                            heartbeat_config)
from heartbeat     import Heartbeat, describe as hb_describe
import firing_state
import controller as ctrl_cfg
from firing_log import FiringLog
import sdnotify
from notifier      import TelegramNotifier, esc, b
from sensor        import create_sensor, SensorInitError, SimulatedSensor
from controller    import (KilnController, ControllerMode, ErrorKind,
                            PREHEAT_MIN, PREHEAT_MAX, PREHEAT_STEP,
                            RUNAWAY_RISE, OVERTEMP_ABS)
from display       import Display
from encoder       import create_encoder, EncoderEvent
from rtc           import create_rtc
from curves_manager import (load_curves, save_curves,
                             next_curve_name, new_curve_template,
                             validate_curve,
                             RATE_MIN, RATE_MAX, TARGET_MIN, TARGET_MAX,
                             SOAK_MIN, SOAK_MAX)

# ──────────────────────────────────────────────
# Constantes
# ──────────────────────────────────────────────

LOOP_HZ         = 0.2    # ciclos por segundo do loop de controle
DISPLAY_HZ      = 2      # atualizações de display por segundo
# PREHEAT_MIN/MAX/STEP vêm de controller (fonte única)
RATE_STEP       = 10     # °C/h por tick do encoder
TARGET_STEP     = 10     # °C por tick do encoder
SOAK_STEP       = 5      # minutos por tick do encoder
# RATE_MIN/MAX, TARGET_MIN/MAX e SOAK_MIN/MAX vêm de curves_manager
SENSOR_RETRY_S  = 5      # intervalo entre tentativas de detectar o termopar
REMOTE_TIMEOUT_S = 10    # espera máxima pela execução de um comando remoto no loop

# Retomada da queima após reinício (queda de energia, travamento, watchdog)
STATE_SAVE_S            = 60        # grava o estado da queima a cada N s
MAX_RESUME_DOWNTIME_S   = 4 * 3600  # fora do ar por mais que isso → não retoma
MAX_RESUMES             = 3         # retomadas permitidas dentro da janela abaixo
RESUME_WINDOW_S         = 3600      #   (mais que isso indica problema recorrente)

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
    CANCEL_CONFIRM = "cancel_confirm"
    FINISHED       = "finished"
    ERROR          = "error"
    CURVE_NEW_SEG  = "curve_new_seg"
    CURVE_ADD_MORE = "curve_add_more"
    CURVE_SOAK     = "curve_soak"
    CURVE_SAVE     = "curve_save"
    CURVE_EDIT_LIST= "curve_edit_list"
    CURVE_EDIT_SEG = "curve_edit_seg"
    MESSAGE        = "message"


# ──────────────────────────────────────────────
# Aplicação principal
# ──────────────────────────────────────────────

class KilnApp:

    def __init__(self):
        # Relés primeiro: garante saídas em "desligado" antes de qualquer coisa
        self.relays     = create_relays()
        self.relays.off()
        # O sensor só é obtido em run() (_acquire_sensor), com retry e
        # tela de erro — sem termopar válido o forno fica bloqueado.
        self.sensor     = None
        self.controller = KilnController()
        self.display    = Display()
        self.encoder    = create_encoder()
        self.rtc        = create_rtc()
        self.curves, self._curve_warnings = load_curves()
        for w in self._curve_warnings:
            print(f"\r[curvas] {w}")
        
        # Controle de notificações de hora em hora
        self.last_hourly_announcement = 0
        
        # Configuração do Telegram — segredos vêm do ambiente / .env
        # (ver config.py e .env.example; nunca colocar no código)
        # Comandos remotos (Telegram) são executados pelo loop principal,
        # nunca direto da thread do Telegram (ver _call_in_main)
        self._commands    = queue.Queue()
        self._main_thread = threading.get_ident()

        load_env_file()
        token, chat_ids, stop_password = telegram_config()

        self.notifier = TelegramNotifier(
            token,
            chat_ids,
            status_callback=self._remote_status,
            stop_callback=self._remote_stop_request if stop_password else None,
            stop_password=stop_password,
            admin_ids=telegram_admin_ids(),
        )

        # Sinal de vida externo (healthchecks.io) durante as queimas
        self.hb = Heartbeat(*heartbeat_config())
        if self._curve_warnings:
            self.notifier.send("⚠️ " + b("Problema no arquivo de curvas:") + "\n" +
                               "\n".join(esc(w) for w in self._curve_warnings))

        # Estado da UI
        self.ui         = UIState.IDLE
        self.msg_title  = ""
        self.msg_text   = ""
        self.msg_return = UIState.MENU
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
        
        self.confirm_scroll = 0
        self.save_scroll    = 0

        # Resumo da última queima
        self.run_final_duration = 0.0
        self.run_max_temp       = 0.0
        self.run_start_str      = ""

        # Controle de tempo
        self._last_control  = 0.0
        self._last_display  = 0.0

        # Registro completo das queimas (logs/ ou /var/log/kiln-controller)
        self.flog = FiringLog()

        # Persistência da queima (retomada após reinício)
        self._last_save     = 0.0
        self._saved_key     = None    # (modo, segmento) da última gravação
        self._state_saved   = False
        self._resume_times  = []      # horários (epoch do RTC) das retomadas

        # Shutdown limpo
        signal.signal(signal.SIGTERM, self._shutdown)
        signal.signal(signal.SIGINT,  self._shutdown)

    def run(self):
        self.display.open()
        print("\r[main] Iniciando kiln controller.")
        sdnotify.ready()
        try:
            self._acquire_sensor()
            self._try_resume()
            while True:
                now = time.monotonic()
                sdnotify.watchdog_ping()
                self._process_commands()   # /stop, /status vindos do Telegram
                self.sensor.poll()   # leitura temporizada do termopar (0,5 s)

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
            self.flog.close()
            self.display.close()
            self.encoder.cleanup()
            self.relays.cleanup()
            print("\r\n[main] Encerrado.")

    # ──────────────────────────────────────────────
    # Ciclo de controle
    # ──────────────────────────────────────────────

    def _acquire_sensor(self):
        """
        Bloqueia até obter um termopar válido (fail-closed).
        Enquanto isso: relés desligados, tela de erro e um único alerta
        no Telegram. Tenta novamente a cada SENSOR_RETRY_S segundos, de modo
        que reconectar o fio/módulo libera o controlador sem reiniciar.
        """
        notified  = False
        last_err  = None
        while True:
            self.relays.off()
            try:
                sensor = create_sensor()
                if (isinstance(sensor, SimulatedSensor)
                        and isinstance(self.relays, HardwareRelays)
                        and os.environ.get("KILN_ALLOW_SIM_WITH_REAL_RELAYS") != "1"):
                    raise SensorInitError(
                        "SIM_TEMP=1 com relés reais — recusado. Use também "
                        "SIM_RELAYS=1 ou KILN_ALLOW_SIM_WITH_REAL_RELAYS=1 (bancada)."
                    )
                self.sensor = sensor
                if notified:
                    self.notifier.send(f"✅ {b('Termopar detectado.')} Controlador liberado.",
                                       critical=True)
                return
            except SensorInitError as e:
                err = str(e)
                if err != last_err:
                    print(f"\r[main] Termopar indisponível: {err}")
                    last_err = err
                if not notified:
                    self.notifier.send(
                        f"🚨 {b('Termopar não detectado na inicialização.')}\n"
                        f"Forno bloqueado, relés desligados.\n"
                        f"Detalhe: {esc(err)}",
                        critical=True,
                    )
                    notified = True

            self.display.show_error("Termopar ausente",
                                    "Relés desligados",
                                    "Tentando novamente...")
            # Mantém o encoder drenado durante a espera
            deadline = time.monotonic() + SENSOR_RETRY_S
            while time.monotonic() < deadline:
                sdnotify.watchdog_ping()
                self._process_commands()
                for _ in self.encoder.poll():
                    pass
                time.sleep(0.05)

    def _generate_status(self, is_hourly=False) -> str:
        """Gera o texto de status atual para o Telegram."""
        if self.sensor is None:
            return f"🚨 {b('Termopar não detectado.')}\nForno bloqueado, relés desligados."
        temp = self.sensor.temperature
        state = self.controller.state
        
        prefix = f"⏱ {b('Atualização de hora em hora')}\n" if is_hourly else f"📊 {b('Status do forno')}\n"
            
        if self.ui == UIState.RUNNING:
            if state.mode == ControllerMode.SOAKING:
                rem_m = int(self.controller.soak_remaining_seconds() // 60)
                return f"{prefix}Modo: Patamar (Soak)\nTemp: {temp:.1f}°C (Alvo: {state.setpoint:.0f}°C)\nRestante: {rem_m} min"
            else:
                seg = state.curve["segments"][state.segment_index]
                return f"{prefix}Curva: {esc(state.curve['name'])} (Seg {state.segment_index+1}/{len(state.curve['segments'])})\nTemp: {temp:.1f}°C (Alvo: {seg['target_temp']}°C)\nTaxa: {state.display_rate:+.0f}°C/h"
                
        if self.ui == UIState.PREHEAT_RUN:
            return f"{prefix}Modo: Pré-aquecimento\nTemp: {temp:.1f}°C (Alvo: {self.preheat_target:.0f}°C)"
            
        if self.ui == UIState.FINISHED:
            return f"{prefix}A última queima foi concluída.\nTemp. Atual (Resfriando): {temp:.1f}°C"
            
        if self.ui == UIState.ERROR:
            st = self.controller.state
            return (f"{prefix}🚨 {b('Em ERRO')}: {esc(st.error_message or st.error_title)}\n"
                    f"Temp: {temp:.1f}°C — relés desligados")
        return f"{prefix}Estado: {esc(self.ui.upper())}\nTemp: {temp:.1f}°C"

    def _control_tick(self):
        temp  = self.sensor.temperature
        fault = self.sensor.fault

        # Registra temperatura máxima durante queima
        if self.ui == UIState.RUNNING:
            if temp > self.run_max_temp:
                self.run_max_temp = temp

        state = self.controller.update(temp, fault)
        if fault and state.error_kind == ErrorKind.SENSOR and self.sensor.fault_reason:
            state.error_message = (f"Falha no termopar por mais de 15 s: "
                                   f"{self.sensor.fault_reason}.")
        self.sensor.update(state.bank_a, state.bank_b)
        self.relays.set(state.bank_a, state.bank_b)
        self._persist_firing(temp)
        self._send_alarms(state, temp)

        # Registro da queima (uma linha por ciclo; resfriamento após o fim)
        wall = self._wall_dt()
        row  = self._log_row(state, temp)
        if self.flog.active and state.mode == ControllerMode.FINISHED:
            self.flog.record(row, wall)
            self.flog.end("queima concluída", wall, row)
        elif self.flog.active and state.mode == ControllerMode.ERROR:
            self.flog.record(row, wall)
            self.flog.end(f"ERRO ({state.error_kind.name if state.error_kind else '?'}): "
                          f"{state.error_message}", wall, row)
        else:
            self.flog.record(row, wall)

        # Sinal de vida externo
        if self.controller.is_active():
            self.hb.update(hb_describe(state, temp, (state.curve or {}).get("name")
                                       if state.mode != ControllerMode.PREHEAT else None))
        elif self.hb.active and state.mode == ControllerMode.FINISHED:
            self.hb.finish("Queima concluída")
        elif self.hb.active and state.mode == ControllerMode.ERROR:
            self.hb.fail(f"ERRO: {state.error_message}")

        # Transições automáticas do controlador
        if state.mode == ControllerMode.FINISHED and self.ui in (UIState.RUNNING, UIState.CANCEL_CONFIRM):
            # Salva o tempo decorrido exato do término
            self.run_final_duration = self.controller.run_elapsed_seconds()
    
            self.ui = UIState.FINISHED
            self.relays.off()
            
            # Alerta de conclusão
            dur_h, dur_m = divmod(int(self.run_final_duration // 60), 60)
            self.notifier.send(f"✅ {b('Queima concluída!')}\nDuração total: {dur_h}h{dur_m:02d}min\n"
                               f"Temp. máx: {self.run_max_temp:.1f}°C", critical=True)

        # Em IDLE, falha de sensor continua silenciosa (forno parado), mas
        # sobretemperatura / aquecimento com relés desligados sempre alarmam.
        if (state.mode == ControllerMode.ERROR and self.ui != UIState.ERROR
                and (self.ui != UIState.IDLE or state.error_kind != ErrorKind.SENSOR)):
            self.ui = UIState.ERROR
            self.relays.off()

            # Alerta de erro
            detail = state.error_message or "Falha no termopar detectada."
            self.notifier.send(f"🚨 {b('ERRO CRÍTICO!')}\n{esc(detail)}\nOs relés foram desligados "
                               f"por segurança na temperatura de {temp:.1f}°C.", critical=True)

        # Aviso automático de hora em hora
        if self.ui in (UIState.RUNNING, UIState.PREHEAT_RUN, UIState.CANCEL_CONFIRM):
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
            self.display.show_confirming(self.curves[self.curve_sel], scroll_idx=self.confirm_scroll)

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

        elif ui == UIState.CANCEL_CONFIRM:
            self.display.show_cancel_confirm()

        elif ui == UIState.FINISHED:
            self.display.show_finished(
                elapsed_s      = self.run_final_duration,
                max_temp       = self.run_max_temp,
                start_time_str = self.run_start_str,
            )

        elif ui == UIState.ERROR:
            self.display.show_error(state.error_title or "Falha no termopar")

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
                scroll_idx=self.save_scroll
            )

        elif ui == UIState.CURVE_EDIT_LIST:
            self.display.show_curve_list_edit(self.curves, self.edit_sel)

        elif ui == UIState.MESSAGE:
            self.display.show_message(self.msg_title, self.msg_text)

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
                self.confirm_scroll = 0
            elif long_:
                self.ui = UIState.MENU

        # ── CONFIRMING ────────────────────────────
        elif ui == UIState.CONFIRMING:
            if delta:
                curve = self.curves[self.curve_sel]
                
                # Total de itens é segmentos + 1 linha do patamar
                total_items = len(curve["segments"]) + 1 
                visible_items = 5 
                
                max_scroll = max(0, total_items - visible_items)
                
                self.confirm_scroll = max(0, min(max_scroll, self.confirm_scroll + delta))
                
            elif click:
                self._start_curve()
            elif long_:
                self.ui = UIState.SELECTING

        # ── RUNNING ───────────────────────────────
        elif ui == UIState.RUNNING:
            if long_:
                self.ui = UIState.CANCEL_CONFIRM

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
                self.ui = UIState.CANCEL_CONFIRM
                
        # ── CANCEL CONFIRM ────────────────────────
        elif ui == UIState.CANCEL_CONFIRM:
            if click:
                self._stop_run()
            elif long_:
                # Se desistir de cancelar, volta para a tela correspondente ao modo atual
                if self.controller.state.mode == ControllerMode.PREHEAT:
                    self.ui = UIState.PREHEAT_RUN
                else:
                    self.ui = UIState.RUNNING

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
                self.edit_soak = max(SOAK_MIN,
                    min(SOAK_MAX, self.edit_soak + delta * SOAK_STEP))
            elif click:
                self.edit_curve["soak_minutes"] = self.edit_soak
                self.save_scroll = 0
                self.ui = UIState.CURVE_SAVE
            elif long_:
                self._cancel_edit()

        # ── NOVA CURVA: SALVAR ────────────────────
        elif ui == UIState.CURVE_SAVE:
            if delta:
                # Calcula o limite de rolagem (segmentos + 1 linha de patamar)
                total_items = len(self.edit_curve["segments"]) + 1
                visible_items = 5
                max_scroll = max(0, total_items - visible_items)                
                self.save_scroll = max(0, min(max_scroll, self.save_scroll + delta))
            elif click:
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

        # ── AVISO ─────────────────────────────────
        elif ui == UIState.MESSAGE:
            if click or long_:
                self.ui = self.msg_return

    # ──────────────────────────────────────────────
    # Ações

    def _show_message(self, title: str, text: str, return_to):
        self.msg_title  = title
        self.msg_text   = text
        self.msg_return = return_to
        self.ui         = UIState.MESSAGE
    # ──────────────────────────────────────────────

    def _menu_confirm(self):
        sel = self.menu_sel
        if sel in (0, 3) and not self.curves:
            self._show_message("SEM CURVAS",
                               "Nenhuma curva válida cadastrada. Use Nova Curva.",
                               UIState.MENU)
            return
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
        err = validate_curve(curve)
        if err:   # defesa extra: curvas já são validadas na carga e ao salvar
            self._show_message("CURVA INVÁLIDA", err, UIState.SELECTING)
            return
        temp = self.sensor.temperature
        try:
            self.controller.start_curve(curve, temp)
        except ValueError as e:      # forno já acima do alvo final
            self._show_message("FORNO QUENTE", str(e), UIState.SELECTING)
            return
        skipped = self.controller.state.skipped_segments
        skip_txt = (f"Segmento(s) 1–{skipped} já atingido(s); começando no {skipped + 1}."
                    if skipped else "")
        self.run_max_temp  = temp
        self.run_start_str = self._wall_dt().strftime("%H:%M")
        self.hb.start(f"Queima iniciada: {curve['name']}")
        self.flog.start("curve", curve["name"], curve, self._log_params(),
                        self._wall_dt(), temp)
        if skip_txt:
            self.flog.event(skip_txt, self._log_row(self.controller.state, temp), self._wall_dt())
        self.ui = UIState.RUNNING
        self.last_hourly_announcement = 0

        # Envia alerta do Telegram
        self.notifier.send(f"🔥 {b('Queima iniciada')}\nCurva: {esc(curve['name'])}\n"
                           f"Temp. atual: {temp:.1f}°C"
                           + (f"\n{esc(skip_txt)}" if skip_txt else ""))

    def _start_preheat(self):
        self.controller.start_preheat(self.preheat_target, self.sensor.temperature)
        self.hb.start(f"Pré-aquecimento a {self.preheat_target:.0f} °C")
        self.flog.start("preheat", f"preaquecimento {self.preheat_target:.0f}C", None,
                        self._log_params(), self._wall_dt(), self.sensor.temperature)
        self.ui = UIState.PREHEAT_RUN
        self.last_hourly_announcement = 0

    def _stop_run(self):
        self.controller.stop()
        self.relays.off()
        self._clear_firing()
        self.flog.end("cancelada pelo operador", self._wall_dt())
        self.hb.finish("Queima cancelada pelo operador")
        self.ui = UIState.IDLE

    # ──────────────────────────────────────────────
    # Persistência / retomada da queima
    # ──────────────────────────────────────────────

    def _send_alarms(self, state, temp):
        """Alertas que não mudam o modo do controlador (vigilância pós-ERRO,
        termopar em falha com o forno parado)."""
        seen = getattr(self, "_alarm_seen", 0)
        if state.alarm_count == seen:
            return
        self._alarm_seen = state.alarm_count
        msg = state.alarm_message
        if state.alarm_critical:
            self.notifier.send(f"🚨 {b('ALERTA')}\n{esc(msg)}\nTemp. atual: {temp:.1f}°C",
                               critical=True)
            if state.alarm_kind == "watch":     # forno esquentando após ERRO
                self.hb.fail(msg)
        else:
            self.notifier.send(f"✅ {esc(msg)}\nTemp. atual: {temp:.1f}°C")
        self.flog.event(f"ALERTA: {msg}", self._log_row(state, temp), self._wall_dt())

    def _wall_dt(self):
        try:
            return self.rtc.now()
        except Exception:
            from datetime import datetime
            return datetime.now()

    def _log_row(self, state, temp) -> dict:
        in_curve = state.mode in (ControllerMode.RAMPING, ControllerMode.SOAKING)
        return {
            "elapsed_s":        self.controller.run_elapsed_seconds(),
            "mode":             state.mode.name,
            "segment":          state.segment_index + 1 if in_curve else None,
            "setpoint_c":       state.setpoint if self.controller.is_active() else None,
            "temp_c":           temp,
            "temp_raw_c":       self.sensor.last_raw,
            "bank_a":           state.bank_a,
            "bank_b":           state.bank_b,
            "b_suppressed":     state.bank_b_suppressed,
            "rate_c_h":         state.current_rate,
            "rate_valid":       state.rate_valid,
            "rate_display_c_h": state.display_rate,
            "sensor_fail":      self.sensor.fail_count,
        }

    @staticmethod
    def _log_params() -> dict:
        """Parâmetros de controle/segurança vigentes, gravados no .json da queima."""
        names = ["HYST_BELOW", "HYST_ABOVE", "MIN_ON_S", "MIN_OFF_S", "HYSTERESIS",
                 "WARMUP_TEMP", "DUAL_BANK_TEMP", "RATE_TOLERANCE", "RATE_WINDOW",
                 "RATE_MIN_WINDOW", "OVERTEMP_ABS", "OVERTEMP_MARGIN", "RUNAWAY_RISE",
                 "STALL_LAG", "STALL_MIN_RISE", "STALL_TIMEOUT_S",
                 "SEGMENT_TIMEOUT_FACTOR", "SEGMENT_TIMEOUT_EXTRA_S", "PREHEAT_MAX_S"]
        p = {n: getattr(ctrl_cfg, n) for n in names if hasattr(ctrl_cfg, n)}
        p["LOOP_S"] = 1.0 / LOOP_HZ
        return p

    def _wall_time(self) -> float:
        """Epoch pelo RTC (DS3231) — o relógio do sistema pode estar errado no boot."""
        try:
            return self.rtc.now().timestamp()
        except Exception:
            return time.time()

    def _persist_firing(self, temp: float, force: bool = False):
        """Grava o estado da queima periodicamente e ao mudar de segmento/modo.
        Remove o arquivo quando o controlador deixa de estar ativo
        (concluída, cancelada, ERRO)."""
        ctrl = self.controller
        if not ctrl.is_active():
            if self._state_saved:
                self._clear_firing()
            return

        s   = ctrl.state
        key = (s.mode, s.segment_index)
        now = time.monotonic()
        if not force and key == self._saved_key and now - self._last_save < STATE_SAVE_S:
            return

        snap = ctrl.snapshot(temp)
        snap.update({
            "saved_at":      self._wall_time(),
            "run_max_temp":  self.run_max_temp,
            "run_start_str": self.run_start_str,
            "resume_times":  self._resume_times,
            "log_file":      self.flog.path if self.flog.active else None,
        })
        try:
            firing_state.save(snap)
            self._state_saved = True
            self._saved_key   = key
            self._last_save   = now
        except OSError as e:
            print(f"\r[main] Falha ao gravar estado da queima: {e}")

    def _clear_firing(self):
        firing_state.clear()
        self._state_saved  = False
        self._saved_key    = None
        self._resume_times = []

    def _try_resume(self):
        """
        Chamado no boot, com o termopar já validado. Se havia queima em
        andamento, retoma — salvo sinais de problema, quando trava em ERRO
        com o motivo e avisa no Telegram.
        """
        snap = firing_state.load()
        if not snap:
            return

        temp      = self.sensor.temperature
        now_wall  = self._wall_time()
        saved_at  = snap.get("saved_at")
        downtime  = max(0.0, now_wall - saved_at) if saved_at else 0.0
        last_temp = snap.get("last_temp", temp)
        recent    = [t for t in snap.get("resume_times", [])
                     if now_wall - t < RESUME_WINDOW_S]

        problem = None
        if downtime > MAX_RESUME_DOWNTIME_S:
            problem = (f"controlador ficou {downtime / 3600:.1f} h fora do ar "
                       f"(limite {MAX_RESUME_DOWNTIME_S / 3600:.0f} h)")
        elif len(recent) >= MAX_RESUMES:
            problem = (f"{len(recent)} reinícios na última hora — "
                       f"possível falha recorrente")
        elif temp > OVERTEMP_ABS or temp > last_temp + RUNAWAY_RISE:
            problem = (f"temperatura subiu de {last_temp:.0f} °C para {temp:.0f} °C "
                       f"com o controlador fora do ar — verificar contatores")

        name = (snap.get("curve") or {}).get("name", "pré-aquecimento")
        if problem:
            self._clear_firing()
            msg = f"Queima '{name}' interrompida e NÃO retomada: {problem}."
            if self.flog.attach(snap.get("log_file"), "não retomada", downtime):
                self.flog.end(f"não retomada após reinício: {problem}", self._wall_dt())
            print(f"\r[main] {msg}")
            self.hb.fail(msg)
            self.controller.fail(ErrorKind.INTERRUPTED, "Queima interrompida", msg)
            self.ui = UIState.ERROR
            self.notifier.send(f"🚨 {b('Queima não retomada após reinício.')}\n{esc(msg)}\n"
                               f"Temp. atual: {temp:.1f}°C. Relés desligados.", critical=True)
            return

        desc = self.controller.resume(snap, temp, downtime)
        self.hb.start(f"Queima retomada após {downtime / 60:.0f} min: {desc}")
        if self.flog.attach(snap.get("log_file"), desc, downtime):
            self.flog.event(f"RETOMADA após {downtime / 60:.0f} min fora do ar "
                            f"(era {last_temp:.1f} °C): {desc}",
                            self._log_row(self.controller.state, temp), self._wall_dt())
        self._resume_times = recent + [now_wall]
        self.run_max_temp  = max(snap.get("run_max_temp", temp), temp)
        self.run_start_str = snap.get("run_start_str", "")
        self.preheat_target = snap.get("preheat_target", self.preheat_target)
        self.last_hourly_announcement = int(self.controller.run_elapsed_seconds() // 3600)
        self.ui = (UIState.PREHEAT_RUN if self.controller.state.mode == ControllerMode.PREHEAT
                   else UIState.RUNNING)
        self._persist_firing(temp, force=True)

        print(f"\r[main] Queima retomada após {downtime / 60:.0f} min fora do ar: {desc}")
        self.notifier.send(f"♻️ {b('Queima retomada após reinício.')}\n"
                           f"Fora do ar: {downtime / 60:.0f} min\n"
                           f"Temp. atual: {temp:.1f}°C (era {last_temp:.1f}°C)\n"
                           f"Retomando: {esc(desc)}", critical=True)

    def _begin_new_curve(self):
        name = next_curve_name(self.curves)
        self.edit_curve       = new_curve_template(name, self.curves)
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
            print(f"\r[main] Curva inválida: {err}")
            self._show_message("CURVA INVÁLIDA", err, UIState.CURVE_SAVE)
            return

        new_list = list(self.curves)
        if self.editing_existing:
            # Substitui a curva original (ids são únicos — garantido na carga)
            for i, c in enumerate(new_list):
                if c["id"] == self.edit_curve["id"]:
                    new_list[i] = self.edit_curve
                    break
            else:
                new_list.append(self.edit_curve)
        else:
            new_list.append(self.edit_curve)

        try:
            save_curves(new_list)
        except (ValueError, OSError) as e:
            print(f"\r[main] Falha ao salvar curvas: {e}")
            self._show_message("ERRO AO SALVAR", str(e), UIState.CURVE_SAVE)
            return

        self.curves = new_list
        print(f"\r[main] Curva '{self.edit_curve['name']}' salva.")
        self.edit_curve = None
        self.ui = UIState.IDLE

    def _cancel_edit(self):
        self.edit_curve = None
        self.ui = UIState.MENU

    def _emergency_stop(self):
        # Usado no encerramento do processo (SIGTERM/Ctrl+C). NÃO apaga o
        # estado da queima: um reinício do serviço/Pi retoma de onde parou.
        # Para abandonar a queima, cancele pelo encoder ou pelo /stop.
        self.relays.off()
        self.controller.stop()
        print("\r[main] Emergency stop — relés desligados.")
        
    # ──────────────────────────────────────────────
    # Comandos remotos (Telegram)
    # ──────────────────────────────────────────────

    def _call_in_main(self, fn, timeout: float = None):
        """
        Executa `fn` na thread do loop principal e devolve o resultado.
        Chamado pela thread do Telegram; o loop drena a fila a cada iteração
        (~20 ms). Levanta TimeoutError se o loop não executar a tempo.
        """
        if threading.get_ident() == getattr(self, "_main_thread", None) \
                or not hasattr(self, "_commands"):
            return fn()
        timeout = REMOTE_TIMEOUT_S if timeout is None else timeout
        done, box = threading.Event(), {}

        def job():
            try:
                box["result"] = fn()
            except Exception as e:
                box["error"] = e
            finally:
                done.set()

        self._commands.put(job)
        if not done.wait(timeout):
            raise TimeoutError(f"loop principal não respondeu em {timeout:.0f} s")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _process_commands(self):
        """Executa os comandos remotos pendentes (no loop principal)."""
        while True:
            try:
                job = self._commands.get_nowait()
            except queue.Empty:
                return
            job()

    def _remote_status(self) -> str:
        """Callback do /status (thread do Telegram)."""
        try:
            return self._call_in_main(self._generate_status)
        except TimeoutError:
            return (f"⚠️ {b('O controlador não respondeu')} em {REMOTE_TIMEOUT_S} s "
                    f"— possível travamento (o watchdog deve reiniciá-lo).")

    def _remote_stop_request(self) -> str:
        """Callback do /stop após a senha (thread do Telegram)."""
        try:
            return self._call_in_main(self._remote_emergency_stop)
        except TimeoutError:
            # Loop travado: corta os relés direto desta thread como último recurso
            self.relays.off()
            print("\r[main] /stop: loop principal não respondeu — relés desligados direto.")
            return ("O controlador não respondeu; os relés foram desligados diretamente. "
                    "Verifique o forno — o watchdog deve reiniciar o programa.")

    def _remote_emergency_stop(self) -> str:
        """Executado no loop principal após /stop + senha. Retorna o texto da resposta."""
        print("\r[main] Desligamento de emergência acionado via Telegram.")
        self.relays.off()
        if not self.controller.is_active():
            return "Nenhuma queima em andamento — relés confirmados desligados."
        self.controller.stop()
        self.relays.off()
        self._clear_firing()
        self.flog.end("parada remota (/stop)", self._wall_dt())
        self.hb.finish("Queima parada remotamente (/stop)")
        self.ui = UIState.IDLE
        return "Os relés foram desligados e a queima foi interrompida."

    def _shutdown(self, *_):
        print("\r[main] Sinal de shutdown recebido.")
        raise KeyboardInterrupt


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

if __name__ == "__main__":
    app = KilnApp()
    app.run()
