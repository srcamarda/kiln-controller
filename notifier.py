"""
notifier.py — Envio e recebimento assíncrono via Telegram.
"""
import threading
import queue
import urllib.request
import urllib.parse
import json
import time

class TelegramNotifier:
    def __init__(self, token: str, chat_ids, status_callback=None):
        self.token = token.strip() if token else ""
        
        if isinstance(chat_ids, str):
            self.chat_ids = [cid.strip() for cid in chat_ids.split(",") if cid.strip()]
        elif isinstance(chat_ids, list):
            self.chat_ids = [str(cid).strip() for cid in chat_ids if str(cid).strip()]
        else:
            self.chat_ids = []

        self.status_callback = status_callback
        self._queue = queue.Queue()
        
        if self.token and self.chat_ids:
            # Thread para envio
            self._sender_thread = threading.Thread(target=self._sender_worker, daemon=True)
            self._sender_thread.start()
            
            # Thread para recebimento (Long Polling)
            if self.status_callback:
                self._receiver_thread = threading.Thread(target=self._receiver_worker, daemon=True)
                self._receiver_thread.start()
                
            print(f"\r[notifier] Telegram ativado. Recebimento: {'ON' if status_callback else 'OFF'}")
        else:
            print("\r[notifier] Telegram desativado.")

    def send(self, message: str, specific_chat_id=None):
        """Adiciona a mensagem à fila. Envia para todos, ou apenas para um ID específico."""
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
                # timeout=30 instrui o Telegram a segurar a resposta até ter algo novo
                req_url = f"{url}?offset={offset}&timeout=30"
                with urllib.request.urlopen(req_url, timeout=35) as response:
                    data = json.loads(response.read().decode("utf-8"))
                    
                    for result in data.get("result", []):
                        offset = result["update_id"] + 1
                        msg = result.get("message", {})
                        text = msg.get("text", "")
                        chat_id = str(msg.get("chat", {}).get("id", ""))
                        
                        # Se for comando de status e de um chat autorizado
                        if text.startswith("/status") and chat_id in self.chat_ids:
                            reply = self.status_callback()
                            self.send(reply, specific_chat_id=chat_id) # Responde apenas para quem pediu
            except Exception:
                # Ignora quedas de internet e tenta novamente após alguns segundos
                time.sleep(5)