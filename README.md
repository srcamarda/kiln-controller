Controladora feita para substituir a placa original (manual) de um forno cerâmico de alta temperatura.

Hardware necessário:
- 1x Raspberry Pi (Modelo 2B no meu projeto)
- 2x Relé de alta amperagem
- 1x Módulo MAX31855
- 1x Termopar tipo K
- 1x Módulo DS3231 (RTC)
- 1x Display LCD RPi
- 1x Encoder Rotativo com clique

## Simulação e segurança

Sem hardware detectado o programa **não inicia o controle** (fail-closed): se o MAX31855 não responder ou retornar leitura inválida, a tela mostra "Termopar ausente", os relés ficam desligados, é enviado um alerta no Telegram e a detecção é repetida a cada 5 s.

Os simuladores só são usados quando pedidos explicitamente:

- `SIM_TEMP=1` — termopar simulado
- `SIM_RELAYS=1` — relés simulados
- `SIM_ENCODER=1` — encoder simulado
- `SIM_RTC=1` — horário do sistema em vez do DS3231

Termopar simulado com relés reais é recusado, a menos que `KILN_ALLOW_SIM_WITH_REAL_RELAYS=1` (teste de bancada, com os contatores desligados do forno).

Desenvolvimento no PC: `SIM_TEMP=1 SIM_RELAYS=1 SIM_ENCODER=1 SIM_RTC=1 python3 main.py`

## Configuração do Telegram

Token, chats autorizados e senha do `/stop` **não ficam no código**. Copie `.env.example` para `.env` (mesma pasta do `main.py`), preencha e proteja o arquivo:

```
cp .env.example .env
nano .env
chmod 600 .env
```

Variáveis já definidas no ambiente (ex.: `Environment=`/`EnvironmentFile=` no systemd) têm precedência sobre o `.env`. Sem token/chat o Telegram fica desativado; sem `KILN_STOP_PASSWORD` o `/stop` remoto fica desativado.
