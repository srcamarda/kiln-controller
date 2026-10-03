"""
notifier.py — Envio e recebimento assíncrono via Telegram.
Inclui comandos /status e /stop.
"""
import threading
import queue
import urllib.request
import urllib.parse
import json
import time
import hmac

class TelegramNotifier:
    def __init__(self, token: str, chat_ids, status_callback=None, stop_callback=None, stop_password=None):
        self.token = token.strip() if token else ""
        
        if isinstance(chat_ids, str):
            self.chat_ids = [cid.strip() for cid in chat_ids.split(",") if cid.strip()]
        elif isinstance(chat_ids, list):
            self.chat_ids = [str(cid).strip() for cid in chat_ids if str(cid).strip()]
        else:
            self.chat_ids = []

        self.status_callback = status_callback
        self.stop_callback = stop_callback
        # Sem senha configurada o /stop fica desativado (nunca usar senha padrão)
        self.stop_password = stop_password or None
        if not self.stop_password:
            self.stop_callback = None
        
        # Dicionário para rastrear quem pediu /stop: {chat_id: timestamp_do_pedido}
        self._pending_stops = {}
        
        self._queue = queue.Queue()
        
        if self.token and self.chat_ids:
            self._sender_thread = threading.Thread(target=self._sender_worker, daemon=True)
            self._sender_thread.start()
            
            if self.status_callback or self.stop_callback:
                self._receiver_thread = threading.Thread(target=self._receiver_worker, daemon=True)
                self._receiver_thread.start()
                
            print(f"\r[notifier] Telegram ativado. Recebimento: {'ON' if (status_callback or stop_callback) else 'OFF'}")
        else:
            print("\r[notifier] Telegram desativado.")

    def send(self, message: str, specific_chat_id=None):
        if self.token and self.chat_ids:
            targets = [specific_chat_id] if specific_chat_id else self.chat_ids
            for cid in targets:
                self._queue.put((str(cid), message))

    def _sender_worker(self):
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        while True:
            chat_id, msg = self._queue.get()
            try:
                data = urllib.parse.urlencode({
                    "chat_id": chat_id, 
                    "text": msg,
                    "parse_mode": "Markdown"
                }).encode("utf-8")
                req = urllib.request.Request(url, data=data)
                with urllib.request.urlopen(req, timeout=5):
                    pass
            except Exception as e:
                print(f"\r[notifier] Erro envio {chat_id}: {e}")
            finally:
                self._queue.task_done()

    def _receiver_worker(self):
        url = f"https://api.telegram.org/bot{self.token}/getUpdates"
        offset = 0
        while True:
            try:
                req_url = f"{url}?offset={offset}&timeout=30"
                with urllib.request.urlopen(req_url, timeout=35) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    
                    for result in data.get("result", []):
                        offset = result["update_id"] + 1
                        msg = result.get("message", {})
                        text = msg.get("text", "").strip()
                        chat_id = str(msg.get("chat", {}).get("id", ""))
                        
                        if chat_id in self.chat_ids and text:
                            # 1. Verifica se estamos esperando uma senha desse chat
                            if chat_id in self._pending_stops:
                                elapsed = time.time() - self._pending_stops[chat_id]
                                del self._pending_stops[chat_id] # Limpa o estado
                                
                                if elapsed <= 60: # 60 segundos de timeout
                                    if hmac.compare_digest(text.encode("utf-8"),
                                                           self.stop_password.encode("utf-8")):
                                        if self.stop_callback:
                                            self.stop_callback()
                                        self.send("🚨 *DESLIGAMENTO EXECUTADO!*\nOs relés foram desativados e a queima foi interrompida com sucesso.", specific_chat_id=chat_id)
                                    else:
                                        self.send("❌ *Senha incorreta.* Operação de emergência cancelada.", specific_chat_id=chat_id)
                                else:
                                    self.send("⌛ *Tempo esgotado.* Operação cancelada. Digite /stop novamente se necessário.", specific_chat_id=chat_id)
                                continue

                            # 2. Processa os comandos normais
                            if text.startswith("/status") and self.status_callback:
                                reply = self.status_callback()
                                self.send(reply, specific_chat_id=chat_id)
                                
                            elif text.startswith("/stop") and self.stop_callback:
                                self._pending_stops[chat_id] = time.time()
                                self.send("⚠️ *ATENÇÃO!* Você solicitou o **DESLIGAMENTO DE EMERGÊNCIA**.\n\nPara confirmar, digite a sua senha de segurança nos próximos 60 segundos:", specific_chat_id=chat_id)
                                
            except Exception:
                time.sleep(5)