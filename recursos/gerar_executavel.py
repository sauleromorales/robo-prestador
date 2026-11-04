"""
Gera o "Robo Prestador.exe" na pasta do projeto (um arquivo so, sem terminal).

Rodar depois de qualquer mudanca nos .py (o .exe leva uma copia do codigo):
    python recursos/gerar_executavel.py
Feche o robozinho antes (o Windows nao deixa sobrescrever o .exe aberto).

config.json, logs/ e dados/ NAO vao dentro do .exe: ficam ao lado dele (caminhos.py).
"""
import os
import subprocess
import sys
import tempfile

PASTA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOME = "Robo Prestador"


def main():
    if not os.path.exists(os.path.join(PASTA, "recursos", "robo.ico")):
        subprocess.check_call([sys.executable, os.path.join(PASTA, "recursos", "gerar_icone.py")])
    trabalho = os.path.join(tempfile.gettempdir(), "robo_prestador_build")
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed",
           "--name", NOME,
           "--icon", os.path.join(PASTA, "recursos", "robo.ico"),
           "--distpath", PASTA,
           "--workpath", os.path.join(trabalho, "build"),
           "--specpath", trabalho,
           "--paths", PASTA,
           # o .ico tambem dentro do .exe: e o icone das janelas (robo.usar_icone)
           "--add-data", os.path.join(PASTA, "recursos", "robo.ico") + os.pathsep + "recursos",
           # importados dentro de funcoes: garante que entram no pacote
           "--hidden-import", "bot", "--hidden-import", "contatos",
           "--hidden-import", "historico", "--hidden-import", "caminhos",
           "--hidden-import", "superfrete", "--hidden-import", "cofre",
           os.path.join(PASTA, "robo.py")]
    print(" ".join(f'"{c}"' if " " in c else c for c in cmd))
    subprocess.check_call(cmd, cwd=PASTA)
    exe = os.path.join(PASTA, NOME + ".exe")
    print(f"\nPronto: {exe} ({os.path.getsize(exe) / 1e6:.0f} MB)")


if __name__ == "__main__":
    main()
