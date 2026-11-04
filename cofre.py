"""
Segredos do robo (ex.: chave da API do SuperFrete) no Gerenciador de Credenciais do
Windows - criptografado pelo proprio Windows para este usuario. Nunca em config.json,
logs ou codigo. Ver/apagar a mao: Painel de Controle > Gerenciador de Credenciais >
Credenciais do Windows > "RoboPrestador/...".
"""
import ctypes
from ctypes import wintypes

PREFIXO = "RoboPrestador/"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR)]


_advapi = ctypes.WinDLL("advapi32", use_last_error=True)
_advapi.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIAL), wintypes.DWORD]
_advapi.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(_CREDENTIAL))]
_advapi.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
_advapi.CredFree.argtypes = [ctypes.c_void_p]


def salvar(nome, segredo):
    dados = segredo.encode("utf-8")
    blob = (ctypes.c_ubyte * len(dados)).from_buffer_copy(dados)
    cred = _CREDENTIAL(Type=_CRED_TYPE_GENERIC, TargetName=PREFIXO + nome, CredentialBlobSize=len(dados),
                       CredentialBlob=ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte)),
                       Persist=_CRED_PERSIST_LOCAL_MACHINE, UserName="robo")
    if not _advapi.CredWriteW(ctypes.byref(cred), 0):
        raise OSError(ctypes.get_last_error(), "nao consegui gravar no Gerenciador de Credenciais")


def ler(nome):
    """O segredo, ou None se nao foi configurado."""
    p = ctypes.POINTER(_CREDENTIAL)()
    if not _advapi.CredReadW(PREFIXO + nome, _CRED_TYPE_GENERIC, 0, ctypes.byref(p)):
        return None
    try:
        c = p.contents
        return bytes(c.CredentialBlob[:c.CredentialBlobSize]).decode("utf-8")
    finally:
        _advapi.CredFree(p)


def apagar(nome):
    return bool(_advapi.CredDeleteW(PREFIXO + nome, _CRED_TYPE_GENERIC, 0))
