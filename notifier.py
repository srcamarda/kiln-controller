"""
notifier.py — Envio assíncrono de notificações via Telegram para múltiplos chats.
"""

import threading
import queue
import urllib.request
import urllib.parse

class TelegramNotifier:
    def __init__(self, token: str, chat_ids):
        self.token = token.strip() if token else ""
        
        # Aceita tanto uma lista de strings quanto uma única string separada por vírgulas
        if isinstance(chat_ids, str):
            self.chat_ids = [cid.strip() for cid in chat_ids.split(",") if cid.strip()]
        elif isinstance(chat_ids, list):
            self.chat_ids = [str(cid).strip() for cid in chat_ids if str(cid).strip()]
        else:
            self.chat_ids = []

        self._queue = queue.Queue()
        
        if self.token and self.chat_ids:
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
            print(f"\r[notifier] Telegram ativado para {len(self.chat_ids)} chat(s).")
        else:
            print("\r[notifier] Telegram desativado (Token ou Chat IDs ausentes).")

    def send(self, message: str):
        """Adiciona a mensagem à fila para ser enviada."""
        if self.token and self.chat_ids:
            self._queue.put(message)

    def _worker(self):
        """Consome a fila e envia as requisições HTTP para cada chat configurado."""
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        
        while True:
            msg = self._queue.get()
            
            # Envia a mesma mensagem para todos os IDs da lista
            for chat_id in self.chat_ids:
                try:
                    data = urllib.parse.urlencode({
                        "chat_id": chat_id, 
                        "text": msg,
                        "parse_mode": "Markdown"
                    }).encode("utf-8")
                    
                    req = urllib.request.Request(url, data=data)
                    with urllib.request.urlopen(req, timeout=5) as response:
                        pass # Sucesso silenciado
                except Exception as e:
                    print(f"\r[notifier] Erro ao enviar mensagem para {chat_id}: {e}")
            
            self._queue.task_done()