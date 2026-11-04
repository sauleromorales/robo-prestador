"""
Gera recursos/robo.ico (o rosto do robo do mockup, num ladrilho azul-marinho) sem bibliotecas extras:
desenha em 4x, reduz com media (borda suave) e grava PNG dentro de um ICO.
Uso: python recursos/gerar_icone.py
"""
import math
import os
import struct
import zlib

TAM, SS = 256, 3                 # tamanho final e supersampling
GRANDE = TAM * SS
# cores do mockup Main.dc.html (04/11): ladrilho azul-marinho com o rosto do robo
NAVY, CHAPA, BRILHO, VISOR = (16, 32, 58), (237, 241, 247), (255, 255, 255), (20, 29, 48)
GRAFITE, AZUL, OLHO = (74, 87, 117), (77, 163, 255), (191, 224, 255)


def dentro_ret_arred(x, y, x1, y1, x2, y2, r):
    if not (x1 <= x <= x2 and y1 <= y <= y2):
        return False
    cx = min(max(x, x1 + r), x2 - r)
    cy = min(max(y, y1 + r), y2 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def cor(u, v):
    """u, v em 0..1 (fracao do icone). Devolve (r, g, b) ou None (transparente)."""
    for ex in (0.405, 0.595):                           # olhos: arcos "felizes"
        d = math.hypot(u - ex, v - 0.60)
        if 0.042 <= d <= 0.078 and v <= 0.60:
            return OLHO
    d = math.hypot(u - 0.5, v - 0.625)                  # sorriso
    if 0.045 <= d <= 0.068 and v >= 0.655:
        return AZUL
    if dentro_ret_arred(u, v, 0.25, 0.38, 0.75, 0.75, 0.09):
        return VISOR
    if dentro_ret_arred(u, v, 0.21, 0.315, 0.46, 0.345, 0.015):
        return BRILHO
    if dentro_ret_arred(u, v, 0.17, 0.28, 0.83, 0.82, 0.13):
        return CHAPA
    for ox in (0.105, 0.835):                           # orelhas
        if dentro_ret_arred(u, v, ox + 0.015, 0.48, ox + 0.045, 0.62, 0.012):
            return AZUL
        if dentro_ret_arred(u, v, ox, 0.44, ox + 0.06, 0.66, 0.025):
            return GRAFITE
    if (u - 0.5) ** 2 + (v - 0.155) ** 2 <= 0.06 ** 2:  # luz da antena
        return OLHO if (u - 0.49) ** 2 + (v - 0.145) ** 2 <= 0.025 ** 2 else AZUL
    if abs(u - 0.5) <= 0.022 and 0.18 <= v <= 0.30:     # haste da antena
        return GRAFITE
    if dentro_ret_arred(u, v, 0.02, 0.02, 0.98, 0.98, 0.22):
        return NAVY                                     # ladrilho de fundo
    return None


def png(largura, altura, rgba):
    linhas = b"".join(b"\x00" + rgba[y * largura * 4:(y + 1) * largura * 4] for y in range(altura))

    def bloco(tipo, dados):
        return struct.pack(">I", len(dados)) + tipo + dados + struct.pack(">I", zlib.crc32(tipo + dados) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + bloco(b"IHDR", struct.pack(">IIBBBBB", largura, altura, 8, 6, 0, 0, 0))
            + bloco(b"IDAT", zlib.compress(linhas, 9)) + bloco(b"IEND", b""))


def main():
    grande = [[cor((x + 0.5) / GRANDE, (y + 0.5) / GRANDE) for x in range(GRANDE)] for y in range(GRANDE)]
    rgba = bytearray()
    for y in range(TAM):
        for x in range(TAM):
            amostras = [grande[y * SS + j][x * SS + i] for j in range(SS) for i in range(SS)]
            cheias = [a for a in amostras if a]
            alfa = round(255 * len(cheias) / len(amostras))
            if cheias:
                r, g, b = (round(sum(c[k] for c in cheias) / len(cheias)) for k in range(3))
            else:
                r = g = b = 0
            rgba += bytes((r, g, b, alfa))
    dados_png = png(TAM, TAM, bytes(rgba))
    # ICO com 1 imagem PNG 256x256 (o Windows reduz para os outros tamanhos)
    ico = struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(dados_png), 22) + dados_png
    destino = os.path.join(os.path.dirname(os.path.abspath(__file__)), "robo.ico")
    open(destino, "wb").write(ico)
    open(destino.replace(".ico", ".png"), "wb").write(dados_png)
    print("gerado:", destino)


if __name__ == "__main__":
    main()
