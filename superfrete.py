"""
Cliente da API do SuperFrete (etiqueta dos Correios para DEVOLVER o produto consertado).
Documentacao: https://superfrete.readme.io/reference  (lida em 04/11/2026)

  cotar()        POST /api/v0/calculator      preco/prazo por servico - nao cobra nada
  criar_envio()  POST /api/v0/cart            envio "pending" no carrinho - nao cobra
  pagar()        POST /api/v0/checkout        PAGA COM O SALDO -> sempre com clique humano
  info()         GET  /api/v0/order/info/{id} status, tracking e print.url (PDF)
  cancelar()     POST /api/v0/order/cancel    so antes de postar; o valor volta ao saldo

A chave (token) fica no Gerenciador de Credenciais do Windows (cofre.py), uma para
producao e outra para o sandbox (cada ambiente gera a sua). Para configurar:
    python superfrete.py --configurar        (ou pelo menu do robozinho)
"""
import json
import sys
import urllib.error
import urllib.request

import cofre

BASES = {"producao": "https://api.superfrete.com", "sandbox": "https://sandbox.superfrete.com"}
SERVICOS = {1: "PAC", 2: "SEDEX", 17: "Mini Envios"}  # so Correios (3 Jadlog, 31 Loggi, 33 J&T)
# a API exige User-Agent "Nome e versao (e-mail de contato)"; o e-mail vem do config
USER_AGENT = "Robo Prestador 1.0 ({email})"


class ErroSuperFrete(Exception):
    pass


class SuperFrete:
    def __init__(self, ambiente="sandbox", email_contato="", token=None):
        if ambiente not in BASES:
            raise ValueError(f"ambiente '{ambiente}' (use producao ou sandbox)")
        self.ambiente, self.base = ambiente, BASES[ambiente]
        self.token = token or cofre.ler(f"superfrete_{ambiente}")
        if not self.token:
            raise ErroSuperFrete(f"chave do SuperFrete ({ambiente}) não configurada - "
                                 "menu do robozinho > Configurar SuperFrete")
        self.ua = USER_AGENT.format(email=email_contato or "sem e-mail")

    # ------------------------------------------------------------------ http
    def _req(self, metodo, caminho, corpo=None):
        dados = json.dumps(corpo).encode("utf-8") if corpo is not None else None
        req = urllib.request.Request(self.base + caminho, data=dados, method=metodo, headers={
            "Authorization": f"Bearer {self.token}", "User-Agent": self.ua,
            "accept": "application/json", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                texto = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # nunca repassa o token; a resposta da API ajuda a entender o erro
            detalhe = e.read().decode("utf-8", "replace")[:300]
            raise ErroSuperFrete(f"{metodo} {caminho}: HTTP {e.code} {detalhe}") from None
        except urllib.error.URLError as e:
            raise ErroSuperFrete(f"{metodo} {caminho}: sem conexão ({e.reason})") from None
        return json.loads(texto) if texto.strip() else {}

    # ------------------------------------------------------------- operacoes
    def cotar(self, cep_origem, cep_destino, pacote, valor_declarado=0, servicos=(1, 2, 17)):
        """pacote = {height, width, length (cm), weight (kg)}. Devolve so os servicos sem erro."""
        corpo = {"from": {"postal_code": _cep(cep_origem)}, "to": {"postal_code": _cep(cep_destino)},
                 "services": ",".join(str(s) for s in servicos), "package": pacote,
                 "options": {"own_hand": False, "receipt": False,
                             "insurance_value": valor_declarado, "use_insurance_value": bool(valor_declarado)}}
        resp = self._req("POST", "/api/v0/calculator", corpo)
        return [s for s in resp if not s.get("has_error")]

    def criar_envio(self, remetente, destinatario, servico, volume, produtos, valor_declarado,
                    declaracao_conteudo=True, etiqueta_interna=""):
        """Coloca o envio no carrinho (status pending). NAO cobra: a cobranca e o pagar()."""
        corpo = {"from": remetente, "to": destinatario, "service": int(servico), "volumes": volume,
                 "products": produtos, "platform": "Robo Prestador",
                 "options": {"insurance_value": valor_declarado or None, "receipt": False, "own_hand": False,
                             "non_commercial": bool(declaracao_conteudo),
                             "tags": [{"tag": etiqueta_interna, "url": ""}] if etiqueta_interna else []}}
        return self._req("POST", "/api/v0/cart", corpo)

    def pagar(self, ids):
        """COBRA DO SALDO. So chamar depois do clique da equipe na janela do robo."""
        return self._req("POST", "/api/v0/checkout", {"orders": list(ids)})

    def info(self, id_envio):
        return self._req("GET", f"/api/v0/order/info/{id_envio}")

    def link_pdf(self, ids):
        """URL do PDF da(s) etiqueta(s) ja paga(s)."""
        return self._req("POST", "/api/v0/tag/print", {"orders": list(ids)}).get("url", "")

    def usuario(self):
        """Saldo (balance) e limites da conta."""
        return self._req("GET", "/api/v0/user")

    def cancelar(self, id_envio, motivo="Cancelado pelo robô a pedido da equipe"):
        return self._req("POST", "/api/v0/order/cancel", {"order": {"id": id_envio, "description": motivo}})


def _cep(cep):
    return "".join(ch for ch in str(cep) if ch.isdigit())


# ------------------------------------------------------------- configuracao
def configurar(pai=None):
    """Janelinha para colar a chave: vai direto para o cofre do Windows (nunca para arquivo)."""
    import tkinter as tk
    from tkinter import messagebox
    raiz = pai or tk.Tk()
    if pai is None:
        raiz.withdraw()
    jan = tk.Toplevel(raiz)
    jan.title("Robô Prestador — chave do SuperFrete")
    jan.configure(bg="white", padx=20, pady=16)
    jan.resizable(False, False)
    tk.Label(jan, text="Chave da API do SuperFrete", bg="white", font=("Segoe UI", 12, "bold")).pack(anchor="w")
    tk.Label(jan, text="Cole a chave gerada em Integrações > Desenvolvedores. Ela fica guardada\n"
                       "no Gerenciador de Credenciais do Windows, não em arquivo.",
             bg="white", fg="#4F5B73", font=("Segoe UI", 9), justify="left").pack(anchor="w", pady=(2, 10))
    amb = tk.StringVar(value="producao")
    linha = tk.Frame(jan, bg="white")
    linha.pack(anchor="w")
    for valor, texto in (("producao", "Produção (web.superfrete.com)"), ("sandbox", "Sandbox (teste)")):
        tk.Radiobutton(linha, text=texto, variable=amb, value=valor, bg="white",
                       font=("Segoe UI", 9)).pack(side="left", padx=(0, 12))
    campo = tk.Entry(jan, show="•", width=52, font=("Segoe UI", 10))
    campo.pack(pady=(10, 12), ipady=4)
    campo.focus_set()

    def salvar():
        chave = campo.get().strip()
        if len(chave) < 20:
            messagebox.showwarning("Robô Prestador", "Cole a chave inteira.", parent=jan)
            return
        cofre.salvar(f"superfrete_{amb.get()}", chave)
        messagebox.showinfo("Robô Prestador", f"Chave do SuperFrete ({amb.get()}) guardada.", parent=jan)
        jan.destroy()
        if pai is None:
            raiz.destroy()

    tk.Button(jan, text="Guardar", command=salvar, bg="#1F5FD6", fg="white", relief="flat",
              font=("Segoe UI", 10, "bold"), padx=16, pady=4, cursor="hand2").pack(anchor="e")
    jan.bind("<Return>", lambda e: salvar())
    if pai is None:
        jan.protocol("WM_DELETE_WINDOW", raiz.destroy)
        raiz.mainloop()


if __name__ == "__main__":
    if "--configurar" in sys.argv:
        configurar()
    elif "--testar" in sys.argv:
        # so cotacao (nao cobra nada): python superfrete.py --testar producao 88300000 01001000
        _, _, amb, origem, destino = sys.argv[:5]
        for s in SuperFrete(amb).cotar(origem, destino, {"height": 20, "width": 20, "length": 20, "weight": 2}):
            print(f"{s['name']:12} R$ {s['price']:>8}  {s.get('delivery_time')} dias úteis")
