#!/usr/bin/env python
"""Barra dado pessoal e segredo ANTES de ele ir para o git.

Regra do dono (15/10/2026): nenhum telefone, e-mail, nome de pessoa, senha,
chave ou credencial pode existir no codigo-fonte -- nem em comentario, nem em
exemplo. No codigo fica so o FORMATO (ex.: "(DD) 9XXXX-XXXX"); o valor real
mora fora do git (instance/dados_locais.json e o cofre).

Dois niveis de procura:
  1. PADROES (sempre): telefone, e-mail, CPF/CNPJ, IP de rede interna, id de
     grupo/contato do WhatsApp, atribuicao de senha/chave/token, chave privada.
  2. LISTA LOCAL (se existir `instance/denylist_sensiveis.json`, que NAO vai
     para o git): nomes, e-mails, telefones e segredos REAIS que o banco e o
     cofre conhecem -- pega o nome de uma pessoa mesmo sem padrao nenhum.

USO
  python scripts/verificar_sensiveis.py                 # arquivos versionados
  python scripts/verificar_sensiveis.py --staged        # o que vai no commit
  python scripts/verificar_sensiveis.py --instalar-hook # barra todo commit
Linha revisada e inofensiva (ex.: id de atividade do Strava): termine-a com
o marcador  sensivel-ok  num comentario.
Sai com codigo 1 se achar algo. Nunca imprime o valor inteiro -- so mascarado.
"""
import argparse
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import os

RAIZ = Path(__file__).resolve().parents[1]


def _achar_lista():
    """Onde fica a lista local: variavel de ambiente, pasta do usuario, ou instance/."""
    candidatos = [os.environ.get('MOVIMENTE_DENYLIST', ''),
                  str(Path.home() / '.movimente' / 'denylist_sensiveis.json'),
                  str(RAIZ / 'instance' / 'denylist_sensiveis.json')]
    for c in candidatos:
        if c and Path(c).exists():
            return Path(c)
    return Path(candidatos[1])


DENYLIST = _achar_lista()
MARCADOR = 'sensivel-ok'
EXTENSOES = {'.py', '.html', '.js', '.css', '.md', '.txt', '.ini', '.json', '.cfg',
             '.yml', '.yaml', '.sh', '.ps1', '.bat', '.mako', '.svg', '.toml', '.sql', '',
             '.vbs', '.service', '.timer', '.exemplo'}
IGNORAR_CAMINHOS = ('scripts/verificar_sensiveis.py', 'ferramentas/verificar_sensiveis.py', 'LICENSE',
                    'migrations/versions/', 'site/migrations/versions/')
COMUNS = {'maria', 'paulo', 'pedro', 'carlos', 'marcos', 'junior', 'filho', 'neto', 'santos', 'silva',
          'souza', 'sousa', 'costa', 'lima', 'rocha', 'ribeiro', 'pereira', 'oliveira', 'alves', 'martins'}
EXEMPLOS_MAC = {'00:1A:2B:3C:4D:5E', '11:22:33:44:55:66', 'AA:BB:CC:DD:EE:FF', '00:00:00:00:00:00'}
DOMINIOS_OK = {'exemplo.com', 'example.com', 'teste.com', 'email.com',
               'users.noreply.github.com'}

PADROES = [
    ('telefone', re.compile(
        r'(?<![\w.-])(?:\+?55[\s.-]?)?\(?[1-9]\d\)?[\s.-]?9?[\s.-]?\d{4}[\s.-]?\d{4}(?![\d])')),
    ('jid-whatsapp', re.compile(
        r'\d{8,}(?:-\d{5,})?@(?:c\.us|g\.us|lid|s\.whatsapp\.net)')),
    ('email', re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b')),
    ('mac', re.compile(r'\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b')),
    ('cpf-cnpj', re.compile(r'\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b')),
    ('ip-interno', re.compile(
        r'\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}'
        r'|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b')),
    ('segredo', re.compile(
        r'(?i)(?:senha|password|passwd|secret|token|api[_-]?key|chave|credential)\w*'
        r'\s*[:=]\s*["\']([^"\'\s]{8,})["\']')),
    ('chave-privada', re.compile(r'BEGIN [A-Z ]*PRIVATE KEY|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}')),
    ('credencial-na-url', re.compile(r'://[^/\s:@$]+:[^/\s@$]{3,}@')),
    # usuario do computador dentro de caminho (Linux/WSL/Windows): identifica a maquina e a pessoa
    ('caminho-de-usuario', re.compile(r'(?:/home/|/mnt/c/Users/|C:\\+Users\\+)(?!USUARIO\b)[A-Za-z0-9._-]+')),
]
# valor que e so NOME de constante ou texto de exemplo nao e segredo
NAO_E_SEGREDO = re.compile(r'^(?:[A-Za-z%][A-Za-z_%-]*|x+|\*+|seu_.*|sua_.*|<.*>|\{.*\}|%.*|https?://[^@\s]*)$')


def sem_acento(txt):
    return ''.join(c for c in unicodedata.normalize('NFD', txt.lower())
                   if unicodedata.category(c) != 'Mn')


def mascarar(valor):
    v = valor.strip()
    return v if len(v) <= 4 else v[:2] + '*' * min(len(v) - 3, 8) + v[-1]


def carregar_lista_local():
    if not DENYLIST.exists():
        return None
    dados = json.loads(DENYLIST.read_text(encoding='utf-8'))
    perm_arq = DENYLIST.with_name('permitidos.json')
    permitidos = set()
    if perm_arq.exists():
        permitidos = {sem_acento(x) for x in json.loads(perm_arq.read_text(encoding='utf-8')).get('nomes', [])}
    dados['nomes'] = [n for n in dados.get('nomes', []) if sem_acento(n) not in permitidos
                      and not any(sem_acento(n).startswith(p + ' ') for p in permitidos)]
    dados['primeiros'] = [n for n in dados.get('primeiros', []) if sem_acento(n) not in permitidos]
    ok_seg = []
    if perm_arq.exists():
        ok_seg = json.loads(perm_arq.read_text(encoding='utf-8')).get('segredos_ok', [])
    dados['segredos'] = [x for x in dados.get('segredos', [])
                         if not any(o in x or x in o for o in ok_seg)]
    nomes = sorted({sem_acento(n) for n in dados.get('nomes', []) if len(n) >= 7}, key=len, reverse=True)
    rx_nomes = re.compile(r'\b(?:' + '|'.join(re.escape(n) for n in nomes) + r')\b') if nomes else None
    return {
        'rx_nomes': rx_nomes,
        'emails': {e.lower() for e in dados.get('emails', [])},
        'telefones': {t for t in dados.get('telefones', []) if len(t) >= 8},
        'segredos': [s for s in dados.get('segredos', []) if len(s) >= 6],
        'ids': {str(i) for i in dados.get('ids', [])},
        'rx_termos': (re.compile('|'.join(re.escape(sem_acento(t)) for t in dados.get('termos', [])))
                      if dados.get('termos') else None),
        'rx_primeiros': re.compile(r'\b(?:' + '|'.join(re.escape(sem_acento(n)) for n in sorted(
            dados.get('primeiros', []), key=len, reverse=True) if sem_acento(n) not in COMUNS) + r')\b'),
    }


def so_digitos(txt):
    return re.sub(r'\D', '', txt)


def analisar(caminho, texto, lista):
    achados = []
    for n, linha in enumerate(texto.splitlines(), 1):
        if MARCADOR in linha:
            continue
        for tipo, rx in PADROES:
            for m in rx.finditer(linha):
                valor = m.group(0)
                if tipo == 'email':
                    dominio = valor.split('@', 1)[1].lower()
                    if dominio in DOMINIOS_OK or dominio in ('c.us', 'g.us', 'lid') or dominio.endswith('.service'):
                        continue
                if tipo == 'mac' and valor.upper().replace('-', ':') in EXEMPLOS_MAC:
                    continue
                if tipo == 'segredo' and NAO_E_SEGREDO.match(m.group(1)):
                    continue
                achados.append((caminho, n, tipo, mascarar(valor)))
        if lista:
            baixa = sem_acento(linha)
            if lista['rx_nomes']:
                for m in lista['rx_nomes'].finditer(baixa):
                    achados.append((caminho, n, 'nome-de-pessoa', mascarar(m.group(0))))
            if lista.get('rx_termos'):
                for m in lista['rx_termos'].finditer(baixa):
                    achados.append((caminho, n, 'termo-proibido', mascarar(m.group(0))))
            if lista.get('rx_primeiros'):
                for m in lista['rx_primeiros'].finditer(baixa):
                    achados.append((caminho, n, 'primeiro-nome', mascarar(m.group(0))))
            for e in re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+', linha):
                if e.lower() in lista['emails']:
                    achados.append((caminho, n, 'email-real', mascarar(e)))
            dig = so_digitos(linha)
            for t in lista['telefones']:
                if t in dig:
                    achados.append((caminho, n, 'telefone-real', mascarar(t)))
                    break
            for s in lista['segredos']:
                if s in linha:
                    achados.append((caminho, n, 'SEGREDO-REAL', mascarar(s)))
            for i in lista['ids']:
                if i in linha:
                    achados.append((caminho, n, 'id-de-pessoa', mascarar(i)))
    return achados


def git(*args):
    return subprocess.run(['git', '-C', str(RAIZ), *args], capture_output=True).stdout


def arquivos(staged):
    if staged:
        nomes = git('diff', '--cached', '--name-only', '--diff-filter=ACM', '-z').decode('utf-8').split('\0')
    else:
        nomes = git('ls-files', '-z').decode('utf-8').split('\0')
    for nome in filter(None, nomes):
        if nome.startswith(IGNORAR_CAMINHOS) or Path(nome).suffix.lower() not in EXTENSOES:
            continue
        if staged:
            dados = git('show', ':' + nome)
        else:
            p = RAIZ / nome
            if not p.exists():
                continue
            dados = p.read_bytes()
        if b'\0' in dados[:4096]:
            continue
        yield nome, dados.decode('utf-8', errors='replace')


def instalar_hook():
    hook = RAIZ / '.git' / 'hooks' / 'pre-commit'
    hook.write_text('#!/bin/sh\npython scripts/verificar_sensiveis.py --staged || {\n'
                    '  echo "Commit barrado: dado pessoal ou segredo no codigo."; exit 1; }\n',
                    encoding='utf-8', newline='\n')
    print('hook instalado em', hook)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--staged', action='store_true')
    ap.add_argument('--instalar-hook', action='store_true')
    ap.add_argument('--raiz', help='raiz do repositorio (padrao: a pasta acima de scripts/)')
    ap.add_argument('--extra', nargs='*', default=[], help='arquivos fora do git a incluir')
    args = ap.parse_args()
    global RAIZ
    if args.raiz:
        RAIZ = Path(args.raiz).resolve()
    if args.instalar_hook:
        return instalar_hook()
    lista = carregar_lista_local()
    if lista is None:
        print('aviso: sem instance/denylist_sensiveis.json -- so os padroes genericos.', file=sys.stderr)
    achados = []
    for nome, texto in arquivos(args.staged):
        achados += analisar(nome, texto, lista)
    for extra in args.extra:
        achados += analisar(extra, Path(extra).read_text(encoding='utf-8', errors='replace'), lista)
    for caminho, n, tipo, mascara in achados:
        print(f'{caminho}:{n}: {tipo}: {mascara}')
    print(f'\n{len(achados)} achado(s) em {len({a[0] for a in achados})} arquivo(s).')
    return 1 if achados else 0


if __name__ == '__main__':
    sys.exit(main())
