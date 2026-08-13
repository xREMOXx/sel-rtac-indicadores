"""Checagem do pareamento abre/fecha. Roda com: python test_pareamento.py

Sem framework, so assert. O pareamento e a unica logica nao trivial do painel:
se ele errar, o KPI de "pendentes de fechamento" acusa religador aberto que na
verdade esta fechado, que foi exatamente o bug do RL-01 em 2026-07-02.
"""

import historico_aberturas as h


def ev(ts, tipo, msg="ABERTO"):
    return {"Timestamp": ts, "EventType": tipo, "Message": msg}


def test_par_simples():
    pares = h._pair_open_close([
        ev("2026-07-01T10:00:00", "Alarmed"),
        ev("2026-07-01T10:05:00", "Normalized", "FECHADO"),
    ])
    assert pares == [{"abertura": "2026-07-01T10:00:00",
                      "fechamento": "2026-07-01T10:05:00"}], pares


def test_reconhecimento_nao_e_abertura():
    """Acknowledged repete o Message "ABERTO" mas nao e transicao fisica."""
    pares = h._pair_open_close([
        ev("2026-07-01T10:00:00", "Alarmed"),
        ev("2026-07-01T10:01:00", "Acknowledged"),
        ev("2026-07-01T10:05:00", "Normalized", "FECHADO"),
    ])
    assert len(pares) == 1, pares
    assert pares[0]["fechamento"] == "2026-07-01T10:05:00"


def test_alarmed_repetido_e_o_mesmo_evento():
    """Caso RL-01 2026-07-02: ciclo de religamento gravado em duplicidade.

    O religador precisa fechar pra abrir de novo, entao dois Alarmed seguidos
    sao um evento so. Antes isso virava 2 aberturas, uma delas sem fechamento.
    """
    pares = h._pair_open_close([
        ev("2026-07-02T01:58:18", "Alarmed"),
        ev("2026-07-02T01:58:20", "Alarmed"),
        ev("2026-07-02T01:58:22", "Normalized", "FECHADO"),
        ev("2026-07-02T01:58:23", "Normalized", "FECHADO"),
    ])
    assert pares == [{"abertura": "2026-07-02T01:58:18",
                      "fechamento": "2026-07-02T01:58:22"}], pares
    assert all(p["fechamento"] for p in pares), "nao pode sobrar pendencia"


def test_aberto_de_verdade():
    """Sem nenhum Normalized depois, ai sim o ponto esta aberto agora."""
    pares = h._pair_open_close([
        ev("2026-07-01T10:00:00", "Alarmed"),
        ev("2026-07-01T10:05:00", "Normalized", "FECHADO"),
        ev("2026-07-01T11:00:00", "Alarmed"),
    ])
    assert len(pares) == 2, pares
    assert pares[1]["fechamento"] is None


def test_fora_de_ordem():
    """A API nao garante ordem; o pareamento ordena antes de casar."""
    pares = h._pair_open_close([
        ev("2026-07-01T10:05:00", "Normalized", "FECHADO"),
        ev("2026-07-01T10:00:00", "Alarmed"),
    ])
    assert len(pares) == 1 and pares[0]["fechamento"] == "2026-07-01T10:05:00", pares


def test_normalized_solto_nao_vira_par():
    assert h._pair_open_close([ev("2026-07-01T10:05:00", "Normalized", "FECHADO")]) == []
    assert h._pair_open_close([]) == []


if __name__ == "__main__":
    testes = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in testes:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(testes)} checagens passaram")
