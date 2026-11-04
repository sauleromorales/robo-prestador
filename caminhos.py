"""
Pasta da aplicacao: onde ficam config.json, logs/ e dados/.

Rodando como script, e a pasta dos .py. Empacotado no "Robo Prestador.exe"
(PyInstaller), o __file__ aponta para uma pasta temporaria - entao usa a pasta
onde o .exe esta.
"""
import os
import sys

if getattr(sys, "frozen", False):
    PASTA = os.path.dirname(os.path.abspath(sys.executable))
else:
    PASTA = os.path.dirname(os.path.abspath(__file__))
