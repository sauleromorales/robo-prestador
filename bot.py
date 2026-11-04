"""
Robo Portal do Prestador (seguradora) (v9)

Conecta no Vivaldi do robo (aberto pelo proprio robo, ja logado com 2FA).

Fluxo por ciclo:
  1. abre a lista de Ordens de Servico (menu -> Buscar -> rola ate o fim)
  2. separa as OSs com Status=Nova numa das etapas_ativas do config
     (Agendar Retirada so no canal Leva & Traz)
  3. para cada uma: abre a OS, CONFERE status/etapa/canal de novo, preenche
     o card da etapa e passa pelo confirmar() antes do clique final
  4. depois do clique, rele a etapa: so e OK se ela mudou
  5. registra tudo em logs/execucoes.csv (+ print em caso de erro)

Etapas:
  Iniciar OS        data = hoje, responsavel, clica Iniciar
  Agendar Retirada  data contato = hoje, responsavel, contato = Sim,
                    contato com = nome do cliente, agendamento = agora,
                    CEP/telefone/endereco copiados das Informacoes da OS, Salvar

Modos (config.json):
  "dry_run": true      -> preenche mas NUNCA clica no botao final
  "modo": "assistido"  -> pede confirmacao antes de cada clique final
  "modo": "automatico" -> clica sem perguntar, em loop a cada N minutos

Uso:
  python robo.py                      robozinho na tela (recomendado)
  python bot.py [--uma-vez] [--debug] so terminal
  python bot.py --anotar-sinistro 1000001 "texto" [--tipo "Anotações Prestadores"]
  python bot.py --anotar 2000001 "texto"          (pelo nº da OS, se o sinistro tiver varias)
"""
import csv
import json
import os
import re
import subprocess
import sys
import time
import traceback
import unicodedata
import webbrowser
from datetime import datetime

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import contatos
import historico

from caminhos import PASTA  # noqa: E402  (pasta do .exe ou dos .py)

os.chdir(PASTA)

cfg = json.load(open("config.json", encoding="utf-8"))
os.makedirs(cfg["pasta_logs"], exist_ok=True)
LOG_CSV = os.path.join(cfg["pasta_logs"], "execucoes.csv")
LOG_TXT = os.path.join(cfg["pasta_logs"], "robo.log")
CABECALHO_CSV = ["data_hora", "sinistro", "os", "etapa", "status", "detalhe"]

BASE = cfg["portal_url"].rstrip("/")
URL_LISTA = cfg.get("lista_os_url") or f"{BASE}/PortalPrestador/service-orders"
STATUS_ALVO = cfg.get("status_alvo", "Nova")
DIAS_MAXIMO = cfg.get("dias_maximo", 120)  # OS com mais dias "Em aberto" e ignorada
ETAPA_INICIAR = "Iniciar OS"
ETAPA_AGENDAR = "Agendar Retirada"
ETAPA_RETIRAR = "Retirar Produto"  # depois do agendamento; o bot so le (planilha)
ETAPAS_ATIVAS = cfg.get("etapas_ativas", [ETAPA_INICIAR])
CANAL_LEVA_TRAZ = cfg.get("canal_leva_traz", "Leva & Traz")
TIPO_ANOTACAO = "Anotações Prestadores"

# ---- seletores confirmados no HTML real (pasta descoberta/) ---------------
# cabecalho da OS: igual no card da lista e na pagina da OS (nth=0 e o rotulo)
def campo_cabecalho(fieldtitle):
    return f"app-so-info-field[fieldtitle='{fieldtitle}'] span.value >> nth=1"


SEL_ETAPA = campo_cabecalho("stage")
SEL_STATUS = campo_cabecalho("status")
SEL_CANAL = campo_cabecalho("channel")
SEL_NUM_OS = campo_cabecalho("soNumber")
SEL_SINISTRO = campo_cabecalho("serviceOrders.lossNumber")
SEL_MARCA = campo_cabecalho("brand")
SEL_PRODUTO = campo_cabecalho("product")

# lista de OSs
SEL_MENU_LISTA = "a[href='/PortalPrestador/service-orders']:visible"
SEL_BUSCAR = "app-search-bar .exclude-phone button.search-button"
SEL_BUSCA_TEXTO = "app-search-bar .exclude-phone input.text-input"
SEL_BUSCA_TIPO = "app-search-bar .exclude-phone app-search-type-select"
TIPO_BUSCA_OS = "Nº OS Seguradora"
TIPO_BUSCA_SINISTRO = "Nº do Sinistro"
# modo filtros (botao "Filtros" troca a barra de busca por esta barra)
SEL_ABRIR_FILTROS = "app-search-bar .desktop-only button:has-text('Filtros')"
SEL_FILTROS_BARRA = "app-so-filter-bar"
SEL_FILTRO_STATUS = "app-so-filter-bar app-simple-select[label='serviceOrders.soStatus'] mat-select"
SEL_FILTROS_LUPA = "app-so-filter-bar button.btn-circle:has(i.fa-search)"
SEL_RESULTADOS = "div.search-results"
SEL_CARD_LISTA = "app-so-card"
SEL_SETA_CARD = "button:has(i.fa-chevron-right)"

# bloco INFORMACOES DA OS (carregado sob demanda)
SEL_INFO_TOGGLE = "app-so-info app-collapse-button button"
SEL_INFO_CAMPO = "#collapseInfo.show app-info-field .info-field-name"

# card INICIAR OS
CARD_INI = "#cardStartOS"
SEL_INI_COLLAPSE = f"{CARD_INI} #collapseStartAdd"
SEL_INI_TOGGLE = f"{CARD_INI} app-collapse-button button"
SEL_INI_EDITAR = f"{CARD_INI} button:has-text('Editar')"
SEL_INI_DATA = f"{CARD_INI} input[type='date']"
SEL_INI_OS_PRESTADOR = f"{CARD_INI} input[placeholder^='Insira o número da OS']"
SEL_INI_BOTAO = f"{CARD_INI} button.btn-primary:has-text('Iniciar')"

# card AGENDAR RETIRADA
CARD_AR = "#cardPickupSchedule"
SEL_AR_COLLAPSE = f"{CARD_AR} #collapseScheduling"
SEL_AR_TOGGLE = f"{CARD_AR} app-collapse-button button"
SEL_AR_EDITAR = f"{CARD_AR} button:has-text('Editar')"
SEL_AR_DATA_CONTATO = f"{CARD_AR} app-date-input-field:has(label:text-is('Data Contato')) input"
SEL_AR_DATA_AGENDA = f"{CARD_AR} app-date-input-field:has(label:text-is('Data de Agendamento')) input"
SEL_AR_COM_CONTATO = f"{CARD_AR} app-so-scheduling-with-contact"
SEL_AR_CEP = f"{CARD_AR} input[name='zipCode']"
SEL_AR_TELEFONE = f"{CARD_AR} app-so-scheduling-address div.value:has(> div.title span:text-is('Telefone')) input"
SEL_AR_SALVAR = f"{CARD_AR} button.save-button"


def campo_ar(rotulo):
    return f"{CARD_AR} app-text-input-field:has(span:text-is('{rotulo}')) input"


# card MENSAGENS
CARD_MSG = "#messagesCard"
SEL_MSG_COLLAPSE = f"{CARD_MSG} #collapseMessage"
SEL_MSG_TOGGLE = f"{CARD_MSG} app-collapse-button button"
SEL_MSG_TEXTO = f"{CARD_MSG} input[name='newMessage']"
SEL_MSG_ENVIAR = f"{CARD_MSG} button.search-button:has(i.fa-paper-plane-o)"
SEL_MSG_HISTORICO = f"{CARD_MSG} .msg_history"

# LGPD: so estes rotulos do bloco Informacoes saem do navegador.
# CPF, data de nascimento, e-mail etc. nunca sao lidos.
CAMPOS_INFO_PERMITIDOS = {
    "nome do cliente": "nome",
    "telefone res.": "tel_res",
    "telefone celular": "tel_cel",
    "endereco": "endereco",
    "numero": "numero",
    "complemento": "complemento",
    "cep": "cep",
    "bairro": "bairro",
    "cidade": "cidade",
    "estado": "estado",
    "produto": "produto",
}


class SessaoExpirada(Exception):
    pass


class SemNavegador(Exception):
    pass


# ----------------------------------------------------------------- utilidades
def salvar_opcao(chave, valor):
    """
    Muda uma opcao na hora (cfg em memoria) e grava no config.json trocando so a
    linha dela - o resto do arquivo (ordem, espacos, textos) fica como esta.
    Usado pelo menu do robozinho (ex.: ligar/desligar o modo teste).
    """
    cfg[chave] = valor
    texto = open("config.json", encoding="utf-8").read()
    novo_valor = json.dumps(valor, ensure_ascii=False)
    padrao = re.compile(rf'("{re.escape(chave)}"\s*:\s*)(true|false|null|-?[\d.]+|"(?:[^"\\]|\\.)*")')
    if padrao.search(texto):
        texto = padrao.sub(lambda m: m.group(1) + novo_valor, texto, count=1)
    else:  # chave nova: entra logo depois da abertura
        texto = texto.replace("{", "{\n  " + json.dumps(chave) + ": " + novo_valor + ",", 1)
    json.loads(texto)  # nunca grava um config quebrado
    with open("config.json", "w", encoding="utf-8") as f:
        f.write(texto)
    log(f"Opcao alterada pelo robozinho: {chave} = {novo_valor}")


def log(msg):
    linha = f"[{datetime.now():%d/%m %H:%M:%S}] {msg}"
    if sys.stdout:
        try:
            print(linha, flush=True)
        except Exception:
            pass
    try:
        with open(LOG_TXT, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except Exception:
        pass


def _preparar_csv():
    """Se o CSV tem outras colunas (versao antiga), guarda ele com a data no nome."""
    if not os.path.exists(LOG_CSV):
        return
    with open(LOG_CSV, encoding="utf-8") as f:
        primeira = f.readline().strip()
    if primeira != ";".join(CABECALHO_CSV):
        os.replace(LOG_CSV, LOG_CSV.replace(".csv", f"_ate_{datetime.now():%Y%m%d_%H%M}.csv"))


_preparar_csv()
historico.importar_csvs_se_vazio(cfg["pasta_logs"])  # 1a vez: aprende com os CSVs antigos


# a equipe trabalha pelo nº do SINISTRO; o robo usa o nº da OS por dentro (e unico
# no portal - um sinistro pode ter mais de uma OS). Este mapa liga os dois.
SINISTRO_DA_OS = {}


def rotulo(numero):
    """Como a OS aparece para a equipe: 'sinistro 1000001 (OS 2000001)'."""
    sin = SINISTRO_DA_OS.get(numero)
    return f"sinistro {sin} (OS {numero})" if sin else f"OS {numero}"


def registrar(os_id, etapa, status, detalhe="", sinistro=None):
    novo = not os.path.exists(LOG_CSV)
    sinistro = sinistro or SINISTRO_DA_OS.get(os_id, "")
    with open(LOG_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f, delimiter=";")
        if novo:
            w.writerow(CABECALHO_CSV)
        w.writerow([datetime.now().isoformat(timespec="seconds"), sinistro, os_id, etapa, status, detalhe])
    historico.anotar(os_id, etapa, status, sinistro, detalhe)  # memoria: nao repetir


def rolar_ate(loc):
    """
    Rola ate o elemento, sem travar: o portal as vezes nunca fica "estavel" e o
    scroll do Playwright espera 30 s (OS 2000002 em 03/11). O clique ja rola sozinho.
    """
    try:
        loc.scroll_into_view_if_needed(timeout=5000)
    except PWTimeout:
        pass


def foto(page, nome):
    caminho = os.path.join(cfg["pasta_logs"], f"{datetime.now():%Y%m%d_%H%M%S}_{nome}.png")
    try:
        page.screenshot(path=caminho, timeout=8000)
    except Exception:
        pass
    return caminho


def hoje_iso():
    return datetime.now().strftime("%Y-%m-%d")


def norm(texto):
    """minusculo, sem acento e sem espacos sobrando - pra comparar textos do portal."""
    t = unicodedata.normalize("NFD", texto or "")
    return " ".join("".join(c for c in t if unicodedata.category(c) != "Mn").lower().split())


# canais em que Iniciar OS espera o cliente confirmar o envio (comparados sem acento)
CANAIS_CONFIRMAR_CLIENTE = [norm(c) for c in cfg.get("canais_confirmar_cliente", ["Logistica"])]
# Balcao: inicia direto e prepara o aviso "aguardando o produto no balcao"
CANAL_BALCAO = norm(cfg.get("canal_balcao", "Balcão"))


def so_digitos(texto):
    return re.sub(r"\D", "", texto or "")


def resumo_erro(e):
    """Primeira linha da excecao (as do Playwright vem com um log enorme)."""
    return f"{type(e).__name__}: {str(e).strip().splitlines()[0] if str(e).strip() else ''}"[:200]


# ------------------------------------------------- painel (terminal ou robozinho)
class PainelTerminal:
    """Como o bot conversa com quem esta olhando. O robo.py troca por uma janela."""

    def estado(self, estado, titulo, detalhe=""):
        log(f"[{estado}] {titulo}" + (f" - {detalhe}" if detalhe else ""))

    def alertar(self):
        pass

    def confirmar(self, pergunta):
        return input(f"  >> {pergunta} [ENTER = sim / n = pular] ").strip().lower() != "n"

    def esperar(self, segundos):
        time.sleep(segundos)

    def checar_pausa(self):
        pass

    def deve_sair(self):
        return False

    def contatos_mudaram(self):
        n = contatos.contar_pendentes()
        if n:
            log(f"{n} cliente(s) da Logistica esperando contato - veja no robozinho")


painel = PainelTerminal()


def aprovada_pela_equipe(numero):
    """
    Logistica com "Confirmou" clicado no robozinho: a pergunta "O robo vai iniciar a OS"
    ja foi a aprovacao humana desta OS, entao vale mesmo com dry_run/assistido
    (config confirmou_inicia_mesmo_em_teste, padrao true).
    """
    return cfg.get("confirmou_inicia_mesmo_em_teste", True) and numero in contatos.confirmados()


def confirmar(numero, etapa, acao, resumo, aprovado=False, perguntar=True):
    """
    Porteiro de todo clique final: respeita dry_run e modo assistido.
    perguntar=False pula so a pergunta do assistido (Iniciar OS, pedido do dono 03/11).
    """
    if aprovado:
        log(f"{rotulo(numero)}: {acao} - aprovado pela equipe (Confirmou)")
        return True
    if cfg.get("dry_run"):
        registrar(numero, etapa, "DRY_RUN", resumo)
        log(f"{rotulo(numero)}: dry_run, nao cliquei ({acao})")
        return False
    if perguntar and cfg["modo"] == "assistido" and not painel.confirmar(f"{acao} no {rotulo(numero)}?"):
        registrar(numero, etapa, "PULADA", resumo + "; pulada pelo usuario")
        return False
    return True


# ------------------------------------------------------------- navegador
def abrir_navegador():
    """
    Abre o Vivaldi do robo: perfil proprio (sessao 2FA salva) e porta de depuracao
    para o robo se conectar. Substitui o antigo abrir_chrome.bat.
    """
    candidatos = [cfg.get("navegador_exe", "")] + [
        os.path.join(os.environ.get(v, ""), "Vivaldi", "Application", "vivaldi.exe")
        for v in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)")]
    exe = next((c for c in candidatos if c and os.path.exists(c)), None)
    if not exe:
        log("Nao achei o vivaldi.exe - informe o caminho em navegador_exe no config.json")
        return
    porta = cfg["cdp_url"].rstrip("/").rsplit(":", 1)[-1]
    perfil = os.path.join(os.environ.get("USERPROFILE", PASTA), "robo_prestador_navegador")
    log(f"Abrindo o navegador do robo ({exe})")
    subprocess.Popen([exe, f"--remote-debugging-port={porta}", f"--user-data-dir={perfil}",
                      "--no-first-run", cfg.get("lista_os_url", URL_LISTA)])


def conectar(p):
    try:
        return p.chromium.connect_over_cdp(cfg["cdp_url"], timeout=10000)
    except Exception as e:
        raise SemNavegador(str(e)) from e


def pegar_aba(browser):
    """A aba do portal; se nao houver, abre uma nova (nao sequestra outra aba)."""
    ctx = browser.contexts[0]
    abas = [pg for pg in ctx.pages if "seguradora" in pg.url.lower()]
    if abas:
        return abas[0]
    page = ctx.new_page()
    page.goto(URL_LISTA, wait_until="networkidle")
    return page


def sessao_ativa(page):
    """False se o portal mandou pra tela de login (sessao expirou)."""
    try:
        if page.locator("input[type='password']").count() > 0:
            return False
        if "login" in page.url.lower() or "auth" in page.url.lower():
            return False
    except Exception:
        pass
    return True


# ------------------------------------------------------------- lista de OSs
def ler_cards(page):
    """Le numero/status/etapa/canal de todos os cards de uma vez (1 ida ao navegador)."""
    return page.locator(SEL_CARD_LISTA).evaluate_all("""cards => cards.map(c => {
        const v = t => {
            const s = c.querySelectorAll(`app-so-info-field[fieldtitle='${t}'] span.value`);
            return s.length > 1 ? s[1].innerText.trim() : '';
        };
        return {numero: v('soNumber').split('/')[0].trim(), status: v('status'),
                etapa: v('stage'), canal: v('channel'), aberto: v('open'),
                sinistro: v('serviceOrders.lossNumber')};
    })""")


def cards_do_sinistro(page, sinistro):
    campo = page.locator("app-so-info-field[fieldtitle='serviceOrders.lossNumber'] span.value",
                         has_text=re.compile(rf"^\s*{re.escape(sinistro)}\s*$"))
    return page.locator(SEL_CARD_LISTA).filter(has=campo)


def card_da_os(page, numero):
    campo = page.locator("app-so-info-field[fieldtitle='soNumber'] span.value",
                         has_text=re.compile(rf"^\s*{re.escape(numero)}\s*/"))
    return page.locator(SEL_CARD_LISTA).filter(has=campo).first


def _abrir_tela_lista(page):
    """Garante a pagina da lista (com a barra de busca OU a barra de filtros)."""
    pronta = f"{SEL_BUSCAR}, {SEL_FILTROS_BARRA}"
    if "/service-orders" in page.url and page.locator(pronta).count():
        return
    link = page.locator(SEL_MENU_LISTA).first
    if link.count():
        link.click()
    else:
        page.goto(URL_LISTA, wait_until="networkidle")
    if not sessao_ativa(page):
        raise SessaoExpirada()
    try:
        page.wait_for_selector(pronta, timeout=20000)
    except PWTimeout:
        # o portal pode demorar a redirecionar para o login: confere de novo
        if not sessao_ativa(page):
            raise SessaoExpirada()
        raise
    page.wait_for_timeout(1000)


def _escolher_status(page, valor):
    page.locator(SEL_FILTRO_STATUS).first.click()
    page.wait_for_timeout(600)
    page.locator("mat-option", has_text=re.compile(rf"^\s*{re.escape(valor)}\s*$")).first.click()
    page.wait_for_timeout(1500)


def _modo_busca(page):
    if page.locator(SEL_BUSCAR).count() == 0:
        page.locator(SEL_FILTROS_LUPA).first.click()  # sai do modo filtros
        page.wait_for_selector(SEL_BUSCAR, timeout=10000)
        page.wait_for_timeout(500)


def _marcar_cards_velhos(page):
    """Carimba os cards na tela antes de mandar recarregar (ver _esperar_lista_nova)."""
    page.evaluate("""() => document.querySelectorAll('app-so-card')
                             .forEach(c => c.setAttribute('data-robo-velho', '1'))""")


def _esperar_lista_nova(page, timeout=20000):
    """
    Espera o portal trocar a lista: so segue quando ha cards e nenhum e da lista
    anterior. Esperar tempo fixo nao basta - em 03/11 o robo leu a lista velha
    (26 OSs em vez de 32; busca por sinistro "achando" 11 OSs).
    """
    page.wait_for_function(
        """() => { const cs = document.querySelectorAll('app-so-card');
                   return cs.length > 0 && ![...cs].some(c => c.hasAttribute('data-robo-velho')); }""",
        timeout=timeout)
    page.wait_for_timeout(500)


def _filtrar_status_nova(page, status=STATUS_ALVO):
    """Barra de filtros com Status da OS = Nova (ou outro). A lista recarrega a cada troca."""
    # a ultima busca APLICADA continua valendo junto com o filtro, mesmo com o campo
    # apagado: limpa o campo e clica Buscar para o portal esquecer a busca anterior
    _modo_busca(page)
    if page.locator(SEL_BUSCA_TEXTO).first.input_value():
        page.locator(SEL_BUSCA_TEXTO).first.fill("")
    page.locator(SEL_BUSCAR).click()
    page.wait_for_timeout(1500)
    if page.locator(SEL_FILTROS_BARRA).count() == 0:
        page.locator(SEL_ABRIR_FILTROS).first.click()
        page.wait_for_selector(SEL_FILTRO_STATUS, timeout=10000)
        page.wait_for_timeout(800)
    if norm(page.locator(SEL_FILTRO_STATUS).first.inner_text()) == norm(status):
        _escolher_status(page, "Todos")  # escolher o mesmo valor nao recarrega a lista
    _marcar_cards_velhos(page)
    _escolher_status(page, status)
    if norm(page.locator(SEL_FILTRO_STATUS).first.inner_text()) != norm(status):
        raise RuntimeError(f"nao consegui aplicar o filtro Status da OS = {status}")


def _escolher_tipo_busca(page, tipo):
    sel = page.locator(SEL_BUSCA_TIPO + " mat-select").first
    if norm(sel.inner_text()) == norm(tipo):
        return
    sel.click()
    page.wait_for_timeout(600)
    page.locator("mat-option", has_text=re.compile(rf"^\s*{re.escape(tipo)}\s*$")).first.click()
    page.wait_for_timeout(600)
    if norm(sel.inner_text()) != norm(tipo):
        raise RuntimeError(f"nao consegui escolher o tipo de busca '{tipo}'")


def _buscar_numero(page, numero, qualquer_status=False, tipo=TIPO_BUSCA_OS):
    """
    Barra de busca com o numero (da OS ou do sinistro, conforme o tipo). O filtro de
    status continua valendo na busca: qualquer_status=True volta ele para "Todos" antes.
    """
    if qualquer_status:
        if page.locator(SEL_FILTROS_BARRA).count() == 0:
            page.locator(SEL_ABRIR_FILTROS).first.click()
            page.wait_for_selector(SEL_FILTRO_STATUS, timeout=10000)
            page.wait_for_timeout(800)
        if norm(page.locator(SEL_FILTRO_STATUS).first.inner_text()) != "todos":
            _escolher_status(page, "Todos")
    _modo_busca(page)
    _escolher_tipo_busca(page, tipo)
    campo = page.locator(SEL_BUSCA_TEXTO).first
    if campo.input_value() != numero:
        campo.fill(numero)
    _marcar_cards_velhos(page)
    page.locator(SEL_BUSCAR).click()


def ir_para_lista(page, ate_achar=None, busca="", qualquer_status=False, tipo=TIPO_BUSCA_OS,
                  status=STATUS_ALVO, rolagens=None):
    """
    Sem busca: lista so as OSs com Status = Nova (filtro do portal) e rola ate o fim.
    Com busca: pesquisa o numero (OS ou sinistro) - resultado curto, sem rolar.
    """
    _abrir_tela_lista(page)
    if busca:
        _buscar_numero(page, busca, qualquer_status, tipo)
    else:
        _filtrar_status_nova(page, status)
    _esperar_lista_nova(page)
    if busca and (not ate_achar or card_da_os(page, ate_achar).count()):
        return

    # o portal as vezes demora a trazer o proximo lote: so considera o fim depois
    # de 3 rolagens seguidas sem cards novos
    container = page.locator(SEL_RESULTADOS)
    anterior, parado = -1, 0
    for _ in range(rolagens or cfg.get("max_rolagens", 30)):
        if ate_achar and card_da_os(page, ate_achar).count():
            return
        atual = page.locator(SEL_CARD_LISTA).count()
        if atual == anterior:
            parado += 1
            if parado >= 3:
                break
        else:
            parado = 0
        anterior = atual
        # sobe um pouco antes de descer: se ja estiver no fim, rolar "pro fim" nao
        # dispara evento e o portal nao carrega o proximo lote
        container.evaluate("el => el.scrollTo(0, Math.max(0, el.scrollHeight - el.clientHeight - 300))")
        page.wait_for_timeout(200)
        container.evaluate("el => el.scrollTo(0, el.scrollHeight)")
        page.wait_for_timeout(2000)


def espera_cliente(etapa, canal):
    return etapa == ETAPA_INICIAR and norm(canal) in CANAIS_CONFIRMAR_CLIENTE


def etapa_e_alvo(etapa, canal, numero=""):
    if etapa not in ETAPAS_ATIVAS:
        return False
    # Logistica: so inicia depois que a equipe marcar que o cliente confirmou o envio
    if espera_cliente(etapa, canal):
        return numero in contatos.confirmados()
    # Balcao (03/11): primeiro o aviso por WhatsApp, so depois do "Ja avisei" inicia
    if etapa == ETAPA_INICIAR and norm(canal) == CANAL_BALCAO:
        return numero in contatos.balcao_avisados()
    if etapa == ETAPA_AGENDAR and canal != CANAL_LEVA_TRAZ:
        return False
    return True


def listar_os_pendentes(page):
    """
    Devolve ([(numero, etapa)] das OSs com Status=Nova numa etapa ativa,
             [numeros] das OSs da Logistica esperando contato com o cliente,
             [numeros] das OSs de Balcao ainda em Iniciar OS - para o aviso,
             {numero: {"dias": dias em aberto, "sinistro": nº}} das OSs Novas,
             [numeros] das Leva & Traz em Retirar Produto - so para a planilha).
    """
    ir_para_lista(page)
    pendentes, logistica, balcao, vistos, extras, retirar = [], [], [], set(), {}, []
    for card in ler_cards(page):
        num = card["numero"]
        if not num or num in vistos:  # a lista as vezes repete cards
            continue
        vistos.add(num)
        if card["status"] != STATUS_ALVO:
            continue
        if card["sinistro"]:
            SINISTRO_DA_OS[num] = card["sinistro"]
        extras[num] = {"sinistro": card["sinistro"], "etapa": card["etapa"]}
        if so_digitos(card["aberto"]):  # "12 dias"
            extras[num]["dias"] = int(so_digitos(card["aberto"]))
        # regra do dono (03/11): so OSs com ate `dias_maximo` dias em aberto (padrao 120);
        # as mais antigas ficam fora de tudo (iniciar, contato, planilha)
        if extras[num].get("dias", 0) > DIAS_MAXIMO:
            continue
        etapa, canal = card["etapa"], card["canal"]
        if espera_cliente(etapa, canal):
            logistica.append(num)
        elif etapa == ETAPA_INICIAR and norm(canal) == CANAL_BALCAO:
            balcao.append(num)
        elif etapa == ETAPA_RETIRAR and canal == CANAL_LEVA_TRAZ:
            retirar.append(num)
        if etapa_e_alvo(etapa, canal, num):
            pendentes.append((num, etapa))
    return pendentes, logistica, balcao, extras, retirar


def sinistro_conhecido(numero):
    """Sinistro da OS: lista do ciclo, depois tarefas (tarefas.json) e memoria (historico.json)."""
    if not numero:
        return None
    sin = SINISTRO_DA_OS.get(numero)
    if not sin:
        tarefas = contatos.carregar()
        sin = (tarefas["os"].get(numero, {}).get("sinistro")
               or tarefas["planilha"].get(numero, {}).get("sinistro")
               or historico._ler()["os"].get(numero, {}).get("sinistro"))
        if sin:
            SINISTRO_DA_OS[numero] = sin
    return sin


def abrir_os_pelo_numero(page, numero=None, qualquer_status=False, sinistro=None, exigir_sinistro=False):
    """
    Abre a OS navegando como uma pessoa: busca -> card -> setinha (>).
    O portal nao renderiza quando a URL da OS e aberta direto.
    Busca pelo Nº do Sinistro (como a equipe); sem sinistro conhecido, pelo nº da OS -
    exceto com exigir_sinistro=True (iniciar/agendar, pedido do dono em 03/11): ai so
    pelo sinistro, e sem ele a OS fica para o proximo ciclo.
    So com o sinistro: precisa existir 1 OS so para ele (senao, use o nº da OS).
    Por padrao so acha OS Nova (o filtro da lista continua valendo).
    """
    sinistro = sinistro or sinistro_conhecido(numero)
    if not sinistro and exigir_sinistro:
        raise RuntimeError(f"OS {numero}: nao sei o nº do sinistro - so busco pelo sinistro para "
                           "iniciar/agendar; tento de novo no proximo ciclo")
    log(f"{rotulo(numero) if numero else 'sinistro ' + sinistro}: buscando no portal pelo "
        + (f"Nº do Sinistro {sinistro}" if sinistro else f"Nº da OS {numero}"))
    if sinistro:
        ir_para_lista(page, ate_achar=numero, busca=sinistro, qualquer_status=qualquer_status,
                      tipo=TIPO_BUSCA_SINISTRO)
        if not numero:
            cards = cards_do_sinistro(page, sinistro)
            oss = sorted({c["numero"] for c in ler_cards(page)} if cards.count() else set())
            if len(oss) != 1:
                raise RuntimeError(f"sinistro {sinistro}: {len(oss)} OS encontradas {oss} - "
                                   "informe o nº da OS")
            numero = oss[0]
            SINISTRO_DA_OS[numero] = sinistro
    else:
        ir_para_lista(page, ate_achar=numero, busca=numero, qualquer_status=qualquer_status)
    card = card_da_os(page, numero)
    if card.count() == 0:
        raise RuntimeError(f"nao achei o card do {rotulo(numero)} na lista")
    rolar_ate(card)
    seta = card.locator(SEL_SETA_CARD).first
    if seta.count() == 0 or not seta.is_visible():
        seta = card.locator("mat-card").first  # fallback: clica no card inteiro
    seta.click()
    page.wait_for_selector(SEL_NUM_OS, timeout=20000)
    page.wait_for_timeout(1000)
    return numero


# ----------------------------------------------------------- pagina da OS
def ler_cabecalho(page):
    return {
        "numero": page.locator(SEL_NUM_OS).inner_text().split("/")[0].strip(),
        "status": page.locator(SEL_STATUS).inner_text().strip(),
        "etapa": page.locator(SEL_ETAPA).inner_text().strip(),
        "canal": page.locator(SEL_CANAL).inner_text().strip(),
        "sinistro": page.locator(SEL_SINISTRO).inner_text().strip(),
    }


def conferir_os(page, numero, etapa_esperada):
    """Segunda conferencia, ja dentro da OS: numero, status, etapa e canal."""
    cab = ler_cabecalho(page)
    if cab["numero"] != numero:
        raise RuntimeError(f"abri a OS errada: tela mostra '{cab['numero']}'")
    if cab["status"] != STATUS_ALVO:
        raise RuntimeError(f"status nao e '{STATUS_ALVO}' (esta '{cab['status']}') - nao vou mexer")
    if cab["etapa"] != etapa_esperada:
        raise RuntimeError(f"etapa nao e '{etapa_esperada}' (esta em '{cab['etapa']}')")
    if not etapa_e_alvo(cab["etapa"], cab["canal"], numero):
        raise RuntimeError(f"etapa '{cab['etapa']}' no canal '{cab['canal']}' nao esta liberada")
    return cab


def ler_info(page):
    """Le o bloco Informacoes da OS, so com os campos da lista branca."""
    if page.locator(SEL_INFO_CAMPO).count() == 0:
        page.locator(SEL_INFO_TOGGLE).first.click()
    page.wait_for_selector(SEL_INFO_CAMPO, timeout=15000)
    page.wait_for_timeout(800)
    # o filtro roda dentro do navegador: campos fora da lista nem chegam no Python
    brutos = page.evaluate(
        """(permitidos) => {
            const norm = s => (s || '').normalize('NFD').replace(/[\\u0300-\\u036f]/g, '')
                                       .toLowerCase().replace(/\\s+/g, ' ').trim();
            const out = {};
            for (const el of document.querySelectorAll('#collapseInfo app-info-field')) {
                const rotulo = el.querySelector('.info-field-name');
                const nome = norm(rotulo && rotulo.innerText);
                if (!permitidos.includes(nome)) continue;
                const valor = el.querySelector('.info-field-value')
                              || [...el.querySelectorAll('span')].filter(s => s !== rotulo).pop();
                out[nome] = valor ? valor.innerText.trim() : '';
            }
            return out;
        }""",
        list(CAMPOS_INFO_PERMITIDOS),
    )
    return {CAMPOS_INFO_PERMITIDOS[k]: v for k, v in brutos.items()}


def telefones_validos(info):
    """Celular primeiro, depois residencial; so digitos com DDD (10 ou 11), sem repetir."""
    tels = []
    for bruto in (info.get("tel_cel"), info.get("tel_res")):
        d = so_digitos(bruto)
        if d.startswith("55") and len(d) > 11:
            d = d[2:]
        d = d.lstrip("0")
        if len(d) in (10, 11) and d not in tels:
            tels.append(d)
    return tels


def escolher_telefone(info):
    tels = telefones_validos(info)
    return tels[0] if tels else ""


# ------------------------------------------------------------ campos/cards
def abrir_card(page, card, sel_collapse, sel_toggle, sel_editar, sel_campo):
    """Expande o card e, se os campos estiverem travados, clica Editar."""
    page.wait_for_selector(card, timeout=15000)
    rolar_ate(page.locator(card).first)
    if page.locator(f"{sel_collapse}.show").count() == 0:
        page.locator(sel_toggle).first.click()
        page.wait_for_selector(f"{sel_collapse}.show", timeout=5000)
        page.wait_for_timeout(600)
    if page.locator(sel_campo).first.is_disabled():
        page.locator(sel_editar).first.click()
        page.wait_for_timeout(600)


def preencher_data(page, seletor, valor):
    """input date/datetime-local nativo: fill no formato ISO + change."""
    campo = page.locator(seletor).first
    if campo.input_value() != valor:
        campo.fill(valor)
        campo.dispatch_event("change")
        page.wait_for_timeout(300)
    return campo.input_value()


def preencher(page, seletor, valor, digitar=False):
    """Apaga e escreve. digitar=True tecla por tecla (campos com mascara)."""
    campo = page.locator(seletor).first
    campo.click()
    campo.fill("")
    if digitar and valor:
        campo.press_sequentially(valor, delay=40)
    else:
        campo.fill(valor)
    campo.dispatch_event("blur")
    return campo.input_value()


def escolher_dropdown(page, escopo, componente, rotulo, valor):
    """Dropdown Bootstrap do portal (nao e <select>); achado pelo rotulo, nao pelo id."""
    base = f"{escopo} {componente}:has(span:text-is('{rotulo}'))"
    botao = page.locator(f"{base} button.dropdown-toggle").first
    if norm(botao.inner_text()) == norm(valor):
        return botao.inner_text().strip()
    botao.click()
    page.wait_for_timeout(400)
    opcoes = page.locator(f"{base} a.dropdown-item")
    opcao = opcoes.filter(has_text=re.compile(rf"^\s*{re.escape(valor)}\s*$"))
    if opcao.count() == 0:
        disponiveis = [o.strip() for o in opcoes.all_inner_texts()]
        raise RuntimeError(f"'{valor}' nao esta no dropdown '{rotulo}'. Opcoes: {disponiveis}")
    opcao.first.click()
    page.wait_for_timeout(400)
    return botao.inner_text().strip()


def ler_etapa(page):
    try:
        return page.locator(SEL_ETAPA).inner_text(timeout=5000).strip()
    except Exception:
        return "?"


def clicar_e_reler_etapa(page, seletor, etapa_antes, numero):
    """
    Clica no botao final e espera a etapa mudar (ate 20 s). Se a tela nao atualizar,
    reabre a OS e confere de novo - em 03/11 o Iniciar da OS 2000003 deu certo, mas
    o robo releu em 2 s, viu a etapa antiga e registrou "cliquei mas nao mudou".
    """
    page.locator(seletor).click()
    for _ in range(40):
        page.wait_for_timeout(500)
        etapa = ler_etapa(page)
        if etapa not in (etapa_antes, "?"):
            return etapa
    log(f"{rotulo(numero)}: a tela nao atualizou em 20 s - reabrindo a OS para conferir")
    try:
        abrir_os_pelo_numero(page, numero, qualquer_status=True)
    except Exception as e:
        log(f"{rotulo(numero)}: nao consegui reabrir para conferir ({resumo_erro(e)})")
    return ler_etapa(page)


def concluir(page, numero, etapa, resumo, etapa_depois):
    """Sucesso = a etapa mudou. Nunca assume."""
    if etapa_depois == etapa:
        caminho = foto(page, f"suspeito_{numero}")
        registrar(numero, etapa, "CLIQUEI_MAS_ETAPA_NAO_MUDOU", f"{resumo} | print: {caminho}")
        log(f"{rotulo(numero)}: cliquei mas a etapa continua '{etapa_depois}' - confira")
        return "CLIQUEI_MAS_ETAPA_NAO_MUDOU"
    registrar(numero, etapa, "OK", f"{resumo}; etapa_depois={etapa_depois}")
    log(f"{rotulo(numero)}: OK (etapa agora: {etapa_depois})")
    return "OK"


# ------------------------------------------------------------ etapa: Iniciar OS
def fazer_iniciar(page, numero):
    abrir_card(page, CARD_INI, SEL_INI_COLLAPSE, SEL_INI_TOGGLE, SEL_INI_EDITAR, SEL_INI_DATA)
    data = preencher_data(page, SEL_INI_DATA, hoje_iso())
    if cfg.get("os_prestador"):
        page.locator(SEL_INI_OS_PRESTADOR).fill(cfg["os_prestador"])
    resp = escolher_dropdown(page, CARD_INI, "app-dropdown-field", "Responsável", cfg["responsavel"])
    resumo = f"data={data}; resp={resp}"
    aprovado = aprovada_pela_equipe(numero)
    if aprovado:
        resumo += "; cliente confirmou o envio"
    elif numero in contatos.balcao_avisados():
        resumo += "; cliente avisado (balcao)"
    log(f"{rotulo(numero)}: {resumo}")

    # sem pergunta no balao: a decisao humana ja foi o Confirmou / Ja avisei
    if not confirmar(numero, ETAPA_INICIAR, "Clicar em INICIAR", resumo, aprovado, perguntar=False):
        return "DRY_RUN" if cfg.get("dry_run") else "PULADA"
    return concluir(page, numero, ETAPA_INICIAR, resumo,
                    clicar_e_reler_etapa(page, SEL_INI_BOTAO, ETAPA_INICIAR, numero))


# ------------------------------------------------------ etapa: Agendar Retirada
def linha_planilha(page, numero, info):
    """Monta a linha Sinistro|Cidade|Produto|Objetivo da aba Atendimentos (pagina da OS aberta)."""
    marca = page.locator(SEL_MARCA).inner_text().strip()
    contatos.adicionar_linha(
        numero,
        sinistro=page.locator(SEL_SINISTRO).inner_text().strip(),
        cidade=(info.get("cidade") or "").strip().upper(),
        produto=(info.get("produto") or page.locator(SEL_PRODUTO).inner_text()).strip().upper(),
        objetivo=cfg.get("objetivo_leva_traz", "COLETA SEGURADORA / {marca}").format(marca=marca.upper()),
    )
    painel.contatos_mudaram()


def fazer_agendar(page, numero):
    # dados SEMPRE da propria OS: o card ja veio preenchido com cliente de outra OS
    info = ler_info(page)
    nome = info.get("nome", "").strip()
    cep = so_digitos(info.get("cep"))
    tel = escolher_telefone(info)
    obrigatorios = {"nome": nome, "CEP": cep, "telefone": tel, "endereco": info.get("endereco"),
                    "numero": info.get("numero"), "bairro": info.get("bairro")}
    faltando = [k for k, v in obrigatorios.items() if not (v or "").strip()]
    if faltando:
        raise RuntimeError(f"Informacoes da OS sem: {', '.join(faltando)} - preencha a mao")

    abrir_card(page, CARD_AR, SEL_AR_COLLAPSE, SEL_AR_TOGGLE, SEL_AR_EDITAR, SEL_AR_DATA_CONTATO)
    data_contato = preencher_data(page, SEL_AR_DATA_CONTATO, hoje_iso())
    resp = escolher_dropdown(page, CARD_AR, "app-dropdown-field", "Responsável", cfg["responsavel"])
    escolher_dropdown(page, CARD_AR, "app-boolean-dropdown-field", "Contato efetuado com cliente?", "Sim")
    page.wait_for_selector(f"{SEL_AR_COM_CONTATO} input", timeout=10000)

    preencher(page, campo_ar("Contato realizado com"), nome)
    agendamento = preencher_data(page, SEL_AR_DATA_AGENDA, datetime.now().strftime("%Y-%m-%dT%H:%M"))

    # CEP primeiro: se ele dispara busca automatica, o resto e escrito por cima depois
    preencher(page, SEL_AR_CEP, cep, digitar=True)
    page.wait_for_timeout(2500)
    esperado = {
        SEL_AR_TELEFONE: tel,
        campo_ar("Endereço"): info["endereco"].strip(),
        campo_ar("Nº"): info["numero"].strip(),
        campo_ar("Bairro"): info["bairro"].strip(),
        campo_ar("Complemento"): (info.get("complemento") or "").strip(),
    }
    for seletor, valor in esperado.items():
        preencher(page, seletor, valor, digitar=(seletor == SEL_AR_TELEFONE))

    # confere tudo antes de salvar
    esperado[SEL_AR_CEP] = cep
    esperado[campo_ar("Contato realizado com")] = nome
    divergentes = []
    for seletor, valor in esperado.items():
        atual = page.locator(seletor).first.input_value()
        if seletor in (SEL_AR_CEP, SEL_AR_TELEFONE):
            ok = so_digitos(atual) == so_digitos(valor)
        else:
            ok = norm(atual) == norm(valor)
        if not ok:
            divergentes.append(seletor.split("'")[-2] if "'" in seletor else seletor)
    if divergentes:
        raise RuntimeError(f"campos nao ficaram como a OS: {divergentes} - nao salvei")

    cidade_card = page.locator(campo_ar("Cidade")).first.input_value().strip()
    if cidade_card and norm(cidade_card) != norm(info.get("cidade")):
        raise RuntimeError(f"cidade do card ('{cidade_card}') diferente da OS "
                           f"('{info.get('cidade')}') - nao salvei")

    resumo = (f"contato={data_contato}; agendamento={agendamento}; resp={resp}; "
              f"cidade={info.get('cidade')}; cep={cep}")
    if not cidade_card:
        resumo += "; cidade do card vazia"
    log(f"{rotulo(numero)}: {resumo}")

    # linha para a aba Atendimentos da planilha: so depois da conferencia (se o robo
    # recusou salvar, nao vai pra planilha); em dry_run tambem gera, pra equipe ver
    linha_planilha(page, numero, info)

    if not confirmar(numero, ETAPA_AGENDAR, "Salvar o AGENDAMENTO DE RETIRADA", resumo):
        return "DRY_RUN" if cfg.get("dry_run") else "PULADA"
    return concluir(page, numero, ETAPA_AGENDAR, resumo,
                    clicar_e_reler_etapa(page, SEL_AR_SALVAR, ETAPA_AGENDAR, numero))


ACOES_POR_ETAPA = {
    ETAPA_INICIAR: fazer_iniciar,
    ETAPA_AGENDAR: fazer_agendar,
}


# ------------------------------------------------------- anotacao (Mensagens)
ETAPA_ANOTAR_LOGISTICA = "Anotacao aguardando produto"
ANOTACAO_LOGISTICA = cfg.get("anotacao_logistica",
                             "Prezados, feito contato com o cliente, que confirmou que fará o envio "
                             "do produto. Aguardando o envio!")


def anotar(page, numero, texto, tipo=TIPO_ANOTACAO, sinistro=None, etapa="Anotacao", aprovado=False):
    """Escreve uma mensagem no card MENSAGENS da OS (ex.: codigo de rastreio)."""
    numero = abrir_os_pelo_numero(page, numero, qualquer_status=True, sinistro=sinistro)
    return anotar_na_os_aberta(page, numero, texto, tipo, etapa, aprovado)


def anotar_na_os_aberta(page, numero, texto, tipo=TIPO_ANOTACAO, etapa="Anotacao", aprovado=False):
    """Igual ao anotar(), com a pagina da OS ja aberta (ex.: logo depois do Iniciar)."""
    cab = ler_cabecalho(page)
    if cab["numero"] != numero:
        raise RuntimeError(f"abri a OS errada: tela mostra '{cab['numero']}'")
    if cab["status"] == "Cancelada":
        raise RuntimeError("OS cancelada - nao vou anotar")

    abrir_card(page, CARD_MSG, SEL_MSG_COLLAPSE, SEL_MSG_TOGGLE, SEL_MSG_TEXTO, SEL_MSG_TEXTO)
    escolher_dropdown(page, CARD_MSG, "app-dropdown-field", "Tipo de mensagem", tipo)
    preencher(page, SEL_MSG_TEXTO, texto)
    resumo = f"tipo={tipo}; texto={texto[:80]}"

    if not confirmar(numero, etapa, f"Enviar a anotação '{texto[:40]}'", resumo, aprovado):
        return "DRY_RUN" if cfg.get("dry_run") else "PULADA"
    page.locator(SEL_MSG_ENVIAR).click()
    for _ in range(20):  # sucesso = a mensagem aparece no historico
        page.wait_for_timeout(500)
        if texto[:30] in page.locator(SEL_MSG_HISTORICO).inner_text():
            registrar(numero, etapa, "OK", resumo)
            return "OK"
    caminho = foto(page, f"anotacao_{numero}")
    registrar(numero, etapa, "ERRO", f"{resumo}; nao apareceu no historico | print: {caminho}")
    return "ERRO"


def anotar_aguardando_produto(page, numero, ja_aberta=True):
    """Logistica iniciada: deixa na OS que o cliente confirmou e o produto esta a caminho."""
    try:
        if ja_aberta:
            st = anotar_na_os_aberta(page, numero, ANOTACAO_LOGISTICA,
                                     etapa=ETAPA_ANOTAR_LOGISTICA, aprovado=True)
        else:
            st = anotar(page, numero, ANOTACAO_LOGISTICA, etapa=ETAPA_ANOTAR_LOGISTICA, aprovado=True)
    except Exception as e:
        caminho = foto(page, f"erro_anotacao_{numero}")
        registrar(numero, ETAPA_ANOTAR_LOGISTICA, "ERRO", f"{resumo_erro(e)} | print: {caminho}")
        st = "ERRO"
    if st == "OK":
        contatos.marcar_anotada(numero)
    return st


def reconciliar_confirmadas(extras):
    """
    Logistica "Confirmou" que ja saiu de Iniciar OS (o robo iniciou mas achou que nao,
    ou alguem iniciou a mao): marca como iniciada - a anotacao entra em seguida em
    anotacoes_pendentes() - e, se a memoria tinha "clique sem efeito", corrige para OK.
    """
    for numero in contatos.confirmados():
        etapa = extras.get(numero, {}).get("etapa")
        if not etapa or etapa == ETAPA_INICIAR:
            continue
        contatos.marcar(numero, "iniciada")
        ultimo = historico._ler()["os"].get(numero, {}).get("acoes", {}).get(ETAPA_INICIAR, {})
        if ultimo.get("status") != "OK":
            registrar(numero, ETAPA_INICIAR, "OK", f"etapa ja era '{etapa}' no ciclo seguinte")
        log(f"{rotulo(numero)}: ja esta em '{etapa}' - marcada como iniciada")
        painel.contatos_mudaram()


ETAPA_ANOTAR_RECUSA = "Anotacao cliente nao envia"
ANOTACAO_NAO_ENVIA = cfg.get("anotacao_nao_envia",
                             "Prezados, feito contato com o cliente, que informou que não fará o envio "
                             "do produto para o conserto.")


def anotacoes_pendentes(page):
    """
    Anotacoes que o robo deve na OS: Logistica iniciada sem "aguardando o envio" (falhou
    antes) e Logistica em que o cliente NAO vai enviar (pedido do dono 04/11: anota na OS
    e a tarefa sai da lista de vez - situacao "encerrada").
    """
    problemas = []
    aprovado = cfg.get("confirmou_inicia_mesmo_em_teste", True)  # o clique da equipe aprovou
    for numero in contatos.recusados_sem_anotacao():
        if painel.deve_sair() or not historico.pode_fazer(numero, ETAPA_ANOTAR_RECUSA, cfg)[0]:
            continue
        painel.checar_pausa()
        painel.estado("trabalhando", "Anotando na OS", f"{rotulo(numero)} - cliente não vai enviar")
        try:
            st = anotar(page, numero, ANOTACAO_NAO_ENVIA, tipo=cfg.get("tipo_anotacao_nao_envia", TIPO_ANOTACAO),
                        sinistro=sinistro_conhecido(numero), etapa=ETAPA_ANOTAR_RECUSA, aprovado=aprovado)
        except Exception as e:
            caminho = foto(page, f"erro_anotacao_{numero}")
            registrar(numero, ETAPA_ANOTAR_RECUSA, "ERRO", f"{resumo_erro(e)} | print: {caminho}")
            st = "ERRO"
        if st == "OK":
            contatos.marcar(numero, "encerrada")
            painel.contatos_mudaram()
        elif st == "ERRO":
            problemas.append(f"{rotulo(numero)}: não consegui anotar 'cliente não vai enviar'")
    for numero in contatos.sem_anotacao():
        if painel.deve_sair() or not historico.pode_fazer(numero, ETAPA_ANOTAR_LOGISTICA, cfg)[0]:
            continue
        painel.checar_pausa()
        painel.estado("trabalhando", "Anotando na OS", f"{rotulo(numero)} - aguardando produto")
        if anotar_aguardando_produto(page, numero, ja_aberta=False) != "OK":
            problemas.append(f"{rotulo(numero)}: OS iniciada, mas não consegui anotar 'aguardando o envio'")
    return problemas


# --------------------------------------------- contato com cliente (Logistica)
def preparar_contatos(page, numeros, tipo):
    """
    So leitura: para cada OS (Logistica ou Balcao) que ainda nao esta na fila de
    contatos, abre a OS e guarda primeiro nome, produto, cidade e telefone.
    """
    if tipo == contatos.LOGISTICA:
        contatos.alterar(lambda d: d.__setitem__("ativos", list(numeros)))
    ja_na_fila = contatos.carregar()["os"]
    problemas = []
    for numero in numeros:
        # tarefas gravadas antes de 03/11 so tinham 1 telefone: rele para pegar todos
        if (numero in ja_na_fila and "telefones" in ja_na_fila[numero]) or painel.deve_sair():
            continue
        if not historico.pode_fazer(numero, f"Contato {tipo}", cfg)[0]:
            continue
        painel.checar_pausa()
        painel.estado("trabalhando", "Preparando contato",
                      f"{rotulo(numero)} - {'Logística' if tipo == contatos.LOGISTICA else 'Balcão'}")
        try:
            abrir_os_pelo_numero(page, numero)
            info = ler_info(page)
            tel = escolher_telefone(info)
            item = {
                "os": numero,
                "sinistro": SINISTRO_DA_OS.get(numero) or page.locator(SEL_SINISTRO).inner_text().strip(),
                "tipo": tipo,
                "nome": contatos.bonito((info.get("nome") or "").split(" ")[0]),
                "produto": contatos.bonito(info.get("produto")),
                "cidade": contatos.bonito(info.get("cidade")),
                "telefone": tel,
                "telefones": telefones_validos(info),
                "situacao": "pronta",
                "pronta_em": contatos.agora(),
            }
            if numero in ja_na_fila:  # so completa os telefones, mantem a situacao
                def completar(d, n=numero, tels=item["telefones"]):
                    d["os"][n]["telefones"] = tels
                    d["os"][n]["telefone"] = d["os"][n].get("telefone") or (tels[0] if tels else "")
                contatos.alterar(completar)
                continue
            contatos.alterar(lambda d: d["os"].__setitem__(numero, item))
            registrar(numero, f"Contato {tipo}", "CONTATO_PRONTO",
                      f"cidade={item['cidade']}" + ("" if tel else "; SEM TELEFONE"))
            if not tel:
                problemas.append(f"{rotulo(numero)}: sem telefone celular nas Informações")
        except Exception as e:
            caminho = foto(page, f"erro_contato_{numero}")
            registrar(numero, f"Contato {tipo}", "ERRO", f"{resumo_erro(e)} | print: {caminho}")
            problemas.append(f"{rotulo(numero)}: {resumo_erro(e)}")
    painel.contatos_mudaram()
    return problemas


def preparar_planilha(page, numeros):
    """
    So leitura: Leva & Traz ja em Retirar Produto (agendadas a mao, ou pelo robo antes)
    que ainda nao tem linha na fila da planilha.
    """
    ja_na_fila = contatos.carregar()["planilha"]
    problemas = []
    for numero in numeros:
        if numero in ja_na_fila or painel.deve_sair():
            continue
        if not historico.pode_fazer(numero, "Planilha", cfg)[0]:
            continue
        painel.checar_pausa()
        painel.estado("trabalhando", "Preparando linha da planilha", f"{rotulo(numero)} - Leva & Traz")
        try:
            abrir_os_pelo_numero(page, numero)
            linha_planilha(page, numero, ler_info(page))
            registrar(numero, "Planilha", "LINHA_PRONTA", "Leva & Traz em Retirar Produto")
        except Exception as e:
            caminho = foto(page, f"erro_planilha_{numero}")
            registrar(numero, "Planilha", "ERRO", f"{resumo_erro(e)} | print: {caminho}")
            problemas.append(f"{rotulo(numero)}: {resumo_erro(e)}")
    return problemas


# ------------------------------------------------ depois do conserto: Assinar OS
# Pedido do dono (03-04/11). Mapeado no portal em 04/11 (sinistro 1000004 / OS 2000004):
# a OS consertada fica com Status "Concluida" na etapa "Assinar OS" ate alguem preencher
# o card ASSINAR OS (Data de finalizacao + Responsavel) e clicar no botao do card
# ("Salvar"). Nao tem card dos Correios nessa etapa: o rastreio vai como anotacao.
# (As "Despesas Aprovadas" em Assinar OS eram todas perda total ou ja assinadas.)
#   Logistica   a equipe cola o codigo de rastreio na janela -> o robo anota o codigo
#               na OS, assina, e o WhatsApp com o rastreio fica pronto para o cliente.
#   Leva & Traz assina quando a equipe clica "Finalizar atendimento" (depois da visita).
#   Balcao      espera o cliente retirar ("Cliente retirou") e assina.
# Perda total (card vermelho) e card ja assinado: nada a fazer.
STATUS_POS_CONSERTO = cfg.get("status_pos_conserto", "Concluída")
ETAPA_ASSINAR = "Assinar OS"
ETAPA_TRIAGEM = "Triagem Assinar OS"
ETAPA_ANOTAR_RASTREIO = "Anotacao rastreio"
SEL_PERDA_TOTAL = "app-so-canceled-total-loss"
ANOTACAO_RASTREIO = cfg.get("anotacao_rastreio",
                            "Prezados, produto consertado e devolvido ao cliente pelos Correios. "
                            "Código de rastreio: {codigo}")

CARD_ASS = "app-so-finish-repair"  # card ASSINAR OS (#cardFinishRepair)
SEL_ASS_COLLAPSE = "#collapseFinishRepair"
SEL_ASS_TOGGLE = f"{CARD_ASS} button[data-target='#collapseFinishRepair']"
SEL_ASS_EDITAR = f"{CARD_ASS} button:has-text('Editar')"
SEL_ASS_DATA = f"{CARD_ASS} input[type='date']"
SEL_ASS_BOTAO = f"{CARD_ASS} button.btn-primary"  # "Salvar" = assinar

_ultima_varredura = {"quando": 0.0}


def _expandir(page, card, sel_collapse, sel_toggle):
    page.wait_for_selector(card, timeout=15000)
    rolar_ate(page.locator(card).first)
    if page.locator(f"{sel_collapse}.show").count() == 0:
        page.locator(sel_toggle).first.click()
        page.wait_for_selector(f"{sel_collapse}.show", timeout=5000)
        page.wait_for_timeout(800)


def assinatura_feita(page):
    """Card ASSINAR OS ja salvo: mostra 'Data de finalizacao: dd/mm/aaaa' sem campo de data."""
    _expandir(page, CARD_ASS, SEL_ASS_COLLAPSE, SEL_ASS_TOGGLE)
    if page.locator(f"{SEL_ASS_DATA}:visible").count():
        return False
    texto = page.locator(CARD_ASS).first.inner_text()
    return bool(re.search(r"finaliza\S*\s*:?\s*\d\d/\d\d/\d{4}", texto))


def listar_para_assinar(page):
    """{numero: card} das OSs Concluida em Assinar OS (lista curta: ~15 s)."""
    ir_para_lista(page, status=STATUS_POS_CONSERTO)
    achadas = {}
    for card in ler_cards(page):
        num = card["numero"]
        if num and card["status"] == STATUS_POS_CONSERTO and card["etapa"] == ETAPA_ASSINAR:
            if card["sinistro"]:
                SINISTRO_DA_OS[num] = card["sinistro"]
            achadas[num] = card
    return achadas


def _abrir_para_assinar(page, numero):
    """Abre pelo sinistro e confere status/etapa/perda total de novo, ja dentro da OS."""
    abrir_os_pelo_numero(page, numero, qualquer_status=True, exigir_sinistro=True)
    cab = ler_cabecalho(page)
    if cab["numero"] != numero:
        raise RuntimeError(f"abri a OS errada: tela mostra '{cab['numero']}'")
    if cab["status"] != STATUS_POS_CONSERTO:
        raise RuntimeError(f"status nao e '{STATUS_POS_CONSERTO}' (esta '{cab['status']}') - nao vou mexer")
    if cab["etapa"] != ETAPA_ASSINAR:
        raise RuntimeError(f"etapa nao e '{ETAPA_ASSINAR}' (esta em '{cab['etapa']}')")
    if page.locator(SEL_PERDA_TOTAL).count():
        raise RuntimeError("OS com perda total - nao vou mexer")
    return cab


def triar_para_assinar(page, numero, card):
    """So leitura, uma vez por OS: perda total / ja assinada -> nada a fazer; senao vira tarefa."""
    abrir_os_pelo_numero(page, numero, qualquer_status=True, exigir_sinistro=True)
    if page.locator(SEL_PERDA_TOTAL).count():
        return registrar(numero, ETAPA_TRIAGEM, "IGNORADA", "perda total")
    if assinatura_feita(page):
        return registrar(numero, ETAPA_TRIAGEM, "IGNORADA", "card ASSINAR OS ja preenchido")
    info = ler_info(page)
    item = {
        "os": numero, "sinistro": SINISTRO_DA_OS.get(numero) or card["sinistro"],
        "canal": card["canal"], "etapa": ETAPA_ASSINAR, "tipo": contatos.ASSINAR,
        "nome": contatos.bonito((info.get("nome") or "").split(" ")[0]),
        "produto": contatos.bonito(info.get("produto")), "cidade": contatos.bonito(info.get("cidade")),
        "telefone": escolher_telefone(info), "telefones": telefones_validos(info),
        "situacao": "pronta",
    }
    if norm(card["canal"]) in CANAIS_CONFIRMAR_CLIENTE and cfg.get("superfrete_ativo", True):
        item["cep"] = so_digitos(info.get("cep"))
        item["valor_nf"] = _valor(_ler_campo_info(page, r"valor de nota"))
        try:
            item["cotacao"] = cotar_etiqueta(item["cep"], item["valor_nf"])
        except Exception as e:  # sem cotacao a equipe ainda pode colar o codigo a mao
            item["aviso"] = f"Não consegui cotar no SuperFrete ({resumo_erro(e)[:80]})"
    contatos.adicionar_pos_conserto(item)
    registrar(numero, ETAPA_TRIAGEM, "TRIADA", f"{card['canal']} -> tarefa na janela")


def fazer_assinar(page, numero, aprovado):
    """Card ASSINAR OS: Data de finalizacao = hoje, Responsavel; clica no botao do card."""
    if assinatura_feita(page):
        registrar(numero, ETAPA_ASSINAR, "OK", "card ja estava assinado")
        return "OK"
    abrir_card(page, CARD_ASS, SEL_ASS_COLLAPSE, SEL_ASS_TOGGLE, SEL_ASS_EDITAR, SEL_ASS_DATA)
    data = preencher_data(page, SEL_ASS_DATA, hoje_iso())
    resp = escolher_dropdown(page, CARD_ASS, "app-dropdown-field", "Responsável", cfg["responsavel"])
    botao = page.locator(SEL_ASS_BOTAO).first
    resumo = f"data={data}; resp={resp}; botao='{botao.inner_text().strip()}'"
    log(f"{rotulo(numero)}: {resumo}")
    if not confirmar(numero, ETAPA_ASSINAR, "Clicar em ASSINAR", resumo, aprovado, perguntar=False):
        return "DRY_RUN" if cfg.get("dry_run") else "PULADA"
    botao.click()
    # assinada = status/etapa mudam, ou o card volta travado mostrando a data
    for _ in range(40):
        page.wait_for_timeout(500)
        try:
            status = page.locator(SEL_STATUS).inner_text(timeout=2000).strip()
            if status != STATUS_POS_CONSERTO or ler_etapa(page) != ETAPA_ASSINAR or assinatura_feita(page):
                registrar(numero, ETAPA_ASSINAR, "OK", f"{resumo}; status={status}")
                log(f"{rotulo(numero)}: assinada (status {status})")
                return "OK"
        except Exception:
            pass
    caminho = foto(page, f"suspeito_assinar_{numero}")
    registrar(numero, ETAPA_ASSINAR, "CLIQUEI_MAS_ETAPA_NAO_MUDOU", f"{resumo} | print: {caminho}")
    return "CLIQUEI_MAS_ETAPA_NAO_MUDOU"


# ------------------------------------------- etiqueta dos Correios pelo SuperFrete
# Pedido do dono (04/11): na Logistica, em vez de entrar no SuperFrete e copiar o codigo,
# a janela mostra PAC/SEDEX ja cotados; a equipe clica no servico (aprovacao da cobranca)
# e o robo cria o envio, PAGA COM O SALDO, abre o PDF para imprimir e segue o fluxo de
# sempre (anota o rastreio na OS, assina, WhatsApp). Padrao do dono: origem = CEP do remetente,
# caixa 30x30x30 cm ate 3 kg (config caixa_padrao). Declaracao de conteudo com o produto
# e o valor da nota da OS. LGPD: o CPF do cliente (exigido pelo SuperFrete na declaracao
# de conteudo) e lido da OS SO aqui, vai so para a API e nunca e gravado nem logado -
# autorizado pelo dono em 04/11.
ETAPA_ETIQUETA = "Etiqueta SuperFrete"
UF = {"acre": "AC", "alagoas": "AL", "amapa": "AP", "amazonas": "AM", "bahia": "BA", "ceara": "CE",
      "distrito federal": "DF", "espirito santo": "ES", "goias": "GO", "maranhao": "MA",
      "mato grosso": "MT", "mato grosso do sul": "MS", "minas gerais": "MG", "para": "PA",
      "paraiba": "PB", "parana": "PR", "pernambuco": "PE", "piaui": "PI", "rio de janeiro": "RJ",
      "rio grande do norte": "RN", "rio grande do sul": "RS", "rondonia": "RO", "roraima": "RR",
      "santa catarina": "SC", "sao paulo": "SP", "sergipe": "SE", "tocantins": "TO"}


def _superfrete():
    import superfrete
    return superfrete.SuperFrete(cfg.get("superfrete_ambiente", "producao"), cfg.get("superfrete_email", ""))


def _caixa():
    return dict({"height": 30, "width": 30, "length": 30, "weight": 3}, **cfg.get("caixa_padrao", {}))


def _ler_campo_info(page, rotulo_regex):
    """Um campo do bloco Informacoes fora da lista branca (valor da nota; CPF so na etiqueta)."""
    if page.locator(SEL_INFO_CAMPO).count() == 0:
        page.locator(SEL_INFO_TOGGLE).first.click()
        page.wait_for_selector(SEL_INFO_CAMPO, timeout=15000)
    return page.evaluate("""(re) => { const rx = new RegExp(re, 'i');
        for (const el of document.querySelectorAll('#collapseInfo app-info-field')) {
            const n = el.querySelector('.info-field-name');
            if (n && rx.test(n.innerText)) { const v = el.querySelector('.info-field-value');
                return v ? v.innerText.trim() : ''; } }
        return ''; }""", rotulo_regex)


def _valor(texto):
    """'R$ 1.299,90' -> 1299.9"""
    t = re.sub(r"[^\d,]", "", texto or "").replace(",", ".")
    try:
        return round(float(t), 2)
    except ValueError:
        return 0.0


def cotar_etiqueta(cep, valor_nf):
    """[{id, nome, preco, dias}] para a janela. Cotacao nao cobra nada."""
    origem = cfg.get("remetente", {}).get("postal_code", "")
    opcoes = _superfrete().cotar(origem, cep, _caixa(), valor_declarado=valor_nf)
    return [{"id": s["id"], "nome": s["name"], "preco": float(s["price"]), "dias": s.get("delivery_time")}
            for s in opcoes]


def _destinatario(page, info):
    cpf = so_digitos(_ler_campo_info(page, r"^\s*cpf"))  # so para a API - nunca gravado/logado
    if len(cpf) != 11:
        raise RuntimeError("CPF do cliente não encontrado nas Informações da OS")
    uf = UF.get(unicodedata.normalize("NFD", (info.get("estado") or "").lower())
                .encode("ascii", "ignore").decode().strip(), (info.get("estado") or "")[:2].upper())
    tel = escolher_telefone(info)
    dest = {"name": (info.get("nome") or "")[:50], "address": (info.get("endereco") or "")[:50],
            "number": (info.get("numero") or "")[:10], "complement": (info.get("complemento") or "")[:20],
            "district": (info.get("bairro") or "NA")[:60], "city": (info.get("cidade") or "")[:50],
            "state_abbr": uf, "postal_code": so_digitos(info.get("cep")), "document": cpf, "email": None}
    if len(tel) == 11:
        dest["phone"] = tel
    faltando = [k for k in ("name", "address", "city", "postal_code") if not dest[k]]
    if faltando or len(dest["postal_code"]) != 8 or len(uf) != 2:
        raise RuntimeError(f"endereço do cliente incompleto na OS ({', '.join(faltando) or 'CEP/UF'})")
    return dest


def gerar_etiqueta(page, numero, item):
    """
    Cria o envio, PAGA com o saldo e devolve (codigo, url_pdf). Chamado so para tarefa que a
    equipe liberou clicando no servico. O id do envio e guardado ANTES de pagar: se algo cair
    no meio, o proximo ciclo continua do mesmo envio (nunca cria/paga dois).
    """
    sf = _superfrete()
    rem = cfg.get("remetente", {})
    if not all(rem.get(k) for k in ("name", "address", "district", "city", "state_abbr", "postal_code")):
        raise RuntimeError("remetente incompleto: preencha 'remetente' no config.json")
    id_envio = item.get("etiqueta_id")
    if not id_envio:
        info = ler_info(page)
        valor = item.get("valor_nf") or _valor(_ler_campo_info(page, r"valor de nota"))
        envio = sf.criar_envio(rem, _destinatario(page, info), item["servico"], _caixa(),
                               [{"name": (info.get("produto") or "Produto consertado")[:80], "quantity": 1,
                                 "unitary_value": valor}],
                               valor, declaracao_conteudo=True, etiqueta_interna=f"Sinistro {item.get('sinistro')}")
        id_envio = envio["id"]
        contatos.marcar_pos_conserto(item["chave"], "liberada", etiqueta_id=id_envio)
        preco = float(envio.get("price") or 0)
        limite = item.get("preco", 0) * 1.15 + 2
        if item.get("preco") and preco > limite:  # cotacao mudou: a equipe confirma de novo
            sf.cancelar(id_envio, "Preço diferente da cotação aprovada")
            contatos.marcar_pos_conserto(item["chave"], "pronta", etiqueta_id=None, cotacao=None,
                                         aviso=f"O frete mudou para R$ {preco:.2f}: escolha de novo")
            registrar(numero, ETAPA_ETIQUETA, "PULADA", f"preço R$ {preco:.2f} > aprovado R$ {item['preco']:.2f}")
            return None, None
    estado = sf.info(id_envio)
    if estado.get("status") in ("canceled", "cancelled"):
        contatos.marcar_pos_conserto(item["chave"], "pronta", etiqueta_id=None,
                                     aviso="A etiqueta foi cancelada no SuperFrete: escolha o serviço de novo")
        raise RuntimeError("etiqueta cancelada no SuperFrete")
    if estado.get("status") == "pending":
        saldo = float(sf.usuario().get("balance") or 0)
        if saldo < float(estado.get("price") or 0):
            raise RuntimeError(f"saldo do SuperFrete insuficiente (R$ {saldo:.2f}); coloque saldo e o robô tenta de novo")
        pago = sf.pagar([id_envio])
        if not pago.get("success"):
            raise RuntimeError(f"o SuperFrete não confirmou o pagamento: {str(pago)[:120]}")
        estado = sf.info(id_envio)
    codigo = estado.get("tracking") or ""
    pdf = (estado.get("print") or {}).get("url") or sf.link_pdf([id_envio])
    if not codigo:
        raise RuntimeError("etiqueta paga, mas o SuperFrete ainda não devolveu o rastreio - tento de novo")
    registrar(numero, ETAPA_ETIQUETA, "OK",
              f"{item.get('servico_nome')} R$ {float(estado.get('price') or 0):.2f}; rastreio {codigo}; envio {id_envio}")
    contatos.marcar_pos_conserto(item["chave"], "liberada", codigo=codigo, pdf=pdf)
    if pdf:
        webbrowser.open(pdf)  # para imprimir; tambem fica no botao "Imprimir etiqueta"
    return codigo, pdf


def concluir_atendimento(page, numero, item):
    """Tarefa liberada pela equipe: (Logistica) anota o rastreio e depois assina."""
    aprovado = item.get("liberada_por") == "equipe" and cfg.get("confirmou_inicia_mesmo_em_teste", True)
    _abrir_para_assinar(page, numero)
    codigo = item.get("codigo")
    if item.get("servico") and not codigo:
        if not aprovado:  # etiqueta cobra do saldo: so com o clique da equipe
            registrar(numero, ETAPA_ETIQUETA, "DRY_RUN", "modo teste: não gerei a etiqueta")
            return "DRY_RUN"
        codigo, _pdf = gerar_etiqueta(page, numero, item)
        if not codigo:
            return "PULADA"
        _abrir_para_assinar(page, numero)
    if codigo and historico.pode_fazer(numero, ETAPA_ANOTAR_RASTREIO, cfg)[0]:
        texto = ANOTACAO_RASTREIO.format(codigo=codigo)
        st = anotar_na_os_aberta(page, numero, texto, etapa=ETAPA_ANOTAR_RASTREIO, aprovado=aprovado)
        if st != "OK":
            return st  # sem a anotacao do rastreio nao assina; tenta no proximo ciclo
        _abrir_para_assinar(page, numero)
    return fazer_assinar(page, numero, aprovado)


def pos_conserto(page, contagem):
    """Uma passada pelas OSs para assinar (no maximo a cada `intervalo_pos_conserto_minutos`)."""
    if not cfg.get("pos_conserto_ativo", True):
        return []
    liberadas = contatos.pos_conserto_do_robo()  # a equipe ja liberou: nao espera o intervalo
    intervalo = cfg.get("intervalo_pos_conserto_minutos", 30) * 60
    if not liberadas and time.time() - _ultima_varredura["quando"] < intervalo:
        return []
    painel.estado("trabalhando", "Conferindo OSs para assinar…")
    achadas = listar_para_assinar(page)
    _ultima_varredura["quando"] = time.time()
    contatos.sincronizar_pos_conserto(achadas)
    problemas, triadas = [], 0
    ja_na_fila = contatos.carregar()["pos_conserto"]
    for numero, card in achadas.items():
        if painel.deve_sair() or triadas >= cfg.get("max_triagem_por_ciclo", 40):
            break
        if (contatos.chave_pos(contatos.ASSINAR, numero) in ja_na_fila
                or not historico.pode_fazer(numero, ETAPA_TRIAGEM, cfg)[0]):
            continue
        painel.checar_pausa()
        painel.estado("trabalhando", "Conferindo OS para assinar", rotulo(numero))
        triadas += 1
        try:
            triar_para_assinar(page, numero, card)
        except Exception as e:
            caminho = foto(page, f"erro_triagem_{numero}")
            registrar(numero, ETAPA_TRIAGEM, "ERRO", f"{resumo_erro(e)} | print: {caminho}")
    # Logistica triada antes da integracao com o SuperFrete (04/11): completa a cotacao
    for item in list(ja_na_fila.values()):
        if (painel.deve_sair() or triadas >= cfg.get("max_triagem_por_ciclo", 40)
                or item.get("situacao") != "pronta" or item["os"] not in achadas
                or norm(item.get("canal") or "") not in CANAIS_CONFIRMAR_CLIENTE
                or "cotacao" in item or item.get("aviso") or not cfg.get("superfrete_ativo", True)):
            continue
        triadas += 1
        try:
            abrir_os_pelo_numero(page, item["os"], qualquer_status=True, exigir_sinistro=True)
            cep = so_digitos(ler_info(page).get("cep"))
            valor = _valor(_ler_campo_info(page, r"valor de nota"))
            contatos.marcar_pos_conserto(item["chave"], "pronta", cep=cep, valor_nf=valor,
                                         cotacao=cotar_etiqueta(cep, valor))
        except Exception as e:
            contatos.marcar_pos_conserto(item["chave"], "pronta",
                                         aviso=f"Não consegui cotar no SuperFrete ({resumo_erro(e)[:80]})")
    for item in contatos.pos_conserto_do_robo():
        numero = item["os"]
        if painel.deve_sair() or numero not in achadas:
            continue
        if not historico.pode_fazer(numero, ETAPA_ASSINAR, cfg)[0]:
            continue
        painel.checar_pausa()
        painel.estado("trabalhando", "Assinando OS", rotulo(numero))
        try:
            st = concluir_atendimento(page, numero, item)
            if st == "OK":
                # Logistica: se ainda nao mandaram o rastreio, a tarefa fica para o WhatsApp
                # (rele a tarefa: a etiqueta acabou de gravar o codigo nela)
                atual = contatos.carregar()["pos_conserto"].get(item["chave"], item)
                falta_avisar = atual.get("codigo") and not atual.get("avisado_em")
                contatos.marcar_pos_conserto(item["chave"], "lancado" if falta_avisar else "assinada")
        except Exception as e:
            caminho = foto(page, f"erro_{numero}")
            registrar(numero, ETAPA_ASSINAR, "ERRO", f"{resumo_erro(e)} | print: {caminho}")
            st = "ERRO"
            problemas.append(f"{rotulo(numero)}: {resumo_erro(e)}")
        if st == "CLIQUEI_MAS_ETAPA_NAO_MUDOU":
            problemas.append(f"{rotulo(numero)}: cliquei em assinar mas não confirmou")
        contagem[st] = contagem.get(st, 0) + 1
    painel.contatos_mudaram()
    return problemas


# ------------------------------------------------------------------ pipeline
def processar_os(page, numero, etapa):
    log(f"{rotulo(numero)}: abrindo ({etapa})")
    abrir_os_pelo_numero(page, numero, exigir_sinistro=True)  # iniciar/agendar: sempre pelo sinistro
    conferir_os(page, numero, etapa)
    cab_sin = SINISTRO_DA_OS.get(numero)
    if cab_sin and ler_cabecalho(page)["sinistro"] != cab_sin:
        raise RuntimeError(f"a OS aberta tem outro sinistro ({ler_cabecalho(page)['sinistro']}, "
                           f"esperado {cab_sin}) - nao vou mexer")
    st = ACOES_POR_ETAPA[etapa](page, numero)
    if st == "OK" and numero in contatos.confirmados():
        contatos.marcar(numero, "iniciada")
        painel.contatos_mudaram()
        # pedido do dono (03/11): depois de iniciar, anotar que aguardamos o produto chegar
        if anotar_aguardando_produto(page, numero) != "OK":
            log(f"{rotulo(numero)}: iniciada, mas a anotacao falhou - tento no proximo ciclo")
    return st


def ciclo(page):
    """Um passada completa. Devolve (contagem por status, lista de problemas)."""
    painel.estado("trabalhando", "Procurando OSs novas…")
    pendentes, logistica, balcao, extras, retirar = listar_os_pendentes(page)
    log(f"{len(pendentes)} OS(s) pendentes: {pendentes}; logistica aguardando cliente: {logistica}; "
        f"balcao: {balcao}")
    contagem = {}
    reconciliar_confirmadas(extras)
    # contatos antes de iniciar: depois de iniciada a OS de Balcao sai de "Iniciar OS"
    problemas = (preparar_contatos(page, logistica, contatos.LOGISTICA)
                 + preparar_contatos(page, balcao, contatos.BALCAO)
                 + preparar_planilha(page, retirar)
                 + anotacoes_pendentes(page))
    contatos.atualizar_extras(extras)
    painel.contatos_mudaram()
    lembradas = []
    for numero, etapa in pendentes:
        if painel.deve_sair():
            break
        if etapa == ETAPA_INICIAR and (aprovada_pela_equipe(numero)
                                       or numero in contatos.balcao_avisados()):
            # teste antigo/"Pular" de antes do Confirmou nao vale mais: a equipe aprovou agora
            historico.esquecer(numero, etapa, status=("PULADA", "DRY_RUN"))
        pode, motivo = historico.pode_fazer(numero, etapa, cfg)
        if not pode:
            lembradas.append(f"{rotulo(numero)}: {motivo}")
            continue
        painel.checar_pausa()
        painel.estado("trabalhando", f"{etapa}", rotulo(numero))
        try:
            st = processar_os(page, numero, etapa)
            if st == "CLIQUEI_MAS_ETAPA_NAO_MUDOU":
                problemas.append(f"{rotulo(numero)}: cliquei mas a etapa não mudou")
        except Exception as e:
            caminho = foto(page, f"erro_{numero}")
            registrar(numero, etapa, "ERRO", f"{resumo_erro(e)} | print: {caminho}")
            log(f"{rotulo(numero)}: ERRO -> {resumo_erro(e)}")
            if "--debug" in sys.argv:
                traceback.print_exc()
            st = "ERRO"
            problemas.append(f"{rotulo(numero)}: {resumo_erro(e)}")
        contagem[st] = contagem.get(st, 0) + 1
    if lembradas:
        log(f"{len(lembradas)} OS(s) puladas pela memoria: " + "; ".join(lembradas))
        contagem["já feitas antes"] = len(lembradas)
    try:
        problemas += pos_conserto(page, contagem)
    except SessaoExpirada:
        raise
    except Exception as e:
        caminho = foto(page, "erro_pos_conserto")
        registrar("-", ETAPA_TRIAGEM, "ERRO", f"{resumo_erro(e)} | print: {caminho}")
        problemas.append(f"OSs para assinar: {resumo_erro(e)}")
    return contagem, problemas


NOMES_STATUS = {"OK": ("feita", "feitas"), "DRY_RUN": ("testada", "testadas"),
                "PULADA": ("pulada", "puladas"), "ERRO": ("com erro", "com erro"),
                "CLIQUEI_MAS_ETAPA_NAO_MUDOU": ("sem efeito", "sem efeito"),
                "já feitas antes": ("já feita antes", "já feitas antes")}


def resumo_ciclo(contagem):
    """{'DRY_RUN': 1, 'já feitas antes': 4} -> '1 testada · 4 já feitas antes' (para o balao)."""
    partes = []
    for st, n in contagem.items():
        um, varios = NOMES_STATUS.get(st, (st, st))
        partes.append(f"{n} {um if n == 1 else varios}")
    return " · ".join(partes) or "nada para fazer"


# ------------------------------------------------------------------- login
# Pedido do dono (04/11): so o PRIMEIRO login do dia pede o codigo do e-mail (2FA). Nas
# outras quedas o navegador do robo ja tem usuario e senha preenchidos: basta atualizar a
# pagina e clicar Entrar. O robo faz isso sozinho, UMA vez por queda (nunca insiste: o
# portal poderia bloquear o usuario). Se aparecer a tela do codigo, pede ajuda.
SEL_LOGIN_SENHA = "input[type='password']"
SEL_LOGIN_ENTRAR = "button:has-text('Entrar'), input[type='submit']"


def relogar(page):
    """Atualiza e clica Entrar. Devolve OK, CODIGO (pediu o codigo do e-mail), RECUSADO ou TELA_DESCONHECIDA."""
    try:
        page.reload(wait_until="networkidle", timeout=30000)
    except PWTimeout:
        pass
    page.wait_for_timeout(2000)
    if sessao_ativa(page):
        return "OK"
    senha = page.locator(SEL_LOGIN_SENHA).first
    entrar = page.locator(SEL_LOGIN_ENTRAR).first
    if not senha.count() or not entrar.count():
        return "TELA_DESCONHECIDA"
    # clicar no campo e um gesto de usuario: o navegador so entrega a senha salva
    # (preenchimento automatico) para a pagina depois de um gesto
    senha.click()
    page.wait_for_timeout(600)
    entrar.click()
    saiu_da_senha = False
    for _ in range(60):  # ate 30 s: o portal passa por paginas /auth/... antes de entrar
        page.wait_for_timeout(500)
        if sessao_ativa(page):
            return "OK"
        if page.locator(SEL_LOGIN_SENHA).count() == 0:
            saiu_da_senha = True
    if saiu_da_senha:
        # saiu da tela de senha mas nao entrou no portal: deve ser a tela do codigo do
        # e-mail. Print para conferir (03-04/11: a 1a queda real deu "CODIGO" em 2 s)
        foto(page, "login_codigo")
        return "CODIGO"
    return "RECUSADO"


def lidar_com_login(browser, login, detalhe=""):
    """Sessao caiu: tenta entrar sozinho (1x); se nao der, pede ajuda. Devolve a espera em s."""
    if cfg.get("relogar_sozinho", True) and not login["tentou"] and browser is not None:
        login["tentou"] = True
        painel.estado("trabalhando", "A sessão caiu — entrando de novo…")
        try:
            resultado = relogar(pegar_aba(browser))
        except Exception as e:
            resultado = f"ERRO {resumo_erro(e)}"
        login["resultado"] = resultado
        registrar("-", "Login", "OK" if resultado == "OK" else f"LOGIN_{resultado.split()[0]}",
                  f"login automatico: {resultado}" + (f" ({detalhe})" if detalhe else ""))
        if resultado == "OK":
            log("login automatico OK - sigo o trabalho")
            return 3
    if not login["avisou"]:
        registrar("-", "-", "SESSAO_EXPIRADA", "faca login no navegador do robo" + (f" ({detalhe})" if detalhe else ""))
        painel.alertar()
        login["avisou"] = True
    if login.get("resultado") == "CODIGO":
        painel.estado("sessao", "Preciso do código do e-mail!",
                      "O portal pediu o código (1º login do dia). Digite na janela do navegador do robô.")
    else:
        painel.estado("sessao", "Preciso de um login!",
                      "Entre no portal (com o código 2FA) na janela do navegador do robô.")
    return 60


def executar(uma_vez=False):
    """Loop principal: conecta, confere sessao, roda ciclos, aguenta quedas."""
    painel.estado("conectando", "Ligando os motores…")
    intervalo = cfg["intervalo_minutos"] * 60
    with sync_playwright() as p:
        browser = None
        tentou_abrir = False
        login = {"tentou": False, "avisou": False}  # 1 tentativa automatica por queda
        while not painel.deve_sair():
            painel.checar_pausa()
            espera = intervalo
            try:
                if browser is None or not browser.is_connected():
                    browser = conectar(p)
                page = pegar_aba(browser)
                if not sessao_ativa(page):
                    raise SessaoExpirada()
                contagem, problemas = ciclo(page)
                login.update(tentou=False, avisou=False, resultado=None)
                feitas = resumo_ciclo(contagem)
                if problemas:
                    painel.estado("erro", f"{len(problemas)} OS com problema",
                                  problemas[0] + " (veja logs)")
                    painel.alertar()
                else:
                    travadas = len(historico.travadas())
                    aviso = f" · {travadas} p/ conferir" if travadas else ""
                    painel.estado("aguardando", "Tudo em dia!",
                                  f"Busca {datetime.now():%H:%M}: {feitas}{aviso}")
            except SemNavegador:
                browser = None
                painel.estado("erro", "Não achei o navegador do robô",
                              "Abrindo o Vivaldi… se pedir, faça o login nele.")
                if not tentou_abrir:
                    abrir_navegador()
                    tentou_abrir = True
                espera = 20
            except SessaoExpirada:
                espera = lidar_com_login(browser, login)
            except Exception as e:
                if "--debug" in sys.argv:
                    traceback.print_exc()
                if browser is not None and not browser.is_connected():
                    browser = None
                # 03/11: a sessao caiu no meio da busca e o erro saiu como "Timeout 30000ms"
                # (print = tela de login). Qualquer erro com a tela de login = pedir login.
                try:
                    caiu = browser is not None and not sessao_ativa(pegar_aba(browser))
                except Exception:
                    caiu = False
                if caiu:
                    espera = lidar_com_login(browser, login, resumo_erro(e))
                    if uma_vez:
                        return
                    painel.esperar(espera)
                    continue
                try:
                    caminho = foto(pegar_aba(browser), "erro_ciclo") if browser else ""
                except Exception:
                    caminho = ""
                registrar("-", "-", "ERRO", f"ciclo: {resumo_erro(e)} | print: {caminho}")
                painel.estado("erro", "Deu um problema na busca", resumo_erro(e))
                painel.alertar()
                espera = 120
            if uma_vez:
                return
            painel.esperar(espera)


def main():
    log(f"Robo v9 (modo={cfg['modo']}, dry_run={cfg.get('dry_run')}, etapas={ETAPAS_ATIVAS})")
    if "--anotar" in sys.argv or "--anotar-sinistro" in sys.argv:
        por_sinistro = "--anotar-sinistro" in sys.argv
        i = sys.argv.index("--anotar-sinistro" if por_sinistro else "--anotar")
        chave, texto = sys.argv[i + 1], sys.argv[i + 2]
        numero, sinistro = (None, chave) if por_sinistro else (chave, None)
        tipo = sys.argv[sys.argv.index("--tipo") + 1] if "--tipo" in sys.argv else TIPO_ANOTACAO
        with sync_playwright() as p:
            try:
                page = pegar_aba(conectar(p))
            except SemNavegador:
                log("Nao conectei no navegador. Abra o Robo Prestador.exe (ele abre o navegador) e faca o login.")
                sys.exit(1)
            log(f"{'sinistro ' + chave if por_sinistro else 'OS ' + chave}: anotacao -> "
                f"{anotar(page, numero, texto, tipo, sinistro)}")
        return
    uma_vez = "--uma-vez" in sys.argv or cfg["modo"] == "assistido" or cfg.get("dry_run")
    executar(uma_vez=uma_vez)


if __name__ == "__main__":
    main()
