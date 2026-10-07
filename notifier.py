"""
notifier.py — Envio e recebimento assíncrono via Telegram.
Inclui comandos /status e /stop.

Formatação: as mensagens usam HTML do Telegram (<b>…</b>). Todo texto
variável (nome de curva, mensagens de erro…) deve passar por `esc()`;
use `b()` para negrito. Se mesmo assim o Telegram recusar a formatação
(erro 400), a mensagem é reenviada como texto simples — nunca descartada.

Entrega: falhas de rede, erro 5xx e limite de taxa (429) são repetidas com
espera crescente. Mensagens críticas (`critical=True`: erros, queima não
retomada, parada remota…) são tentadas por até CRITICAL_MAX_AGE_S; as
demais por até NORMAL_MAX_AGE_S. Toda falha e todo descarte vão para o log.
"""
import hmac
import html
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

NORMAL_MAX_AGE_S   = 10 * 60      # status, início, avisos de hora em hora
CRITICAL_MAX_AGE_S = 6 * 3600     # erros e eventos de segurança
BACKOFF_S          = (2, 5, 10, 30, 60)
SEND_TIMEOUT_S     = 10

STOP_CONFIRM_S     = 60           # prazo para digitar a senha após /stop
STOP_MAX_FAILS     = 3            # senhas erradas seguidas antes do bloqueio
STOP_LOCKOUT_S     = 15 * 60      # duração do bloqueio do /stop


def esc(text) -> str:
    """Escapa texto variável para o modo HTML do Telegram."""
    return html.escape(str(text), quote=False)


def b(text) -> str:
    """Negrito (o texto é escapado)."""
    return f"<b>{esc(text)}</b>"


def to_plain(text: str) -> str:
    """Remove as tags HTML para o reenvio como texto simples."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


class _Retry(Exception):
    def __init__(self, delay=None, reason=""):
        super().__init__(reason or "nova tentativa")
        self.delay = delay


class TelegramNotifier:
    def __init__(self, token: str, chat_ids, status_callback=None, stop_callback=None,
                 stop_password=None, admin_ids=None):
        """
        chat_ids  — chats que recebem as mensagens (privado: id do usuário;
                    grupo: id negativo, ex. -100123…). Comandos só são aceitos
                    vindos desses chats.
        admin_ids — usuários que podem dar comandos (/status, /stop). Padrão:
                    os próprios chat_ids (uso em chat privado).
        """
        self.token = token.strip() if token else ""
        self.chat_ids  = self._ids(chat_ids)
        self._admin_ids = self._ids(admin_ids)
        self._unknown_logged = set()

        self.status_callback = status_callback
        self.stop_callback = stop_callback
        # Sem senha configurada o /stop fica desativado (nunca usar senha padrão)
        self.stop_password = stop_password or None
        if not self.stop_password:
            self.stop_callback = None

        self._pending_stops = {}   # {(chat_id, user_id): instante do /stop}
        self._stop_fails    = {}   # {(chat_id, user_id): senhas erradas seguidas}
        self._stop_locked   = {}   # {(chat_id, user_id): bloqueado até}

        self._queue = queue.Queue()
        self._last_rx_error = None

        if self.token and self.chat_ids:
            self._sender_thread = threading.Thread(target=self._sender_worker, daemon=True)
            self._sender_thread.start()

            if self.status_callback or self.stop_callback:
                self._receiver_thread = threading.Thread(target=self._receiver_worker, daemon=True)
                self._receiver_thread.start()

            print(f"\r[notifier] Telegram ativado. Recebimento: "
                  f"{'ON' if (status_callback or self.stop_callback) else 'OFF'}")
        else:
            print("\r[notifier] Telegram desativado.")

    @property
    def admin_ids(self):
        """Usuários que podem comandar; sem configuração, os próprios chat ids."""
        return self._admin_ids or self.chat_ids

    @staticmethod
    def _ids(value):
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(v).strip() for v in value if str(v).strip()]
        return []

    # ── Envio ─────────────────────────────────

    def send(self, message: str, specific_chat_id=None, critical: bool = False):
        """Enfileira uma mensagem (HTML do Telegram; texto variável com esc())."""
        if self.token and self.chat_ids:
            targets = [specific_chat_id] if specific_chat_id else self.chat_ids
            now = time.monotonic()
            for cid in targets:
                self._queue.put((str(cid), message, critical, now))

    def _sender_worker(self):
        while True:
            chat_id, msg, critical, queued_at = self._queue.get()
            try:
                self._deliver(chat_id, msg, critical, queued_at)
            finally:
                self._queue.task_done()

    def _deliver(self, chat_id, msg, critical, queued_at):
        max_age = CRITICAL_MAX_AGE_S if critical else NORMAL_MAX_AGE_S
        attempt, html_mode = 0, True
        while True:
            try:
                self._post("sendMessage", {
                    "chat_id": chat_id,
                    "text": msg if html_mode else to_plain(msg),
                    **({"parse_mode": "HTML"} if html_mode else {}),
                })
                if attempt:
                    print(f"\r[notifier] Mensagem entregue a {chat_id} após {attempt} nova(s) tentativa(s).")
                return
            except _BadFormat as e:
                if html_mode:
                    print(f"\r[notifier] Formatação recusada ({e}); reenviando como texto simples.")
                    html_mode = False
                    continue
                print(f"\r[notifier] Mensagem recusada pelo Telegram, descartada: {e}")
                return
            except _Fatal as e:
                print(f"\r[notifier] Erro permanente ao enviar para {chat_id}, descartada: {e}")
                return
            except _Retry as r:
                age = time.monotonic() - queued_at
                if age > max_age:
                    print(f"\r[notifier] Desisti de enviar para {chat_id} após {age / 60:.0f} min "
                          f"({'crítica' if critical else 'normal'}): {to_plain(msg)[:80]!r}")
                    return
                delay = r.delay or BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
                attempt += 1
                time.sleep(delay)

    def _post(self, method, params, timeout=SEND_TIMEOUT_S, quiet=False):
        """Chama a API. Classifica erros em _BadFormat, _Fatal ou _Retry."""
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = urllib.parse.urlencode(params).encode("utf-8")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data),
                                        timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8"))
            except Exception:
                body = {}
            desc = body.get("description", str(e))
            if e.code == 429:
                delay = (body.get("parameters") or {}).get("retry_after", 5)
                if not quiet:
                    print(f"\r[notifier] Limite de envio do Telegram; aguardando {delay} s.")
                raise _Retry(delay)
            if e.code >= 500:
                if not quiet:
                    print(f"\r[notifier] Telegram indisponível ({e.code}); nova tentativa.")
                raise _Retry()
            if e.code == 400 and "parse" in desc.lower():
                raise _BadFormat(desc)
            new_id = (body.get("parameters") or {}).get("migrate_to_chat_id")
            if new_id:
                # Grupo virou supergrupo (ex.: ao promover o bot a admin): o id muda
                raise _Fatal(f"o grupo mudou de id — atualize KILN_TELEGRAM_CHAT_IDS "
                             f"para {new_id}")
            raise _Fatal(f"{e.code} {desc}")
        except (urllib.error.URLError, OSError, ValueError) as e:
            if not quiet:
                print(f"\r[notifier] Falha de rede ao enviar ({e}); nova tentativa.")
            raise _Retry(None, str(e))

    # ── Recebimento ───────────────────────────

    def _receiver_worker(self):
        offset = 0
        while True:
            try:
                data = self._post("getUpdates", {"offset": offset, "timeout": 30},
                                  timeout=35, quiet=True)
                if self._last_rx_error:
                    print("\r[notifier] Recebimento de comandos normalizado.")
                    self._last_rx_error = None
                for result in data.get("result", []):
                    offset = result["update_id"] + 1
                    try:
                        self._handle_update(result.get("message") or {})
                    except Exception as e:
                        print(f"\r[notifier] Erro ao tratar comando: {e!r}")
            except (_Retry, _Fatal, _BadFormat, Exception) as e:
                err = f"{type(e).__name__}: {e}"
                if err != self._last_rx_error:
                    print(f"\r[notifier] Erro no recebimento de comandos: {err}")
                    self._last_rx_error = err
                time.sleep(5)

    def _handle_update(self, msg: dict):
        text    = (msg.get("text") or "").strip()
        chat    = msg.get("chat") or {}
        sender  = msg.get("from") or {}
        chat_id = str(chat.get("id", ""))
        from_id = str(sender.get("id", ""))
        if chat_id and chat_id not in self.chat_ids:
            self._log_unknown("chat", chat_id, chat.get("title") or chat.get("username") or "",
                              "KILN_TELEGRAM_CHAT_IDS")
            return
        if not text:
            return
        # Só aceita comandos de usuários autorizados (em grupo, qualquer membro
        # pode escrever; em chat privado from.id == chat.id)
        if from_id not in self.admin_ids:
            if text.startswith("/"):
                self._log_unknown("usuário", from_id,
                                  sender.get("username") or sender.get("first_name") or "",
                                  "KILN_TELEGRAM_ADMIN_IDS")
            return
        # A senha do /stop é esperada de quem pediu, no mesmo chat
        key = (chat_id, from_id)

        now = time.time()

        # 1. Aguardando a senha deste usuário? (um novo comando cancela a espera)
        if key in self._pending_stops and text.startswith("/"):
            self._pending_stops.pop(key, None)
        if key in self._pending_stops:
            elapsed = now - self._pending_stops.pop(key)
            self._delete_message(chat_id, msg.get("message_id"))   # tira a senha do histórico
            if elapsed > STOP_CONFIRM_S:
                self.send(f"⌛ {b('Tempo esgotado.')} Operação cancelada. "
                          f"Digite /stop novamente se necessário.", specific_chat_id=chat_id)
                return
            if hmac.compare_digest(text.encode("utf-8"), self.stop_password.encode("utf-8")):
                self._stop_fails.pop(key, None)
                try:
                    result = self.stop_callback() if self.stop_callback else None
                except Exception as e:
                    print(f"\r[notifier] Erro ao executar /stop: {e!r}")
                    self.send(f"🚨 {b('FALHA AO EXECUTAR O DESLIGAMENTO!')}\n{esc(e)}\n"
                              f"Desligue o forno manualmente.", specific_chat_id=chat_id,
                              critical=True)
                    return
                detail = esc(result) if isinstance(result, str) else \
                    "Os relés foram desativados e a queima foi interrompida."
                self.send(f"🚨 {b('DESLIGAMENTO EXECUTADO!')}\n{detail}",
                          specific_chat_id=chat_id, critical=True)
            else:
                fails = self._stop_fails.get(key, 0) + 1
                self._stop_fails[key] = fails
                if fails >= STOP_MAX_FAILS:
                    self._stop_locked[key] = now + STOP_LOCKOUT_S
                    self._stop_fails.pop(key, None)
                    self.send(f"⛔ {b('Senha incorreta.')} /stop bloqueado por "
                              f"{STOP_LOCKOUT_S // 60} min após {fails} tentativas.",
                              specific_chat_id=chat_id)
                    print(f"\r[notifier] /stop bloqueado para {chat_id} após {fails} senhas erradas.")
                else:
                    self.send(f"❌ {b('Senha incorreta.')} Operação cancelada "
                              f"({fails}/{STOP_MAX_FAILS}).", specific_chat_id=chat_id)
            return

        # 2. Comandos
        if text.startswith("/status") and self.status_callback:
            self.send(self.status_callback(), specific_chat_id=chat_id)

        elif text.startswith("/stop") and self.stop_callback:
            locked_until = self._stop_locked.get(key, 0)
            if now < locked_until:
                self.send(f"⛔ /stop bloqueado por mais {int((locked_until - now) // 60) + 1} min "
                          f"(senhas incorretas). Use o encoder no forno.", specific_chat_id=chat_id)
                return
            self._pending_stops[key] = now
            self.send(f"⚠️ {b('ATENÇÃO!')} Você solicitou o {b('DESLIGAMENTO DE EMERGÊNCIA')}.\n\n"
                      f"Para confirmar, digite a sua senha de segurança nos próximos "
                      f"{STOP_CONFIRM_S} segundos:", specific_chat_id=chat_id)

    def _log_unknown(self, kind, ident, name, var):
        """Loga (uma vez por id) mensagem de chat/usuário não autorizado —
        é assim que se descobre o id de um grupo novo."""
        if ident in self._unknown_logged:
            return
        self._unknown_logged.add(ident)
        label = f" ({name})" if name else ""
        print(f"\r[notifier] Mensagem de {kind} não autorizado ignorada: id={ident}{label}. "
              f"Para autorizar, adicione o id em {var}.")

    def _delete_message(self, chat_id, message_id):
        if not message_id:
            return
        try:
            self._post("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, quiet=True)
        except Exception:
            pass   # sem permissão / mensagem antiga: não é crítico


class _BadFormat(Exception):
    pass


class _Fatal(Exception):
    pass
