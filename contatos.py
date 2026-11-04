"""
Tarefas que o robo prepara e a equipe conclui com um clique (no robozinho):

  WhatsApp Logistica  OS Nova em "Iniciar OS" esperando o cliente confirmar que
                      vai enviar o produto. So depois do "sim" o bot inicia a OS.
  WhatsApp Balcao     avisar o cliente que estamos aguardando o produto no balcao
                      (a OS e iniciada direto, o aviso nao trava nada).
  Planilha L&T        linha Sinistro|Cidade|Produto|Objetivo para a aba Atendimentos.

Guardado em dados/tarefas.json. LGPD: so o necessario para o contato (primeiro
nome, produto, cidade, telefone). Nada de CPF, nascimento ou e-mail.

Situacoes de contato:
  pronta     dados lidos da OS, falta mandar a mensagem
  contatada  WhatsApp aberto com a mensagem
  confirmou  (logistica) cliente disse SIM -> o bot inicia a OS no proximo ciclo
  recusou    (logistica) cliente disse NAO -> o robo anota isso na OS do portal
  encerrada  (logistica) recusa anotada na OS - sai da lista de vez (pedido do dono 04/11)
  iniciada   (logistica) bot iniciou a OS - sai da lista
  avisado    (balcao) mensagem enviada - sai da lista
Situacoes de planilha: pendente -> lancada

Pos-conserto (secao "pos_conserto", chave "assinar:<OS>"): OS Concluida em Assinar OS
  pronta     Logistica: falta colar o codigo de rastreio; Leva & Traz: falta "Finalizar
             atendimento"; Balcao: esperando o cliente retirar
  liberada   a equipe liberou -> o robo anota o rastreio (Logistica) e assina a OS
  lancado    (Logistica) assinada, falta mandar o rastreio ao cliente no WhatsApp
  avisado / assinada   concluida - sai da janela
  saiu       a OS saiu da lista (alguem assinou no portal) - sai da janela
"""
import json
import os
import threading
import urllib.parse
from datetime import datetime

from caminhos import PASTA

ARQUIVO = os.path.join(PASTA, "dados", "tarefas.json")
LOGISTICA, BALCAO = "logistica", "balcao"
CORREIOS, ASSINAR = "correios", "assinar"
PENDENTES = ("pronta", "contatada")
_trava = threading.RLock()

CABECALHO_MSG = (
    "Olá, {nome}! Tudo bem?\n\n"
    "Aqui é da {assistencia}, autorizada da seguradora. Recebemos o seu sinistro "
    "nº {sinistro}, referente ao produto {produto}, pela garantia estendida.\n\n"
)
MENSAGENS_PADRAO = {
    LOGISTICA: CABECALHO_MSG + (
        "O conserto é feito na nossa oficina, e o envio do produto até nós é feito por você.\n\n"
        "Você confirma que vai enviar o produto? Responda *SIM* ou *NÃO*."),
    BALCAO: CABECALHO_MSG + (
        "Estamos aguardando a entrega do seu produto no nosso balcão{balcao}.\n\n"
        "Qualquer dúvida, é só responder esta mensagem."),
    CORREIOS: (
        "Olá, {nome}! Tudo bem?\n\n"
        "Aqui é da {assistencia}, autorizada da seguradora. O conserto do seu produto "
        "{produto} (sinistro nº {sinistro}) foi concluído e ele já foi enviado pelos Correios.\n\n"
        "Código de rastreio: *{rastreio}*\n"
        "Acompanhe em https://rastreamento.correios.com.br\n\n"
        "Qualquer dúvida, é só responder esta mensagem."),
}


def _ler():
    try:
        with open(ARQUIVO, encoding="utf-8") as f:
            dados = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        dados = {}
    dados.setdefault("ativos", [])
    dados.setdefault("os", {})
    dados.setdefault("planilha", {})
    dados.setdefault("pos_conserto", {})
    return dados


def _gravar(dados):
    os.makedirs(os.path.dirname(ARQUIVO), exist_ok=True)
    tmp = ARQUIVO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ARQUIVO)


def carregar():
    with _trava:
        return _ler()


def alterar(funcao):
    """Le, aplica funcao(dados) e grava, tudo sob a trava (bot e janela mexem juntos)."""
    with _trava:
        dados = _ler()
        funcao(dados)
        _gravar(dados)


def agora():
    return datetime.now().isoformat(timespec="seconds")


def dias_desde(iso):
    try:
        return (datetime.now() - datetime.fromisoformat(iso)).days
    except (TypeError, ValueError):
        return 0


def _dias_maximo():
    """dias_maximo do config.json (lido na hora: muda sem reiniciar a janela)."""
    try:
        with open(os.path.join(os.path.dirname(ARQUIVO), "..", "config.json"), encoding="utf-8") as f:
            return json.load(f).get("dias_maximo", 120)
    except (OSError, ValueError):
        return 120


def bonito(texto):
    """'MARIA DA SILVA' -> 'Maria Da Silva'; mantem o que ja vem com maiusculas e minusculas."""
    texto = (texto or "").strip()
    return texto.title() if texto.isupper() else texto


# ------------------------------------------------------------------ contatos
def marcar(numero, situacao):
    def f(d):
        item = d["os"].get(numero)
        if item:
            item["situacao"] = situacao
            item[f"{situacao}_em"] = agora()
    alterar(f)


def atualizar_extras(extras):
    """
    Dados da lista renovados a cada ciclo: dias em aberto (campo 'Em aberto') e
    nº do sinistro (completa tarefas gravadas antes de guardarmos o sinistro).
    """
    def f(d):
        for grupo in ("os", "planilha"):
            for numero, item in d[grupo].items():
                ext = extras.get(numero, {})
                if "dias" in ext and grupo == "os":
                    item["dias_aberto"] = ext["dias"]
                if ext.get("sinistro") and not item.get("sinistro"):
                    item["sinistro"] = ext["sinistro"]
    alterar(f)


def confirmados():
    return {n for n, i in carregar()["os"].items()
            if i.get("tipo", LOGISTICA) == LOGISTICA and i.get("situacao") == "confirmou"}


def balcao_avisados():
    """Balcao com "Ja avisei": o robo pode iniciar a OS."""
    return {n for n, i in carregar()["os"].items()
            if i.get("tipo") == BALCAO and i.get("situacao") == "avisado"}


def recusados_sem_anotacao():
    """
    Logisticas em que o cliente disse que NAO vai enviar e o robo ainda nao anotou na OS.
    So dentro de dias_maximo: em 03/11 a equipe marcou "Nao vai enviar" em 8 OSs de ~1000
    dias so para tira-las da lista - nelas o robo nao escreve nada (continuam escondidas).
    """
    limite = _dias_maximo()
    return sorted(n for n, i in carregar()["os"].items()
                  if i.get("tipo", LOGISTICA) == LOGISTICA and i.get("situacao") == "recusou"
                  and i.get("dias_aberto", 0) <= limite)


def marcar_anotada(numero):
    def f(d):
        if numero in d["os"]:
            d["os"][numero]["anotada_em"] = agora()
    alterar(f)


def sem_anotacao():
    """Logisticas que o robo iniciou mas ainda nao tem a anotacao 'aguardando o envio'."""
    return sorted(n for n, i in carregar()["os"].items()
                  if i.get("tipo", LOGISTICA) == LOGISTICA and i.get("situacao") == "iniciada"
                  and not i.get("anotada_em"))


def visiveis(dados=None, tipo=LOGISTICA):
    """
    O que aparece na janela.
    Logistica: OSs ainda na fila do portal (ativos), ou ja confirmadas.
    Balcao: ate a equipe marcar que avisou (a OS sai de Iniciar OS antes disso).
    """
    dados = dados or carregar()
    ativos = set(dados.get("ativos", []))
    limite = _dias_maximo()
    itens = []
    for n, i in dados["os"].items():
        if i.get("tipo", LOGISTICA) != tipo:
            continue
        if i.get("dias_aberto", 0) > limite:  # regra dos 120 dias (config dias_maximo)
            continue
        sit = i.get("situacao")
        if tipo == BALCAO:
            if sit in PENDENTES:
                itens.append(i)
        elif sit not in ("iniciada", "encerrada") and (n in ativos or sit == "confirmou"):
            itens.append(i)
    ordem = {"pronta": 0, "contatada": 1, "confirmou": 2, "recusou": 3}
    # mais recentes primeiro dentro de cada situacao (OS esquecidas vao pro fim)
    return sorted(itens, key=lambda i: (ordem.get(i.get("situacao"), 9), i.get("dias_aberto", 0), i["os"]))


def mensagem(item, cfg):
    tipo = item.get("tipo", LOGISTICA)
    if tipo == ASSINAR:  # depois do conserto so tem mensagem para o rastreio dos Correios
        tipo = CORREIOS
    modelo = cfg.get(f"mensagem_{tipo}") or MENSAGENS_PADRAO[tipo]
    endereco = (cfg.get("endereco_balcao") or "").strip()
    return modelo.format(
        nome=item.get("nome") or "cliente",
        assistencia=(cfg.get("nome_assistencia") or "").strip() or "assistência técnica",
        os=item["os"],
        sinistro=item.get("sinistro") or item["os"],
        produto=item.get("produto") or "em garantia",
        balcao=f" ({endereco})" if endereco else "",
        rastreio=item.get("codigo") or "",
    )


def numeros_whatsapp(item):
    """
    Numeros para tentar, na ordem: os telefones da OS e, para celular, a versao
    sem o 9 (contas antigas de WhatsApp ainda sao registradas sem ele).
    """
    nums = []
    for t in item.get("telefones") or [item.get("telefone")]:
        if t and t not in nums:
            nums.append(t)
    for t in list(nums):
        if len(t) == 11 and t[2] == "9" and t[:2] + t[3:] not in nums:
            nums.append(t[:2] + t[3:])
    return nums


def link_whatsapp(item, cfg, numero=None):
    """whatsapp:// abre direto o aplicativo; wa.me (navegador) se whatsapp_app = false."""
    numero = numero or item["telefone"]
    texto = urllib.parse.quote(mensagem(item, cfg))
    if cfg.get("whatsapp_app", True):
        return f"whatsapp://send?phone=55{numero}&text={texto}"
    return f"https://wa.me/55{numero}?text={texto}"


def formatar_telefone(numero):
    """47999990001 -> (47) 99999-0001"""
    if len(numero) < 10:
        return numero
    return f"({numero[:2]}) {numero[2:-4]}-{numero[-4:]}"


# ------------------------------------------------------------------ planilha
def adicionar_linha(numero, sinistro, cidade, produto, objetivo):
    """Uma linha por OS; se ja existe (pendente ou lancada), nao duplica."""
    def f(d):
        if numero not in d["planilha"]:
            d["planilha"][numero] = {"os": numero, "sinistro": sinistro, "cidade": cidade,
                                     "produto": produto, "objetivo": objetivo,
                                     "situacao": "pendente", "pendente_em": agora()}
    alterar(f)


def linhas_pendentes(dados=None):
    dados = dados or carregar()
    return sorted((i for i in dados["planilha"].values() if i.get("situacao") == "pendente"),
                  key=lambda i: i["os"])


def marcar_linha(numero, situacao):
    def f(d):
        if numero in d["planilha"]:
            d["planilha"][numero]["situacao"] = situacao
            d["planilha"][numero][f"{situacao}_em"] = agora()
    alterar(f)


def texto_linha(item):
    """Separado por TAB: colado no Excel, cada valor cai numa coluna."""
    return "\t".join(item.get(k, "") for k in ("sinistro", "cidade", "produto", "objetivo"))


# ------------------------------------------------------------- pos-conserto
VISIVEIS_POS = ("pronta", "liberada", "lancado")


def chave_pos(tipo, numero):
    return f"{tipo}:{numero}"


def adicionar_pos_conserto(item):
    """Uma tarefa por (tipo, OS); se ja existe, nao mexe."""
    item = dict(item, chave=chave_pos(item["tipo"], item["os"]))
    item[f"{item['situacao']}_em"] = agora()
    alterar(lambda d: d["pos_conserto"].setdefault(item["chave"], item))


def marcar_pos_conserto(chave, situacao, **extra):
    def f(d):
        item = d["pos_conserto"].get(chave)
        if item:
            item.update(extra, situacao=situacao)
            item[f"{situacao}_em"] = agora()
    alterar(f)


def sincronizar_pos_conserto(achadas):
    """
    achadas = {OS: card} da lista do ciclo. Tarefa cuja OS saiu daquela etapa (alguem
    assinou no portal) sai da janela. "lancado" fica: falta o WhatsApp do rastreio.
    """
    def f(d):
        for item in d["pos_conserto"].values():
            card = achadas.get(item["os"])
            if item.get("situacao") in ("pronta", "liberada") and (
                    not card or card["etapa"] != item.get("etapa")):
                item["situacao"], item["saiu_em"] = "saiu", agora()
            if card and card.get("sinistro") and not item.get("sinistro"):
                item["sinistro"] = card["sinistro"]
    alterar(f)


def pos_conserto_visiveis(dados=None):
    dados = dados or carregar()
    ordem = {"pronta": 0, "lancado": 1, "liberada": 2}
    itens = [i for i in dados["pos_conserto"].values() if i.get("situacao") in VISIVEIS_POS]
    return sorted(itens, key=lambda i: (ordem.get(i["situacao"], 9), i.get("tipo"), i["os"]))


def pos_conserto_do_robo():
    """O que a equipe ja liberou e o robo faz no proximo ciclo."""
    return [i for i in carregar()["pos_conserto"].values() if i.get("situacao") == "liberada"]


def _espera_equipe(item):
    sit = item.get("situacao")
    if sit == "lancado":
        return True
    # Balcao parado esperando o cliente vir retirar nao conta no numero vermelho
    return sit == "pronta" and "balc" not in (item.get("canal") or "").lower()


# --------------------------------------------------------------------- total
def contar_pendentes():
    d = carregar()
    log = sum(1 for i in visiveis(d, LOGISTICA) if i.get("situacao") in PENDENTES)
    pos = sum(1 for i in pos_conserto_visiveis(d) if _espera_equipe(i))
    return log + len(visiveis(d, BALCAO)) + len(linhas_pendentes(d)) + pos
