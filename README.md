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

## Limites de segurança (software)

Qualquer um destes desliga os relés e trava em ERRO (tela + alerta no Telegram); os valores ficam no topo do `controller.py`:

- **Sobretemperatura**: acima de 1300 °C em qualquer modo (inclusive parado), ou 50 °C acima do alvo do modo ativo.
- **Aquece desligado**: subida de 30 °C com os dois bancos desligados — indica contator colado.
- **Temp. não sobe**: com aquecimento ligado e mais de 10 °C abaixo do setpoint, a temperatura não sobe 10 °C em 40 min (resistência queimada, termopar fora do lugar).
- **Tempo excedido**: segmento acima de 2× a duração teórica + 1 h; pré-aquecimento acima de 24 h.

Esses limites não substituem um limitador de sobretemperatura **em hardware**, em série com os contatores: se o Raspberry Pi travar ou um contator colar, o software não consegue desligar o forno.
