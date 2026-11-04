"""
Script de DESCOBERTA. Roda uma vez, nao clica em nada.
Conecta no Chrome aberto pelo abrir_chrome.bat, pega a aba do portal
que estiver ativa e salva em descoberta/ :
  - o HTML do bloco "INICIAR OS"
  - o HTML da pagina inteira
  - um screenshot
Depois me manda os arquivos .html pra eu ajustar os seletores do bot.py.

Uso:
  1. abrir_chrome.bat -> logar -> navegar ate uma OS que esteja na etapa Iniciar OS
  2. python descobrir.py
"""
import json
import os
from datetime import datetime
from playwright.sync_api import sync_playwright

os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # pasta do projeto
cfg = json.load(open("config.json", encoding="utf-8"))
os.makedirs("descoberta", exist_ok=True)
carimbo = datetime.now().strftime("%Y%m%d_%H%M%S")

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(cfg["cdp_url"])
    ctx = browser.contexts[0]
    # pega a aba do portal (a primeira cuja URL contenha "seguradora"), senao a ultima aba
    paginas = [pg for pg in ctx.pages if "seguradora" in pg.url.lower()]
    page = paginas[0] if paginas else ctx.pages[-1]
    print("Aba usada:", page.url)

    page.screenshot(path=f"descoberta/{carimbo}_tela.png", full_page=True)

    with open(f"descoberta/{carimbo}_pagina.html", "w", encoding="utf-8") as f:
        f.write(page.content())

    # tenta isolar o bloco "INICIAR OS"
    bloco = page.locator("text=INICIAR OS").first
    if bloco.count():
        html_bloco = bloco.evaluate(
            "el => { let n = el; for (let i = 0; i < 6 && n.parentElement; i++) n = n.parentElement; return n.outerHTML; }"
        )
        with open(f"descoberta/{carimbo}_bloco_iniciar_os.html", "w", encoding="utf-8") as f:
            f.write(html_bloco)
        print("Bloco INICIAR OS salvo.")
    else:
        print("Nao achei o texto 'INICIAR OS' nesta aba. Abra uma OS nessa etapa e rode de novo.")

    # lista todos os inputs/selects/botoes visiveis com seus atributos
    campos = page.evaluate("""
      () => [...document.querySelectorAll('input, select, button, [role=combobox], [role=listbox]')]
        .filter(e => e.offsetParent !== null)
        .map(e => ({tag: e.tagName, type: e.type, id: e.id, name: e.name,
                    placeholder: e.placeholder, text: (e.innerText||'').trim().slice(0,60),
                    cls: e.className}))
    """)
    with open(f"descoberta/{carimbo}_campos.json", "w", encoding="utf-8") as f:
        json.dump(campos, f, ensure_ascii=False, indent=2)
    print(f"{len(campos)} campos listados em descoberta/{carimbo}_campos.json")
