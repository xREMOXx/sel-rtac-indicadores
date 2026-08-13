"""
Indicador de aberturas, com drill-down: ano -> mes -> equipamentos que
atuaram naquele mes (hora da abertura + duracao ate o fechamento seguinte).

So GET, sem sessao. Enumera os equipamentos (religador/disjuntor/chave) via
/api/v1/alarms com filtro de Category, depois busca o historico COMPLETO
(ABERTO e FECHADO, sem filtro) de cada um via
/api/v1/alarms/{TagName}/history -- precisa dos dois estados pra parear
cada abertura com o fechamento que veio depois e calcular a duracao.

Uso:
    python historico_aberturas.py
    abra http://localhost:8422 no navegador
"""

import base64
import html
import json
import logging
import os
import pathlib
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dotenv

# sel_rtac_scraper.py fica na RAIZ do projeto, um nivel acima desta pasta:
# e compartilhado com os outros scripts (live_138kv, eventos_abertura), entao
# nao tem copia dele aqui.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import sel_rtac_scraper as s

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("historico_aberturas")

# Onde procurar logo.png e nomes_equipamento.json. Congelado pelo PyInstaller,
# __file__ aponta pra pasta temporaria de extracao, que some a cada execucao --
# os arquivos de cada instalacao ficam ao lado do .exe, nao dentro dele.
_BASE = (pathlib.Path(sys.executable).parent if getattr(sys, "frozen", False)
         else pathlib.Path(__file__).resolve().parent)

# O load_dotenv() sem argumento do scraper resolve o .env a partir do
# diretorio ATUAL, subindo. Roda a partir de outra pasta (ou como servico, onde
# o diretorio atual e o system32) e ele nao acha nada: o load_config sai com
# "Faltando no .env" e parece credencial errada, nao caminho errado. Apontar o
# arquivo explicitamente tira a dependencia de onde o processo foi iniciado.
# Aqui ao lado tem prioridade sobre a raiz, que e o .env compartilhado com os
# outros scripts do projeto.
for _env in (_BASE / ".env", _BASE.parent / ".env"):
    if _env.is_file():
        dotenv.load_dotenv(_env)
        break

PORT = 8422
POLL_SECONDS = 60  # dado historico nao muda rapido, sem motivo pra bater toda hora

# "0.0.0.0" publica em todas as interfaces -- decisao da operacao, para o painel
# ser aberto das outras maquinas. Nao ha autenticacao nenhuma nesse servidor:
# quem alcanca a porta ve o inventario da subestacao e o historico completo de
# interrupcoes. Quem limita o alcance e a regra de firewall, restrita a sub-rede
# da operacao -- sem ela, ou o Windows bloqueia tudo, ou libera pra rede inteira.
#
# Nao vale bindar o IP fixo da placa: quebra o acesso por localhost e morre no
# dia que o DHCP trocar o endereco.
#
# Voltar pra "127.0.0.1" restringe o painel a esta maquina.
BIND_HOST = "0.0.0.0"


def _lan_ip() -> str:
    """IP da interface que sai pra rede. Nao abre conexao de verdade (UDP nao
    faz handshake); so faz o SO escolher a rota e revelar o IP de origem --
    socket.gethostbyname(hostname) devolve 127.0.0.1 em varias maquinas."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()

EQUIPMENT_FILTER = "Category=='*RELIGADOR*',Category=='*DISJUNTOR*',Category=='*CHAVE*'"

# Apelido operacional de cada equipamento. O Category do alarme e o Comment do
# dicionario de tags so repetem o codigo ("RL-01 RELIGADOR"); o nome do
# alimentador so existe no projeto de HMI, nos DiagramTitle do formato
# "RL-01 (5009) - NOME DO ALIMENTADOR". Extraido de
# GET /api/v1/hmi/projects/<SEU_PROJETO_HMI> (JSON com o XML do HMI em LZMA
# alone dentro do campo Contents, base64); GET /api/v1/hmi/projects lista os
# nomes de projeto disponiveis no seu RTAC.
#
# O cadastro e de cada instalacao, entao vive fora do codigo e nao e
# versionado: copie nomes_equipamento.exemplo.json pra nomes_equipamento.json e
# preencha. Chave = codigo como aparece no inicio do Category.
#
# Sem o arquivo, ou para um equipamento que nao esteja nele, o painel mostra so
# o codigo -- e o que ja acontece com os que nao tem nome no HMI (chaves de
# interligacao, bancos de capacitor e os disjuntores da subestacao), sem
# quebrar nada.
def _carrega_nomes() -> dict:
    caminho = _BASE / "nomes_equipamento.json"
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


NOMES_EQUIPAMENTO = _carrega_nomes()

state = {"anos": {}, "tags": {}, "fetched_at": None, "error": None}
state_lock = threading.Lock()


def _paged_get(cfg, path, extra_filter=None):
    items = []
    offset = 0
    while True:
        params = {"offset": offset, "limit": 200}
        if extra_filter:
            params["filter"] = extra_filter
        r = s.api_get(cfg, path, params=params)
        page = r.json()
        items.extend(page)
        total = int(r.headers.get("Pagination-Total", len(items)))
        offset += len(page)
        if not page or offset >= total:
            break
    return items


def _pair_open_close(historico: list) -> list:
    """Ordena por timestamp e pareia cada transicao real de abertura com o
    fechamento seguinte do mesmo tag.

    O historico de alarme tem uma entrada por MUDANCA DE ESTADO DO REGISTRO,
    nao so por transicao fisica do ponto -- reconhecer (acknowledge) um
    alarme gera uma entrada nova com o mesmo Message ("ABERTO" continua
    "ABERTO" depois de reconhecido) e o mesmo EventId, so troca EventType
    pra "Acknowledged". Usar Message pra detectar abertura conta esse
    reconhecimento como uma abertura nova. EventType e o campo certo:
    "Alarmed" = o ponto realmente saiu do estado normal agora;
    "Normalized" = realmente voltou. Acknowledged/Unacknowledged/Comment sao
    so anotacoes sobre o MESMO evento e ficam de fora do pareamento.

    "Alarmed" repetido enquanto o ponto ja esta aberto e IGNORADO, nao abre um
    par novo. Fisicamente o religador precisa fechar pra poder abrir de novo,
    entao dois "Alarmed" seguidos sem "Normalized" no meio sao o mesmo evento
    registrado duas vezes, nao duas atuacoes. Caso real: RL-01 em 2026-07-02
    gravou Alarmed 01:58:18, Alarmed 01:58:20, Normalized 01:58:22, Normalized
    01:58:23 -- um ciclo de religamento de 4s. Fechar o primeiro "Alarmed" como
    orfao inventava uma abertura a mais E uma pendencia de fechamento num
    religador que estava fechado. "Normalized" repetido ja era ignorado pelo
    mesmo motivo (o historico tem 124 Normalized pra 8 Alarmed nesse tag).

    So a ultima abertura sem nenhum "Normalized" depois dela fica com
    fechamento None -- essa sim e um ponto realmente aberto agora."""
    events = sorted(historico, key=lambda e: e["Timestamp"])
    pairs = []
    open_ts = None
    for ev in events:
        event_type = ev.get("EventType")
        if event_type == "Alarmed":
            if open_ts is None:
                open_ts = ev["Timestamp"]
        elif event_type == "Normalized" and open_ts is not None:
            pairs.append({"abertura": open_ts, "fechamento": ev["Timestamp"]})
            open_ts = None
    if open_ts is not None:
        pairs.append({"abertura": open_ts, "fechamento": None})
    return pairs


def poll_loop(cfg: dict) -> None:
    while True:
        try:
            equipment = _paged_get(cfg, "/api/v1/alarms", EQUIPMENT_FILTER)
            anos = {}
            tags = {}
            total_aberturas = 0
            for eq in equipment:
                tag = eq["TagName"]
                label = eq["Category"]
                tags[label] = tag
                historico = _paged_get(cfg, f"/api/v1/alarms/{tag}/history")
                for pair in _pair_open_close(historico):
                    total_aberturas += 1
                    ano = pair["abertura"][:4]
                    mes = pair["abertura"][:7]
                    duracao_seg = None
                    if pair["fechamento"]:
                        t0 = time.strptime(pair["abertura"][:19], "%Y-%m-%dT%H:%M:%S")
                        t1 = time.strptime(pair["fechamento"][:19], "%Y-%m-%dT%H:%M:%S")
                        duracao_seg = int(time.mktime(t1) - time.mktime(t0))
                    ano_node = anos.setdefault(ano, {"meses": {}})
                    mes_node = ano_node["meses"].setdefault(mes, {"equipamentos": {}})
                    eq_list = mes_node["equipamentos"].setdefault(label, [])
                    eq_list.append({
                        "abertura": pair["abertura"],
                        "fechamento": pair["fechamento"],
                        "duracao_seg": duracao_seg,
                    })
            with state_lock:
                state["anos"] = anos
                state["tags"] = tags
                state["fetched_at"] = time.time() * 1000
                state["error"] = None
            log.info("%d equipamentos, %d aberturas no total", len(equipment), total_aberturas)
        except Exception as exc:
            with state_lock:
                state["error"] = str(exc)
            log.error("falha no polling: %s", exc)
        time.sleep(POLL_SECONDS)


# Logo opcional. Ponha um logo.png ao lado deste script e ele vai embutido na
# pagina como data URI -- a pagina fica autocontida, sem arquivo externo nem
# request pra internet a partir da rede do RTAC. Sem o arquivo o painel sobe
# sem marca e o resto funciona igual. Identidade visual e de cada
# distribuidora, entao o arquivo nao e versionado.
_LOGO_PATH = _BASE / "logo.png"
LOGO_B64 = (base64.b64encode(_LOGO_PATH.read_bytes()).decode()
            if _LOGO_PATH.exists() else "")

# Titulo da aba e alt do logo. Cada instalacao poe o seu.
TITULO = os.getenv("PAINEL_TITULO", "Aberturas por equipamento")


PAGE = """<!doctype html>
<html lang="pt-br">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITULO__</title>
__FAVICON__
<style>
  /* Paleta neutra de partida. Troque os tokens de marca (--brand, --brand-mid,
     --brand-deep, --lime) pelas cores da sua distribuidora; os de dado e de
     texto ja passam contraste e nao precisam mexer.

     A --lime fica FORA das series de dados de proposito, e a regra vale pra
     qualquer marca que voce ponha no lugar: contra o verde da marca ela nao
     alcanca o piso de separacao pra visao normal, e contra o laranja quebra no
     daltonismo (deutan). E cor de marca -- header, detalhe, status. As series
     1-3 (verde / laranja / azul, nessa ordem) passam o validador de CVD nos
     dois modos; nao trocar sem revalidar. */
  :root {
    color-scheme: light;
    --page:           #edecec;
    --surface-1:      #ffffff;
    --surface-2:      #f4f7f4;
    --brand:          #005041;
    --brand-mid:      #006855;
    --brand-deep:     #084136;
    --lime:           #b3c52d;
    --on-brand:       #ffffff;
    --link:           #006855;
    --text-primary:   #212529;
    --text-secondary: #4a5551;
    --muted:          #8a938f;
    --border:         #d8ded9;
    --track:          #e4e9e4;
    --series-1:       #0e8c6a;
    --series-2:       #d9480f;
    --series-3:       #2a78d6;
    --critical:       #c92a2a;
    --shadow:         0 1px 2px rgba(8,65,54,0.06), 0 6px 18px rgba(8,65,54,0.07);
    --heat-1: #fff7bc; --heat-2: #fed976; --heat-3: #fd8d3c; --heat-4: #e31a1c; --heat-5: #800026;
    --heat-ink-1: #212529; --heat-ink-2: #212529; --heat-ink-3: #212529;
    --heat-ink-4: #ffffff; --heat-ink-5: #ffffff;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --page:           #0e1512;
      --surface-1:      #16211c;
      --surface-2:      #1e2b25;
      --brand:          #12866d;
      --brand-mid:      #006855;
      --brand-deep:     #05261f;
      --lime:           #b3c52d;
      --on-brand:       #ffffff;
      --link:           #4fd0a8;
      --text-primary:   #ffffff;
      --text-secondary: #b3c1bb;
      --muted:          #7d8b85;
      --border:         rgba(255,255,255,0.10);
      --track:          #26332d;
      --series-1:       #25a886;
      --series-2:       #e86a2e;
      --series-3:       #4a8ce8;
      --critical:       #e66767;
      --shadow:         0 1px 2px rgba(0,0,0,0.4), 0 6px 18px rgba(0,0,0,0.35);
      --heat-1: #5c4310; --heat-2: #946a16; --heat-3: #c79020; --heat-4: #da6326; --heat-5: #d02b35;
      --heat-ink-1: #ffffff; --heat-ink-2: #ffffff; --heat-ink-3: #1a1a1a;
      --heat-ink-4: #1a1a1a; --heat-ink-5: #ffffff;
    }
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --page:           #0e1512;
    --surface-1:      #16211c;
    --surface-2:      #1e2b25;
    --brand:          #12866d;
    --brand-mid:      #006855;
    --brand-deep:     #05261f;
    --lime:           #b3c52d;
    --on-brand:       #ffffff;
    --link:           #4fd0a8;
    --text-primary:   #ffffff;
    --text-secondary: #b3c1bb;
    --muted:          #7d8b85;
    --border:         rgba(255,255,255,0.10);
    --track:          #26332d;
    --series-1:       #25a886;
    --series-2:       #e86a2e;
    --series-3:       #4a8ce8;
    --critical:       #e66767;
    --shadow:         0 1px 2px rgba(0,0,0,0.4), 0 6px 18px rgba(0,0,0,0.35);
    --heat-1: #5c4310; --heat-2: #946a16; --heat-3: #c79020; --heat-4: #da6326; --heat-5: #d02b35;
    --heat-ink-1: #ffffff; --heat-ink-2: #ffffff; --heat-ink-3: #1a1a1a;
    --heat-ink-4: #1a1a1a; --heat-ink-5: #ffffff;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--page); color: var(--text-primary);
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         -webkit-font-smoothing: antialiased; }

  .topbar { background: linear-gradient(100deg, var(--brand-deep) 0%, var(--brand-mid) 100%);
            color: var(--on-brand); border-bottom: 3px solid var(--lime); }
  .topbar .inner { max-width: 1120px; margin: 0 auto; padding: 18px 20px 20px;
                   display: flex; align-items: center; justify-content: space-between; gap: 18px; flex-wrap: wrap; }
  .topbar .brand { display: flex; align-items: center; gap: 16px; min-width: 0; }
  .topbar .logo { height: 40px; width: auto; flex-shrink: 0; }
  .topbar .rule { width: 1px; align-self: stretch; background: rgba(255,255,255,0.22); }
  .topbar h1 { font-size: 20px; font-weight: 650; margin: 0 0 3px; letter-spacing: -0.01em; }
  .topbar .sub { font-size: 13px; margin: 0; opacity: 0.82; }
  .status { display: flex; align-items: center; gap: 7px; font-size: 12px;
            background: rgba(255,255,255,0.12); border-radius: 999px; padding: 5px 11px; white-space: nowrap; }
  .status .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--lime); box-shadow: 0 0 0 3px rgba(179,197,45,0.25); }
  .status.stale .dot { background: #ffd166; box-shadow: 0 0 0 3px rgba(255,209,102,0.25); }
  @media (max-width: 560px) { .topbar .rule, .topbar .sub { display: none; } }

  .wrap { max-width: 1120px; margin: 0 auto; padding: 18px 20px 48px; }

  .crumbs { font-size: 13px; color: var(--text-secondary); margin: 0 0 16px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .crumbs a { color: var(--link); text-decoration: none; cursor: pointer; font-weight: 550; }
  .crumbs a:hover { text-decoration: underline; }
  .crumbs .sep { color: var(--muted); }
  .crumbs .here { font-weight: 600; color: var(--text-primary); }
  .back { display: inline-flex; align-items: center; gap: 6px; margin-right: 4px; padding: 5px 12px 5px 9px;
          border: 1px solid var(--border); border-radius: 999px; background: var(--surface-1);
          color: var(--link); font-size: 13px; font-weight: 550; cursor: pointer; font-family: inherit; }
  .back:hover { background: var(--surface-2); }
  .back .arrow { font-size: 14px; line-height: 1; }

  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(186px, 1fr)); gap: 12px; margin-bottom: 16px; }
  .kpi { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
         padding: 14px 16px 15px; box-shadow: var(--shadow); position: relative; overflow: hidden; }
  .kpi::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
                 background: linear-gradient(var(--brand-mid), var(--lime)); }
  .kpi.alert::before { background: var(--critical); }
  .kpi .k-label { font-size: 11px; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase; color: var(--muted); margin-bottom: 8px; }
  .kpi .k-value { font-size: 30px; font-weight: 650; line-height: 1.05; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
  .kpi.alert .k-value { color: var(--critical); }
  .kpi .k-foot { font-size: 12px; color: var(--text-secondary); margin-top: 6px; }

  .grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
  @media (min-width: 900px) { .grid.split { grid-template-columns: 1.35fr 1fr; align-items: start; } }

  .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 12px;
          box-shadow: var(--shadow); padding: 16px 18px 18px; }
  .card h2 { font-size: 13px; font-weight: 650; margin: 0 0 2px; letter-spacing: -0.005em; }
  .card .hint { font-size: 12px; color: var(--muted); margin: 0 0 14px; }

  .barrow { display: flex; align-items: center; gap: 12px; padding: 7px 8px; border-radius: 8px; }
  .barrow.clickable { cursor: pointer; }
  .barrow.clickable:hover { background: var(--surface-2); }
  .barrow .label { width: 42%; max-width: 300px; flex-shrink: 0; font-size: 13px; font-weight: 550;
                   overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .label .cod { font-weight: 650; font-variant-numeric: tabular-nums; }
  .label .apelido { color: var(--text-secondary); font-weight: 450; margin-left: 7px; }
  .barrow .track { flex: 1; background: var(--track); border-radius: 4px; height: 14px; }
  .barrow .fill { background: var(--series-1); height: 100%; border-radius: 0 4px 4px 0; min-width: 3px; }
  .barrow .count { width: 42px; text-align: right; font-variant-numeric: tabular-nums; font-size: 13px; font-weight: 600; }
  .barrow .chev { color: var(--muted); font-size: 12px; width: 10px; }

  .trend { display: flex; align-items: flex-end; gap: 4px; height: 140px; padding: 0 2px; }
  .trend .col { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; height: 100%; cursor: default; }
  .trend .col .bar { background: var(--series-1); border-radius: 4px 4px 0 0; min-height: 2px; }
  .trend .col.zero .bar { background: var(--track); }
  .trend .col:hover .bar { filter: brightness(1.12); }
  .trend-x { display: flex; gap: 4px; margin-top: 6px; }
  .trend-x span { flex: 1; text-align: center; font-size: 10px; color: var(--muted); }

  .stack { display: flex; height: 16px; border-radius: 4px; overflow: hidden; gap: 2px; background: var(--track); margin-bottom: 14px; }
  .stack .seg { height: 100%; }
  .legend { display: flex; flex-direction: column; gap: 9px; }
  .legend .item { display: flex; align-items: center; gap: 9px; font-size: 13px; }
  .legend .sw { width: 10px; height: 10px; border-radius: 3px; flex-shrink: 0; }
  .legend .nm { flex: 1; color: var(--text-secondary); }
  .legend .n { font-variant-numeric: tabular-nums; font-weight: 600; }
  .legend .pc { font-variant-numeric: tabular-nums; color: var(--muted); width: 42px; text-align: right; }

  .calendar { display: grid; gap: 12px; }
  .calendar .weekday-row,
  .calendar .days { display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 6px; }
  .calendar .weekday-row span { text-align: center; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
  .cal-day { min-height: 70px; border-radius: 12px; border: 1px solid var(--border); background: var(--surface-2); padding: 10px 10px 8px; display: grid; gap: 6px; cursor: pointer; text-align: left; color: var(--text-primary); }
  .cal-day:hover:not(.empty) { border-color: var(--brand-mid); box-shadow: inset 0 0 0 1px rgba(29, 105, 84, 0.12); }
  .cal-day.empty { background: transparent; border-color: transparent; cursor: default; }
  .cal-day .cal-num { font-size: 13px; font-weight: 700; line-height: 1; }
  .cal-day .cal-count { margin-top: auto; font-size: 11px; color: var(--text-secondary); justify-self: end; }
  /* Rampa de calor por numero de aberturas no dia. Sequencial YlOrRd: claro ->
     escuro no tema claro, escuro -> saturado no escuro. A tinta de cada degrau
     foi escolhida pra ficar >= 4.5:1 sobre o proprio fundo (texto de 11px). */
  .cal-day.h1 { background: var(--heat-1); border-color: transparent; color: var(--heat-ink-1); }
  .cal-day.h2 { background: var(--heat-2); border-color: transparent; color: var(--heat-ink-2); }
  .cal-day.h3 { background: var(--heat-3); border-color: transparent; color: var(--heat-ink-3); }
  .cal-day.h4 { background: var(--heat-4); border-color: transparent; color: var(--heat-ink-4); }
  .cal-day.h5 { background: var(--heat-5); border-color: transparent; color: var(--heat-ink-5); }
  .cal-day[class*="h"] .cal-count { color: inherit; opacity: 0.82; }
  .cal-day.selected { outline: 2px solid var(--text-primary); outline-offset: -2px; }
  .heat-legend { display: flex; align-items: center; gap: 8px; margin-top: 12px; font-size: 11px; color: var(--muted); }
  .heat-legend .chip { width: 26px; height: 12px; border-radius: 3px; }
  .cal-note { margin-top: 10px; font-size: 12px; color: var(--text-secondary); display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .cal-note button { border: none; background: transparent; color: var(--link); cursor: pointer; padding: 0; font: inherit; }

  .eqrow { border-bottom: 1px solid var(--border); }
  .eqrow:last-child { border-bottom: none; }
  .events { padding: 2px 8px 12px 12px; }
  .ev { display: flex; align-items: baseline; justify-content: space-between; gap: 12px;
        padding: 6px 8px; font-size: 12.5px; font-variant-numeric: tabular-nums; border-radius: 6px; }
  .ev:hover { background: var(--surface-2); }
  .ev .dur { color: var(--text-secondary); }
  .ev .semfech { color: var(--critical); font-weight: 550; }

  .empty { color: var(--muted); font-size: 13px; padding: 28px; text-align: center; }
  .hint code { font-family: ui-monospace, "Cascadia Mono", Consolas, monospace; font-size: 0.94em; }
  .err { background: rgba(201,42,42,0.08); border: 1px solid var(--critical); color: var(--critical);
         font-size: 13px; padding: 12px 14px; border-radius: 10px; margin-bottom: 16px; }

  .tooltip { position: fixed; pointer-events: none; display: none; z-index: 10;
             background: var(--surface-1); border: 1px solid var(--border); border-radius: 8px;
             padding: 8px 10px; font-size: 12px; color: var(--text-primary); box-shadow: var(--shadow); }
  .tooltip .t-label { color: var(--text-secondary); margin-bottom: 3px; }
  .tooltip .t-value { font-weight: 650; font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<div class="viz-root">
  <header class="topbar">
    <div class="inner">
      <div class="brand">
        __LOGO_IMG__
        <div>
          <h1>Atuação de equipamentos da rede de distribuição</h1>
          <p class="sub">Religadores, disjuntores e chaves da distribuição. Aberturas registradas e tempo até o restabelecimento.</p>
        </div>
      </div>
      <div class="status" id="status"><span class="dot"></span><span id="status-txt">carregando...</span></div>
    </div>
  </header>
  <div class="wrap">
    <div id="errbox"></div>
    <div class="crumbs" id="crumbs"></div>
    <div class="kpis" id="kpis"></div>
    <div id="content"></div>
  </div>
  <div class="tooltip" id="tooltip"></div>
</div>
<script>
const MESES_PT = ["", "Janeiro", "Fevereiro", "Marco", "Abril", "Maio", "Junho",
                  "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro"];
const MESES_ABBR = ["", "jan", "fev", "mar", "abr", "mai", "jun",
                    "jul", "ago", "set", "out", "nov", "dez"];
// Category vem como texto livre do RTAC; a ordem importa, primeiro que bater vence.
// Ordem = ordem de render na barra empilhada e na legenda; as series foram
// validadas nessa adjacencia (verde -> laranja -> azul).
const TIPOS = [
  {nome: "Religador", re: /RELIGADOR/i, cssVar: "--series-1"},
  {nome: "Disjuntor", re: /DISJUNTOR/i, cssVar: "--series-2"},
  {nome: "Chave",     re: /CHAVE/i,     cssVar: "--series-3"},
  {nome: "Outro",     re: /.?/,         cssVar: "--muted"},
];

let DATA = null;
let EVENTS = [];       // flat: {ano, mes, equip, tipo, abertura, fechamento, duracao_seg}
let path = [];         // [] = anos; [ano] = meses; [ano, mesKey] = equipamentos
let expanded = new Set();
let selectedDay = null;

// O drill-down vive no hash da URL, entao o botao Voltar do navegador, o
// Alt+Seta e o F5 funcionam sem estado paralelo pra sincronizar.
function hashPath() {
  const h = decodeURIComponent(location.hash.slice(1));
  return h ? h.split("/").slice(0, 2) : [];
}

function go(p) {
  path = p;
  expanded.clear();
  selectedDay = null;
  const h = "#" + p.join("/");
  if (h !== location.hash) history.pushState(null, "", h || location.pathname);
  render();
}

addEventListener("popstate", () => { path = hashPath(); expanded.clear(); render(); });
addEventListener("keydown", e => {
  if (e.key === "Escape" && path.length) history.back();
});

const esc = s => String(s).replace(/[&<>"']/g, c =>
  ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));

function tipoDe(categoria) { return TIPOS.find(t => t.re.test(categoria)) || TIPOS[TIPOS.length - 1]; }

// "RL-01 RELIGADOR" -> codigo RL-01 | "AT TT-2 DISJUNTOR DJ532" -> codigo
// "AT TT-2", complemento "DJ532".
//
// O apelido SO vem do NOMES_EQUIPAMENTO. O complemento do Category nao serve
// de nome: em "BC-02 CHAVE VACUO" o "VACUO" e o tipo construtivo da chave, nao
// o nome dela, e mostrar "BC-02 VACUO" da a entender que e um alimentador
// chamado Vacuo. Sem entrada no cadastro, aparece so o codigo; o Category
// inteiro e a tag continuam no title, ao passar o mouse.
function identidade(categoria) {
  const m = categoria.match(/\\b(RELIGADOR|DISJUNTOR|CHAVE)\\b/i);
  const codigo = (m ? categoria.slice(0, m.index) : categoria).trim() || categoria;
  const complemento = m ? categoria.slice(m.index + m[0].length).trim() : "";
  return {codigo, apelido: (DATA.nomes || {})[codigo] || "", complemento,
          tag: (DATA.tags || {})[categoria] || ""};
}

// Rotulo de duas partes: codigo em destaque + apelido em texto secundario.
function rotulo(categoria) {
  const id = identidade(categoria);
  return `<span class="cod">${esc(id.codigo)}</span>` +
         (id.apelido ? `<span class="apelido">${esc(id.apelido)}</span>` : "");
}

function tituloEquip(categoria) {
  const id = identidade(categoria);
  return esc(`${categoria}${id.tag ? " | tag " + id.tag : ""}`);
}
function mesLabel(mesKey) { return MESES_PT[+mesKey.slice(5, 7)] || mesKey; }

function fmtDuracao(seg) {
  if (seg == null) return null;
  if (seg < 60) return `${seg}s`;
  const m = Math.floor(seg / 60), s = seg % 60;
  if (m < 60) return `${m}min ${s}s`;
  const h = Math.floor(m / 60), mm = m % 60;
  if (h < 24) return `${h}h ${mm}min`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

function flatten(anos) {
  const out = [];
  for (const [ano, anoNode] of Object.entries(anos))
    for (const [mes, mesNode] of Object.entries(anoNode.meses))
      for (const [equip, evs] of Object.entries(mesNode.equipamentos))
        for (const ev of evs)
          out.push({ano, mes, equip, tipo: tipoDe(equip), ...ev});
  return out;
}

function scoped() {
  if (path.length === 0) return EVENTS;
  if (path.length === 1) return EVENTS.filter(e => e.ano === path[0]);
  return EVENTS.filter(e => e.mes === path[1]);
}

function countBy(evs, keyFn) {
  const m = new Map();
  for (const e of evs) { const k = keyFn(e); m.set(k, (m.get(k) || 0) + 1); }
  return m;
}

async function tick() {
  try {
    const r = await fetch("/data.json", {cache: "no-store"});
    DATA = await r.json();
  } catch (e) { return; }
  EVENTS = flatten(DATA.anos || {});
  render();
}

// ---------- blocos ----------

function bars(items, {clickable, colorOf} = {}) {
  const max = Math.max(...items.map(i => i.value), 1);
  return items.map(i => `
    <div class="barrow ${clickable ? "clickable" : ""}" ${i.onclick ? `onclick="${i.onclick}"` : ""}>
      <div class="label" title="${i.title || esc(i.label)}">${i.html || esc(i.label)}</div>
      <div class="track"><div class="fill" style="width:${i.value / max * 100}%${colorOf ? `;background:var(${colorOf(i)})` : ""}"></div></div>
      <div class="count">${i.value}</div>
      ${clickable ? '<div class="chev">&rsaquo;</div>' : ""}
    </div>`).join("");
}

function cardKPIs(evs) {
  const abertos = evs.filter(e => e.fechamento == null).length;
  const duracoes = evs.map(e => e.duracao_seg).filter(d => d != null);
  const media = duracoes.length ? Math.round(duracoes.reduce((a, b) => a + b, 0) / duracoes.length) : null;
  // Tempo acumulado em aberto. Na visao inicial o escopo e o ANO VIGENTE, nao
  // todo o historico: acumulado de varios anos somados nao serve de indicador.
  // Nos niveis de drill acompanha o periodo selecionado, como os outros cards.
  const anoVigente = String(new Date().getFullYear());
  const evsAcum = path.length === 0 ? EVENTS.filter(e => e.ano === anoVigente) : evs;
  const escopoAcum = path.length === 0 ? `ano vigente (${anoVigente})`
    : path.length === 1 ? `exercício de ${path[0]}` : `${mesLabel(path[1])} de ${path[0]}`;
  const acumulado = evsAcum.reduce((soma, e) => soma + (e.duracao_seg || 0), 0);

  const tiles = [
    {label: "Aberturas registradas", value: evs.length, foot: path.length === 0 ? "todo o histórico disponível" :
      path.length === 1 ? `exercício de ${path[0]}` : `${mesLabel(path[1])} de ${path[0]}`},
    {label: "Tempo acumulado em aberto", value: acumulado ? fmtDuracao(acumulado) : "--",
     foot: `${escopoAcum}, somando ${evsAcum.length} ocorrência${evsAcum.length === 1 ? "" : "s"}`},
    {label: "Tempo médio de restabelecimento", value: media == null ? "--" : fmtDuracao(media),
     foot: `apurado sobre ${duracoes.length} de ${evs.length} ocorrências`},
    {label: "Pendentes de fechamento", value: abertos, alert: abertos > 0,
     foot: abertos ? "sem retorno ao estado normal" : "todas normalizadas"},
  ];
  return tiles.map(t => `
    <div class="kpi ${t.alert ? "alert" : ""}">
      <div class="k-label">${t.label}</div>
      <div class="k-value">${t.value}</div>
      <div class="k-foot">${t.foot}</div>
    </div>`).join("");
}

function cardTipos(evs) {
  const porTipo = TIPOS.map(t => ({t, n: evs.filter(e => e.tipo === t).length})).filter(x => x.n > 0);
  const total = evs.length || 1;
  if (!porTipo.length) return "";
  return `<div class="card">
    <h2>Composição por classe de equipamento</h2>
    <p class="hint">Classificação derivada do campo <code>Category</code> do alarme no RTAC.</p>
    <div class="stack">
      ${porTipo.map(x => `<div class="seg" style="width:${x.n / total * 100}%;background:var(${x.t.cssVar})"
         data-tip="${x.t.nome}" data-tipval="${x.n} aberturas (${Math.round(x.n / total * 100)}%)"></div>`).join("")}
    </div>
    <div class="legend">
      ${porTipo.map(x => `<div class="item">
        <span class="sw" style="background:var(${x.t.cssVar})"></span>
        <span class="nm">${x.t.nome}</span>
        <span class="n">${x.n}</span>
        <span class="pc">${Math.round(x.n / total * 100)}%</span>
      </div>`).join("")}
    </div>
  </div>`;
}

function cardTopEquip(evs) {
  const top = [...countBy(evs, e => e.equip).entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 8);
  if (!top.length) return "";
  const byName = new Map(evs.map(e => [e.equip, e.tipo]));
  return `<div class="card">
    <h2>Equipamentos com maior número de atuações</h2>
    <p class="hint">Top ${top.length} no período selecionado. Passe o mouse para ver a tag no RTAC.</p>
    ${bars(top.map(([nome, n]) => ({html: rotulo(nome), title: tituloEquip(nome),
                                    value: n, tipo: byName.get(nome)})),
           {colorOf: i => i.tipo.cssVar})}
  </div>`;
}

function cardTrend12() {
  const hoje = new Date();
  const keys = [];
  for (let i = 11; i >= 0; i--) {
    const d = new Date(hoje.getFullYear(), hoje.getMonth() - i, 1);
    keys.push({k: `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`, m: d.getMonth() + 1, y: d.getFullYear()});
  }
  const counts = countBy(EVENTS, e => e.mes);
  const vals = keys.map(x => counts.get(x.k) || 0);
  const max = Math.max(...vals, 1);
  return `<div class="card">
    <h2>Evolução nos últimos 12 meses</h2>
    <p class="hint">Aberturas por mês de competência. Clique na coluna para detalhar o mês.</p>
    <div class="trend">
      ${keys.map((x, i) => `<div class="col ${vals[i] ? "" : "zero"}" style="cursor:${vals[i] ? "pointer" : "default"}"
          ${vals[i] ? `onclick="go(['${x.y}','${x.k}'])"` : ""}
          data-tip="${MESES_PT[x.m]} ${x.y}" data-tipval="${vals[i]} abertura${vals[i] === 1 ? "" : "s"}">
        <div class="bar" style="height:${vals[i] / max * 100}%"></div>
      </div>`).join("")}
    </div>
    <div class="trend-x">${keys.map(x => `<span>${MESES_ABBR[x.m]}</span>`).join("")}</div>
  </div>`;
}

// Degrau de calor por contagem ABSOLUTA, nao relativa ao pico do mes: assim a
// mesma cor significa a mesma coisa em qualquer mes, e um mes calmo nao pinta
// de vermelho o dia que teve duas aberturas.
const HEAT_LABELS = ["1", "2", "3", "4", "5 ou mais"];
function heatClass(n) { return n ? " h" + Math.min(n, 5) : ""; }

function cardCalendarMonth(evs, year, mesKey) {
  const [y, m] = mesKey.split("-").map(Number);
  const first = new Date(y, m - 1, 1);
  const daysInMonth = new Date(y, m, 0).getDate();
  const startDow = (first.getDay() + 6) % 7;
  const counts = countBy(evs, e => e.abertura.slice(0, 10));
  const weekdayLabels = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"];
  const days = [];
  for (let i = 0; i < startDow; i++) days.push(`<span class="cal-day empty"></span>`);
  for (let day = 1; day <= daysInMonth; day++) {
    const dayKey = `${year}-${String(m).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
    const count = counts.get(dayKey) || 0;
    days.push(`<button type="button" class="cal-day${selectedDay === dayKey ? " selected" : ""}${heatClass(count)}" onclick="selectDay('${dayKey}')">
      <span class="cal-num">${day}</span>
      ${count ? `<span class="cal-count">${count} abertura${count === 1 ? "" : "s"}</span>` : ""}
    </button>`);
  }
  const extra = (7 - days.length % 7) % 7;
  for (let i = 0; i < extra; i++) days.push(`<span class="cal-day empty"></span>`);
  return `<div class="card">
    <h2>Calendário de ${mesLabel(mesKey)} de ${year}</h2>
    <p class="hint">Clique no dia para filtrar as ocorrências do mês.</p>
    <div class="calendar">
      <div class="weekday-row">${weekdayLabels.map(d => `<span>${d}</span>`).join("")}</div>
      <div class="days">${days.join("")}</div>
    </div>
    <div class="heat-legend">
      <span>Aberturas no dia:</span>
      ${HEAT_LABELS.map((rot, i) => `<span class="chip" style="background:var(--heat-${i + 1})"></span><span>${rot}</span>`).join("")}
    </div>
    ${selectedDay ? `<div class="cal-note">Mostrando ${counts.get(selectedDay) || 0} abertura${(counts.get(selectedDay) || 0) === 1 ? "" : "s"} em ${selectedDay.slice(8)}. <button type="button" onclick="selectDay(null)">Ver todos</button></div>` : ""}
  </div>`;
}

function selectDay(day) {
  selectedDay = selectedDay === day ? null : day;
  render();
}

function cardEventos(evs) {
  const grupos = [...countBy(evs, e => e.equip).entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  const emptyText = selectedDay
    ? `Nenhuma abertura registrada em ${selectedDay}.`
    : `Nenhum equipamento atuou no mês.`;
  if (!grupos.length) return `<div class="card"><div class="empty">${emptyText}</div></div>`;
  const max = grupos[0][1];
  return `<div class="card">
    <h2>Detalhamento por equipamento</h2>
    <p class="hint">Clique no equipamento para ver data, hora e duração de cada ocorrência.</p>
    ${grupos.map(([nome, n]) => {
      const aberto = expanded.has(nome);
      const tipo = evs.find(e => e.equip === nome).tipo;
      const lista = evs.filter(e => e.equip === nome)
        .sort((a, b) => b.abertura.localeCompare(a.abertura))
        .map(ev => {
          const dur = fmtDuracao(ev.duracao_seg);
          return `<div class="ev">
            <span>${new Date(ev.abertura).toLocaleString("pt-BR")}</span>
            ${dur ? `<span class="dur">${dur} até o restabelecimento</span>`
                  : `<span class="semfech">sem fechamento registrado</span>`}
          </div>`;
        }).join("");
      return `<div class="eqrow">
        <div class="barrow clickable" data-eq="${esc(nome)}">
          <div class="label" title="${tituloEquip(nome)}">${rotulo(nome)}</div>
          <div class="track"><div class="fill" style="width:${n / max * 100}%;background:var(${tipo.cssVar})"></div></div>
          <div class="count">${n}</div>
          <div class="chev">${aberto ? "&#9662;" : "&rsaquo;"}</div>
        </div>
        ${aberto ? `<div class="events">${lista}</div>` : ""}
      </div>`;
    }).join("")}
  </div>`;
}

// O nome do equipamento chega por data-eq, nao por onclick="toggle('...')".
// Dentro de um atributo onclick o nome vai escapado pra HTML, entao um
// equipamento com apostrofo ou & ("CHAVE O'HIGGINS & CIA") chegava aqui como
// "O&#39;HIGGINS &amp; CIA" e nunca casava com a chave crua do Set -- a linha
// simplesmente nao expandia. Lido via dataset, o navegador ja devolve o texto
// decodificado.
document.addEventListener("click", ev => {
  const alvo = ev.target.closest("[data-eq]");
  if (alvo) toggle(alvo.dataset.eq);
});

function toggle(nome) {
  expanded.has(nome) ? expanded.delete(nome) : expanded.add(nome);
  render();
}

// ---------- render ----------

function render() {
  if (!DATA) return;
  const errbox = document.getElementById("errbox");
  errbox.innerHTML = DATA.error
    ? `<div class="err">Falha na leitura da API do RTAC. Dados podem estar desatualizados. O detalhe do erro está no log do servidor.</div>` : "";

  const st = document.getElementById("status");
  const idadeMin = DATA.fetched_at ? (Date.now() - DATA.fetched_at) / 60000 : null;
  st.classList.toggle("stale", idadeMin == null || idadeMin > 3);
  document.getElementById("status-txt").textContent = DATA.fetched_at
    ? `dados de ${new Date(DATA.fetched_at).toLocaleTimeString("pt-BR")}`
    : "aguardando primeiro ciclo de coleta";

  // hash apontando pra ano/mes que nao existe mais nos dados volta pro topo
  if (path.length && !DATA.anos[path[0]]) path = [];
  if (path.length === 2 && !DATA.anos[path[0]].meses[path[1]]) path = [path[0]];

  const crumbs = [path.length === 0 ? '<span class="here">Todos os anos</span>'
                                    : '<a onclick="go([])">Todos os anos</a>'];
  if (path.length >= 1) crumbs.push(path.length === 1 ? `<span class="here">${path[0]}</span>`
    : `<a onclick="go(['${path[0]}'])">${path[0]}</a>`);
  if (path.length === 2) crumbs.push(`<span class="here">${mesLabel(path[1])}</span>`);
  document.getElementById("crumbs").innerHTML =
    (path.length ? '<button class="back" onclick="history.back()"><span class="arrow">&larr;</span>Voltar</button>' : "")
    + crumbs.join('<span class="sep">&rsaquo;</span>');

  const evs = scoped();
  document.getElementById("kpis").innerHTML = cardKPIs(evs);

  const content = document.getElementById("content");
  if (!EVENTS.length) {
    content.innerHTML = `<div class="card"><div class="empty">Nenhuma abertura registrada no período disponível.</div></div>`;
    return;
  }

  if (path.length === 0) {
    const anos = Object.keys(DATA.anos).sort().reverse();
    const counts = countBy(EVENTS, e => e.ano);
    content.innerHTML = `
      <div class="grid split">
        <div class="card">
          <h2>Aberturas por exercício</h2>
          <p class="hint">Clique no ano para abrir a distribuição mensal.</p>
          ${bars(anos.map(a => ({label: a, value: counts.get(a) || 0, onclick: `go(['${a}'])`})), {clickable: true})}
        </div>
        ${cardTipos(evs)}
      </div>
      <div class="grid split" style="margin-top:16px">
        ${cardTrend12()}
        ${cardTopEquip(evs)}
      </div>`;
  } else if (path.length === 1) {
    const meses = Object.keys(DATA.anos[path[0]].meses).sort().reverse();
    const counts = countBy(evs, e => e.mes);
    content.innerHTML = `
      <div class="grid split">
        <div class="card">
          <h2>Aberturas por mês em ${path[0]}</h2>
          <p class="hint">Clique no mês para ver os equipamentos que atuaram.</p>
          ${bars(meses.map(m => ({label: mesLabel(m), value: counts.get(m) || 0, onclick: `go(['${path[0]}','${m}'])`})), {clickable: true})}
        </div>
        ${cardTipos(evs)}
      </div>
      <div class="grid" style="margin-top:16px">${cardTopEquip(evs)}</div>`;
  } else {
    const filtered = selectedDay ? evs.filter(e => e.abertura.slice(0, 10) === selectedDay) : evs;
    content.innerHTML = `
      <div class="grid">${cardCalendarMonth(evs, path[0], path[1])}</div>
      <div class="grid split" style="margin-top:16px">
        ${cardEventos(filtered)}
        ${cardTipos(evs)}
      </div>`;
  }
}

// tooltip unico, delegado -- so nas marcas que nao tem rotulo direto
const tooltip = document.getElementById("tooltip");
document.addEventListener("mousemove", ev => {
  const el = ev.target.closest("[data-tip]");
  if (!el) { tooltip.style.display = "none"; return; }
  tooltip.innerHTML = `<div class="t-label">${el.dataset.tip}</div><div class="t-value">${el.dataset.tipval}</div>`;
  tooltip.style.display = "block";
  tooltip.style.left = Math.min(ev.clientX + 14, window.innerWidth - tooltip.offsetWidth - 8) + "px";
  tooltip.style.top = (ev.clientY - tooltip.offsetHeight - 12) + "px";
});

path = hashPath();
// Intervalo fixo deixava quem abre a pagina antes do primeiro poll do servidor
// terminar olhando pra tela vazia por um ciclo inteiro -- parecia que a pagina
// so atualizava no F5, que e o que dispara o tick na hora. Sem dado ainda,
// tenta de novo rapido; com dado na mao, volta pro ciclo normal.
(async function loop() {
  await tick();
  const vazio = !EVENTS.length && !(DATA && DATA.error);
  setTimeout(loop, vazio ? 3000 : __POLL_MS__);
})();
</script>
</body>
</html>
"""

# Sem logo.png o <img> e o favicon somem inteiros, junto com a barrinha que
# separa o logo do titulo -- barra solta, sem nada antes, fica pior que nada.
_TITULO_HTML = html.escape(TITULO, quote=True)
_LOGO_IMG = (f'<img class="logo" src="data:image/png;base64,{LOGO_B64}"'
             f' alt="{_TITULO_HTML}">\n        <div class="rule"></div>'
             if LOGO_B64 else "")
_FAVICON = (f'<link rel="icon" href="data:image/png;base64,{LOGO_B64}">'
            if LOGO_B64 else "")

PAGE = (PAGE.replace("__LOGO_IMG__", _LOGO_IMG)
            .replace("__FAVICON__", _FAVICON)
            .replace("__TITULO__", _TITULO_HTML)
            .replace("__POLL_MS__", str(POLL_SECONDS * 1000))
            .replace("__POLL__", str(POLL_SECONDS)))


class Handler(BaseHTTPRequestHandler):
    # O padrao do BaseHTTPRequestHandler anuncia "BaseHTTP/0.6 Python/3.14.3",
    # que entrega a versao exata do interpretador pra quem so pediu a pagina.
    server_version = "painel-aberturas"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def _cabecalhos_de_seguranca(self):
        """CSP fecha o unico caminho teorico de XSS que sobraria: mesmo que um
        Category vindo do RTAC escapasse do esc(), nada pode ser buscado nem
        enviado pra fora. 'unsafe-inline' e obrigatorio porque a pagina e um
        arquivo so, com <style>, <script> e onclick embutidos; data: cobre o
        logo embutido; connect-src 'self' limita o fetch ao /data.json local."""
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; img-src data:; "
                         "connect-src 'self'; base-uri 'none'; form-action 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def do_GET(self):
        if self.path == "/data.json":
            with state_lock:
                payload = json.dumps({
                    "anos": state["anos"],
                    "tags": state["tags"],
                    "nomes": NOMES_EQUIPAMENTO,
                    # So o SINAL de falha vai pro cliente, nunca o texto da
                    # excecao: str(exc) de um erro do requests carrega a URL
                    # inteira ("...for url: https://<ip do RTAC>/api/v1/..."),
                    # o que entregaria o endereco e a estrutura da API do RTAC
                    # a qualquer visitante. O detalhe fica no log local, que e
                    # onde quem opera o painel vai procurar.
                    "error": state["error"] is not None,
                    "fetched_at": state["fetched_at"],
                }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self._cabecalhos_de_seguranca()
            self.end_headers()
            self.wfile.write(payload)
        else:
            payload = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self._cabecalhos_de_seguranca()
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)


def main() -> None:
    cfg = s.load_config()
    if not cfg["verify_tls"]:
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    threading.Thread(target=poll_loop, args=(cfg,), daemon=True).start()

    server = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    log.info("servindo em http://localhost:%d (Ctrl+C pra parar)", PORT)
    if BIND_HOST == "0.0.0.0":
        log.info("acessivel na rede em http://%s:%d", _lan_ip(), PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
