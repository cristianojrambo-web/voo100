"""
flight_watch.py

Protótipo de acompanhamento de viagem com escalas (POA -> GRU -> MUC -> TSR)
usando somente fontes gratuitas:
  - Aviationstack (status de voo: atraso, cancelamento, gate)
  - OpenSky Network (posição em tempo real do avião, quando em voo)
  - CallMeBot (aviso via WhatsApp para o celular cadastrado)

IMPORTANTE: este script precisa rodar num computador com acesso normal à
internet (o ambiente onde eu testo código não tem acesso de rede liberado
para essas APIs). Rode localmente ou num servidor/PC que fique ligado
durante a viagem.

Credenciais necessárias (todas gratuitas), via variáveis de ambiente:
  AVIATIONSTACK_KEY      -> cadastro em https://aviationstack.com/ (free tier)
  OPENSKY_CLIENT_ID      -> cadastro em https://opensky-network.org/
  OPENSKY_CLIENT_SECRET  -> gerado junto com o client id (OAuth2)
  CALLMEBOT_PHONE        -> seu número com código do país, ex: +5551999999999
  CALLMEBOT_APIKEY       -> recebido por WhatsApp ao ativar o CallMeBot

Instalação:
  pip install requests --break-system-packages   (ou num virtualenv normal)

Uso:
  python flight_watch.py            -> roda uma verificação e sai
  python flight_watch.py --loop 900 -> roda em loop, verificando a cada 900s (15 min)
"""

import json
import os
import sys
import time
import argparse
from datetime import datetime, timezone
import urllib.parse

import requests

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO DA VIAGEM (ajuste números de voo e datas conforme a reserva real)
# ---------------------------------------------------------------------------
# Cada "leg" (trecho) é independente: número de voo próprio, data própria.
# "buffer_min" é o tempo de conexão já aprovado na emissão do bilhete (em
# minutos) até o próximo trecho -- usamos isso como referência de segurança,
# em vez de tentar estimar o MCT (tempo mínimo de conexão) do aeroporto.
# "connection_type" é só informativo por enquanto (ajuda a calibrar a margem
# de alerta): "domestic", "international_immigration", "international_transit".

TRIP = {
    # ENSAIO com voos reais de amanhã/depois (17-18/07), antes da passagem
    # do Lucas existir de fato. buffer_min aqui são valores de TESTE (pra
    # validar o cálculo de risco), não o tempo real de conexão de uma reserva.
    "viajante": "Ensaio",
    "legs": [
        {
            "id": "leg1_poa_gru",
            "flight_iata": "LA3419",        # POA -> GRU, sexta 17/07 (real, confirmado)
            "date": "2026-07-17",
            "origem": "POA",
            "destino": "GRU",
            "connection_type": None,        # é o primeiro trecho, sem conexão antes
        },
        {
            "id": "leg2_gru_muc",
            "flight_iata": "LH505",         # Lufthansa GRU -> MUC (opera seg/qua/sex)
            "date": "2026-07-17",
            "origem": "GRU",
            "destino": "MUC",
            "connection_type": "domestic_to_international",
            "buffer_min": 150,               # 2h30 de teste (POA-GRU dom. -> GRU-MUC intl)
        },
        {
            "id": "leg3_muc_tsr",
            "flight_iata": "LH4092",        # Lufthansa City MUC -> TSR (opera diariamente)
            "date": "2026-07-18",
            "origem": "MUC",
            "destino": "TSR",
            "connection_type": "international_immigration",
            "buffer_min": 180,               # 3h de teste (imigração Schengen em MUC)
        },
    ],
}

# Config de TESTE: um voo real de hoje, só pra validar a esteira
# (Aviationstack -> comparação de estado -> mensagem -> CallMeBot).
# Use "python flight_watch.py --test" para rodar contra isso em vez da TRIP real.
TEST_TRIP = {
    "viajante": "Teste",
    "legs": [
        {
            "id": "teste_ad2950",
            "flight_iata": "AD2950",
            "date": "2026-07-16",
            "origem": "?",
            "destino": "?",
            "connection_type": None,
        },
    ],
}

STATE_FILE = os.path.join(os.path.dirname(__file__), "flight_watch_state.json")

# Abaixo do percentual restante de buffer, dispara alerta de risco.
RISK_YELLOW_PCT = 50   # menos de 50% do buffer original restante -> atenção
RISK_RED_PCT = 20      # menos de 20% do buffer original restante -> agir agora

# ---------------------------------------------------------------------------
# ESTADO PERSISTENTE (pra só avisar quando algo MUDA, não repetir toda hora)
# ---------------------------------------------------------------------------

def load_state(state_file=None):
    state_file = state_file or STATE_FILE
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state, state_file=None):
    state_file = state_file or STATE_FILE
    with open(state_file, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# AVIATIONSTACK: status do voo (atraso, cancelamento, gate, horários)
# ---------------------------------------------------------------------------

def get_flight_status(flight_iata, flight_date):
    api_key = os.environ.get("AVIATIONSTACK_KEY")
    if not api_key:
        raise RuntimeError("Defina a variável de ambiente AVIATIONSTACK_KEY")

    url = "http://api.aviationstack.com/v1/flights"
    # NOTA: descobrimos que o plano free da Aviationstack devolve
    # "function_access_restricted" quando mandamos o parâmetro flight_date
    # junto (isso parece cair na feature paga "Flight Schedules"/"Future
    # Flight"). Testando sem o flight_date, usando só o "Real-Time Flights"
    # que é a feature que o plano free realmente inclui.
    params = {
        "access_key": api_key,
        "flight_iata": flight_iata,
    }
    resp = requests.get(url, params=params, timeout=15)
    if resp.status_code != 200:
        # Mostra o corpo do erro (a Aviationstack manda um código específico
        # tipo "function_access_restricted" -- útil pra saber exatamente o
        # que o plano free está bloqueando).
        print(f"[Aviationstack] HTTP {resp.status_code} -- corpo da resposta: {resp.text[:500]}")
        resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        print(f"[Aviationstack] erro na resposta: {data['error']}")
        return None

    results = data.get("data", [])
    if not results:
        return None
    return results[0]  # normalmente só há um voo com esse número/data


def summarize_status(raw):
    """Extrai os campos que nos interessam pra comparar mudanças."""
    if raw is None:
        return None
    dep = raw.get("departure", {}) or {}
    arr = raw.get("arrival", {}) or {}
    return {
        "status": raw.get("flight_status"),           # scheduled, active, landed, cancelled, incident, diverted
        "dep_scheduled": dep.get("scheduled"),
        "dep_estimated": dep.get("estimated"),
        "dep_actual": dep.get("actual"),
        "dep_delay_min": dep.get("delay"),
        "dep_gate": dep.get("gate"),
        "dep_terminal": dep.get("terminal"),
        "arr_scheduled": arr.get("scheduled"),
        "arr_estimated": arr.get("estimated"),
        "arr_actual": arr.get("actual"),
        "arr_delay_min": arr.get("delay"),
        "arr_gate": arr.get("gate"),
        "arr_terminal": arr.get("terminal"),
        "callsign_hint": (raw.get("flight", {}) or {}).get("icao"),
    }


# ---------------------------------------------------------------------------
# OPENSKY NETWORK: posição em tempo real (só enquanto o voo está no ar)
# ---------------------------------------------------------------------------

_opensky_token_cache = {"token": None, "expires_at": 0}


def get_opensky_token():
    now = time.time()
    if _opensky_token_cache["token"] and now < _opensky_token_cache["expires_at"] - 30:
        return _opensky_token_cache["token"]

    client_id = os.environ.get("OPENSKY_CLIENT_ID")
    client_secret = os.environ.get("OPENSKY_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None  # segue sem live map se não configurado

    token_url = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"
    resp = requests.post(
        token_url,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        timeout=15,
    )
    resp.raise_for_status()
    payload = resp.json()
    _opensky_token_cache["token"] = payload["access_token"]
    _opensky_token_cache["expires_at"] = now + payload.get("expires_in", 1700)
    return _opensky_token_cache["token"]


def get_live_position(expected_callsign):
    """
    Busca todas as aeronaves em voo e filtra pelo callsign esperado.
    OpenSky não permite filtrar por callsign diretamente -- trazemos o
    estado geral e comparamos localmente (best effort: o callsign real do
    transponder pode não bater 100% com o código comercial do voo).
    """
    token = get_opensky_token()
    if not token or not expected_callsign:
        return None

    url = "https://opensky-network.org/api/states/all"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    target = expected_callsign.strip().upper()
    for state in data.get("states") or []:
        callsign = (state[1] or "").strip().upper()
        if callsign == target or callsign.startswith(target):
            return {
                "lat": state[6],
                "lon": state[5],
                "altitude_m": state[7],
                "velocity_ms": state[9],
                "heading": state[10],
                "on_ground": state[8],
            }
    return None


# ---------------------------------------------------------------------------
# CALLMEBOT: envio da mensagem para o celular cadastrado (WhatsApp)
# ---------------------------------------------------------------------------

def send_whatsapp(message):
    phone = os.environ.get("CALLMEBOT_PHONE")
    apikey = os.environ.get("CALLMEBOT_APIKEY")
    if not phone or not apikey:
        print("[AVISO] CALLMEBOT_PHONE/CALLMEBOT_APIKEY não configurados. Mensagem só no log:")
        print(message)
        return

    url = "https://api.callmebot.com/whatsapp.php"
    params = {"phone": phone, "text": message, "apikey": apikey}
    try:
        resp = requests.get(url, params=params, timeout=15)
        print(f"[CallMeBot] status={resp.status_code} resp={resp.text[:200]}")
    except Exception as e:
        print(f"[CallMeBot] erro ao enviar: {e}")


# ---------------------------------------------------------------------------
# LÓGICA DE EVENTOS: compara estado antigo com novo e gera mensagens
# ---------------------------------------------------------------------------

def parse_iso(dt_str):
    if not dt_str:
        return None
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))


def detect_events(leg, old, new):
    events = []
    if old is None:
        events.append(("info", f"Monitoramento iniciado para {leg['id']} ({leg['flight_iata']} em {leg['date']})."))
        return events

    # A Aviationstack free só devolve a instância mais recente de cada número
    # de voo (sem filtro de data). Se o horário de partida programado mudou
    # de um dia pro outro entre duas checagens, não é um "atraso" nem uma
    # "mudança de gate" de verdade -- é simplesmente outra ocorrência do
    # mesmo número de voo (ex: virou a semana). Nesse caso, só avisa que é
    # uma instância nova e não compara os outros campos.
    old_dep_day = (old.get("dep_scheduled") or "")[:10]
    new_dep_day = (new.get("dep_scheduled") or "")[:10]
    if old_dep_day and new_dep_day and old_dep_day != new_dep_day:
        events.append(("info", f"Voo {leg['flight_iata']}: nova ocorrência detectada "
                                 f"({old_dep_day} -> {new_dep_day}). Reiniciando comparação."))
        return events

    if old["status"] != new["status"]:
        if new["status"] == "cancelled":
            events.append(("red", f"O voo {leg['flight_iata']} ({leg['origem']}->{leg['destino']}) foi CANCELADO. "
                                    f"Vá até o balcão da companhia aérea em {leg['origem']} e solicite remarcação imediatamente."))
        elif new["status"] == "diverted":
            events.append(("red", f"O voo {leg['flight_iata']} foi DESVIADO de rota. Acompanhe o novo destino/pouso e procure a companhia aérea."))
        elif new["status"] == "active" and old["status"] == "scheduled":
            events.append(("green", f"O voo {leg['flight_iata']} decolou."))
        elif new["status"] == "landed":
            events.append(("green", f"O voo {leg['flight_iata']} pousou em {leg['destino']}."))

    old_delay = old.get("dep_delay_min") or 0
    new_delay = new.get("dep_delay_min") or 0
    if new_delay and new_delay != old_delay and new_delay >= 15:
        events.append(("yellow", f"Voo {leg['flight_iata']} com atraso de {new_delay} min na partida de {leg['origem']}."))

    if old.get("dep_gate") != new.get("dep_gate") and new.get("dep_gate"):
        events.append(("yellow", f"Portão de embarque do voo {leg['flight_iata']} em {leg['origem']}: {new['dep_gate']}"
                                   + (f" (terminal {new['dep_terminal']})" if new.get("dep_terminal") else "")))

    if old.get("arr_gate") != new.get("arr_gate") and new.get("arr_gate"):
        events.append(("info", f"Portão de chegada do voo {leg['flight_iata']} em {leg['destino']}: {new['arr_gate']}"))

    return events


def evaluate_connection_buffer(prev_leg, prev_status, next_leg, next_status):
    """
    Compara o horário estimado/real de chegada do trecho anterior com o
    horário estimado/real de partida do próximo trecho, e verifica quanto
    do buffer original (definido na reserva) ainda resta.
    """
    buffer_min = next_leg.get("buffer_min")
    if not buffer_min:
        return None  # sem valor de referência cadastrado, não avalia risco

    arr_time = parse_iso(prev_status.get("arr_estimated") or prev_status.get("arr_scheduled"))
    dep_time = parse_iso(next_status.get("dep_estimated") or next_status.get("dep_scheduled"))
    if not arr_time or not dep_time:
        return None

    real_buffer_min = (dep_time - arr_time).total_seconds() / 60

    # Trava de sanidade: como a Aviationstack free só devolve a instância
    # "mais recente" de cada número de voo (sem filtro de data), rodar isso
    # ANTES do dia real da viagem pode trazer dois trechos de dias diferentes
    # (ex: trecho 1 de hoje, trecho 2 do último dia em que operou). Isso gera
    # buffers absurdos (negativos ou enormes). Só avalia risco se o intervalo
    # for plausível: positivo e dentro de uma janela razoável de conexão.
    JANELA_MAX_MIN = 48 * 60  # 48h -- acima disso, não é uma conexão real
    if real_buffer_min < 0 or real_buffer_min > JANELA_MAX_MIN:
        return {
            "level": "gray",
            "message": f"ainda sem dados suficientes pra avaliar a conexão (dá pra confiar perto do dia da viagem)",
            "pct_restante": None,
        }

    pct_restante = max(0, min(100, (real_buffer_min / buffer_min) * 100))

    if pct_restante <= RISK_RED_PCT:
        level = "red"
        msg = (f"RISCO ALTO: restam ~{int(real_buffer_min)} min (era {buffer_min} min). "
               f"Procure a equipe da companhia aérea assim que desembarcar.")
    elif pct_restante <= RISK_YELLOW_PCT:
        level = "yellow"
        msg = (f"apertada: restam ~{int(real_buffer_min)} min (era {buffer_min} min). "
               f"Vá direto para o próximo portão sem parar.")
    else:
        level = "green"
        msg = f"tranquila: ~{int(real_buffer_min)} min de folga."

    return {"level": level, "message": msg, "pct_restante": pct_restante}


# ---------------------------------------------------------------------------
# PAINEL DO OBSERVADOR: visão rápida tipo sinal (verde/amarelo/vermelho)
# ---------------------------------------------------------------------------

def traffic_light(status):
    """Classifica o estado atual de um trecho em verde/amarelo/vermelho."""
    if status is None:
        return "cinza", "sem dados ainda"
    if status["status"] in ("cancelled", "diverted", "incident"):
        return "vermelho", status["status"]
    delay = status.get("dep_delay_min") or status.get("arr_delay_min") or 0
    if delay and delay >= 30:
        return "vermelho", f"atraso de {delay} min"
    if delay and delay >= 15:
        return "amarelo", f"atraso de {delay} min"
    if status["status"] == "landed":
        return "verde", "pousou"
    if status["status"] == "active":
        return "verde", "em voo"
    return "verde", status["status"] or "no horário"


def print_panel(trip, statuses):
    print("\n===== PAINEL DO OBSERVADOR =====")
    for leg in trip["legs"]:
        cor, motivo = traffic_light(statuses.get(leg["id"]))
        print(f"  [{cor.upper():8}] {leg['origem']}->{leg['destino']} "
              f"({leg['flight_iata']}): {motivo}")
    print("=================================\n")


# ---------------------------------------------------------------------------
# DASHBOARD WEB: página estática (docs/index.html) pra abrir no celular
# ---------------------------------------------------------------------------

DOCS_DIR = os.path.join(os.path.dirname(__file__), "docs")

# Paleta escura, estilo app de rastreamento de voo (Flighty/FR24) em vez de
# formulário corporativo claro.
_COR_HEX = {
    "verde": "#3fb950",
    "amarelo": "#d29922",
    "vermelho": "#f85149",
    "cinza": "#8b949e",
}
_COR_BG = {
    "verde": "rgba(63,185,80,0.15)",
    "amarelo": "rgba(210,153,34,0.15)",
    "vermelho": "rgba(248,81,73,0.15)",
    "cinza": "rgba(139,148,158,0.15)",
}


def _fmt_hora(iso_str):
    dt = parse_iso(iso_str)
    if not dt:
        return "--"
    return dt.strftime("%d/%m %H:%M UTC")


def flight_progress_pct(status):
    """% estimado do trajeto já percorrido (baseado em horário, não em GPS real).
    Só faz sentido pra voo 'active'. Retorna None se não der pra calcular."""
    if not status or status.get("status") != "active":
        return None
    inicio = parse_iso(status.get("dep_actual") or status.get("dep_estimated") or status.get("dep_scheduled"))
    fim = parse_iso(status.get("arr_estimated") or status.get("arr_scheduled"))
    if not inicio or not fim or fim <= inicio:
        return None
    agora = datetime.now(timezone.utc)
    pct = (agora - inicio).total_seconds() / (fim - inicio).total_seconds() * 100
    return max(2, min(98, pct))  # nunca 0 nem 100 cravado, fica sempre visível no meio do caminho


_ICONE_STATUS = {
    "scheduled": "🕒",
    "active": "✈️",
    "landed": "🛬",
    "cancelled": "🚫",
    "diverted": "⚠️",
    "incident": "⚠️",
}

_NIVEL_PARA_COR = {"red": "vermelho", "yellow": "amarelo", "green": "verde", "gray": "cinza"}


def overall_status(trip, statuses, connection_risks):
    """Pior situação entre todos os trechos + conexões -- pro banner geral do topo."""
    piores = {"vermelho": 0, "amarelo": 0, "verde": 0, "cinza": 0}
    for leg in trip["legs"]:
        cor, _ = traffic_light(statuses.get(leg["id"]))
        piores[cor] = piores.get(cor, 0) + 1
    for risk in connection_risks.values():
        cor = _NIVEL_PARA_COR.get(risk["level"], "cinza")
        if cor in ("vermelho", "amarelo"):
            piores[cor] += 1

    if piores["vermelho"]:
        return "vermelho", "Atenção: há um problema que precisa de ação agora."
    if piores["amarelo"]:
        return "amarelo", "Fique de olho: há um atraso ou conexão apertada."
    return "verde", "Tudo certo até agora."


def _timeline_html(trip, statuses):
    passos = []
    for i, leg in enumerate(trip["legs"]):
        cor, _ = traffic_light(statuses.get(leg["id"]))
        fg = _COR_HEX[cor]
        passos.append(
            f"<div class='passo'>"
            f"<div class='bolinha' style='background:{fg}'>{i + 1}</div>"
            f"<div class='passo-label'>{leg['origem']}</div>"
            f"</div>"
        )
        if i < len(trip["legs"]) - 1:
            passos.append("<div class='linha'></div>")
    passos.append(
        f"<div class='passo'><div class='bolinha bolinha-fim'>&#127937;</div>"
        f"<div class='passo-label'>{trip['legs'][-1]['destino']}</div></div>"
    )
    return f"<div class='timeline'>{''.join(passos)}</div>"


def _leg_card_html(leg, status, connection_risk, position):
    cor, motivo = traffic_light(status)
    bg, fg = _COR_BG[cor], _COR_HEX[cor]
    ativo = bool(status and status.get("status") == "active")
    dot_class = "dot dot-pulse" if ativo else "dot"

    detalhes = ""
    if status:
        detalhes = (
            f"<div class='detalhe'><span class='detalhe-label'>PARTIDA</span> "
            f"{_fmt_hora(status.get('dep_estimated') or status.get('dep_scheduled'))}"
            f"{' · gate ' + status['dep_gate'] if status.get('dep_gate') else ''}"
            f"{' · term. ' + status['dep_terminal'] if status.get('dep_terminal') else ''}</div>"
            f"<div class='detalhe'><span class='detalhe-label'>CHEGADA</span> "
            f"{_fmt_hora(status.get('arr_estimated') or status.get('arr_scheduled'))}"
            f"{' · gate ' + status['arr_gate'] if status.get('arr_gate') else ''}"
            f"{' · term. ' + status['arr_terminal'] if status.get('arr_terminal') else ''}</div>"
        )

    progresso_html = ""
    telemetria_html = ""
    mapa_html = ""
    if ativo:
        pct = flight_progress_pct(status)
        if pct is not None:
            progresso_html = (
                f"<div class='progresso'>"
                f"<div class='progresso-trilho'>"
                f"<div class='progresso-fill' style='width:{pct:.0f}%'></div>"
                f"<div class='progresso-aviao' style='left:{pct:.0f}%'>&#9992;</div>"
                f"</div>"
                f"<div class='progresso-labels'><span>{leg['origem']}</span><span>{leg['destino']}</span></div>"
                f"</div>"
            )

        if position and position.get("lat") and position.get("lon"):
            mapa_id = f"mapa_{leg['id']}"
            alt_km = round((position.get("altitude_m") or 0) / 1000, 1)
            vel_kmh = round((position.get("velocity_ms") or 0) * 3.6)
            heading = round(position.get("heading") or 0)
            telemetria_html = (
                f"<div class='telemetria'>"
                f"<div class='stat'><span class='stat-valor'>{alt_km}</span><span class='stat-label'>km alt.</span></div>"
                f"<div class='stat'><span class='stat-valor'>{vel_kmh}</span><span class='stat-label'>km/h</span></div>"
                f"<div class='stat'><span class='stat-valor'>{heading}&deg;</span><span class='stat-label'>rumo</span></div>"
                f"</div>"
            )
            mapa_html = (
                f"<div id='{mapa_id}' class='mapa'></div>"
                f"<script>"
                f"(function(){{var m=L.map('{mapa_id}',{{attributionControl:false,zoomControl:false}})"
                f".setView([{position['lat']},{position['lon']}],5);"
                f"L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png').addTo(m);"
                f"var icon=L.divIcon({{html:'&#9992;',className:'plane-icon',iconSize:[24,24]}});"
                f"L.marker([{position['lat']},{position['lon']}],{{icon:icon}}).addTo(m);"
                f"}})();"
                f"</script>"
            )
        else:
            mapa_html = "<div class='mapa-indisponivel'>📍 Mapa ao vivo indisponível no momento</div>"

    conexao_html = ""
    if connection_risk:
        rc_cor = _NIVEL_PARA_COR.get(connection_risk["level"], "cinza")
        rc_bg, rc_fg = _COR_BG[rc_cor], _COR_HEX[rc_cor]
        conexao_html = (f"<div class='conexao' style='background:{rc_bg};color:{rc_fg};border-color:{rc_fg}44'>"
                         f"Conexão em {leg['origem']}: {connection_risk['message']}</div>")

    return f"""
    <div class="card">
      <div class="card-header">
        <span class="{dot_class}" style="background:{fg}"></span>
        <span class="rota">{leg['origem']} <span class="seta">&rarr;</span> {leg['destino']}</span>
        <span class="voo">{leg['flight_iata']}</span>
        <span class="badge" style="background:{bg};color:{fg}">{cor.upper()}</span>
      </div>
      <div class="motivo">{motivo}</div>
      {progresso_html}
      {mapa_html}
      {telemetria_html}
      {detalhes}
      {conexao_html}
    </div>
    """


def render_dashboard_html(trip, statuses, connection_risks, positions):
    os.makedirs(DOCS_DIR, exist_ok=True)
    cards = "\n".join(
        _leg_card_html(leg, statuses.get(leg["id"]), connection_risks.get(leg["id"]), positions.get(leg["id"]))
        for leg in trip["legs"]
    )
    timeline = _timeline_html(trip, statuses)
    banner_cor, banner_texto = overall_status(trip, statuses, connection_risks)
    banner_bg, banner_fg = _COR_BG[banner_cor], _COR_HEX[banner_cor]
    atualizado = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Painel do Observador -- {trip['viajante']}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
    background: #0d1117; margin:0; padding:16px; color:#e6edf3;
    max-width:640px; margin-left:auto; margin-right:auto;
  }}
  .mono {{ font-family: ui-monospace, "SF Mono", Consolas, monospace; }}
  h1 {{ font-size:1.1rem; margin:0 0 2px; font-weight:600; letter-spacing:0.02em; }}
  .atualizado {{ font-size:0.72rem; color:#8b949e; margin-bottom:16px; }}

  .banner {{ border-radius:10px; padding:12px 14px; margin-bottom:18px; font-weight:600; font-size:0.9rem;
             border:1px solid currentColor; }}

  .timeline {{ display:flex; align-items:flex-start; margin-bottom:22px; padding:6px 0 0; overflow-x:auto; }}
  .passo {{ display:flex; flex-direction:column; align-items:center; flex-shrink:0; }}
  .bolinha {{ width:30px; height:30px; border-radius:50%; color:#0d1117; display:flex;
              align-items:center; justify-content:center; font-size:0.78rem; font-weight:700; }}
  .bolinha-fim {{ background:#e6edf3; }}
  .passo-label {{ font-size:0.68rem; color:#8b949e; margin-top:5px; font-family: ui-monospace, monospace; }}
  .linha {{ height:2px; background:#30363d; flex:1; min-width:24px; margin:15px 4px 0; }}

  .card {{ background:#161b22; border:1px solid #30363d; border-radius:12px;
           padding:16px 18px; margin-bottom:14px; }}
  .card-header {{ display:flex; align-items:center; gap:9px; margin-bottom:8px; flex-wrap:wrap; }}
  .dot {{ width:9px; height:9px; border-radius:50%; flex-shrink:0; }}
  .dot-pulse {{ animation: pulso 1.6s ease-in-out infinite; }}
  @keyframes pulso {{
    0% {{ box-shadow: 0 0 0 0 currentColor; opacity:1; }}
    70% {{ box-shadow: 0 0 0 6px transparent; opacity:0.7; }}
    100% {{ box-shadow: 0 0 0 0 transparent; opacity:1; }}
  }}
  .badge {{ font-size:0.68rem; font-weight:700; padding:3px 9px; border-radius:999px; margin-left:auto;
            letter-spacing:0.03em; }}
  .rota {{ font-weight:700; font-size:1.05rem; font-family: ui-monospace, monospace; }}
  .seta {{ color:#8b949e; font-weight:400; }}
  .voo {{ font-size:0.8rem; color:#8b949e; font-family: ui-monospace, monospace; }}
  .motivo {{ font-size:0.88rem; margin-bottom:10px; color:#c9d1d9; }}
  .detalhe {{ font-size:0.78rem; color:#8b949e; line-height:1.7; font-family: ui-monospace, monospace; }}
  .detalhe-label {{ color:#484f58; letter-spacing:0.05em; }}
  .conexao {{ margin-top:10px; padding:9px 11px; border-radius:8px; font-size:0.82rem; border:1px solid; }}

  .progresso {{ margin:10px 0 12px; }}
  .progresso-trilho {{ position:relative; height:4px; background:#30363d; border-radius:99px; margin:14px 0 4px; }}
  .progresso-fill {{ position:absolute; left:0; top:0; height:100%; background:#58a6ff; border-radius:99px; }}
  .progresso-aviao {{ position:absolute; top:50%; transform:translate(-50%,-50%) rotate(90deg);
                       font-size:1rem; margin-top:1px; }}
  .progresso-labels {{ display:flex; justify-content:space-between; font-size:0.7rem; color:#8b949e;
                        font-family: ui-monospace, monospace; }}

  .mapa {{ height:220px; border-radius:10px; margin-top:8px; filter:saturate(0.9); }}
  .mapa-indisponivel {{ font-size:0.75rem; color:#484f58; margin-top:10px; font-style:italic; }}

  .telemetria {{ display:flex; gap:18px; margin-top:12px; padding-top:12px; border-top:1px solid #21262d; }}
  .stat {{ display:flex; flex-direction:column; }}
  .stat-valor {{ font-family: ui-monospace, monospace; font-size:1rem; font-weight:600; color:#e6edf3; }}
  .stat-label {{ font-size:0.65rem; color:#8b949e; letter-spacing:0.04em; margin-top:2px; }}
</style>
</head>
<body>
  <h1>✈️ Painel do Observador -- {trip['viajante']}</h1>
  <div class="atualizado">Última atualização: {atualizado} (automático)</div>

  <div class="banner" style="background:{banner_bg};color:{banner_fg}">{banner_texto}</div>

  {timeline}

  {cards}
</body>
</html>"""

    out_path = os.path.join(DOCS_DIR, "index.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


# ---------------------------------------------------------------------------
# FREQUÊNCIA ADAPTATIVA: mais espaçado longe da viagem, mais frequente perto
# ---------------------------------------------------------------------------

def seconds_until_next_check(trip, statuses):
    """
    Olha o próximo trecho que ainda não pousou e decide de quanto em quanto
    tempo faz sentido checar de novo -- sem gastar cota à toa quando a
    viagem ainda está longe, e checando bastante nas horas críticas.
    """
    now = datetime.now(timezone.utc)
    proximo = None
    for leg in trip["legs"]:
        status = statuses.get(leg["id"])
        if status is None or status["status"] in ("landed", "cancelled", "diverted"):
            continue
        dep = parse_iso(status.get("dep_estimated") or status.get("dep_scheduled"))
        if dep:
            proximo = dep
            break

    if proximo is None:
        return 6 * 3600  # nada relevante encontrado, verifica de novo em 6h

    horas_restantes = (proximo - now).total_seconds() / 3600
    if horas_restantes > 24:
        return 6 * 3600      # mais de 1 dia -> a cada 6h
    if horas_restantes > 3:
        return 3600          # entre 3h e 24h -> a cada 1h
    if horas_restantes > 0.5:
        return 900           # entre 30min e 3h -> a cada 15min
    return 300               # menos de 30min ou já em voo -> a cada 5min


# ---------------------------------------------------------------------------
# LOOP PRINCIPAL
# ---------------------------------------------------------------------------

def run_once(trip=None, state_file=None):
    trip = trip or TRIP
    state_file = state_file or STATE_FILE
    state = load_state(state_file)
    legs = trip["legs"]
    statuses = {}
    positions = {}

    for leg in legs:
        try:
            raw = get_flight_status(leg["flight_iata"], leg["date"])
        except Exception as e:
            print(f"[{leg['id']}] Aviationstack falhou nesta checagem ({e}). Pulando este trecho por agora.")
            statuses[leg["id"]] = state.get(leg["id"])  # mantém o último estado conhecido
            continue

        new_status = summarize_status(raw)
        statuses[leg["id"]] = new_status

        if new_status is None:
            print(f"[{leg['id']}] Sem dados ainda para {leg['flight_iata']} em {leg['date']}.")
            continue

        old_status = state.get(leg["id"])
        events = detect_events(leg, old_status, new_status)
        for level, msg in events:
            print(f"[{leg['id']}][{level}] {msg}")
            # WhatsApp avisa em problema (vermelho/amarelo) E em mudança real
            # de status (verde: decolou/pousou) -- só fica de fora o que é
            # puramente informativo (monitoramento iniciado, nova ocorrência,
            # portão de chegada avulso), que fica só no painel.
            if level in ("red", "yellow", "green"):
                send_whatsapp(f"[{trip['viajante']} - {leg['origem']}->{leg['destino']}] {msg}")

        # Se o voo está em voo, tenta pegar posição ao vivo (best effort).
        # O OpenSky é um "nice to have" (mapa) -- se falhar (timeout, rede do
        # runner do GitHub Actions bloqueada, instabilidade do serviço, etc.),
        # NÃO pode derrubar o resto do monitoramento (avisos, painel, estado).
        if new_status["status"] == "active":
            callsign = new_status.get("callsign_hint") or leg["flight_iata"]
            try:
                pos = get_live_position(callsign)
            except Exception as e:
                print(f"[{leg['id']}][mapa] OpenSky indisponível agora ({e}). Seguindo sem mapa.")
                pos = None
            if pos:
                print(f"[{leg['id']}][mapa] lat={pos['lat']} lon={pos['lon']} alt={pos['altitude_m']}m")
                positions[leg["id"]] = pos

        state[leg["id"]] = new_status

    # Avalia buffers de conexão entre trechos consecutivos
    connection_risks = {}
    for i in range(len(legs) - 1):
        prev_leg, next_leg = legs[i], legs[i + 1]
        prev_status = statuses.get(prev_leg["id"])
        next_status = statuses.get(next_leg["id"])
        if prev_status and next_status:
            risk = evaluate_connection_buffer(prev_leg, prev_status, next_leg, next_status)
            if risk:
                connection_risks[next_leg["id"]] = risk
                print(f"[conexao {next_leg['origem']}][{risk['level']}] {risk['message']}")
                if risk["level"] in ("red", "yellow"):
                    send_whatsapp(f"[{trip['viajante']} - conexão {next_leg['origem']}] {risk['message']}")

    print_panel(trip, statuses)
    render_dashboard_html(trip, statuses, connection_risks, positions)
    save_state(state, state_file)
    return statuses


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", type=int, default=0,
                         help="Se definido, roda em loop a cada N segundos fixos (ex: 900 = 15min).")
    parser.add_argument("--adaptive", action="store_true",
                         help="Roda em loop com intervalo adaptativo (espaçado longe da viagem, "
                              "frequente perto do horário do voo). Ignora --loop.")
    parser.add_argument("--test", action="store_true",
                         help="Roda contra o TEST_TRIP (um voo real de hoje) em vez da viagem real.")
    parser.add_argument("--test-whatsapp", action="store_true",
                         help="Só manda uma mensagem de teste pelo CallMeBot e sai (não consulta nenhum voo).")
    parser.add_argument("--check", type=str, default=None,
                         help="Consulta avulsa: mostra o status atual de QUALQUER número de voo "
                              "(ex: --check LH505), sem salvar estado nem mandar WhatsApp. Útil pra "
                              "testar rapidinho se a Aviationstack tem dado de um voo agora.")
    args = parser.parse_args()

    if args.test_whatsapp:
        send_whatsapp("Teste do flight_watch.py: se você recebeu isso, o CallMeBot está funcionando.")
        return

    if args.check:
        raw = get_flight_status(args.check, None)
        status = summarize_status(raw)
        if status is None:
            print(f"[{args.check}] Sem dado em tempo real agora (provavelmente não está "
                  f"operando hoje, ou fora da janela real-time da Aviationstack).")
        else:
            print(json.dumps(status, ensure_ascii=False, indent=2))
        return

    trip = TEST_TRIP if args.test else TRIP
    state_file = os.path.join(os.path.dirname(__file__), "flight_watch_state.test.json") if args.test else STATE_FILE

    if args.adaptive:
        print("Rodando em loop ADAPTATIVO (intervalo varia conforme a proximidade do voo). Ctrl+C para parar.")
        while True:
            try:
                statuses = run_once(trip, state_file)
                sleep_s = seconds_until_next_check(trip, statuses)
            except Exception as e:
                print(f"[ERRO] {e}")
                sleep_s = 900  # fallback se algo falhar na checagem
            print(f"[loop adaptativo] próxima checagem em {sleep_s}s (~{sleep_s // 60}min)")
            time.sleep(sleep_s)
    elif args.loop:
        print(f"Rodando em loop a cada {args.loop}s. Ctrl+C para parar.")
        while True:
            try:
                run_once(trip, state_file)
            except Exception as e:
                print(f"[ERRO] {e}")
            time.sleep(args.loop)
    else:
        run_once(trip, state_file)


if __name__ == "__main__":
    main()
