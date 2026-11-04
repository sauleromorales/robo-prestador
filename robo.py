"""
Robozinho do Robo Prestador - janela animada que mostra o que o bot esta fazendo.

Enquanto ele esta na tela, o bot esta rodando. Fechar o robozinho = desligar o bot.
  verde      trabalhando          azul     aguardando a proxima busca
  amarelo    precisa de login / esta perguntando algo
  vermelho   deu problema (treme e apita)   cinza   pausado

Arrastar com o mouse muda ele de lugar. Botao direito: menu.
Clique: abre as tarefas da equipe (WhatsApp da Logistica e do Balcao, linhas
da planilha do Leva & Traz). O numero vermelho na cabeca e quantas faltam.

Uso: clique duplo em "Robo Prestador.exe" (ou: pythonw robo.py)
Com argumentos (--anotar-sinistro, --uma-vez...) roda o bot.py sem janela.
"""
import glob
import math
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from datetime import datetime
import webbrowser
from tkinter import messagebox

import contatos
import historico

try:
    import winsound
except ImportError:
    winsound = None

W = 300  # largura do mockup Robo-Ocioso.dc.html (balao ate 250 px)
BALAO_BAIXO = 190   # o balao cresce pra cima a partir daqui (antes 112: o texto vazava)
ESCALA_ROBO = 0.85    # robo do mockup Robo-Ocioso.dc.html (viewBox 200x240) -> ~170x204 px
H = BALAO_BAIXO + 12 + int(240 * ESCALA_ROBO) + 6  # balao + robo + sombra
TRANSPARENTE = "#ff00fe"
ESCURO = "#2b3a4a"
TEXTO = "#22303c"
CORES = {
    "conectando": "#8b7cf6",
    "trabalhando": "#2ecc71",
    "aguardando": "#4DA3FF",  # azul do mockup
    "pausado": "#9aa5b1",
    "sessao": "#f5a623",
    "pergunta": "#f5a623",
    "erro": "#e74c3c",
    "parado": "#e74c3c",
}
ESTADOS_ALERTA = ("erro", "sessao", "parado")
PORTA_INSTANCIA = 47391  # so um robozinho por vez


def ret_arred(c, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def _mistura(c1, c2, t):
    """Cor entre c1 (t=0) e c2 (t=1): faz o papel da opacidade/gradiente do mockup."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def apitar(tipo):
    if winsound is None:
        return
    try:
        winsound.MessageBeep(winsound.MB_ICONHAND if tipo == "erro" else winsound.MB_ICONASTERISK)
    except Exception:
        pass


# ------------------------------------- WhatsApp Desktop: confere o foco e aperta Enter
try:
    import ctypes
    from ctypes import wintypes
    _user32 = ctypes.windll.user32
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
except (ImportError, AttributeError):
    _user32 = None


def titulo_em_foco():
    if _user32 is None:
        return ""
    h = _user32.GetForegroundWindow()
    n = _user32.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(h, buf, n + 1)
    return buf.value


def whatsapp_em_foco():
    """A janela da frente e o aplicativo do WhatsApp (nao o navegador, nao o robo)?"""
    t = titulo_em_foco().lower()
    return "whatsapp" in t and not any(x in t for x in ("vivaldi", "chrome", "edge", "tarefas do rob"))


def apertar_enter():
    VK_RETURN, KEYEVENTF_KEYUP = 0x0D, 0x0002
    _user32.keybd_event(VK_RETURN, 0, 0, 0)
    _user32.keybd_event(VK_RETURN, 0, KEYEVENTF_KEYUP, 0)


def janela_de_erro(msg):
    r = tk.Tk()
    r.withdraw()
    messagebox.showerror("Robô Prestador", msg)
    r.destroy()


# ------------------------------------------------- ponte bot (thread) <-> janela
class PainelTk:
    """Implementa o painel do bot.py; tudo que mexe na tela passa pela fila."""

    def __init__(self, log):
        self.log = log
        self.fila = queue.Queue()
        self.sair = threading.Event()
        self.pausado = threading.Event()
        self.acordar = threading.Event()
        self._respondeu = threading.Event()
        self._resposta = False

    # ---- chamados pelo bot
    def estado(self, estado, titulo, detalhe=""):
        self.log(f"[{estado}] {titulo}" + (f" - {detalhe}" if detalhe else ""))
        self.fila.put(("estado", estado, titulo, detalhe))

    def alertar(self):
        self.fila.put(("alerta",))

    def confirmar(self, pergunta):
        self._respondeu.clear()
        self.fila.put(("pergunta", pergunta))
        while not self._respondeu.wait(0.5):
            if self.sair.is_set():
                return False
        return self._resposta

    def esperar(self, segundos):
        self.acordar.clear()
        self.fila.put(("proxima", time.time() + segundos))
        self.acordar.wait(segundos)

    def checar_pausa(self):
        if not self.pausado.is_set():
            return
        self.estado("pausado", "Pausado", "Botão direito → Retomar")
        while self.pausado.is_set() and not self.sair.is_set():
            time.sleep(0.5)

    def deve_sair(self):
        return self.sair.is_set()

    def contatos_mudaram(self):
        self.fila.put(("contatos",))

    # ---- chamados pela janela
    def responder(self, sim):
        self._resposta = sim
        self._respondeu.set()


# ----------------------------------------------------------------- a janela
class Robozinho:
    def __init__(self, root, painel, bot):
        self.root, self.painel, self.bot = root, painel, bot
        self.estado, self.titulo, self.detalhe = "conectando", "Ligando os motores…", ""
        self.proxima = None
        self.pergunta = None
        self.tremer_ate = 0
        self.t0 = time.time()
        self._balao_atual = None
        self._botoes = []
        self.n_contatos = contatos.contar_pendentes()
        self.janela_contatos = None
        self.ultima_busca = None

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.wm_attributes("-transparentcolor", TRANSPARENTE)
        x = root.winfo_screenwidth() - W - 20
        y = root.winfo_screenheight() - H - 60
        root.geometry(f"{W}x{H}+{x}+{y}")

        self.c = tk.Canvas(root, width=W, height=H, bg=TRANSPARENTE, highlightthickness=0)
        self.c.pack()
        self.c.bind("<ButtonPress-1>", self._pegar)
        self.c.bind("<B1-Motion>", self._arrastar)
        self.c.bind("<ButtonRelease-1>", self._soltar)
        self.c.bind("<Button-3>", self._menu)

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="Tarefas (WhatsApp e planilha)", command=self.abrir_contatos)
        self.menu.add_command(label="Rodar agora", command=self.rodar_agora)
        self.menu.add_command(label="OSs esperando conferência", command=self.abrir_travadas)
        self.menu.add_command(label="Pausar", command=self.pausar_retomar)
        self.var_teste = tk.BooleanVar(value=bool(self.bot.cfg.get("dry_run")))
        self.menu.add_checkbutton(label="Modo teste (não clica no portal)", variable=self.var_teste,
                                  command=self.trocar_modo_teste)
        self.menu.add_command(label="Configurar SuperFrete (chave da API)…", command=self.configurar_superfrete)
        self.menu.add_separator()
        self.menu.add_command(label="Abrir histórico (execucoes.csv)",
                              command=lambda: self.abrir(self.bot.LOG_CSV))
        self.menu.add_command(label="Abrir último print de erro", command=self.ultimo_print)
        self.menu.add_command(label="Abrir pasta de logs",
                              command=lambda: self.abrir(self.bot.cfg["pasta_logs"]))
        self.menu.add_separator()
        self.menu.add_command(label="Desligar o robô", command=self.desligar)

        self._animar()
        self._ler_fila()

    # ---------------------------------------------------------------- eventos
    def _pegar(self, e):
        self._dx, self._dy = e.x, e.y
        self._arrastou = False

    def _arrastar(self, e):
        if abs(e.x - self._dx) + abs(e.y - self._dy) > 4:
            self._arrastou = True
        self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _soltar(self, e):
        # clique (sem arrastar) no robo abre a lista; no balao nao faz nada
        if not self._arrastou and e.y > BALAO_BAIXO + 6:
            self.abrir_contatos()

    def abrir_travadas(self):
        if getattr(self, "janela_travadas", None) and self.janela_travadas.top.winfo_exists():
            self.janela_travadas.montar()
            self.janela_travadas.top.lift()
        else:
            self.janela_travadas = JanelaTravadas(self)

    def abrir_contatos(self):
        if self.janela_contatos and self.janela_contatos.top.winfo_exists():
            self.janela_contatos.top.deiconify()
            self.janela_contatos.top.lift()
            self.janela_contatos.top.focus_force()
        else:
            self.janela_contatos = JanelaTarefas(self)

    def contatos_mudaram(self):
        self.n_contatos = contatos.contar_pendentes()
        if self.janela_contatos and self.janela_contatos.top.winfo_exists():
            self.janela_contatos.montar()

    def _menu(self, e):
        self.menu.entryconfig(3, label="Retomar" if self.painel.pausado.is_set() else "Pausar")
        self.var_teste.set(bool(self.bot.cfg.get("dry_run")))
        self.menu.tk_popup(e.x_root, e.y_root)

    def trocar_modo_teste(self):
        """Liga/desliga o modo teste pelo menu (sem editar o config.json na mao)."""
        ligar = self.var_teste.get()
        if not ligar:
            assistido = self.bot.cfg.get("modo") == "assistido"
            aviso = ("Ele vai perguntar no balão antes de cada clique." if assistido
                     else "ATENÇÃO: o modo está AUTOMÁTICO — ele clica sem perguntar.")
            if not messagebox.askyesno(
                    "Robô Prestador",
                    "Desligar o modo teste?\n\nA partir de agora o robô CLICA DE VERDADE no portal "
                    f"(Iniciar OS, Salvar agendamento, anotações).\n{aviso}", parent=self.root):
                self.var_teste.set(True)
                return
        try:
            self.bot.salvar_opcao("dry_run", ligar)
        except Exception as e:
            self.var_teste.set(not ligar)
            messagebox.showerror("Robô Prestador", f"Não consegui salvar a opção:\n{e}", parent=self.root)
            return
        self._balao_atual = None  # redesenha o balao (linha "modo teste")
        if ligar:
            self._mudar_estado(self.estado, "Modo teste ligado", "Preencho, mas não clico em nada.")
        else:
            self._mudar_estado(self.estado, "Modo teste desligado", "Agora eu clico de verdade no portal.")

    def configurar_superfrete(self):
        import superfrete
        superfrete.configurar(self.root)

    def abrir(self, caminho):
        try:
            os.startfile(os.path.abspath(caminho))
        except OSError:
            pass

    def ultimo_print(self):
        prints = sorted(glob.glob(os.path.join(self.bot.cfg["pasta_logs"], "*.png")))
        if prints:
            self.abrir(prints[-1])

    def rodar_agora(self):
        self.painel.pausado.clear()
        self.painel.acordar.set()

    def pausar_retomar(self):
        if self.painel.pausado.is_set():
            self.painel.pausado.clear()
        else:
            self.painel.pausado.set()
            self._mudar_estado("pausado", "Pausando…", "Termino a OS atual e paro.")
        self.painel.acordar.set()

    def desligar(self):
        if messagebox.askyesno("Robô Prestador", "Desligar o robô?\nEle para de verificar as OSs.",
                               parent=self.root):
            self.painel.sair.set()
            self.painel.acordar.set()
            self.root.destroy()

    def _responder(self, sim):
        self.pergunta = None
        self._balao_atual = None
        self.painel.responder(sim)
        self._mudar_estado("trabalhando", "Ok!" if sim else "Pulei essa.", "")

    # ------------------------------------------------------------ fila do bot
    def _mudar_estado(self, estado, titulo, detalhe):
        if estado in ESTADOS_ALERTA and estado != self.estado:
            self.tremer_ate = time.time() + 1.5
            if self.bot.cfg.get("som_alertas", True):
                apitar("erro" if estado != "sessao" else "aviso")
        if estado == "trabalhando":
            self.proxima = None
        if estado == "aguardando" and self.estado != "aguardando":
            self.ultima_busca = datetime.now()  # cabecalho da janela de tarefas
        self.estado, self.titulo, self.detalhe = estado, titulo, detalhe

    def _ler_fila(self):
        try:
            while True:
                msg = self.painel.fila.get_nowait()
                if msg[0] == "estado":
                    if self.pergunta is None:
                        self._mudar_estado(*msg[1:])
                elif msg[0] == "alerta":
                    self.tremer_ate = time.time() + 1.5
                elif msg[0] == "pergunta":
                    self.pergunta = msg[1]
                    self.estado = "pergunta"
                    self.tremer_ate = time.time() + 0.8
                    apitar("aviso")
                    self.root.lift()
                elif msg[0] == "proxima":
                    self.proxima = msg[1]
                elif msg[0] == "contatos":
                    self.contatos_mudaram()
        except queue.Empty:
            pass
        self.root.after(100, self._ler_fila)

    # --------------------------------------------------------------- desenho
    def _texto_balao(self):
        # cores e fontes do mockup Main.dc.html (04/11)
        if self.pergunta:
            return [(self.pergunta, ("Segoe UI", 10, "bold"), TXT)]
        partes = [(self.titulo, ("Segoe UI", 11, "bold"), TXT)]
        if self.detalhe:
            d = self.detalhe if len(self.detalhe) <= 150 else self.detalhe[:147] + "…"
            partes.append((d, ("Segoe UI", 9, "bold"), TXT3))
        if self.proxima and self.estado in ("aguardando", "erro", "sessao"):
            falta = max(0, int(self.proxima - time.time()))
            quando = f"{falta // 60 + 1} min" if falta >= 60 else f"{falta} s"
            verbo = "Próxima conferência" if self.estado == "aguardando" else "Tento de novo"
            partes.append((f"{verbo} em {quando}", ("Segoe UI", 8, "bold"), TXT3))
        if self.n_contatos:
            s = "s" if self.n_contatos > 1 else ""
            partes.append((f"{self.n_contatos} tarefa{s} para a equipe — clique em mim",
                           ("Segoe UI", 9, "bold"), LARANJA_ESC))
        if self.bot.cfg.get("dry_run"):
            partes.append(("modo teste: não clico em nada", ("Segoe UI", 8, "italic"), TXT3))
        return partes

    def _desenhar_balao(self):
        """Balao do mockup Robo-Ocioso.dc.html: branco, sombra, bolinha na cor do estado, ponta em losango."""
        partes = self._texto_balao()
        chave = (tuple(p[0] for p in partes), self.estado)
        if chave == self._balao_atual:
            return
        self._balao_atual = chave
        c = self.c
        c.delete("balao")
        for b in self._botoes:
            b.destroy()
        self._botoes = []
        cor = CORES.get(self.estado, ESCURO)
        pad, recuo = 16, 16 + 9 + 10                    # borda, bolinha (9 px) e espaco
        quebra = 250 - recuo - pad                    # max-width 250 do mockup
        extra = 38 if self.pergunta else 0              # botoes Sim/Pular
        while True:
            y, largura = 0, 0
            for texto, fonte, cor_txt in partes:
                item = c.create_text(0, y, text=texto, anchor="nw", width=quebra,
                                     font=fonte, fill=cor_txt, tags="balao")
                x1, _, x2, y2 = c.bbox(item)
                largura, y = max(largura, x2 - x1), y2 + 1
            # nao cabe: tira a linha menos importante (a ultima) e desenha de novo
            if y + extra + 2 * 12 <= BALAO_BAIXO - 4 or len(partes) <= 2:
                break
            c.delete("balao")
            partes = partes[:-1]
        if self.pergunta:
            x = 0
            for rot, sim, estilo in (("Sim, pode", True, "primario"), ("Pular", False, "linha")):
                b = Botao(c, rot, lambda v=sim: self._responder(v), estilo, altura=28, tam=9)
                c.create_window(x, y + 6, window=b, anchor="nw", tags="balao")
                x += b.largura + 8
                self._botoes.append(b)
            largura = max(largura, x - 8)
            y += extra
        bw = min(250, recuo + largura + pad)
        bx1, baixo = (W - bw) / 2, BALAO_BAIXO
        topo = baixo - (y + 2 * 12)
        c.move("balao", bx1 + recuo, topo + 12)
        sombra = ret_arred(c, bx1 + 1, topo + 5, bx1 + bw + 1, baixo + 5, 14, fill="#C9CFDA",
                           outline="", tags="balao")
        fundo = ret_arred(c, bx1, topo, bx1 + bw, baixo, 14, fill="white", outline="", tags="balao")
        ponta = c.create_polygon(W / 2 - 7, baixo - 1, W / 2, baixo + 7, W / 2 + 7, baixo - 1,
                                 fill="white", outline="", tags="balao")
        # bolinha de status com halo (box-shadow 0 0 0 4px da cor a 16%)
        dy = topo + 12 + 10
        c.create_oval(bx1 + pad - 4, dy - 8.5, bx1 + pad + 13, dy + 8.5, fill=_mistura(cor, "#FFFFFF", 0.84),
                      outline="", tags="balao")
        c.create_oval(bx1 + pad, dy - 4.5, bx1 + pad + 9, dy + 4.5, fill=cor, outline="", tags="balao")
        for item in (ponta, fundo, sombra):
            c.tag_lower(item)

    def _desenhar_robo(self):
        """
        O robo do mockup Robo-Ocioso.dc.html (04/11): cabeca e tronco brancos, visor escuro,
        fones e bracos grafite parados, olhos de LED em arco que piscam, luz do peito que
        "respira". A cor do estado vai nas luzes (antena, fones, ombros, peito, base); em
        erro / login / pausa mudam os olhos e aparece o "?" ou o "zzz".
        """
        c = self.c
        c.delete("robo")
        t = time.time() - self.t0
        est = self.estado
        cor = CORES.get(est, ESCURO)
        luz_clara = _mistura(cor, "#FFFFFF", 0.6)
        tag = {"tags": "robo"}
        s = ESCALA_ROBO
        flutua = -5 * (0.5 - 0.5 * math.cos(2 * math.pi * t / 3.6))  # 0 -> -5 px em 3,6 s
        if est == "pausado":
            flutua *= 0.3
        dx = math.sin(t * 45) * 4 if time.time() < self.tremer_ate else 0
        ox, oy = W / 2 - 100 * s + dx, BALAO_BAIXO + 12

        def P(x, y, sobe=True):
            return ox + x * s, oy + (y + (flutua if sobe else 0)) * s

        def r(x1, y1, x2, y2, raio, **kw):
            (a1, b1), (a2, b2) = P(x1, y1), P(x2, y2)
            ret_arred(c, a1, b1, a2, b2, raio * s, **tag, **kw)

        def bola(x, y, raio, **kw):
            a1, b1 = P(x, y)
            c.create_oval(a1 - raio * s, b1 - raio * s, a1 + raio * s, b1 + raio * s, **tag, **kw)

        def led(x, y, raio):  # o radialGradient do mockup: borda na cor, miolo claro
            bola(x, y, raio, fill=cor, outline="")
            bola(x - raio * 0.15, y - raio * 0.15, raio * 0.55, fill=luz_clara, outline="")

        chapa, contorno, grafite, visor = "#EDF1F7", "#AEBBCF", "#2C3549", "#141D30"

        # sombra no chao: encolhe quando o robo sobe
        enc = 1 - 0.12 * (flutua / -5)
        (sx, sy) = P(100, 232, sobe=False)
        c.create_oval(sx - 52 * s * enc, sy - 6 * s, sx + 52 * s * enc, sy + 6 * s,
                      fill=_mistura(NAVY, "#DCDDE3", 0.62 + 0.13 * (1 - enc) / 0.12), outline="", **tag)

        # antena
        r(97, 12, 103, 32, 3, fill=grafite, outline="")
        led(100, 10, 7)
        # cabeca
        r(40, 30, 160, 122, 22, fill=chapa, outline=contorno, width=1.5)
        r(46, 36, 98, 46, 5, fill="white", outline="")
        # fones
        for x1 in (28, 158):
            r(x1, 62, x1 + 14, 92, 6, fill=grafite, outline="")
            r(x1 + 3, 70, x1 + 11, 84, 3, fill=cor, outline="")
        # visor (com o reflexo de cima)
        r(52, 44, 148, 108, 16, fill=visor, outline="")
        r(58, 49, 142, 63, 7, fill="#1F2839", outline="")

        # olhos (LED) e boca
        piscando = (t % 4.6) > 4.6 * 0.94
        olho = "#BFE0FF" if est == "aguardando" else luz_clara
        brilho = _mistura(cor, visor, 0.55)
        for x0 in (70, 108):
            ex, ey = P(x0 + 11, 77)
            if est in ("erro", "parado"):
                c.create_line(ex - 7, ey - 7, ex + 7, ey + 7, fill=cor, width=3, capstyle="round", **tag)
                c.create_line(ex - 7, ey + 7, ex + 7, ey - 7, fill=cor, width=3, capstyle="round", **tag)
            elif est == "pausado" or piscando:
                a1, b1 = P(x0, 79)
                a2, b2 = P(x0 + 22, 79)
                c.create_line(a1, b1, a2, b2, fill=olho, width=4 * s, capstyle="round", **tag)
            elif est in ("sessao", "pergunta"):
                c.create_oval(ex - 7, ey - 7, ex + 7, ey + 7, fill=cor, outline="", **tag)
                c.create_oval(ex - 2, ey - 4, ex + 2, ey, fill="white", outline="", **tag)
            else:  # arco "feliz" do mockup: halo largo por tras e o traco claro por cima
                pts = [*P(x0, 82), *P(x0 + 11, 68), *P(x0 + 22, 82)]
                c.create_line(*pts, smooth=True, width=7 * s, fill=brilho, capstyle="round", **tag)
                c.create_line(*pts, smooth=True, width=4 * s, fill=olho, capstyle="round", **tag)
        mx, my = P(100, 97)
        if est in ("erro", "parado"):
            c.create_arc(mx - 8, my - 1, mx + 8, my + 11, start=20, extent=140, style="arc",
                         outline=cor, width=3, **tag)
        elif est == "pausado":
            c.create_line(mx - 6, my + 1, mx + 6, my + 1, fill=olho, width=3, capstyle="round", **tag)
        elif est in ("sessao", "pergunta"):
            c.create_oval(mx - 4, my - 3, mx + 4, my + 5, outline=cor, width=3, **tag)
        else:
            c.create_line(*P(90, 95), *P(100, 102), *P(110, 95), smooth=True, width=3 * s,
                          fill=cor, capstyle="round", **tag)

        # pescoco, bracos (parados), ombros, tronco, peito e base
        r(88, 122, 112, 134, 4, fill=grafite, outline="")
        for x1 in (30, 148):
            r(x1, 140, x1 + 22, 194, 11, fill=grafite, outline="")
            bola(x1 + 11, 148, 5, fill=cor, outline="")
        r(54, 132, 146, 208, 20, fill=chapa, outline=contorno, width=1.5)
        r(60, 138, 100, 146, 4, fill="white", outline="")
        bola(100, 170, 13, fill="#0B1120", outline="")
        respira = 0.55 + 0.45 * (0.5 - 0.5 * math.cos(2 * math.pi * t / 2.4))  # opacidade .55 -> 1
        if est == "trabalhando":
            respira = 0.55 + 0.45 * (0.5 - 0.5 * math.cos(2 * math.pi * t / 0.8))
        bola(100, 170, 8, fill=_mistura("#0B1120", cor, respira), outline="")
        bola(98.5, 168.5, 3.5, fill=_mistura("#0B1120", luz_clara, respira), outline="")
        if self.bot.cfg.get("dry_run"):
            x, y = P(100, 197)
            c.create_text(x, y, text="TESTE", font=("Segoe UI", 7, "bold"), fill=grafite, **tag)
        r(64, 206, 136, 222, 8, fill=grafite, outline="")
        r(72, 211, 128, 215, 2, fill=_mistura(grafite, cor, 0.7), outline="")

        # contador de tarefas para a equipe
        if self.n_contatos:
            bx, by = P(44, 34)
            c.create_oval(bx - 12, by - 12, bx + 12, by + 12, fill="#E74C3C", outline="white", width=2, **tag)
            c.create_text(bx, by, text=str(min(self.n_contatos, 99)), fill="white",
                          font=("Segoe UI", 9, "bold"), **tag)

        # zzz quando pausado / ? quando precisa de alguem (desenhados com linhas:
        # texto sobre fundo transparente fica com borda rosa no Windows)
        hx, hy = P(172, 34)
        if est == "pausado":
            for i in range(2):
                fase = (t * 0.6 + i * 0.5) % 1
                zx, zy, z = hx + 4 + fase * 14, hy - 4 - fase * 26, 4 + i * 2
                c.create_line(zx - z, zy - z, zx + z, zy - z, zx - z, zy + z, zx + z, zy + z,
                              fill=grafite, width=2, **tag)
        elif est in ("sessao", "pergunta"):
            qx, qy = hx + 10, hy + 2 * math.sin(t * 4)
            c.create_arc(qx - 7, qy - 16, qx + 7, qy - 2, start=-60, extent=240,
                         style="arc", outline=cor, width=3, **tag)
            c.create_line(qx + 3, qy - 3, qx, qy + 3, qx, qy + 6, fill=cor, width=3, **tag)
            c.create_oval(qx - 2, qy + 10, qx + 2, qy + 14, fill=cor, outline="", **tag)

    def _animar(self):
        self._desenhar_robo()
        self._desenhar_balao()
        self.root.after(40, self._animar)


# ------------------------------------------------- janela: tarefas da equipe
# ------------------------------------------- janela: tarefas (design Main.dc.html)
# Visual do mockup "Tarefas do Robo" que o dono colocou na pasta em 04/11: fundo claro,
# cabecalho azul-marinho com o robo, cartoes brancos arredondados, botao azul com
# "sombra" embaixo. A fonte do mockup (Nunito) nao existe no Windows: Segoe UI negrito.
NAVY = "#10203A"
AZUL = "#1F5FD6"
AZUL_ESC = "#164AA8"
AZUL_CLARO = "#4DA3FF"
AZUL_FUNDO = "#E6F0FF"
LARANJA = "#FF8A3D"
LARANJA_ESC = "#C95A1A"
LARANJA_FUNDO = "#FDEFE6"
FUNDO = "#EEF3F8"
TXT, TXT2, TXT3 = "#10203A", "#2E3A52", "#4F5B73"
TXT_CAB = "#B9C6DD"
BORDA = "#DDE4EE"
SOMBRA = "#DCE3EC"
FONTE = "Segoe UI"
ICONES = "Segoe MDL2 Assets"
ICO = {"chat": "", "loja": "", "planilha": "", "assinar": "",
       "check": "", "relogio": "", "fechar": ""}
NOME_CANAL = {"logistica": "Logística", "leva & traz": "Leva & Traz"}


def _f(tam, peso="bold"):
    return (FONTE, tam, peso)


def _sin(item):
    """A equipe trabalha pelo nº do sinistro (o da OS aparece pequeno, so de referencia)."""
    return f"Sinistro {item['sinistro']}" if item.get("sinistro") else f"OS {item['os']}"


def _tels(item):
    tels = item.get("telefones") or ([item["telefone"]] if item.get("telefone") else [])
    return " · ".join(contatos.formatar_telefone(t) for t in tels)


def _canal(item):
    c = item.get("canal") or ""
    return NOME_CANAL.get(c.lower(), c)


class Caixa(tk.Canvas):
    """Container de cantos arredondados (Frame do tk e quadrado): o fundo e desenhado."""

    def __init__(self, pai, cor="white", fundo=FUNDO, raio=18, pad=(18, 16), sombra=True):
        super().__init__(pai, bg=fundo, highlightthickness=0, height=20)
        self.cor, self.raio, self.px, self.py = cor, raio, pad[0], pad[1]
        self.extra = 4 if sombra else 0
        self.dentro = tk.Frame(self, bg=cor)
        self._win = self.create_window(self.px, self.py, window=self.dentro, anchor="nw")
        self.bind("<Configure>", self._redesenhar)
        self.dentro.bind("<Configure>", self._ajustar_altura)

    def _ajustar_altura(self, _e=None):
        h = self.dentro.winfo_reqheight() + 2 * self.py + self.extra
        if int(float(self.cget("height"))) != h:
            self.configure(height=h)

    def _redesenhar(self, _e=None):
        w, h = self.winfo_width(), self.winfo_height()
        self.itemconfigure(self._win, width=max(20, w - 2 * self.px))
        self.delete("fundo")
        base = h - self.extra - 1
        if self.extra:
            ret_arred(self, 2, self.extra, w - 2, base + self.extra, self.raio, fill=SOMBRA, outline="",
                      tags="fundo")
        ret_arred(self, 0, 0, w - 1, base, self.raio, fill=self.cor, outline="", tags="fundo")
        self.tag_lower("fundo")


class Botao(tk.Canvas):
    """Botao arredondado do mockup: primario (azul com degrau), linha (contorno) e verde."""
    ESTILOS = {  # fundo, fundo_hover, texto, degrau, contorno
        "primario": (AZUL, AZUL_ESC, "white", AZUL_ESC, None),
        "linha": ("white", FUNDO, TXT2, None, BORDA),
        "escuro": ("#1E2D45", "#2B3B57", "white", None, None),
    }

    def __init__(self, pai, texto, comando, estilo="primario", icone=None, altura=38, fundo_pai="white",
                 tam=10):
        self.fundo, self.hover, self.cor_txt, self.degrau, self.contorno = self.ESTILOS[estilo]
        self.texto, self.icone, self.comando, self.altura = texto, icone, comando, altura
        self.fonte = tkfont.Font(family=FONTE, size=tam, weight="bold")
        self.fonte_ico = tkfont.Font(family=ICONES, size=tam)
        largura = self.fonte.measure(texto) + 30 + (self.fonte_ico.measure(icone) + 8 if icone else 0)
        super().__init__(pai, width=largura, height=altura + (4 if self.degrau else 0), bg=fundo_pai,
                         highlightthickness=0, cursor="hand2")
        self.largura, self.ativo, self._sobre, self._apertado = largura, True, False, False
        self.bind("<Enter>", lambda e: self._estado(sobre=True))
        self.bind("<Leave>", lambda e: self._estado(sobre=False, apertado=False))
        self.bind("<ButtonPress-1>", lambda e: self._estado(apertado=True))
        self.bind("<ButtonRelease-1>", self._soltou)
        self._desenhar()

    def habilitar(self, sim):
        self.ativo = sim
        self.configure(cursor="hand2" if sim else "arrow")
        self._desenhar()

    def _estado(self, sobre=None, apertado=None):
        if sobre is not None:
            self._sobre = sobre
        if apertado is not None:
            self._apertado = apertado
        self._desenhar()

    def _soltou(self, e):
        dentro = 0 <= e.x <= self.largura and 0 <= e.y <= self.altura + 4
        self._estado(apertado=False)
        if dentro and self.ativo:
            self.comando()

    def _desenhar(self):
        self.delete("all")
        w, h = self.largura, self.altura
        fundo = self.hover if (self._sobre and self.ativo) else self.fundo
        desce = 2 if (self._apertado and self.degrau and self.ativo) else 0
        if not self.ativo:
            fundo = "#9DB7EC" if self.degrau else FUNDO
        if self.degrau and not desce:
            ret_arred(self, 0, 4, w - 1, h + 3, 12, fill="#B4C7EE" if not self.ativo else self.degrau,
                      outline="")
        ret_arred(self, 0, desce, w - 1, h - 1 + desce, 12, fill=fundo,
                  outline=self.contorno or "", width=2 if self.contorno else 1)
        cor = self.cor_txt if self.ativo else ("white" if self.degrau else "#9AA5B1")
        x = w / 2
        if self.icone:
            lt = self.fonte.measure(self.texto)
            li = self.fonte_ico.measure(self.icone)
            x0 = (w - lt - li - 8) / 2
            self.create_text(x0 + li / 2, h / 2 + desce, text=self.icone, font=self.fonte_ico, fill=cor)
            x = x0 + li + 8 + lt / 2
        self.create_text(x, h / 2 + desce, text=self.texto, font=self.fonte, fill=cor)


def pilula(pai, texto, cor, fundo, fundo_pai="white", tam=9, icone=None):
    """Etiqueta arredondada (dias em aberto, canal, situacao)."""
    f = tkfont.Font(family=FONTE, size=tam, weight="bold")
    fi = tkfont.Font(family=ICONES, size=tam - 1)
    li = fi.measure(icone) + 6 if icone else 0
    w, h = f.measure(texto) + 22 + li, f.metrics("linespace") + 10
    c = tk.Canvas(pai, width=w, height=h, bg=fundo_pai, highlightthickness=0)
    ret_arred(c, 0, 0, w - 1, h - 1, h // 2, fill=fundo, outline="")
    if icone:
        c.create_text(11 + li / 2 - 3, h / 2, text=icone, font=fi, fill=cor)
    c.create_text(11 + li + f.measure(texto) / 2, h / 2, text=texto, font=f, fill=cor)
    return c


def ladrilho(pai, conteudo, cor, fundo, lado=46, raio=14, fonte=None, fundo_pai="white"):
    """Quadrado arredondado com uma letra ou um icone (avatar do cartao, icone da secao)."""
    c = tk.Canvas(pai, width=lado, height=lado, bg=fundo_pai, highlightthickness=0)
    ret_arred(c, 0, 0, lado - 1, lado - 1, raio, fill=fundo, outline="")
    c.create_text(lado / 2, lado / 2, text=conteudo, fill=cor, font=fonte or _f(15))
    return c


class JanelaTarefas:
    """O que o robo preparou e precisa de um clique da equipe (visual do Main.dc.html)."""

    def __init__(self, ui):
        self.ui, self.bot, self.cfg = ui, ui.bot, ui.bot.cfg
        self.top = tk.Toplevel(ui.root, bg=FUNDO)
        self.top.title("Tarefas do Robô")
        alto = min(760, self.top.winfo_screenheight() - 90)
        self.top.geometry(f"920x{alto}")
        self.top.minsize(760, 480)
        self._tentativa = {}  # OS -> indice do numero tentado no "Outro nº"
        self._quadro = 0

        self.cab = tk.Canvas(self.top, height=128, bg=NAVY, highlightthickness=0)
        self.cab.pack(fill="x")
        self.cab.bind("<Configure>", lambda e: self._desenhar_cabecalho())

        rodape = tk.Frame(self.top, bg="white")
        rodape.pack(side="bottom", fill="x")
        tk.Frame(rodape, bg=BORDA, height=1).pack(fill="x")
        linha = tk.Frame(rodape, bg="white")
        linha.pack(fill="x", padx=24, pady=10)
        tk.Label(linha, text=ICO["relogio"], font=(ICONES, 11), bg="white", fg=TXT3).pack(side="left")
        self.txt_rodape = tk.Label(linha, text="", font=_f(10), bg="white", fg=TXT3, anchor="w")
        self.txt_rodape.pack(side="left", fill="x", expand=True, padx=(8, 0))
        Botao(linha, "Conferir agora", self._conferir_agora, "linha", altura=34, tam=9).pack(side="right")

        area = tk.Frame(self.top, bg=FUNDO)
        area.pack(fill="both", expand=True)
        self.tela = tk.Canvas(area, bg=FUNDO, highlightthickness=0)
        barra = tk.Scrollbar(area, orient="vertical", command=self.tela.yview)
        self.lista = tk.Frame(self.tela, bg=FUNDO)
        self.lista.bind("<Configure>", lambda e: self.tela.configure(scrollregion=self.tela.bbox("all")))
        janela = self.tela.create_window(0, 0, window=self.lista, anchor="nw")
        self.tela.bind("<Configure>", lambda e: self.tela.itemconfigure(janela, width=e.width))
        self.tela.configure(yscrollcommand=barra.set)
        self.tela.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")
        self.top.bind("<MouseWheel>", lambda e: self.tela.yview_scroll(-e.delta // 120, "units"))
        self.montar()
        self._animar()

    # ------------------------------------------------------------- cabecalho
    def _status(self):
        est = self.ui.estado
        quando = getattr(self.ui, "ultima_busca", None)
        hora = f" · última conferência às {quando:%H:%M}" if quando else ""
        if est in ("erro", "parado"):
            return "Robô com problema — veja o balão" + hora, LARANJA
        if est == "sessao":
            return "Esperando o login no portal (2FA)", LARANJA
        if est == "pausado":
            return "Robô pausado" + hora, "#9AA5B1"
        if est == "trabalhando":
            return "Robô trabalhando agora" + hora, AZUL_CLARO
        return "Robô ativo" + hora, AZUL_CLARO

    def _robo(self, c, ox, oy, s, piscando):
        """O robo do mockup (viewBox 200x240) em escala s, desenhado com formas do Canvas."""
        def r(x1, y1, x2, y2, raio, **kw):
            ret_arred(c, ox + x1 * s, oy + y1 * s, ox + x2 * s, oy + y2 * s, raio * s, tags="robo", **kw)

        def bola(cx, cy, rr, **kw):
            c.create_oval(ox + (cx - rr) * s, oy + (cy - rr) * s, ox + (cx + rr) * s, oy + (cy + rr) * s,
                          tags="robo", **kw)

        grafite, chapa = "#3A4660", "#EDF1F7"
        r(97, 12, 103, 32, 3, fill=grafite, outline="")
        bola(100, 10, 7, fill=AZUL_CLARO, outline="")
        r(28, 62, 42, 92, 6, fill=grafite, outline="")
        r(158, 62, 172, 92, 6, fill=grafite, outline="")
        r(31, 70, 39, 84, 3, fill=AZUL_CLARO, outline="")
        r(161, 70, 169, 84, 3, fill=AZUL_CLARO, outline="")
        r(40, 30, 160, 122, 22, fill=chapa, outline="")
        r(46, 36, 98, 46, 5, fill="white", outline="")
        r(52, 44, 148, 108, 16, fill="#141D30", outline="")
        if not piscando:
            for x in (70, 108):
                c.create_line(ox + x * s, oy + 82 * s, ox + (x + 11) * s, oy + 68 * s, ox + (x + 22) * s,
                              oy + 82 * s, smooth=True, width=max(2, 4 * s * 1.4), fill="#BFE0FF",
                              capstyle="round", tags="robo")
        c.create_line(ox + 90 * s, oy + 95 * s, ox + 100 * s, oy + 102 * s, ox + 110 * s, oy + 95 * s,
                      smooth=True, width=2, fill=AZUL_CLARO, capstyle="round", tags="robo")
        r(88, 122, 112, 134, 4, fill=grafite, outline="")
        r(30, 140, 52, 194, 11, fill=grafite, outline="")
        r(148, 140, 170, 194, 11, fill=grafite, outline="")
        bola(41, 148, 5, fill=AZUL_CLARO, outline="")
        bola(159, 148, 5, fill=AZUL_CLARO, outline="")
        r(54, 132, 146, 208, 20, fill=chapa, outline="")
        r(60, 138, 100, 146, 4, fill="white", outline="")
        bola(100, 170, 13, fill="#0B1120", outline="")
        bola(100, 170, 8, fill=AZUL_CLARO, outline="")
        r(64, 206, 136, 222, 8, fill=grafite, outline="")
        r(72, 211, 128, 215, 2, fill=AZUL_CLARO, outline="")

    def _desenhar_cabecalho(self):
        c = self.cab
        if not c.winfo_exists():
            return
        w, h = c.winfo_width(), int(float(c.cget("height")))
        c.delete("all")
        c.create_oval(w - 190, -90, w + 70, 170, fill="#173052", outline="")   # circulos do fundo
        c.create_oval(w - 330, h - 60, w - 150, h + 120, fill="#322F3A", outline="")
        t = self._quadro * 0.06
        sobe = round(2 * math.sin(t * 1.85))              # flutua 4 px (3,4 s no mockup)
        piscando = (self._quadro % 70) in (66, 67)        # pisca a cada ~4 s
        self._robo(c, 22, 14 + sobe, 0.42, piscando)
        c.create_text(126, 46, text="Tarefas do Robô", anchor="w", font=_f(20), fill="white")
        texto, cor = self._status()
        c.create_oval(127, 76, 135, 84, fill=cor, outline="")
        c.create_text(144, 80, text=texto, anchor="w", font=_f(10), fill=TXT_CAB)
        total = contatos.contar_pendentes()
        fn, fp = tkfont.Font(family=FONTE, size=23, weight="bold"), tkfont.Font(family=FONTE, size=10, weight="bold")
        lb = fn.measure(str(total)) + fp.measure("pendentes") + 44
        x2 = w - 24
        ret_arred(c, x2 - lb, 38, x2, 92, 16, fill="#23324A", outline="#2D3B51")
        c.create_text(x2 - lb + 18, 66, text=str(total), anchor="w", font=fn, fill=AZUL_CLARO)
        c.create_text(x2 - 18, 68, text="pendentes", anchor="e", font=fp, fill=TXT_CAB)

    def _animar(self):
        if not self.top.winfo_exists():
            return
        self._quadro += 1
        self._desenhar_cabecalho()
        if self._quadro % 25 == 0:
            self._atualizar_rodape()
        self.top.after(60, self._animar)

    def _atualizar_rodape(self):
        prox = getattr(self.ui, "proxima", None)
        if self.ui.estado == "trabalhando":
            quando = "Conferindo agora"
        elif prox:
            falta = max(0, int(prox - time.time()))
            quando = f"Próxima conferência em {falta // 60 + 1} min" if falta >= 60 else \
                f"Próxima conferência em {falta} s"
        else:
            quando = "Próxima conferência em breve"
        self.txt_rodape.configure(text=f"{quando} · Perda total e OS já assinadas não aparecem aqui")

    def _conferir_agora(self):
        self.ui.rodar_agora()
        self.txt_rodape.configure(text="Conferindo agora… · Perda total e OS já assinadas não aparecem aqui")

    # --------------------------------------------------------------- montagem
    def montar(self):
        for filho in self.lista.winfo_children():
            filho.destroy()
        dados = contatos.carregar()
        log_itens = contatos.visiveis(dados, contatos.LOGISTICA)
        bal_itens = contatos.visiveis(dados, contatos.BALCAO)
        plan_itens = contatos.linhas_pendentes(dados)
        pos_itens = contatos.pos_conserto_visiveis(dados)
        secoes = [
            ("chat", AZUL_CLARO, "WhatsApp — o cliente vai enviar o produto?",
             "Mande a mensagem e marque a resposta aqui. Se confirmou, eu inicio a OS pra você.",
             log_itens, sum(1 for i in log_itens if i.get("situacao") in contatos.PENDENTES),
             self._linha_logistica),
            ("loja", AZUL_CLARO, "WhatsApp — Balcão: avisar que aguardamos o produto",
             "Mande a mensagem e marque Já avisei: aí eu inicio a OS.",
             bal_itens, len(bal_itens), self._linha_balcao),
            ("planilha", AZUL_CLARO, "Planilha — Leva & Traz: lançar na aba Atendimentos",
             "Copie a linha e cole (Ctrl+V) na coluna Sinistro da primeira linha vazia: cada valor cai "
             "na sua coluna. Depois marque Já lancei.",
             plan_itens, len(plan_itens), self._linha_planilha),
            ("assinar", LARANJA, "Depois do conserto — assinar OS",
             "Logística: escolha PAC ou SEDEX — eu gero a etiqueta, anoto o rastreio na OS, assino e "
             "deixo o WhatsApp pronto. Leva & Traz e Balcão aparecem na vez deles.",
             pos_itens, sum(1 for i in pos_itens if i.get("situacao") in ("pronta", "lancado")
                            and not ("balc" in (i.get("canal") or "").lower() and i["situacao"] == "pronta")),
             self._linha_pos),
        ]
        algum = False
        for icone, cor, titulo, ajuda, itens, pend, desenhar in secoes:
            if not itens:
                continue
            algum = True
            self._secao(icone, cor, titulo, ajuda, pend)
            for item in itens:
                desenhar(item)
        if not algum:
            self._tudo_em_dia()
        tk.Frame(self.lista, bg=FUNDO, height=18).pack()
        self._atualizar_rodape()

    def _secao(self, icone, cor, titulo, ajuda, pendentes):
        f = tk.Frame(self.lista, bg=FUNDO)
        f.pack(fill="x", padx=24, pady=(24, 8))
        ladrilho(f, ICO[icone], cor, NAVY, lado=42, raio=13, fonte=(ICONES, 15),
                 fundo_pai=FUNDO).pack(side="left")
        pilula(f, str(pendentes), cor, NAVY, fundo_pai=FUNDO, tam=10).pack(side="right")
        textos = tk.Frame(f, bg=FUNDO)
        textos.pack(side="left", fill="x", expand=True, padx=12)
        tk.Label(textos, text=titulo, font=_f(14), bg=FUNDO, fg=TXT, anchor="w").pack(fill="x")
        tk.Label(textos, text=ajuda, font=_f(9), bg=FUNDO, fg=TXT3, anchor="w", justify="left",
                 wraplength=640).pack(fill="x")

    def _tudo_em_dia(self):
        cx = Caixa(self.lista, cor=NAVY, pad=(22, 18))
        cx.pack(fill="x", padx=24, pady=(24, 0))
        d = cx.dentro
        bola = tk.Canvas(d, width=46, height=46, bg=NAVY, highlightthickness=0)
        bola.create_oval(0, 0, 45, 45, fill=AZUL_CLARO, outline="")
        bola.create_text(23, 23, text=ICO["check"], font=(ICONES, 16, "bold"), fill=NAVY)
        bola.pack(side="left")
        t = tk.Frame(d, bg=NAVY)
        t.pack(side="left", padx=16)
        tk.Label(t, text="Tudo em dia!", font=_f(14), bg=NAVY, fg="white", anchor="w").pack(fill="x")
        tk.Label(t, text="Pode voltar ao que estava fazendo — eu te aviso quando aparecer algo novo.",
                 font=_f(10), bg=NAVY, fg=TXT_CAB, anchor="w").pack(fill="x")

    def _cartao(self, letra, cor_letra, fundo_letra, nome, cidade, etiqueta, detalhe,
                item_whats=None, telefone=None, aviso=None, cor_aviso=LARANJA_ESC):
        """Cartao branco: avatar | nome, cidade, etiqueta / detalhe / telefone | acoes (devolvidas)."""
        cx = Caixa(self.lista)
        cx.pack(fill="x", padx=24, pady=6)
        d = cx.dentro
        ladrilho(d, (letra or "?")[:1].upper(), cor_letra, fundo_letra).pack(side="left", anchor="n")
        acoes = tk.Frame(d, bg="white")
        acoes.pack(side="right", padx=(12, 0))  # antes do meio: os botoes nunca sao cortados
        meio = tk.Frame(d, bg="white")
        meio.pack(side="left", fill="x", expand=True, padx=(16, 0))
        l1 = tk.Frame(meio, bg="white")
        l1.pack(fill="x")
        tk.Label(l1, text=nome, font=_f(13), bg="white", fg=TXT).pack(side="left")
        if cidade:
            tk.Label(l1, text=cidade, font=_f(10), bg="white", fg=TXT3).pack(side="left", padx=(8, 0))
        if etiqueta:
            pilula(l1, *etiqueta).pack(side="left", padx=(8, 0))
        tk.Label(meio, text=detalhe, font=_f(10), bg="white", fg=TXT2, anchor="w", justify="left",
                 wraplength=420).pack(fill="x", pady=(4, 0))
        if telefone is not None:
            l3 = tk.Frame(meio, bg="white")
            l3.pack(fill="x", pady=(6, 0))
            if item_whats is not None and telefone:
                zap = ladrilho(l3, ICO["chat"], AZUL, AZUL_FUNDO, lado=28, raio=9, fonte=(ICONES, 10))
                zap.configure(cursor="hand2")
                zap.bind("<Button-1>", lambda e, i=item_whats: self.whatsapp(i))
                zap.pack(side="left", padx=(0, 8))
            tk.Label(l3, text=telefone or "sem telefone na OS", font=_f(10), bg="white", fg=TXT3).pack(side="left")
            if item_whats is not None and telefone:
                self._link(l3, "WhatsApp", lambda i=item_whats: self.whatsapp(i), AZUL)
                if len(contatos.numeros_whatsapp(item_whats)) > 1:
                    self._link(l3, "Outro nº", lambda i=item_whats: self.outro_numero(i))
                self._link(l3, "Copiar msg", lambda i=item_whats: self.copiar(contatos.mensagem(i, self.cfg)))
        if aviso:
            tk.Label(meio, text=aviso, font=_f(9), bg="white", fg=cor_aviso, anchor="w", justify="left",
                     wraplength=420).pack(fill="x", pady=(6, 0))
        return acoes

    def _link(self, pai, texto, comando, cor=TXT3):
        """Acao discreta (Desfazer, Outro nº...): texto que sublinha ao passar o mouse."""
        lb = tk.Label(pai, text=texto, font=_f(9), bg="white", fg=cor, cursor="hand2")
        lb.pack(side="left", padx=(10, 0))
        lb.bind("<Button-1>", lambda e: comando())
        lb.bind("<Enter>", lambda e: lb.configure(font=(FONTE, 9, "bold underline"), fg=TXT))
        lb.bind("<Leave>", lambda e: lb.configure(font=_f(9), fg=cor))
        return lb

    def _antiga(self, item):
        return item.get("dias_aberto", 0) >= self.cfg.get("dias_os_antiga", 30)

    def _enviado(self, item):
        d = contatos.dias_desde(item.get("contatada_em"))
        return "hoje" if d == 0 else f"há {d} dia{'s' if d > 1 else ''}"

    @staticmethod
    def _dias(item):
        d = item.get("dias_aberto")
        if d is None:
            return None
        return (f"{d} dia{'s' if d != 1 else ''} em aberto", LARANJA_ESC, LARANJA_FUNDO)

    def _detalhe(self, item):
        return " · ".join(x for x in (_sin(item), f"OS {item['os']}", item.get("produto")) if x)

    # ----------------------------------------------------------------- linhas
    def _linha_logistica(self, item):
        sit = item.get("situacao")
        aviso, cor = None, LARANJA_ESC
        if sit == "contatada":
            aviso, cor = f"Mensagem enviada {self._enviado(item)} — aguardando a resposta", AZUL_ESC
        elif sit == "pronta" and self._antiga(item):
            aviso = "OS ANTIGA — confira no portal antes de mandar mensagem."
        a = self._cartao(item.get("nome"), LARANJA_ESC, LARANJA_FUNDO, item.get("nome") or _sin(item),
                         item.get("cidade"), self._dias(item), self._detalhe(item),
                         item_whats=item, telefone=_tels(item), aviso=aviso, cor_aviso=cor)
        if sit in contatos.PENDENTES:
            Botao(a, "Confirmou", lambda: self.responder(item, True), icone=ICO["check"]).pack(side="right")
            Botao(a, "Não vai enviar", lambda: self.responder(item, False), "linha").pack(side="right", padx=8,
                                                                                          anchor="n")
        else:
            confirmou = sit == "confirmou"
            teste = (" (modo teste: só preenche)" if confirmou and self.cfg.get("dry_run")
                     and not self.cfg.get("confirmou_inicia_mesmo_em_teste", True) else "")
            texto = ("Confirmou · vou iniciar a OS" + teste if confirmou
                     else "Não vai enviar · vou anotar na OS")
            self._link(a, "Desfazer", lambda: self.desfazer(item)).pack(side="right")
            pilula(a, texto, AZUL_ESC if confirmou else TXT2, AZUL_FUNDO if confirmou else FUNDO,
                   tam=9, icone=ICO["check"] if confirmou else None).pack(side="right")

    def _linha_balcao(self, item):
        sit = item.get("situacao")
        aviso, cor = None, LARANJA_ESC
        if sit == "contatada":
            aviso, cor = f"WhatsApp aberto {self._enviado(item)} — marque Já avisei depois de enviar", AZUL_ESC
        elif self._antiga(item):
            aviso = "OS ANTIGA — confira no portal antes de mandar mensagem."
        a = self._cartao(item.get("nome"), LARANJA_ESC, LARANJA_FUNDO, item.get("nome") or _sin(item),
                         item.get("cidade"), self._dias(item), self._detalhe(item),
                         item_whats=item, telefone=_tels(item), aviso=aviso, cor_aviso=cor)
        Botao(a, "Já avisei", lambda: self._mudou(item, "avisado", "CLIENTE_AVISADO"),
              icone=ICO["check"]).pack(side="right")

    def _linha_planilha(self, item):
        a = self._cartao(item.get("cidade"), AZUL, AZUL_FUNDO, _sin(item), item.get("cidade"),
                         (item.get("objetivo"), TXT2, FUNDO) if item.get("objetivo") else None,
                         " · ".join(x for x in (f"OS {item['os']}", item.get("produto")) if x))
        Botao(a, "Já lancei", lambda: self.lancou(item), icone=ICO["check"]).pack(side="right")
        Botao(a, "Copiar linha", lambda: self.copiar(contatos.texto_linha(item)), "linha").pack(
            side="right", padx=8, anchor="n")

    def _linha_pos(self, item):
        sit, canal = item.get("situacao"), (item.get("canal") or "").lower()
        codigo = item.get("codigo")
        avisar = bool(codigo) and not item.get("avisado_em")
        teste = (sit == "liberada" and self.cfg.get("dry_run") and not (
            item.get("liberada_por") == "equipe" and self.cfg.get("confirmou_inicia_mesmo_em_teste", True)))
        if teste:
            aviso = "Modo teste: o robô só preenche, não assina."
        else:
            aviso = item.get("aviso") if sit == "pronta" else None
        a = self._cartao(item.get("nome"), AZUL, AZUL_FUNDO, item.get("nome") or _sin(item), item.get("cidade"),
                         (_canal(item), TXT2, FUNDO), self._detalhe(item),
                         item_whats=item if avisar else None, telefone=_tels(item), aviso=aviso)
        if sit == "lancado":
            Botao(a, "Já avisei", lambda: self.avisou_rastreio(item), icone=ICO["check"]).pack(side="right")
            pilula(a, "OS assinada · mande o rastreio", AZUL_ESC, AZUL_FUNDO, icone=ICO["check"]).pack(
                side="right", padx=8)
        elif sit == "liberada":
            if not item.get("etiqueta_id"):  # depois de criada a etiqueta nao tem volta pela janela
                self._link(a, "Desfazer", lambda: self._pos(item, "pronta", "DESFEITO")).pack(side="right")
            if avisar:
                Botao(a, "Já avisei", lambda: self.avisou_rastreio(item), "linha").pack(side="right", padx=8)
            if item.get("pdf"):
                Botao(a, "Imprimir etiqueta", lambda: webbrowser.open(item["pdf"]), "linha").pack(
                    side="right", padx=(0, 8))
            texto = (f"Rastreio {codigo} lançado" if codigo else
                     f"Gerando etiqueta {item.get('servico_nome')}…" if item.get("servico") else
                     "Liberada · vou assinar")
            pilula(a, texto, AZUL_ESC, AZUL_FUNDO, icone=ICO["check"]).pack(side="right")
        elif "balc" in canal:
            Botao(a, "Cliente retirou", lambda: self.liberar_assinatura(item, "o cliente RETIROU o produto"),
                  icone=ICO["check"]).pack(side="right")
            pilula(a, "Aguardando o cliente retirar", TXT2, FUNDO).pack(side="right", padx=8)
        elif "leva" in canal:
            Botao(a, "Finalizar atendimento",
                  lambda: self.liberar_assinatura(item, "o atendimento foi FINALIZADO na visita"),
                  icone=ICO["check"]).pack(side="right")
        else:  # Logistica: gera a etiqueta pelo SuperFrete ou cola o codigo de rastreio
            caixa = tk.Frame(a, bg="white")
            caixa.pack(side="right")
            # o mais barato primeiro (em destaque): dentro de SC o SEDEX costuma sair abaixo do PAC
            opcoes = sorted(item.get("cotacao") or [], key=lambda o: o["preco"])
            if opcoes:
                tk.Label(caixa, text="Gerar etiqueta (cobra do saldo do SuperFrete)", font=_f(9), bg="white",
                         fg=LARANJA_ESC, anchor="w").pack(fill="x")
                linha_sf = tk.Frame(caixa, bg="white")
                linha_sf.pack(fill="x", pady=(4, 8))
                for op in opcoes:
                    rot = f"{op['nome']} R$ {op['preco']:.2f}".replace(".", ",") + f" · {op['dias']}d"
                    Botao(linha_sf, rot, lambda o=op: self.gerar_etiqueta(item, o),
                          "primario" if op is opcoes[0] else "linha", tam=9).pack(side="left", padx=(0, 6))
            # colar o codigo a mao so de reserva (etiqueta feita no site/agencia, SuperFrete fora
            # do ar): com cotacao fica escondido atras de um link (pedido do dono, 04/11)
            linha = tk.Frame(caixa, bg="white")
            if opcoes:
                link = tk.Frame(caixa, bg="white")
                link.pack(fill="x")
                self._link(link, "Já gerei a etiqueta por fora",
                           lambda: (link.pack_forget(), linha.pack(fill="x", pady=(2, 0)))).pack(side="right")
            else:
                tk.Label(caixa, text="Já postou? Cole o código de rastreio", font=_f(9), bg="white",
                         fg=LARANJA_ESC, anchor="w").pack(fill="x")
                linha.pack(fill="x", pady=(4, 0))
            var = tk.StringVar()
            campo = tk.Entry(linha, textvariable=var, width=16, font=_f(11), bg="#F7F9FC", fg=TXT,
                             relief="flat", highlightthickness=2, highlightbackground=BORDA,
                             highlightcolor=AZUL, insertbackground=TXT)
            campo.pack(side="left", ipady=7)
            botao = Botao(linha, "Lançar", lambda: self.lancar_codigo(item, var.get()))
            botao.pack(side="left", padx=(8, 0))
            botao.habilitar(False)
            var.trace_add("write", lambda *_: botao.habilitar(len("".join(var.get().split())) >= 8))
            campo.bind("<Return>", lambda e: botao.ativo and self.lancar_codigo(item, var.get()))

    # ------------------------------------------------------------------ acoes
    def _pos(self, item, situacao, status_csv, **extra):
        contatos.marcar_pos_conserto(item["chave"], situacao, **extra)
        self.bot.registrar(item["os"], "Assinar OS", status_csv, "pela janela do robozinho",
                           sinistro=item.get("sinistro"))
        self.ui.contatos_mudaram()

    def lancar_codigo(self, item, codigo):
        codigo = "".join(codigo.split()).upper()
        if len(codigo) < 8:
            messagebox.showwarning("Robô Prestador", "Cole o código de rastreio (ex.: AB123456789BR).",
                                   parent=self.top)
            return
        if not messagebox.askyesno("Robô Prestador", f"Lançar o código {codigo} no {_sin(item)}?\n\n"
                                   "O robô anota o rastreio na OS (Mensagens), ASSINA a OS (data de hoje) "
                                   "e o WhatsApp com o rastreio fica pronto aqui para o cliente.",
                                   parent=self.top):
            return
        self._pos(item, "liberada", "CODIGO_INFORMADO", codigo=codigo, liberada_por="equipe")
        historico.esquecer(item["os"], "Assinar OS")
        historico.esquecer(item["os"], "Anotacao rastreio")
        self.ui.painel.acordar.set()

    def gerar_etiqueta(self, item, opcao):
        preco = f"R$ {opcao['preco']:.2f}".replace(".", ",")
        if not messagebox.askyesno(
                "Robô Prestador", f"Gerar a etiqueta {opcao['nome']} ({preco}, {opcao['dias']} dias úteis) "
                f"para o {_sin(item)}?\n\nO valor é COBRADO DO SALDO do SuperFrete. Depois o robô abre o PDF "
                "para imprimir, anota o rastreio na OS, ASSINA a OS e prepara o WhatsApp.", parent=self.top):
            return
        self._pos(item, "liberada", "ETIQUETA_APROVADA", liberada_por="equipe", servico=opcao["id"],
                  servico_nome=opcao["nome"], preco=opcao["preco"], aviso=None)
        historico.esquecer(item["os"], "Assinar OS")
        historico.esquecer(item["os"], "Anotacao rastreio")
        self.ui.painel.acordar.set()

    def avisou_rastreio(self, item):
        # antes de o robo assinar continua "liberada"; depois de assinada, sai da janela
        situacao = "avisado" if item.get("situacao") == "lancado" else item["situacao"]
        contatos.marcar_pos_conserto(item["chave"], situacao, avisado_em=contatos.agora())
        self.bot.registrar(item["os"], "Assinar OS", "CLIENTE_AVISADO", f"rastreio {item.get('codigo')}",
                           sinistro=item.get("sinistro"))
        self.ui.contatos_mudaram()

    def liberar_assinatura(self, item, motivo):
        if not messagebox.askyesno("Robô Prestador", f"Confirma que {motivo} ({_sin(item)})?\n\n"
                                   "O robô vai ASSINAR a OS no portal (data de hoje).", parent=self.top):
            return
        self._pos(item, "liberada", "LIBERADA_PELA_EQUIPE", liberada_por="equipe")
        historico.esquecer(item["os"], "Assinar OS")
        self.ui.painel.acordar.set()

    def _mudou(self, item, situacao, status_csv):
        contatos.marcar(item["os"], situacao)
        self.bot.registrar(item["os"], f"Contato {item.get('tipo', contatos.LOGISTICA)}", status_csv,
                           "pela janela do robozinho", sinistro=item.get("sinistro"))
        self.ui.contatos_mudaram()

    def _abrir_link(self, url):
        """whatsapp:// abre o aplicativo; se ele nao estiver instalado, vai pelo navegador."""
        try:
            os.startfile(url)
        except OSError:
            webbrowser.open(url.replace("whatsapp://send?phone=", "https://wa.me/").replace("&text=", "?text=", 1))

    def whatsapp(self, item, numero=None):
        if not item.get("telefone"):
            messagebox.showwarning("Robô Prestador", f"O {_sin(item)} não tem celular nas Informações da OS.\n"
                                   "Confira no portal e fale com o cliente por outro meio.", parent=self.top)
            return
        numero = numero or contatos.numeros_whatsapp(item)[0]
        self.top.title(f"Tarefas do Robô — {_sin(item)}: abrindo {contatos.formatar_telefone(numero)}")
        self._abrir_link(contatos.link_whatsapp(item, self.cfg, numero))
        if item.get("situacao") == "pronta":
            self._mudou(item, "contatada", "WHATSAPP_ABERTO")
        if self.cfg.get("whatsapp_enviar_sozinho", False) and self.cfg.get("whatsapp_app", True) and _user32:
            self._esperar_e_enviar(item, numero, time.time(), None)

    def _esperar_e_enviar(self, item, numero, inicio, em_foco_desde):
        """
        Espera o WhatsApp ficar na frente com a conversa carregada e aperta Enter.
        Se o foco sair do WhatsApp, recomeca a contagem; se nao vier em 15 s, desiste.
        """
        agora = time.time()
        espera = self.cfg.get("whatsapp_espera_segundos", 3)
        tel = contatos.formatar_telefone(numero)
        if whatsapp_em_foco():
            em_foco_desde = em_foco_desde or agora
            if agora - em_foco_desde >= espera:
                apertar_enter()
                self.bot.registrar(item["os"], f"Contato {item.get('tipo', contatos.LOGISTICA)}",
                                   "WHATSAPP_ENVIADO", f"Enter automatico para {tel}")
                if self.top.winfo_exists():
                    self.top.title(f"Tarefas do Robô — {_sin(item)}: mensagem enviada para {tel}")
                return
        else:
            em_foco_desde = None
        if agora - inicio > 15:
            if self.top.winfo_exists():
                self.top.title("Tarefas do Robô")
                messagebox.showinfo("Robô Prestador", f"Não consegui enviar sozinho para o {_sin(item)} "
                                    "(o WhatsApp não ficou na frente).\nSe a conversa abriu, é só apertar Enter nela.",
                                    parent=self.top)
            return
        self.top.after(300, lambda: self._esperar_e_enviar(item, numero, inicio, em_foco_desde))

    def outro_numero(self, item):
        """'Numero nao esta no WhatsApp': tenta o proximo (outro telefone da OS, ou sem o 9)."""
        numeros = contatos.numeros_whatsapp(item)
        i = (self._tentativa.get(item["os"], 0) + 1) % len(numeros)
        self._tentativa[item["os"]] = i
        self.whatsapp(item, numeros[i])
        self.top.title(f"Tarefas do Robô — {_sin(item)}: tentando {contatos.formatar_telefone(numeros[i])} "
                       f"({i + 1} de {len(numeros)})")

    def copiar(self, texto):
        self.top.clipboard_clear()
        self.top.clipboard_append(texto)
        self.top.title("Tarefas do Robô — copiado!")
        self.top.after(2000, lambda: self.top.winfo_exists() and self.top.title("Tarefas do Robô"))

    def responder(self, item, sim):
        pergunta = (f"O cliente do {_sin(item)} confirmou que VAI ENVIAR o produto?\n\n"
                    "O robô vai INICIAR a OS de verdade e anotar nela que aguardamos o produto chegar."
                    if sim else
                    f"O cliente do {_sin(item)} NÃO vai enviar o produto?\n\n"
                    "O robô NÃO inicia a OS: anota nas Mensagens da OS que o cliente não vai "
                    "enviar o produto, e a tarefa sai da lista.")
        if not messagebox.askyesno("Robô Prestador", pergunta, parent=self.top):
            return
        if sim:
            self._mudou(item, "confirmou", "CLIENTE_CONFIRMOU")
            # confirmacao e fato novo: esquece teste/pulo/erro anteriores do Iniciar OS
            # (em 03/11 um DRY_RUN da manha fazia o robo pular a OS confirmada)
            historico.esquecer(item["os"], "Iniciar OS")
            self.ui.painel.acordar.set()  # se estiver esperando, ja roda o ciclo
        else:
            self._mudou(item, "recusou", "CLIENTE_RECUSOU")

    def desfazer(self, item):
        self._mudou(item, "contatada" if item.get("contatada_em") else "pronta", "DESFEITO")

    def lancou(self, item):
        contatos.marcar_linha(item["os"], "lancada")
        self.bot.registrar(item["os"], "Planilha", "LANCADA", "pela janela do robozinho",
                           sinistro=item.get("sinistro"))
        self.ui.contatos_mudaram()


# ------------------------------------------ janela: OSs esperando conferencia
EXPLICA_STATUS = {
    "PULADA": "Pulada por alguém — o robô não pergunta de novo por 24 h",
    "ERRO": "Deu erro — o robô tenta no máximo 3 vezes por dia",
    "CLIQUEI_MAS_ETAPA_NAO_MUDOU": "Clicou e a etapa não mudou — confira no portal antes de repetir",
}


class JanelaTravadas:
    """O que a memoria do robo esta segurando: puladas, erros e cliques sem efeito."""

    def __init__(self, ui):
        self.ui = ui
        self.top = tk.Toplevel(ui.root, bg=FUNDO)
        self.top.title("OSs esperando conferência")
        self.top.geometry("760x440")
        tk.Label(self.top, text="O robô não vai mexer nestas OSs sozinho até alguém conferir.",
                 bg=FUNDO, fg=TEXTO, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=14, pady=(12, 2))
        tk.Label(self.top, text="Confira no portal e clique em Repetir para o robô tentar de novo no próximo ciclo.",
                 bg=FUNDO, fg="#5b6774", font=("Segoe UI", 9)).pack(anchor="w", padx=14, pady=(0, 8))
        self.lista = tk.Frame(self.top, bg=FUNDO)
        self.lista.pack(fill="both", expand=True)
        self.montar()

    def montar(self):
        for filho in self.lista.winfo_children():
            filho.destroy()
        itens = historico.travadas()
        if not itens:
            tk.Label(self.lista, text="Nada esperando conferência.", bg=FUNDO, fg="#5b6774",
                     font=("Segoe UI", 10)).pack(pady=30)
            return
        for it in itens[:12]:
            f = tk.Frame(self.lista, bg="white", highlightbackground="#dde4ec", highlightthickness=1)
            f.pack(fill="x", padx=12, pady=3)
            tk.Frame(f, bg=CORES["erro"] if it["status"] != "PULADA" else CORES["pergunta"], width=6
                     ).pack(side="left", fill="y")
            tk.Button(f, text="Repetir", command=lambda i=it: self.repetir(i), bg="#3fa9f5", fg="white",
                      relief="flat", font=("Segoe UI", 9, "bold"), cursor="hand2", padx=8
                      ).pack(side="right", padx=8)
            meio = tk.Frame(f, bg="white")
            meio.pack(side="left", fill="x", expand=True, padx=10, pady=6)
            quem = f"Sinistro {it['sinistro']}" if it.get("sinistro") else f"OS {it['os']}"
            tk.Label(meio, text=f"{quem} · {it['etapa']} · {it['quando'][8:10]}/{it['quando'][5:7]} "
                     f"{it['quando'][11:16]}", bg="white", fg=TEXTO, font=("Segoe UI", 10, "bold")).pack(anchor="w")
            tk.Label(meio, text=EXPLICA_STATUS.get(it["status"], it["status"]), bg="white", fg="#5b6774",
                     font=("Segoe UI", 9), wraplength=520, justify="left").pack(anchor="w")
        if len(itens) > 1:
            tk.Button(self.lista, text=f"Repetir todas ({len(itens)})", command=self.repetir_todas,
                      bg="#e3e8ee", fg=TEXTO, relief="flat", font=("Segoe UI", 9, "bold"), cursor="hand2",
                      padx=8, pady=3).pack(anchor="e", padx=12, pady=8)

    def repetir(self, it):
        if it["status"] == "CLIQUEI_MAS_ETAPA_NAO_MUDOU" and not messagebox.askyesno(
                "Robô Prestador", "O robô já clicou nesta OS e a etapa não mudou.\n"
                "Você conferiu no portal que ela ainda precisa ser feita?", parent=self.top):
            return
        historico.esquecer(it["os"], it["etapa"])
        self.montar()
        self.ui.painel.acordar.set()

    def repetir_todas(self):
        if messagebox.askyesno("Robô Prestador", "Liberar todas estas OSs para o robô tentar de novo?",
                               parent=self.top):
            historico.esquecer(status=("PULADA", "ERRO", "CLIQUEI_MAS_ETAPA_NAO_MUDOU"))
            self.montar()
            self.ui.painel.acordar.set()


# --------------------------------------------------------------------- main
def navegador_respondendo(cdp_url):
    import urllib.request
    try:
        urllib.request.urlopen(cdp_url.rstrip("/") + "/json/version", timeout=2)
        return True
    except OSError:
        return False


def usar_icone(root):
    """
    Icone do robo (recursos/robo.ico) em todas as janelas: sem isso a janela de tarefas
    aparecia na barra de tarefas com a pena do tkinter. No .exe o .ico vai junto
    (--add-data no gerar_executavel.py) e fica em sys._MEIPASS.
    """
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    ico = os.path.join(base, "recursos", "robo.ico")
    try:
        # agrupa as janelas com o icone do robo na barra (e nao com o do Python)
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("RoboPrestador.App")
    except Exception:
        pass
    if os.path.exists(ico):
        try:
            root.iconbitmap(default=ico)  # default=: vale tambem para as Toplevel
        except tk.TclError:
            pass


def main():
    # com argumentos (--anotar-sinistro, --uma-vez...) o .exe vira o bot.py de terminal
    if any(a.startswith("--") for a in sys.argv[1:]):
        import bot
        bot.main()
        return

    trava = socket.socket()
    try:
        trava.bind(("127.0.0.1", PORTA_INSTANCIA))
    except OSError:
        janela_de_erro("O robô já está rodando (procure o robozinho na tela).")
        return

    try:
        import bot
    except Exception as e:
        janela_de_erro(f"Não consegui carregar o bot.py:\n{type(e).__name__}: {e}")
        return

    # um clique so: se o Vivaldi do robo nao esta aberto, ja abre agora
    if not navegador_respondendo(bot.cfg["cdp_url"]):
        bot.abrir_navegador()

    painel = PainelTk(bot.log)
    bot.painel = painel
    root = tk.Tk()
    root.title("Robô Prestador")
    usar_icone(root)
    ui = Robozinho(root, painel, bot)

    def rodar_bot():
        try:
            bot.executar(uma_vez=False)
        except Exception as e:
            bot.log("Robo parou:\n" + traceback.format_exc())
            painel.estado("parado", "Eu parei!", f"{bot.resumo_erro(e)} - me reinicie.")
            return
        if not painel.deve_sair():
            painel.estado("parado", "Eu parei!", "Me reinicie pelo Robo Prestador.exe.")

    threading.Thread(target=rodar_bot, daemon=True).start()
    root.mainloop()
    painel.sair.set()


if __name__ == "__main__":
    main()
