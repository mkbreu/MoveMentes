#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VERSOES — o que esta instalado, o que existe publicado, e a troca do WAHA.

19/09/2026 (plano, item 67 — desenho do dono). So o master supremo ve e decide.
Esta peca nao decide nada sozinha: COLETA (uma vez por dia) e EXECUTA a troca
que o dono escolheu na tela.

  * WAHA ............ instalado (imagem do container) + publicados no Docker Hub
                      (nome e data), `latest` no fim, como ultimo recurso
  * motor WebJS ..... vem DENTRO da imagem do WAHA — nao se troca a parte
  * Docker, Debian, kernel do WSL ... so a versao (trocar exige recriar tudo)

A TROCA DO WAHA (so ela): recusa na janela do ranking; copia fresca das sessoes;
guarda o compose e o .env; troca a versao nos dois (o `provisionar.py` le
`WAHA_IMAGE` do .env); confere se a correcao da logo (Utils.js, feita para o
whatsapp-web.js 1.34.7) ainda serve — se o motor da versao nova for outro, a
montagem sai; recria SO o WAHA; relata como cada sessao voltou. Nao toca no
codigo de conexao, nao apaga sessao, nao volta sozinho (decisao do dono).

USO
    versoes.py --coletar            # imprime o JSON
    versoes.py --relatar            # coleta e conta ao site (1x por dia)
    versoes.py --do-site            # executa a troca pedida no site, se houver
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import requests          # noqa: E402
import wahasessao        # noqa: E402

BASE = Path(__file__).resolve().parent.parent
COMPOSE = BASE / 'docker-compose.yml'
ENVFILE = BASE / '.env'
CARIMBO = BASE / 'estado-pareamento' / 'versoes_relatadas.txt'
REPO = 'devlikeapro/waha'
MONTAGEM_LOGO = './patches/whatsapp-web.js/Utils.js:'
MOTOR_DA_CORRECAO = '1.34.7'


def _cmd(args, timeout=60):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or '').strip(), (r.stderr or '').strip()
    except Exception as exc:
        return 1, '', str(exc)[:200]


def _container():
    return wahasessao.carregar_env().get('WAHA_CONTAINER') or 'auto-waha-pls'


def publicados(limite=40):
    """[(tag, 'AAAA-MM-DD')] do WAHA no Docker Hub, do mais novo ao mais velho.

    So as tags fixas do motor WEBJS (`latest-AAAA.M.B`); `latest` entra no fim.
    """
    try:
        r = requests.get('https://hub.docker.com/v2/repositories/%s/tags' % REPO,
                         params={'page_size': 100, 'name': 'latest-20'}, timeout=20)
        dados = r.json().get('results') or []
    except Exception:
        return None
    tags = [(t['name'], (t.get('last_updated') or '')[:10]) for t in dados
            if re.fullmatch(r'latest-\d{4}\.\d+\.\d+', t.get('name') or '')]

    def chave(t):
        return tuple(int(x) for x in t[0].split('-', 1)[1].split('.'))
    tags.sort(key=chave, reverse=True)
    return tags[:limite]


def coletar():
    c = _container()
    _r, imagem, _e = _cmd(['docker', 'inspect', '-f', '{{.Config.Image}}', c])
    _r, pkg, _e = _cmd(['docker', 'exec', c, 'cat',
                        '/app/node_modules/whatsapp-web.js/package.json'])
    motor = ''
    try:
        motor = json.loads(pkg).get('version') or ''
    except Exception:
        pass
    _r, docker, _e = _cmd(['docker', 'version', '--format', '{{.Server.Version}}'])
    _r, kernel, _e = _cmd(['uname', '-r'])
    debian = ''
    try:
        debian = Path('/etc/debian_version').read_text().strip()
    except Exception:
        pass
    instalada = imagem.split(':', 1)[1] if ':' in imagem else imagem
    return {
        'coletado_em': datetime.now().isoformat(timespec='seconds'),
        'container': c,
        'waha': {'instalada': instalada, 'publicadas': publicados()},
        'motor_webjs': {'instalada': motor,
                        'nota': 'vem dentro do WAHA — muda quando o WAHA muda'},
        'docker': {'instalada': docker},
        'debian': {'instalada': debian},
        'wsl_kernel': {'instalada': kernel},
        'correcao_logo_montada': MONTAGEM_LOGO in COMPOSE.read_text(encoding='utf-8'),
    }


def _api():
    env = wahasessao.carregar_env()
    return ((env.get('MOVIMENTE_API_URL') or 'https://movementes.engekon.com').rstrip('/'),
            {'X-Automation-Token': env.get('MOVIMENTE_API_TOKEN') or ''})


def relatar(forcar=False):
    """Conta ao site. Uma vez por dia (o Docker Hub nao precisa de mais)."""
    if not forcar and CARIMBO.exists():
        try:
            if time.time() - float(CARIMBO.read_text().strip()) < 20 * 3600:
                return 0
        except Exception:
            pass
    dados = coletar()
    api, cab = _api()
    try:
        r = requests.post('%s/api/versoes' % api, headers=cab, json=dados, timeout=30)
        ok = r.status_code == 200
    except Exception as exc:
        print('versoes: nao consegui contar ao site (%s)' % exc)
        return 0
    if ok:
        CARIMBO.parent.mkdir(exist_ok=True)
        CARIMBO.write_text(str(time.time()))
        print('versoes: relatadas (WAHA %s, motor %s)' % (
            dados['waha']['instalada'], dados['motor_webjs']['instalada']))
    return 0


def _na_janela_do_ranking():
    """Ha ranking agendado nos proximos 40 min ou nos ultimos 40? (o site diz)"""
    api, cab = _api()
    try:
        agenda = requests.get('%s/api/agenda/agora' % api, headers=cab, timeout=15).json()
        horarios = [a['horario'] for a in agenda.get('agenda') or [] if a.get('ativo')]
    except Exception:
        return True                     # nao sei -> nao troca (seguro)
    agora = datetime.now()
    for h in horarios:
        try:
            hh, mm = [int(x) for x in h.split(':')]
        except Exception:
            continue
        marcado = agora.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if abs((agora - marcado).total_seconds()) < 40 * 60:
            return True
    return False


def trocar(tag, log=print):
    """Troca a versao do WAHA para `tag` ('latest-AAAA.M.B' ou 'latest')."""
    linhas = []

    def conta(t):
        linhas.append(t)
        log('   versoes: ' + t)

    publicadas = [t for t, _d in (publicados() or [])]
    if tag != 'latest' and tag not in publicadas:
        conta('RECUSADA: a versao %s nao existe no Docker Hub.' % tag)
        return linhas
    if _na_janela_do_ranking():
        conta('RECUSADA: perto do horario do ranking (40 min antes/depois). '
              'Peca de novo fora da janela.')
        return linhas
    novo = '%s:%s' % (REPO, tag)
    atual = COMPOSE.read_text(encoding='utf-8')
    m = re.search(r'image:\s*(devlikeapro/waha:[^\s]+)', atual)
    if not m:
        conta('RECUSADA: nao achei a linha da imagem do WAHA no compose.')
        return linhas
    if m.group(1) == novo:
        conta('ja esta na versao %s — nada a trocar.' % tag)
        return linhas

    # 1) copia fresca de cada sessao pronta (sem ela, nao troca)
    r, saida, erro = _cmd([sys.executable, str(Path(__file__).with_name('copia_sessao.py')),
                           '--garantir-a-viva'], timeout=600)
    conta('copia da sessao: %s' % ('conferida' if r == 0 else 'FALHOU (%s)' % (erro or saida)[-120:]))
    if r != 0:
        conta('troca CANCELADA: sem copia fresca nao se recria o WAHA.')
        return linhas

    # 2) baixar a versao nova ANTES de mexer em qualquer arquivo
    r, _s, erro = _cmd(['docker', 'pull', novo], timeout=1800)
    if r != 0:
        conta('troca CANCELADA: nao consegui baixar %s (%s).' % (novo, erro[-120:]))
        return linhas
    conta('versao %s baixada.' % tag)

    # 3) o motor da versao nova: a correcao da logo so vale para o 1.34.7
    _r, pkg, _e = _cmd(['docker', 'run', '--rm', '--entrypoint', 'cat', novo,
                        '/app/node_modules/whatsapp-web.js/package.json'], timeout=120)
    try:
        motor_novo = json.loads(pkg).get('version') or '?'
    except Exception:
        motor_novo = '?'
    conta('motor WebJS da versao nova: %s' % motor_novo)

    # 4) guardar e trocar compose e .env
    carimbo = time.strftime('%Y%m%d_%H%M%S')
    pasta = Path('/home/USUARIO/backups/compose'); pasta.mkdir(parents=True, exist_ok=True)
    shutil.copy2(COMPOSE, pasta / ('docker-compose.yml.antes_versao_' + carimbo))
    shutil.copy2(ENVFILE, pasta / ('env.antes_versao_' + carimbo))
    novo_compose = atual.replace(m.group(1), novo, 1)
    if motor_novo != MOTOR_DA_CORRECAO and MONTAGEM_LOGO in novo_compose:
        novo_compose = '\n'.join(
            ('#' + l + '  # tirada em %s: motor %s != %s' % (carimbo, motor_novo, MOTOR_DA_CORRECAO))
            if MONTAGEM_LOGO in l and not l.lstrip().startswith('#') else l
            for l in novo_compose.split('\n'))
        conta('correcao da logo TIRADA (era para o motor %s; o novo e %s) — '
              'conferir se a logo sai (plano, item 25).' % (MOTOR_DA_CORRECAO, motor_novo))
    COMPOSE.write_text(novo_compose, encoding='utf-8')
    env = ENVFILE.read_text(encoding='utf-8')
    if re.search(r'^WAHA_IMAGE=', env, flags=re.M):
        env = re.sub(r'^WAHA_IMAGE=.*$', 'WAHA_IMAGE=' + novo, env, flags=re.M)
    else:
        env = env.rstrip('\n') + '\nWAHA_IMAGE=' + novo + '\n'
    ENVFILE.write_text(env, encoding='utf-8')
    conta('compose e .env trocados para %s (copias em %s).' % (novo, pasta))

    # 5) recriar SO o WAHA
    r, _s, erro = _cmd(['docker', 'compose', '--project-directory', str(BASE),
                        'up', '-d', '--force-recreate', 'waha'], timeout=600)
    conta('WAHA recriado: %s' % ('ok' if r == 0 else 'FALHOU (%s)' % erro[-120:]))

    # 6) como as sessoes voltaram (so le, nao mexe)
    e = wahasessao.carregar_env()
    base, chave = e.get('WAHA_BASE') or 'http://localhost:3000', e.get('WAHA_API_KEY', '')
    fim = time.time() + 300
    while time.time() < fim and wahasessao.listar(base, chave) is None:
        time.sleep(10)
    time.sleep(90)
    for item in wahasessao.listar(base, chave) or []:
        nome = item.get('name')
        st, mo = wahasessao.estado_de(wahasessao.detalhar(base, chave, nome) or {})
        conta('sessao %s: %s/%s%s' % (nome, st or '?', mo or '?',
              '' if wahasessao.pronta(st, mo) else ' — se nao voltar, clique em Conectar'))
    conta('troca concluida para %s.' % tag)
    return linhas


def do_site():
    api, cab = _api()
    try:
        r = requests.post('%s/api/versoes/pedido' % api, headers=cab, timeout=30)
        tag = (r.json() or {}).get('trocar_waha') if r.status_code == 200 else None
    except Exception as exc:
        print('versoes: nao consegui perguntar ao site (%s)' % exc)
        return 0
    if not tag:
        return 0
    try:
        linhas = trocar(tag)
    except Exception as exc:
        linhas = ['a troca parou com erro: %s' % str(exc)[:200]]
    try:
        requests.post('%s/api/versoes/resultado' % api, headers=cab,
                      json={'linhas': linhas}, timeout=30)
    except Exception:
        pass
    relatar(forcar=True)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--coletar', action='store_true')
    p.add_argument('--relatar', action='store_true')
    p.add_argument('--do-site', action='store_true')
    a = p.parse_args()
    if a.coletar:
        print(json.dumps(coletar(), ensure_ascii=False, indent=1))
    elif a.relatar:
        return relatar(forcar=True)
    elif a.do_site:
        do_site()
        return relatar()
    else:
        p.print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
