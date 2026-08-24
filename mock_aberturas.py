"""Painel de aberturas com dados FALSOS, pra ver o que quebra com o tempo.

Nao fala com o RTAC nem le a configuracao da instalacao: importa do modulo
real so o que e logica (PAGE, _pair_open_close), gera historico sintetico de
varios anos com cadastro proprio e serve na 8423, ao lado do painel de verdade
na 8422. Se a interface quebra aqui, quebra la -- e roda em maquina que nunca
viu um .env.

O historico gerado passa pelo _pair_open_close REAL, nao por um atalho que
devolve pares prontos: o pareamento e a logica que mais tem chance de errar,
entao ele tem que estar no caminho do teste.

Casos plantados de proposito (todos ja quebraram ou tem chance de quebrar):
  * ano inteiro sem nenhum evento no meio da serie (2021)
  * mes vazio dentro de ano cheio
  * dia com 12 aberturas, muito acima do topo da rampa de calor (5+)
  * duracao de varios dias, pra estourar o formato "XhYmin"
  * 29/02 de ano bissexto (2024)
  * evento no primeiro e no ultimo dia do mes
  * abertura sem fechamento no fim da serie (pendencia de verdade)
  * Alarmed duplicado, que e o bug do RL-01: nao pode virar duas aberturas
  * equipamento sem apelido cadastrado, que aparece so com o codigo
  * equipamento sem palavra de classe no Category
  * nome com apostrofo e & , que passam por esc() e por atributo onclick

Uso:
    python mock_aberturas.py
    http://localhost:8423
"""

import json
import logging
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import historico_aberturas as h

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mock_aberturas")

PORT = 8423
SEMENTE = 42          # fixa: mesma rodada gera sempre o mesmo cenario
ANO_INICIAL = 2019

EQUIPAMENTOS = [
    ("RL-01 RELIGADOR", "VirtualTagList1.RL_01"),
    ("RL-09 RELIGADOR", "VirtualTagList1.RL_09"),
    ("RL-17 RELIGADOR", "VirtualTagList1.RL_17"),
    ("CNT-03 RELIGADOR", "VirtualTagList1.CNT_03"),      # sem apelido cadastrado
    ("BC-02 CHAVE VÁCUO", "VirtualTagList1.BC_02"),
    ("AT TT-2 DISJUNTOR DJ532", "TT2_SEL387E_DNP.BI_00003"),
    ("CHAVE O'HIGGINS & CIA", "VirtualTagList1.OHIGGINS"),  # apostrofo e &
    ("EQUIPAMENTO SEM CLASSE", "VirtualTagList1.ORFAO"),    # sem RELIGADOR/DISJUNTOR/CHAVE
]

ANO_VAZIO = 2021          # serie tem buraco no meio
MES_VAZIO = (2023, 5)     # maio de 2023 sem nada

# Cadastro proprio, nao o do h.NOMES_EQUIPAMENTO: aquele vem de um
# nomes_equipamento.json que so existe em instalacao configurada, e sem ele o
# mock cairia num cenario onde NENHUM equipamento tem apelido -- perdendo o
# caso de renderizacao com nome. Aqui os dois caminhos existem sempre:
# CNT-03 e os de baixo ficam de fora de proposito.
NOMES = {
    "RL-01": "ALIMENTADOR NORTE",
    "RL-09": "ALIMENTADOR SUL",
    "RL-17": "INTERLIGAÇÃO LESTE",
}


def _ts(ano, mes, dia, hora, minuto, seg):
    return f"{ano:04d}-{mes:02d}-{dia:02d}T{hora:02d}:{minuto:02d}:{seg:02d}.000000+00:00"


def _distubio(rng, ano, mes, dia, duracao_seg, duplicar_alarmed):
    """Uma atuacao: Alarmed, as vezes repetido, e o Normalized correspondente.

    O RTAC repete registro: Alarmed duplicado a poucos segundos e Normalized
    duplicado sao o padrao no dado real (o RL-01 tem 124 Normalized pra 8
    Alarmed). O mock reproduz isso pra que o pareamento seja testado de fato.
    """
    hora, minuto = rng.randrange(24), rng.randrange(60)
    eventos = [{"Timestamp": _ts(ano, mes, dia, hora, minuto, 0),
                "EventType": "Alarmed", "Message": "ABERTO"}]
    if duplicar_alarmed:
        eventos.append({"Timestamp": _ts(ano, mes, dia, hora, minuto, 2),
                        "EventType": "Alarmed", "Message": "ABERTO"})
    if rng.random() < 0.3:
        eventos.append({"Timestamp": _ts(ano, mes, dia, hora, minuto, 3),
                        "EventType": "Acknowledged", "Message": "ABERTO"})
    if duracao_seg is None:
        return eventos  # fica aberto: sem Normalized depois

    fim = time.gmtime(time.mktime((ano, mes, dia, hora, minuto, 0, 0, 0, 0)) + duracao_seg)
    eventos.append({"Timestamp": _ts(*fim[:6]), "EventType": "Normalized", "Message": "FECHADO"})
    if rng.random() < 0.5:
        eventos.append({"Timestamp": _ts(*fim[:5], min(fim[5] + 1, 59)),
                        "EventType": "Normalized", "Message": "FECHADO"})
    return eventos


def gerar(ano_final: int) -> dict:
    """Historico bruto por equipamento, do jeito que a API do RTAC devolveria."""
    rng = random.Random(SEMENTE)
    historicos = {cat: [] for cat, _ in EQUIPAMENTOS}
    duracoes = [8, 45, 90, 300, 1800, 7200, 86400 * 2, 86400 * 5]

    for ano in range(ANO_INICIAL, ano_final + 1):
        if ano == ANO_VAZIO:
            continue
        for mes in range(1, 13):
            if (ano, mes) == MES_VAZIO:
                continue
            if ano == ano_final and mes > time.gmtime().tm_mon:
                continue  # nao inventa evento no futuro
            for cat, _ in EQUIPAMENTOS:
                for _ in range(rng.choice([0, 0, 1, 1, 2, 3, 6])):
                    dia = rng.randrange(1, 29)
                    historicos[cat] += _distubio(
                        rng, ano, mes, dia, rng.choice(duracoes), rng.random() < 0.25)

    cat0 = EQUIPAMENTOS[0][0]
    # dia bem acima do topo da rampa de calor (a legenda para em "5 ou mais")
    for _ in range(12):
        historicos[cat0] += _distubio(rng, ano_final - 1, 3, 15, 120, False)
    # 29/02 de bissexto, primeiro e ultimo dia do mes
    historicos[cat0] += _distubio(rng, 2024, 2, 29, 600, False)
    historicos[cat0] += _distubio(rng, 2024, 1, 1, 600, False)
    historicos[cat0] += _distubio(rng, 2024, 1, 31, 600, False)
    # pendencia real: ultima transicao e Alarmed sem Normalized depois
    historicos[EQUIPAMENTOS[1][0]].append(
        {"Timestamp": _ts(ano_final, time.gmtime().tm_mon, 1, 3, 30, 0),
         "EventType": "Alarmed", "Message": "ABERTO"})
    return historicos


def montar_estado(ano_final: int) -> dict:
    """Mesma montagem do poll_loop real: pareia e agrupa em ano/mes/equipamento."""
    anos, total = {}, 0
    for cat, historico in gerar(ano_final).items():
        for par in h._pair_open_close(historico):
            total += 1
            duracao = None
            if par["fechamento"]:
                t0 = time.strptime(par["abertura"][:19], "%Y-%m-%dT%H:%M:%S")
                t1 = time.strptime(par["fechamento"][:19], "%Y-%m-%dT%H:%M:%S")
                duracao = int(time.mktime(t1) - time.mktime(t0))
            no_ano = anos.setdefault(par["abertura"][:4], {"meses": {}})
            no_mes = no_ano["meses"].setdefault(par["abertura"][:7], {"equipamentos": {}})
            no_mes["equipamentos"].setdefault(cat, []).append({
                # mesmo tratamento do painel real: o gerador carimba
                # "+00:00" igual ao RTAC, e o offset falso e descartado aqui
                "abertura": h._hora_local(par["abertura"]),
                "fechamento": h._hora_local(par["fechamento"]),
                "duracao_seg": duracao,
            })
    log.info("%d equipamentos, %d aberturas, anos %s",
             len(EQUIPAMENTOS), total, ", ".join(sorted(anos)))
    return {
        "anos": anos,
        "tags": dict(EQUIPAMENTOS),
        "nomes": NOMES,
        "error": None,
        "fetched_at": time.time() * 1000,
    }


ESTADO = montar_estado(time.gmtime().tm_year)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/data.json":
            corpo = json.dumps(ESTADO).encode("utf-8")
            tipo = "application/json"
        else:
            corpo = h.PAGE.encode("utf-8")
            tipo = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.end_headers()
        self.wfile.write(corpo)


def main() -> None:
    servidor = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log.info("MOCK (dados falsos) em http://localhost:%d (Ctrl+C pra parar)", PORT)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
