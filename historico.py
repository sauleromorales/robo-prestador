"""
Memoria do robo: o que ja foi feito em cada OS, para nao repetir a cada ciclo.

O logs/execucoes.csv continua sendo o registro completo (uma linha por acao); este
arquivo guarda so o ULTIMO resultado de cada (OS, etapa) e responde "posso fazer?".

Regras (pode_fazer):
  OK                           ja feito -> nunca refaz
  CLIQUEI_MAS_ETAPA_NAO_MUDOU  clicou e o portal nao mudou -> NUNCA clica de novo
                               sozinho (podia iniciar/salvar duas vezes); precisa
                               alguem conferir e usar "Repetir" no robozinho
  DRY_RUN                      ja testado -> nao repete enquanto dry_run continuar
                               ligado (com dry_run desligado, faz de verdade)
  PULADA                       alguem clicou "Pular" -> nao pergunta de novo por
                               `horas_pulada` horas (padrao 24)
  ERRO                         tenta de novo, no maximo `max_erros_dia` vezes por dia
                               (padrao 3); depois espera o dia seguinte

Guardado em dados/historico.json, chave = nº da OS (com o sinistro junto).
"""
import json
import os
import threading
from datetime import datetime, timedelta

from caminhos import PASTA

ARQUIVO = os.path.join(PASTA, "dados", "historico.json")
_trava = threading.RLock()

# status do CSV que contam como resultado de acao (os outros - WhatsApp aberto,
# cliente confirmou etc. - ficam so no CSV e no tarefas.json)
STATUS_DE_ACAO = {"OK", "DRY_RUN", "PULADA", "ERRO", "CLIQUEI_MAS_ETAPA_NAO_MUDOU"}
# sucesso de etapas que so leem a OS: valem como OK (e apagam um ERRO anterior)
# TRIADA/IGNORADA: triagem pos-conserto ja feita (vira tarefa ou "nada a fazer")
STATUS_SUCESSO = {"CONTATO_PRONTO": "OK", "LINHA_PRONTA": "OK", "TRIADA": "OK", "IGNORADA": "OK"}


def _ler():
    try:
        with open(ARQUIVO, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"os": {}}


def _gravar(dados):
    os.makedirs(os.path.dirname(ARQUIVO), exist_ok=True)
    tmp = ARQUIVO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ARQUIVO)


def _aplicar(d, numero, etapa, status, sinistro, detalhe, quando):
    os_ = d["os"].setdefault(numero, {"acoes": {}})
    if sinistro:
        os_["sinistro"] = sinistro
    a = os_["acoes"].setdefault(etapa, {})
    dia = quando.date().isoformat()
    if status == "ERRO":
        erros = a.get("erros_hoje", 0) if a.get("dia_erros") == dia else 0
        a["erros_hoje"], a["dia_erros"] = erros + 1, dia
    a.update(status=status, quando=quando.isoformat(timespec="seconds"), detalhe=detalhe[:200])


def anotar(numero, etapa, status, sinistro="", detalhe=""):
    """Chamado pelo registrar() do bot a cada acao."""
    status = STATUS_SUCESSO.get(status, status)
    if status not in STATUS_DE_ACAO or not numero or numero == "-":
        return
    with _trava:
        d = _ler()
        _aplicar(d, numero, etapa, status, sinistro, detalhe, datetime.now())
        _gravar(d)


def importar_csvs_se_vazio(pasta_logs):
    """
    Na primeira vez (sem dados/historico.json), aprende com os CSVs que ja existem
    em logs/ - nada do que aconteceu antes da memoria existir se perde.
    Formatos: v2 (data;os;status;detalhe - so Iniciar OS, INICIADA = OK),
    v9 (data;os;etapa;status;detalhe) e atual (data;sinistro;os;etapa;status;detalhe).
    """
    import csv
    import glob
    if os.path.exists(ARQUIVO):
        return 0
    linhas = []
    for arq in glob.glob(os.path.join(pasta_logs, "execucoes*.csv")):
        with open(arq, encoding="utf-8") as f:
            leitor = csv.reader(f, delimiter=";")
            cab = next(leitor, [])
            for l in leitor:
                if len(l) != len(cab):
                    continue  # linha quebrada (traceback antigo) ou de outro formato
                r = dict(zip(cab, l))
                status = "OK" if r.get("status") == "INICIADA" else r.get("status")
                status = STATUS_SUCESSO.get(status, status)
                linhas.append((r["data_hora"], r.get("os"), r.get("etapa", "Iniciar OS"), status,
                               r.get("sinistro", ""), r.get("detalhe", "")))
    d = {"os": {}}
    n = 0
    for data_hora, numero, etapa, status, sinistro, detalhe in sorted(linhas):
        if status in STATUS_DE_ACAO and numero and numero != "-":
            _aplicar(d, numero, etapa, status, sinistro, detalhe, datetime.fromisoformat(data_hora))
            n += 1
    with _trava:
        _gravar(d)
    return n


def pode_fazer(numero, etapa, cfg):
    """(True, "") ou (False, motivo curto para o log/balao)."""
    with _trava:
        a = _ler()["os"].get(numero, {}).get("acoes", {}).get(etapa)
    if not a:
        return True, ""
    st, quando = a.get("status"), datetime.fromisoformat(a["quando"])
    if st == "OK":
        return False, f"já feito em {quando:%d/%m %H:%M}"
    if st == "CLIQUEI_MAS_ETAPA_NAO_MUDOU":
        return False, "clicou e a etapa não mudou: confira no portal e use Repetir"
    if st == "DRY_RUN" and cfg.get("dry_run"):
        return False, f"já testado em {quando:%d/%m %H:%M} (modo teste)"
    if st == "PULADA" and datetime.now() - quando < timedelta(hours=cfg.get("horas_pulada", 24)):
        return False, f"pulada em {quando:%d/%m %H:%M}"
    if st == "ERRO" and a.get("dia_erros") == datetime.now().date().isoformat() \
            and a.get("erros_hoje", 0) >= cfg.get("max_erros_dia", 3):
        return False, f"{a['erros_hoje']} erros hoje - tento amanhã"
    return True, ""


def travadas():
    """OSs que precisam de gente: puladas, com erro repetido ou clique sem efeito."""
    with _trava:
        d = _ler()
    saida = []
    for numero, os_ in d["os"].items():
        for etapa, a in os_.get("acoes", {}).items():
            if a.get("status") in ("PULADA", "ERRO", "CLIQUEI_MAS_ETAPA_NAO_MUDOU"):
                saida.append({"os": numero, "sinistro": os_.get("sinistro", ""), "etapa": etapa, **a})
    return sorted(saida, key=lambda x: x["quando"], reverse=True)


def esquecer(numero=None, etapa=None, status=("PULADA", "ERRO", "CLIQUEI_MAS_ETAPA_NAO_MUDOU", "DRY_RUN")):
    """
    "Repetir": apaga a memoria (de uma OS/etapa, ou de todas) para o robo tentar de
    novo. Nunca apaga OK - o que foi feito de verdade continua lembrado.
    """
    with _trava:
        d = _ler()
        n = 0
        for num, os_ in d["os"].items():
            if numero and num != numero:
                continue
            for et in list(os_.get("acoes", {})):
                if (not etapa or et == etapa) and os_["acoes"][et].get("status") in status:
                    del os_["acoes"][et]
                    n += 1
        _gravar(d)
    return n
