# flight-watch

Acompanha uma viagem com escalas (voos independentes, sem depender de PNR
único), usando só fontes gratuitas:

- **Aviationstack** (real-time flights, plano free) — status, atraso, gate, cancelamento.
- **OpenSky Network** — posição em tempo real do avião, quando em voo.
- **CallMeBot** — aviso automático por WhatsApp quando há problema ou mudança real de status.

Roda automaticamente a cada 15 minutos via GitHub Actions (`.github/workflows/monitor.yml`),
sem precisar de nenhum computador ligado. A cada rodada, atualiza um painel
web em `docs/index.html` (publicado via GitHub Pages) com o status de cada
trecho, colorido tipo sinal de trânsito (verde/amarelo/vermelho), detalhes
de horário/gate, risco de conexão entre trechos, e um mini-mapa ao vivo
quando o voo está no ar.

## Configuração

1. Crie os 5 Secrets do repositório (Settings → Secrets and variables → Actions):
   - `AVIATIONSTACK_KEY`
   - `OPENSKY_CLIENT_ID`
   - `OPENSKY_CLIENT_SECRET`
   - `CALLMEBOT_PHONE`
   - `CALLMEBOT_APIKEY`

2. Ative o GitHub Pages (Settings → Pages → Source: "Deploy from a branch",
   branch `main`, pasta `/docs`). A página fica em
   `https://SEU-USUARIO.github.io/NOME-DO-REPO/`.

3. Edite o dicionário `TRIP` em `flight_watch.py` com os voos reais da viagem
   (número de cada trecho, e o `buffer_min` de cada conexão assim que a
   passagem for emitida).

## Uso local (opcional, pra testar antes de subir)

```
pip install -r requirements.txt
python flight_watch.py --check LH505      # consulta avulsa de qualquer voo
python flight_watch.py --test-whatsapp    # testa o envio de WhatsApp
python flight_watch.py                    # roda uma vez (é isso que o Actions chama)
python flight_watch.py --adaptive         # loop local, opcional (o Actions já faz o agendamento)
```
