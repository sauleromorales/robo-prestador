"""
Descoberta da LISTA de OSs. Nao clica em nada.
Rode com a aba do navegador do robo aberta na pagina "Ordens de servico".
Salva em descoberta/: html da pagina, print, e um resumo dos blocos
que contem "Iniciar OS".
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
    page = ctx.pages[0]
    print("Aba usada:", page.url)
    page.wait_for_timeout(1500)

    page.screenshot(path=f"descoberta/{carimbo}_lista_tela.png", full_page=True)
    with open(f"descoberta/{carimbo}_lista_pagina.html", "w", encoding="utf-8") as f:
        f.write(page.content())

    resumo = page.evaluate("""
      () => [...document.querySelectorAll('tr, mat-row, mat-card, .card, li, a')]
        .filter(e => e.innerText && e.innerText.includes('Iniciar OS') && e.innerText.length < 800)
        .map(e => ({tag: e.tagName, cls: e.className, href: e.getAttribute('href'),
                    texto: e.innerText.replace(/\\s+/g,' ').slice(0,300)}))
    """)
    with open(f"descoberta/{carimbo}_lista_blocos.json", "w", encoding="utf-8") as f:
        json.dump(resumo, f, ensure_ascii=False, indent=2)
    print(f"{len(resumo)} blocos com 'Iniciar OS' em descoberta/{carimbo}_lista_blocos.json")
