#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agente de provisionamento — cria a instancia WAHA de um admin, do zero.

O QUE ISTO RESOLVE
O admin de uma cidade aperta um botao no site e nao precisa saber o que e
porta, container, worker ou banco. Ele digita o telefone dele; o resto acontece
aqui. Se nao existisse, alguem com acesso ao Dell teria que fazer a mao para
cada cidade nova — e o sistema deixaria de ser descentralizado.

POR QUE O AGENTE VIVE AQUI E NAO NO SITE
O site roda no Synology e nao alcanca o Docker do Dell. Por isso a ponte e uma
FILA: o site enfileira o pedido (com a porta ja escolhida por ele, que conhece
as regras da cidade) e este agente executa quando a maquina sobe.

UMA INSTANCIA POR ADMIN, E ISSO NAO E LUXO
Em 01/08/2026 duas instancias dividiram o banco `waha_all` e a 3000 acabou
rodando a sessao criada na 3001. A mesma credencial aberta em dois lugares faz
o WhatsApp derrubar uma; o sintoma foi `check-exists` devolvendo HTTP 500 e a
pipeline abortando no envio, o que nao lembra em nada a causa real. Por isso
cada instancia nasce com:

    porta propria, chave propria, volumes proprios,
    WAHA_NAMESPACE proprio          -> banco de config e sessoes
    WAHA_SESSION_NAMESPACE proprio  -> banco da CREDENCIAL de cada sessao

As duas variaveis de namespace importam: sem a segunda, o padrao vira o nome do
motor ("webjs") em todas as instancias e as credenciais voltam a se misturar.

PAREAMENTO POR CODIGO, NAO POR QR
Decisao do operador, e a certa para este fluxo: o codigo e texto, sobrevive a
latencia da fila e chega por e-mail. QR e imagem que expira em ~60s — ruim para
um fluxo assincrono.

⚠ SUSPEITA REGISTRADA: em 01/08 uma sessao pareada por `request-code` subiu sem
o job "RemoteAuth Backup Sync" e ficou sem copia gravada, morrendo em todo
reinicio. Nao foi provado que a causa e o codigo. Por isso este agente CONFERE
a copia depois de conectar (etapa `conectado`) e avisa se nao houver.
"""
import argparse
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent.parent
ENV = BASE / '.env'
REGISTRO = BASE / '.instancias.json'

log = logging.getLogger('provisionar')


# ===================== CONFIGURACAO =====================

def carregar_env():
    """Le o .env sem quebrar em senha com '@' — ver COMANDOS.md secao 6."""
    env = {}
    if not ENV.exists():
        return env
    for linha in ENV.read_text(encoding='utf-8').splitlines():
        linha = linha.strip()
        if linha and not linha.startswith('#') and '=' in linha:
            chave, valor = linha.split('=', 1)
            env[chave.strip()] = valor.strip().strip('"').strip("'")
    return env


CFG = carregar_env()
API_URL = CFG.get('MOVIMENTE_API_URL', 'https://movementes.engekon.com').rstrip('/')
API_TOKEN = CFG.get('MOVIMENTE_API_TOKEN', '')
PG_USER = CFG.get('POSTGRES_USER', 'automation')
PG_PASS_URL = CFG.get('POSTGRES_PASSWORD_URL', CFG.get('POSTGRES_PASSWORD', ''))
PG_CONTAINER = CFG.get('POSTGRES_CONTAINER', 'auto-postgres')
REDE = CFG.get('DOCKER_NETWORK', 'movimente-automacao_default')
# A MESMA IMAGEM QUE O COMPOSE PINA (13/09/2026). O padrao aqui era
# `waha:latest`, entao o container criado por este script nascia numa versao
# diferente da do resto do projeto: em 12/09 o orfao subiu na 2026.8.2 enquanto
# o oficial roda a 2026.8.1. Duas versoes do mesmo motor na mesma maquina
# transformam qualquer comparacao de log em adivinhacao. `WAHA_IMAGE` no .env
# continua mandando, para quando a troca for de proposito.
IMAGEM = CFG.get('WAHA_IMAGE', 'devlikeapro/waha:latest-2026.8.1')


def _headers():
    return {'X-Automation-Token': API_TOKEN, 'Content-Type': 'application/json'}


# ===================== REGISTRO LOCAL DAS INSTANCIAS =====================
# A chave de API de cada instancia fica AQUI, no Dell, e nao volta para o site:
# o site nao precisa dela (quem fala com o WAHA e este agente) e credencial que
# nao viaja e credencial que nao vaza no caminho.

def registro():
    if REGISTRO.exists():
        try:
            return json.loads(REGISTRO.read_text(encoding='utf-8'))
        except Exception as exc:
            log.warning('registro ilegivel (%s) — comecando vazio', exc)
    return {}


def salvar_registro(dados):
    REGISTRO.write_text(json.dumps(dados, indent=2, ensure_ascii=False),
                        encoding='utf-8')
    try:
        os.chmod(REGISTRO, 0o600)
    except Exception:
        pass


# ===================== DOCKER =====================

def docker(*args, timeout=180):
    """Roda docker e devolve (codigo, saida). Nunca levanta excecao."""
    try:
        r = subprocess.run(['docker'] + list(args), capture_output=True,
                           text=True, timeout=timeout)
        return r.returncode, ((r.stdout or '') + (r.stderr or '')).strip()
    except Exception as exc:
        return 1, str(exc)


def container_existe(nome):
    codigo, saida = docker('ps', '-a', '--filter', 'name=^%s$' % nome,
                           '--format', '{{.Names}}')
    return codigo == 0 and nome in saida.splitlines()


def container_rodando(nome):
    codigo, saida = docker('ps', '--filter', 'name=^%s$' % nome,
                           '--format', '{{.Names}}')
    return codigo == 0 and nome in saida.splitlines()


def porta_publicada(nome):
    """A porta que o container publica no host. None quando nao publica nada.

    Igual a do `registrar_instancias.py` de proposito: as duas leem a verdade
    do mesmo lugar (`docker port`), e duas pecas que leem do mesmo lugar nao
    tem como divergir — que era a causa raiz de 02-03/08.
    """
    codigo, saida = docker('port', nome, '3000')
    if codigo != 0 or not saida:
        return None
    for linha in saida.splitlines():
        achado = re.search(r':(\d+)\s*$', linha.strip())
        if achado:
            return int(achado.group(1))
    return None


def chave_do_container(nome):
    """A chave que o container REALMENTE usa, lida do `docker inspect`.

    QUEM SABE A CHAVE E O CONTAINER, NAO O ARQUIVO (16/08/2026)

    A chave saia do `.instancias.json`, indexada por PORTA. Esse arquivo virou
    mentira sem ninguem perceber: em 16/08 ele ainda dizia que a porta 3001 era
    `auto-waha-principal-palmeiropolis`, com a chave da epoca da P-64 — enquanto
    a 3001 e o `auto-waha-suplente-pls` desde 14/08, com outra chave. Todo
    pareamento do suplente por aqui teria levado HTTP 401 do primeiro passo ao
    ultimo, e o motivo apareceria na tela como "nao consegui criar a sessao".
    (Fica igual ao `registrar_instancias.py`, que ja fazia certo — duas peças
    lendo do mesmo lugar nao tem como divergir.)

    Devolve None quando o container nao existe: ai a chave nova e sorteada, que
    e o caminho correto para instancia que ainda vai nascer.
    """
    codigo, saida = docker('inspect', nome, '--format',
                           '{{range .Config.Env}}{{println .}}{{end}}')
    if codigo != 0:
        return None
    for linha in (saida or '').splitlines():
        if linha.startswith('WAHA_API_KEY='):
            return linha.split('=', 1)[1].strip() or None
    return None


def criar_banco(nome):
    """Cria o banco da instancia se ainda nao existir.

    O WAHA cria sozinho os bancos de que precisa, mas so consegue se o usuario
    tiver permissao de CREATE — e o banco de conexao inicial precisa existir
    antes, senao ele nem conecta para descobrir isso.
    """
    seguro = re.sub(r'[^a-z0-9_]', '', nome.lower())
    if not seguro:
        return False
    codigo, saida = docker(
        'exec', PG_CONTAINER, 'psql', '-U', PG_USER, '-d', 'postgres', '-tAc',
        "SELECT 1 FROM pg_database WHERE datname='%s'" % seguro)
    if codigo == 0 and saida.strip() == '1':
        log.info('   banco %s ja existe', seguro)
        return True
    codigo, saida = docker(
        'exec', PG_CONTAINER, 'psql', '-U', PG_USER, '-d', 'postgres', '-c',
        'CREATE DATABASE %s' % seguro)
    if codigo == 0:
        log.info('   banco %s criado', seguro)
        return True
    log.error('   nao consegui criar o banco %s: %s', seguro, saida[:200])
    return False


def subir_instancia(porta, nome_container, sufixo, chave):
    """Sobe o container do WAHA desta instancia. Idempotente.

    Devolve A PORTA REAL em que o container ficou (0 quando nao subiu).

    POR QUE DEVOLVE A PORTA (13/09/2026). Esta funcao pula para a proxima porta
    quando a pedida esta ocupada (16-0-B) — so que devolvia `True`, e quem
    chamou continuava achando que era a porta do pedido. Medido em 12/09 15:33,
    duas linhas seguidas do mesmo log:

        auto-waha-principal-pls criado na porta 3001
        instancia respondendo em http://localhost:3000    <- OUTRO container

    O pareamento inteiro foi feito na 3000 (o container antigo autenticou as
    15:35:10), enquanto o recem-criado ficou 25 horas em tela de QR, sem dono.
    Porta que muda e nao se conta vira duas verdades sobre o mesmo objeto.

    `sufixo` entra nos namespaces e nos volumes: e o que garante que esta
    instancia nao encoste nos bancos nem nos arquivos de outra.
    """
    if container_rodando(nome_container):
        # RODANDO NAO E SAUDAVEL (19/08/2026).
        #
        # `docker ps` lista tambem quem esta em laco de reinicio: o status vem
        # "Restarting (1) 2 seconds ago" e o nome aparece igualzinho ao de um
        # container saudavel. Foi assim que o `auto-postgres` passou por bom
        # enquanto morria e ressuscitava a cada segundo — e quem esperava o
        # codigo de pareamento so via a tela parada.
        #
        # Reiniciar sozinho ja e a tentativa do Docker; se ele esta nisso, mais
        # uma nao ajuda. O que resolve e recriar do zero, que e o caminho logo
        # abaixo.
        _, situacao = docker('ps', '--filter', 'name=^%s$' % nome_container,
                             '--format', '{{.Status}}')
        if 'restarting' in (situacao or '').lower():
            log.warning('   %s esta em laco de reinicio (%s) — nao adianta '
                        'esperar. Removendo para recriar do zero.',
                        nome_container, (situacao or '').strip())
            docker('rm', '-f', nome_container)
        else:
            log.info('   %s ja esta de pe', nome_container)
            return porta_publicada(nome_container) or porta

    if container_existe(nome_container):
        # CONTAINER PARADO PODE SER CADAVER, NAO PAUSA (14/08/2026).
        #
        # Em 12/22 um `docker run` falhou em "programming external
        # connectivity" (a porta pedida ja era do suplente) e DEIXOU o container
        # criado e quebrado. Duas horas depois o pedido seguinte achou esse
        # cadaver, deu `start` nele e o declarou pronto — mas ele nasceu sem
        # rede, nao resolvia `auto-postgres`, morria em 7 segundos e o
        # `restart: unless-stopped` o ressuscitava: 72 reinicios enquanto a tela
        # dizia "ligando o computador da automacao".
        #
        # `start` com codigo 0 so prova que o Docker aceitou o pedido. Quem
        # prova que subiu e ele CONTINUAR de pe alguns segundos depois.
        log.info('   %s existe parado — subindo', nome_container)
        codigo, saida = docker('start', nome_container)
        if codigo == 0:
            time.sleep(8)
            if container_rodando(nome_container):
                return porta_publicada(nome_container) or porta
            log.warning('   %s subiu e caiu — era cadaver de um pedido que '
                        'falhou. Removendo para recriar do zero.', nome_container)
        else:
            log.warning('   start falhou (%s) — vou recriar', saida[:160])
        docker('rm', '-f', nome_container)

    banco_base = 'waha_%s' % sufixo
    if not criar_banco(banco_base):
        return 0

    url_pg = ('postgresql://%s:%s@%s:5432/%s?sslmode=disable'
              % (PG_USER, PG_PASS_URL, PG_CONTAINER, banco_base))

    args = [
        'run', '-d',
        '--name', nome_container,
        '--restart', 'unless-stopped',
        '--network', REDE,
        '-p', '%d:3000' % porta,
        '-e', 'WHATSAPP_DEFAULT_ENGINE=WEBJS',
        # Identidade fixa: sem ela o worker vira o hostname do container, que
        # muda a cada recriacao, e a sessao gravada fica orfa.
        '-e', 'WAHA_WORKER_ID=w-%s' % sufixo,
        '-e', 'WAHA_WORKER_RESTART_SESSIONS=True',
        # As DUAS variaveis de namespace. Ver o cabecalho deste arquivo.
        '-e', 'WAHA_NAMESPACE=%s' % sufixo,
        # `s-` na frente, como manda a secao 3 do DICIONARIO: separa o banco da
        # CREDENCIAL do banco de config e deixa os dois legiveis lado a lado
        # (`waha_principal-pls` e `waha_s-principal-pls_principal-pls`).
        '-e', 'WAHA_SESSION_NAMESPACE=s-%s' % sufixo,
        '-e', 'WAHA_API_KEY=%s' % chave,
        '-e', 'WAHA_DASHBOARD_ENABLED=true',
        '-e', 'WAHA_DASHBOARD_USERNAME=adm%s' % sufixo,
        '-e', 'WAHA_DASHBOARD_PASSWORD=%s' % chave,
        '-e', 'WHATSAPP_SWAGGER_USERNAME=adm%s' % sufixo,
        '-e', 'WHATSAPP_SWAGGER_PASSWORD=%s' % chave,
        '-e', 'WAHA_PRINT_QR=false',
        '-e', 'WHATSAPP_SESSIONS_POSTGRESQL_URL=%s' % url_pg,
        '-e', 'WAHA_AUTO_START_DELAY_SECONDS=5',
        '-e', 'TZ=America/Sao_Paulo',
        '-v', 'waha-sessions-%s:/app/.sessions' % sufixo,
        '-v', 'waha-media-%s:/app/.media' % sufixo,
        '--log-opt', 'max-size=50m', '--log-opt', 'max-file=3',
        IMAGEM,
    ]
    # A PORTA OCUPADA NAO ENCERRA O PEDIDO — PULA PARA A PROXIMA (regra 16-0-B).
    #
    # "A menor livre a partir de 3000, em ordem crescente" e a regra. Quem
    # escolhe e o site, mas ele testa a porta DE FORA (do DS215j) — e o que ele
    # nao alcanca ele considera livre. Em 14/08 a 3001 nao tinha ponte de rede,
    # entao o site a deu como livre quando por dentro ela e do suplente.
    #
    # Quem sabe a verdade sobre a porta e esta maquina. Entao aqui, se o Docker
    # recusar por porta ocupada, sobe-se para a proxima e tenta de novo — em vez
    # de morrer deixando um container quebrado para tras.
    tentativas = 0
    while True:
        codigo, saida = docker(*args, timeout=300)
        if codigo == 0 or tentativas >= 5:
            break
        texto = (saida or '').lower()
        ocupada = ('address already in use' in texto
                   or 'port is already allocated' in texto
                   or 'programming external connectivity' in texto)
        if not ocupada:
            break
        if container_existe(nome_container):
            docker('rm', '-f', nome_container)      # nao deixa cadaver
        tentativas += 1
        porta += 1
        log.warning('   porta ocupada nesta maquina — tentando a %d', porta)
        for i, a in enumerate(args):
            if a == '-p':
                args[i + 1] = '%d:3000' % porta
                break

    if codigo != 0:
        # PEDIDO QUE FALHA NAO DEIXA CADAVER (regra 16-0-C).
        # O `docker run` cria o container ANTES de configurar a rede: quando
        # essa parte falha, ele fica para tras, parado, com
        # `restart: unless-stopped`. Foi assim que nasceu o
        # `auto-waha-principal-palmeiropolis` que ciclou 72 vezes em 14/08 — e,
        # antes dele, o `auto-waha-3001` com 854 reinicios em 11/08.
        # Duas vezes o mesmo defeito: marcar a linha como "falhou" no banco nao
        # e encerrar o pedido; encerrar e remover o que ficou.
        log.error('   nao consegui subir %s: %s', nome_container, saida[:300])
        if container_existe(nome_container):
            docker('rm', '-f', nome_container)
            log.info('   container incompleto removido — nao vai ficar '
                     'reiniciando sozinho')
        return 0
    log.info('   %s criado na porta %s', nome_container, porta)
    return porta_publicada(nome_container) or porta


def esperar_waha(porta, chave, limite=240):
    """Espera a API responder. WORKING nao basta — aqui so queremos o /health."""
    base = 'http://localhost:%d' % porta
    fim = time.time() + limite
    while time.time() < fim:
        try:
            r = requests.get(base + '/health', headers={'X-Api-Key': chave},
                             timeout=10)
            if r.status_code in (200, 401):   # 401 = de pe, so recusou a chave
                return r.status_code == 200
        except Exception:
            pass
        time.sleep(5)
    return False


# ===================== A SESSAO =====================

def criar_sessao(porta, chave, sessao, recriar=False):
    base = 'http://localhost:%d' % porta
    h = {'X-Api-Key': chave, 'Content-Type': 'application/json'}
    if recriar:
        try:
            requests.delete('%s/api/sessions/%s' % (base, sessao),
                            headers=h, timeout=30)
            time.sleep(3)
        except Exception:
            pass
    try:
        r = requests.post('%s/api/sessions' % base, headers=h,
                          json={'name': sessao, 'start': True}, timeout=60)
        # 409 = ja existe, e isso e sucesso para o nosso proposito.
        return r.status_code in (200, 201, 409)
    except Exception as exc:
        log.error('   falha ao criar a sessao %s: %s', sessao, exc)
        return False


def esperar_estado(porta, chave, sessao, alvos, limite=200,
                   terminais=('FAILED', 'STOPPED')):
    """Espera a sessao chegar num dos estados pedidos. Devolve o estado ou None.

    ESTADO TERMINAL ENCERRA A ESPERA (13/09/2026). `FAILED` e `STOPPED` nao
    viram `WORKING` sozinhos — a sessao so sai dali por acao de alguem (regra
    15-A). Esperar o limite inteiro nesse caso e tempo jogado fora: medido hoje
    as 19:00, foram 200 segundos olhando uma sessao que ja estava em FAILED
    desde o primeiro segundo, para so entao pedir um codigo que nao tinha como
    vir. Quem chamou decide o que fazer com o terminal; aqui so se devolve
    depressa. `terminais=()` volta ao comportamento antigo.
    """
    base = 'http://localhost:%d' % porta
    fim = time.time() + limite
    ultimo = None
    while time.time() < fim:
        try:
            r = requests.get('%s/api/sessions/%s' % (base, sessao),
                             headers={'X-Api-Key': chave}, timeout=15)
            if r.status_code == 200:
                ultimo = (r.json() or {}).get('status')
                if ultimo in alvos:
                    return ultimo
                if ultimo in (terminais or ()):
                    return ultimo
        except Exception:
            pass
        time.sleep(5)
    return ultimo


def confirmar_conectada(porta, chave, sessao, limite=150):
    """A sessao esta MESMO de pe? (ok, detalhe em portugues).

    POR QUE NAO BASTA `WORKING` (13/09/2026, regra do dono e regra 16).

    `WORKING` e' o estado do WORKER, nao do WhatsApp. Com o motor em
    `UNLAUNCHED` a sessao aparece WORKING e qualquer leitura devolve HTTP 500 —
    e foi assim que a tela passou dias dizendo "conectado" com o aparelho
    desvinculado do outro lado. Por isso aqui se exige, nesta ordem:

      1. `status == WORKING` E `engine.state == CONNECTED` (regra 16);
      2. o GRUPO responder a uma leitura de verdade.

    O item 2 e' o unico que prova que a injecao do whatsapp-web.js funcionou
    nesta subida: quando ela falha (`injectToFunction`, ERROS.md 4.50), a sessao
    fica WORKING+CONNECTED e mesmo assim nao se le nada. Sem essa prova,
    "conectado" e' promessa.

    A leitura tem paciencia de proposito: aparelho recem-pareado esta baixando
    historico e demora. Timeout curto aqui vira falso negativo — foi o que
    derrubou o pareamento das 13:51 de 12/09, quando um `timeout=3` fez a
    prontidao reiniciar uma sessao pareada havia um minuto.
    """
    base = 'http://localhost:%d' % porta
    cabecalho = {'X-Api-Key': chave}
    grupo = CFG.get('WHATSAPP_GROUP_ID', '')
    fim = time.time() + limite
    ultimo = 'a instancia nao respondeu'
    while time.time() < fim:
        try:
            r = requests.get('%s/api/sessions/%s' % (base, sessao),
                             headers=cabecalho, timeout=15)
            if r.status_code == 200:
                dados = r.json() or {}
                status = dados.get('status')
                motor = ((dados.get('engine') or {}).get('state')) or '-'
                if status in ('FAILED', 'STOPPED'):
                    return False, 'a sessao caiu para %s' % status
                if status == 'WORKING' and motor == 'CONNECTED':
                    if not grupo:
                        return True, ('WORKING + CONNECTED (sem WHATSAPP_GROUP_ID '
                                      'no .env para conferir a leitura)')
                    rg = requests.get(
                        '%s/api/%s/chats/%s/messages' % (base, sessao, grupo),
                        headers=cabecalho,
                        params={'limit': 1, 'downloadMedia': 'false'},
                        timeout=30)
                    if rg.status_code == 200:
                        return True, 'WORKING + CONNECTED e o grupo respondeu a leitura'
                    ultimo = ('conectada, mas o grupo nao respondeu (HTTP %s) — '
                              'e o sinal de injecao falhada' % rg.status_code)
                else:
                    ultimo = 'status=%s motor=%s' % (status, motor)
        except Exception as exc:
            ultimo = str(exc)[:120]
        time.sleep(10)
    return False, ultimo


def pedir_codigo(porta, chave, sessao, telefone):
    """Codigo de pareamento. O WEBJS sobe um Chromium: precisa de folga antes."""
    base = 'http://localhost:%d' % porta
    h = {'X-Api-Key': chave, 'Content-Type': 'application/json'}
    for tentativa in (1, 2, 3):
        try:
            r = requests.post('%s/api/%s/auth/request-code' % (base, sessao),
                              headers=h, json={'phoneNumber': telefone},
                              timeout=60)
            if r.status_code in (200, 201):
                codigo = (r.json() or {}).get('code')
                if codigo:
                    return codigo
            log.warning('   request-code tentativa %s: HTTP %s', tentativa,
                        r.status_code)
        except Exception as exc:
            log.warning('   request-code tentativa %s falhou: %s', tentativa, exc)
        # REPETIR NA MESMA PAGINA NUNCA IA FUNCIONAR (13/09/2026).
        #
        # Medido as 19:00, com o dono clicando em Conectar: a 1a tentativa
        # morreu no meio (`TargetCloseError` em `exposeFunction`) e DEIXOU o
        # ouvinte do codigo registrado na pagina. A 2a e a 3a bateram sempre no
        # mesmo erro do WAHA:
        #
        #     Failed to add page binding with name onCodeReceivedEvent:
        #     window['onCodeReceivedEvent'] already exists!
        #
        # Isto e' do lado do Chromium, nao da rede: enquanto a pagina for a
        # mesma, repetir o pedido so' gasta 20 segundos e volta HTTP 500. Entao
        # antes de tentar de novo a sessao e reiniciada, e a pagina nasce limpa.
        #
        # Reiniciar aqui nao tem o risco de sempre (carga de pagina = sorteio de
        # LOGOUT, ERROS.md 4.50): quem pede codigo esta em SCAN_QR_CODE ou
        # FAILED — nao ha vinculo para perder.
        if tentativa < 3:
            try:
                requests.post('%s/api/sessions/%s/restart' % (base, sessao),
                              headers={'X-Api-Key': chave}, timeout=40)
                log.info('   pagina reiniciada antes de pedir o codigo de novo')
                esperar_estado(porta, chave, sessao,
                               ('SCAN_QR_CODE', 'WORKING', 'STARTING'), limite=90)
            except Exception as exc:
                log.warning('   nao consegui reiniciar a sessao: %s', exc)
        time.sleep(20)
    return None


def copia_gravada(sufixo, sessao):
    """A sessao tem copia no banco? Conectado e gravado sao coisas diferentes.

    Uma sessao pode ficar WORKING/CONNECTED por dias com zero arquivos
    gravados — e morrer em todo reinicio sem que nada avise.

    O BANCO SE DESCOBRE, NAO SE CALCULA (regra 15-B; corrigido em 16/08/2026)

    Aqui o nome era montado na mao: `waha_webjs_<sufixo>_<sessao>`. Na varredura
    de 16/08 esse calculo apontava, para o principal, um banco que nao existe —
    e o parecido que existe (`waha_webjs_principal-pls`) guarda uma copia de
    **07/08**, cadaver da migracao da P-03. A copia viva mora em
    `waha_s-principal-pls_principal-pls`, porque o session namespace e POR
    INSTANCIA. Ou seja: a unica conferencia que existia entre "conectou" e
    "vai sobreviver ao reinicio" estava olhando para o banco errado.

    Quem sabe descobrir e o `wahasessao.py`, o mesmo modulo que a pipeline usa
    (regra 16: um lugar so decide). `None` continua sendo NAO SEI — diferente de
    zero, que e "nao ha copia".
    """
    try:
        import wahasessao
        quantos, _quando, erro = wahasessao.copia_da_sessao(sessao)
        if erro:
            log.warning('   nao consegui conferir a copia de %s: %s', sessao, erro)
            return None
        return quantos
    except Exception as exc:
        log.warning('   falha ao conferir a copia de %s: %s', sessao, exc)
        return None


# ===================== CONVERSA COM O SITE =====================

def avisar_etapa(pedido_id, etapa, mensagem=None):
    try:
        requests.post('%s/api/waha/etapa' % API_URL, headers=_headers(),
                      json={'id': pedido_id, 'etapa': etapa,
                            'mensagem': mensagem}, timeout=20)
    except Exception as exc:
        log.debug('nao consegui avisar a etapa %s: %s', etapa, exc)


def marcar(pedido_id, situacao, codigo=None, mensagem=None):
    try:
        requests.post('%s/api/waha/marcar' % API_URL, headers=_headers(),
                      json={'id': pedido_id, 'situacao': situacao,
                            'codigo': codigo, 'mensagem': mensagem}, timeout=30)
    except Exception as exc:
        log.warning('nao consegui marcar o pedido %s: %s', pedido_id, exc)


def buscar_fila():
    try:
        r = requests.get('%s/api/waha/pendentes' % API_URL,
                         headers=_headers(), timeout=45)
        if r.status_code != 200:
            log.warning('/api/waha/pendentes HTTP %s', r.status_code)
            return []
        return (r.json() or {}).get('pendentes') or []
    except Exception as exc:
        log.error('nao consegui ler a fila: %s', exc)
        return []


# ===================== O FLUXO =====================

# ============================================================
#  ETAPA 0 — A MAQUINA ESTA EM CONDICAO DE PAREAR? (19/08/2026)
#
#  QUEM APERTA "CONECTAR" NAO SABE O QUE E PONTE, DISCO OU CONTAINER.
#  Ate hoje este agente comecava direto na instancia, assumindo que o resto da
#  maquina estava bom. Quando nao estava, o pedido morria com uma frase que nao
#  ajudava ninguem ("nao consegui criar a sessao") e alguem com acesso ao Dell
#  tinha que arrumar a mao — exatamente o que o botao existe para evitar.
#
#  Os tres tropecos abaixo aconteceram de verdade entre 17 e 19/08:
#
#    disco   o ext4 do WSL corrompeu e foi remontado somente-leitura. Docker nao
#            sobe, container nao nasce, log nao grava. Nao ha conserto automatico
#            — mas ha diferenca enorme entre avisar isso e dizer "falhou".
#    docker  a distro sobe sem o daemon, e "docker: command failed" nao diz nada
#            a quem esta com o celular na mao esperando um codigo.
#    ponte   o IP interno do WSL MUDA a cada arranque. O `netsh portproxy` que
#            leva a porta 3000/3001 para dentro do WSL fica apontando para um
#            endereco que nao existe mais, e o site perde o WhatsApp EM SILENCIO.
#            Foi refeito a mao em 19/08 — e mao nao escala para outras cidades.
#
#  Cada passo conserta o que da e SEGUE; o que nao tem conserto automatico para
#  a rodada com o motivo em portugues (regra 16-D: toda espera tem desfecho, e
#  16-I: o alerta diz o que fazer, nao so o que houve).
# ============================================================

def disco_gravavel():
    """(ok, detalhe). O ext4 do WSL aceita escrita?

    Sem isto nada mais importa: em 18/08 o disco foi remontado somente-leitura e
    a automacao ficou dois dias fora do ar dando erro em toda parte, menos no
    lugar certo.
    """
    alvo = BASE / '.teste_escrita'
    try:
        alvo.write_text('ok', encoding='utf-8')
        alvo.unlink()
        return True, 'disco grava normalmente'
    except OSError as exc:
        return False, ('o disco do WSL esta somente-leitura (%s). Isso e '
                       'corrupcao do ext4 e NAO tem conserto automatico: '
                       'precisa de gente para reparar ou recriar a distro.'
                       % exc.strerror or exc)


def docker_de_pe(tentar_subir=True):
    """(ok, detalhe). O daemon responde? Se nao, tenta iniciar uma vez."""
    codigo, _ = docker('info', '--format', '{{.ServerVersion}}', timeout=30)
    if codigo == 0:
        return True, 'docker respondendo'
    if not tentar_subir:
        return False, 'o Docker nao esta de pe'
    log.info('   Docker parado — iniciando...')
    for comando in (['sudo', 'systemctl', 'start', 'docker'],
                    ['sudo', 'service', 'docker', 'start']):
        try:
            subprocess.run(comando, capture_output=True, text=True, timeout=90)
        except Exception:
            continue
        for _ in range(15):
            time.sleep(2)
            if docker('info', '--format', '{{.ServerVersion}}', timeout=20)[0] == 0:
                return True, 'docker iniciado agora'
    return False, ('o Docker nao subiu. Costuma ser o disco do WSL somente-'
                   'leitura ou o servico desabilitado.')


def ip_do_wsl():
    """O IP interno da distro, que MUDA a cada arranque. None se nao der."""
    try:
        r = subprocess.run(['hostname', '-I'], capture_output=True,
                           text=True, timeout=20)
        return (r.stdout or '').split()[0] if r.stdout.strip() else None
    except Exception:
        return None


def _netsh(*args, timeout=60):
    """Roda o netsh do WINDOWS de dentro do WSL (interop /mnt/c).

    Precisa de privilegio de administrador — que a tarefa agendada so tem depois
    do `Movimente-se_Elevar_Tarefas.ps1`. Sem ele o netsh responde "requer
    elevacao", e o certo e DIZER isso, nao seguir como se estivesse tudo bem.
    """
    exe = '/mnt/c/Windows/System32/netsh.exe'
    if not os.path.exists(exe):
        return 1, 'netsh nao encontrado (interop do Windows indisponivel)'
    try:
        r = subprocess.run([exe] + list(args), capture_output=True,
                           text=True, timeout=timeout, errors='replace')
        return r.returncode, ((r.stdout or '') + (r.stderr or '')).strip()
    except Exception as exc:
        return 1, str(exc)


def ponte_conferida(porta, ip=None):
    """(ok, detalhe). A ponte da porta aponta para o IP de agora?

    Conserta sozinha quando esta errada: e um comando, e o custo de nao fazer e
    o site nao enxergar o WhatsApp sem nada dar erro.
    """
    ip = ip or ip_do_wsl()
    if not ip:
        return False, 'nao consegui descobrir o IP interno do WSL'

    codigo, saida = _netsh('interface', 'portproxy', 'show', 'all')
    if codigo != 0:
        return False, 'nao consegui ler as pontes de rede: %s' % saida[:120]

    for linha in (saida or '').splitlines():
        partes = linha.split()
        if len(partes) >= 4 and partes[1] == str(porta):
            if partes[2] == ip:
                return True, 'ponte da porta %s ja aponta para %s' % (porta, ip)
            log.info('   a ponte da porta %s aponta para %s, mas o WSL esta em '
                     '%s — refazendo.', porta, partes[2], ip)
            break
    else:
        log.info('   nao ha ponte para a porta %s — criando.', porta)

    _netsh('interface', 'portproxy', 'delete', 'v4tov4',
           'listenport=%s' % porta, 'listenaddress=0.0.0.0')
    codigo, saida = _netsh('interface', 'portproxy', 'add', 'v4tov4',
                           'listenport=%s' % porta, 'listenaddress=0.0.0.0',
                           'connectport=%s' % porta, 'connectaddress=%s' % ip)
    if codigo != 0:
        if 'elev' in (saida or '').lower() or 'admin' in (saida or '').lower():
            return False, ('a ponte da porta %s precisa ser refeita, mas a '
                           'tarefa do Windows nao tem privilegio de '
                           'administrador. Rode uma vez o '
                           'Movimente-se_Elevar_Tarefas.ps1.' % porta)
        return False, 'nao consegui refazer a ponte da porta %s: %s' % (porta, saida[:120])
    return True, 'ponte da porta %s refeita para %s' % (porta, ip)


def banco_da_sessao_utilizavel(sufixo, sessao):
    """(ok, detalhe). A copia gravada da sessao ainda e LEGIVEL?

    POR QUE LER NAO BASTA PERGUNTAR SE EXISTE (19/08/2026)
    Depois da corrupcao do ext4, a tabela `files` continuava la, com a linha
    certa e o tamanho certo — e `md5(content)` respondia
    "missing chunk number 0 for toast value ...". O WAHA entrava em laco
    tentando ler, a sessao nunca saia de STARTING, e nada no status dizia por
    que. Ate `TRUNCATE` travava nessa tabela; so `DROP DATABASE` resolveu.

    Aqui a pergunta e feita do jeito que doi: `md5` percorre o conteudo inteiro.
    Se nao ler, o banco e descartado — a sessao vai precisar de pareamento de
    qualquer forma, e e melhor descobrir isso agora que depois do codigo enviado.
    """
    banco = 'waha_s-%s_%s' % (sufixo, sessao)
    codigo, saida = docker(
        'exec', PG_CONTAINER, 'psql', '-U', PG_USER,
        '-d', banco, '-tAc', 'select md5(content) from files limit 1;', timeout=90)

    if codigo != 0:
        texto = (saida or '').lower()
        if 'does not exist' in texto:
            return True, 'ainda nao ha banco desta sessao — nasce no pareamento'
        if 'missing chunk' in texto or 'toast' in texto:
            log.warning('   a copia da sessao %s esta corrompida.', sessao)
            if _repor_do_arquivo(sessao):
                return True, 'a copia estava corrompida e foi reposta do arquivo'
            log.warning('   sem copia utilizavel em arquivo — descartando o '
                        'banco %s para o WAHA recria-lo limpo.', banco)
            docker('exec', PG_CONTAINER, 'psql', '-U', PG_USER, '-d', 'postgres',
                   '-c', 'drop database if exists "%s" with (force);' % banco,
                   timeout=120)
            return True, ('a copia da sessao estava corrompida e nao havia '
                          'arquivo para repor; vai precisar parear')
        return True, 'nao deu para conferir a copia (%s) — sigo assim mesmo' % saida[:80]

    # BANCO EXISTE MAS TABELA VAZIA: e o caso de quem teve a copia APAGADA por
    # um restart em FAILED (regra 15-C). O zip em arquivo resolve sem celular.
    if not (saida or '').strip():
        log.warning('   a sessao %s esta sem copia no banco.', sessao)
        if _repor_do_arquivo(sessao):
            return True, 'a copia estava faltando e foi reposta do arquivo'
        return True, ('sem copia no banco e sem arquivo para repor — vai '
                      'precisar parear')
    return True, 'copia da sessao legivel'


def _repor_do_arquivo(sessao):
    """Tenta repor a copia a partir de `copias-sessao/`. True se conseguiu.

    A rede de seguranca da regra 17 existia e nunca era usada: quando a copia
    do banco sumia ou corrompia, o caminho era sempre parear de novo — uma
    pessoa, um celular, e o ranking parado ate ela aparecer. O zip guardado
    resolve sozinho na maioria das vezes.
    """
    try:
        import copia_sessao
    except Exception as exc:                     # pragma: no cover
        log.warning('   nao consegui carregar copia_sessao: %s', exc)
        return False
    ok, detalhe = copia_sessao.repor(sessao)
    log.info('   %s', detalhe)
    return ok


def conferir_maquina(porta, sufixo=None, sessao=None, pid=None):
    """(ok, motivo). Etapa 0: poe a maquina em condicao antes de mexer na sessao.

    Devolve (False, motivo) so no que NAO tem conserto automatico. Tudo que da
    para consertar e consertado aqui, em silencio quando ja estava bom — log de
    "esta tudo bem" a cada passo esconde o que importa.
    """
    if pid:
        avisar_etapa(pid, 'subindo', 'conferindo a maquina (disco, Docker, rede)')

    ok, detalhe = disco_gravavel()
    log.info('   [%s] disco    %s', 'OK ' if ok else ' X ', detalhe)
    if not ok:
        return False, detalhe

    ok, detalhe = docker_de_pe()
    log.info('   [%s] docker   %s', 'OK ' if ok else ' X ', detalhe)
    if not ok:
        return False, detalhe

    # A PONTE NAO REPROVA O PAREAMENTO, de proposito. Ela e o caminho do SITE
    # ate o WAHA; o pareamento em si acontece aqui dentro, por localhost, e
    # funciona mesmo com a ponte quebrada. Reprovar aqui seria impedir o
    # conserto justamente quando ele e possivel.
    ok, detalhe = ponte_conferida(porta)
    log.info('   [%s] ponte    %s', 'OK ' if ok else ' ! ', detalhe)

    if sufixo and sessao:
        ok, detalhe = banco_da_sessao_utilizavel(sufixo, sessao)
        log.info('   [%s] copia    %s', 'OK ' if ok else ' ! ', detalhe)

    return True, 'maquina em condicao'


def env_do_container(nome, variavel):
    """Le uma variavel de ambiente DO CONTAINER que esta de pe.

    Do container, e nao do `.env` nem do registro: o arquivo guarda a
    intencao, o container guarda o que esta valendo. Quando divergem, quem
    manda e o que roda.
    """
    codigo, saida = docker('inspect', nome, '--format',
                           '{{range .Config.Env}}{{println .}}{{end}}')
    if codigo != 0:
        return None
    for linha in saida.splitlines():
        if linha.startswith(variavel + '='):
            return linha.split('=', 1)[1].strip()
    return None


def instancia_da_cidade(cidade):
    """O WAHA que JA serve esta cidade, se houver. (porta, container, sufixo, chave).

    ===== POR QUE ISTO EXISTE (08/09/2026) =====
    O desenho e UM CONTAINER POR CIDADE, com principal e suplente como duas
    SESSOES dentro dele — foi assim que a fusao liberou ~590 MB de RAM numa
    maquina de 3,9 GB.

    So que o pedido 35 (08/09 15:20) pediu o suplente de volta e este script
    criou `auto-waha-suplente-pls` na porta 3002: um segundo WAHA da MESMA
    cidade, com namespace proprio. Ele obedecia a porta que o site mandou e
    derivava o nome da SESSAO, sem nunca perguntar se a cidade ja tinha casa.

    Agora pergunta. Se a cidade ja tem um WAHA DE PE, a sessao nasce dentro
    dele; a porta pedida e ignorada, e isso vai para o log.

    A trava: so' reaproveita container que esta RODANDO e do qual se consegue
    ler a chave. Container parado ou ilegivel nao serve de resposta — "nao
    consegui saber" nunca pode virar "entao nao existe" (regra 15-A).
    """
    if not cidade:
        return None
    alvo = str(cidade).strip().lower()
    for porta, dados in sorted(registro().items()):
        if str(dados.get('cidade', '')).strip().lower() != alvo:
            continue
        nome = dados.get('container')
        if not nome or not container_existe(nome):
            continue
        if not container_rodando(nome):
            # CONTAINER PARADO NAO E CONTAINER INEXISTENTE (13/09/2026).
            #
            # Em 12/09 as 15:32 o `auto-waha-pls` estava PARADO e este teste o
            # descartou como se a cidade nao tivesse casa. Nasceu um SEGUNDO
            # WAHA da mesma cidade (`auto-waha-principal-pls`, porta 3001), que
            # ficou 25 horas em tela de QR enquanto toda passada procurava na
            # 3000 e dizia "NENHUMA sessao de pe". Os rankings das 05:30 e
            # 15:30 de 13/09 nao sairam por causa disso.
            #
            # Casa parada se LIGA. So se nao ligar e que se cogita outra — e ai
            # o motivo fica no log, em vez de aparecer como container orfao.
            log.info('   %s tem o WAHA %s parado — ligando em vez de criar '
                     'outro.', cidade, nome)
            codigo, saida = docker('start', nome, timeout=120)
            if codigo != 0 or not container_rodando(nome):
                log.warning('   nao consegui ligar %s (%s) — sigo para criar.',
                            nome, (saida or '').strip()[:150])
                continue
        chave = chave_do_container(nome)
        if not chave:
            log.warning('   container %s esta de pe mas nao consegui ler a chave '
                        '— nao vou reaproveita-lo', nome)
            continue
        # O sufixo TEM de ser o do container, senao o banco de credencial da
        # sessao nova nasce com nome que ninguem procura depois.
        sufixo = env_do_container(nome, 'WAHA_SESSION_NAMESPACE') or dados.get('sufixo')
        if sufixo and sufixo.startswith('s-'):
            sufixo = sufixo[2:]
        return int(porta), nome, sufixo, chave
    return None


def atender(pedido):
    """Provisiona um pedido de ponta a ponta. Devolve True se o codigo saiu."""
    pid = pedido['id']
    porta = pedido.get('porta')
    sessao = pedido.get('sessao')
    telefone = re.sub(r'\D', '', pedido.get('telefone') or '')
    # O SUFIXO SAI DO NOME DA SESSAO, NAO DA PORTA.
    #
    # A sessao ja chega no formato <papel>-<cidade> (`principal-pls`), que e o
    # padrao da secao 3 do docs/DICIONARIO.md. Derivar da porta produzia
    # `p3002`: funciona (isola o banco, que e o essencial), mas obriga quem for
    # depurar a descobrir qual porta era qual cidade — exatamente o tipo de
    # traducao mental que fez o ranking nao sair em 02-03/08.
    #
    # Metade segura da P-03: vale so para instancia CRIADA daqui em diante. As
    # duas que ja existem (`waha_all` e `waha_suplente`, do docker-compose)
    # ficam como estao — renomea-las troca o banco e pede QR, e QR exige alguem
    # com o celular na mao (regra 17-A). Nenhuma instancia foi criada por este
    # script ate hoje, entao a troca nao deixa nada orfao para tras.
    nome_container = pedido.get('instancia') or ('auto-waha-%s' % (sessao or porta))
    sufixo = sessao or ('p%s' % porta)

    log.info('=' * 60)
    log.info('Pedido %s | %s | %s | sessao %s | porta %s',
             pid, pedido.get('cidade'), pedido.get('papel'), sessao, porta)

    # ---- UM CONTAINER POR CIDADE (08/09/2026) ----
    # Antes de qualquer coisa: a cidade ja tem casa? Se tem, a sessao entra
    # nela. Ver `instancia_da_cidade` para o incidente que gerou isto.
    ja = instancia_da_cidade(pedido.get('cidade'))
    # A TRAVA ESTAVA MORTA DESDE QUE NASCEU (13/09/2026).
    #
    # O teste era `if ja and not pedido.get('instancia')`, e o site NUNCA manda
    # esse campo vazio: `whatsapp.fila_pendente` preenche com
    # `auto-waha-<sessao>` quando o banco tem NULL. Logo a condicao era sempre
    # falsa e esta trava — escrita em 08/09 justamente para impedir um segundo
    # WAHA na mesma cidade — jamais rodou.
    #
    # Foi assim que em 12/09 15:32 o pedido ignorou o `auto-waha-pls` que
    # estava DE PE na 3000, tentou subir outro container na mesma porta, levou
    # "porta ocupada" e criou `auto-waha-principal-pls` na 3001: duas
    # instancias da mesma cidade, contra o desenho da fusao de 08/09 (uma
    # instancia por CIDADE, com principal e suplente como sessoes dentro dela).
    #
    # Quem decide o container e a MAQUINA (regra 16-0-D); o nome que vem do
    # site e sugestao, e so vale quando a cidade ainda nao tem casa.
    if ja:
        porta_ja, cont_ja, sufixo_ja, chave_ja = ja
        if cont_ja != nome_container or int(porta_ja) != int(porta or 0):
            log.info('   %s ja tem o WAHA %s na porta %s — a sessao "%s" nasce '
                     'DENTRO dele (a porta %s pedida foi ignorada).',
                     pedido.get('cidade'), cont_ja, porta_ja, sessao, porta)
        nome_container, porta, sufixo = cont_ja, porta_ja, (sufixo_ja or sufixo)

    if not porta or not sessao or not telefone:
        marcar(pid, 'falhou', mensagem='pedido incompleto (porta/sessao/telefone)')
        return False

    marcar(pid, 'em_andamento')

    # ---- chave da instancia: a do CONTAINER manda (16/08/2026) ----
    # Ordem da verdade: o container que esta de pe > o arquivo de registro >
    # uma chave nova. O arquivo continua existindo (ele guarda o painel e a
    # cidade), mas deixou de mandar na chave — ver `chave_do_container`.
    reg = registro()
    dados = reg.get(str(porta)) or {}
    chave_real = chave_do_container(nome_container)
    if chave_real and chave_real != dados.get('api_key'):
        log.warning('   a chave guardada para a porta %s nao era a do container '
                    '%s — usando a do container', porta, nome_container)
    chave = chave_real or dados.get('api_key') or secrets.token_urlsafe(18)
    reg[str(porta)] = {
        'api_key': chave, 'container': nome_container, 'sufixo': sufixo,
        'sessao': sessao, 'cidade': pedido.get('cidade'),
        'papel': pedido.get('papel'), 'painel': 'adm%s' % sufixo,
    }
    salvar_registro(reg)

    # ---- 0. a maquina esta em condicao? (19/08/2026) ----
    # Vem antes da instancia porque de nada adianta criar container num disco
    # somente-leitura, ou parear com a ponte de rede apontando para um IP que
    # nao existe mais. O que da para consertar, e consertado aqui.
    ok, motivo = conferir_maquina(porta, sufixo=sufixo, sessao=sessao, pid=pid)
    if not ok:
        marcar(pid, 'falhou', mensagem=motivo)
        return False

    # ---- 1. instancia de pe ----
    avisar_etapa(pid, 'subindo', 'preparando a instancia %s' % nome_container)
    porta_real = subir_instancia(porta, nome_container, sufixo, chave)
    if not porta_real:
        marcar(pid, 'falhou', mensagem='nao consegui subir a instancia %s' % nome_container)
        return False
    if int(porta_real) != int(porta or 0):
        # A PORTA REAL MANDA, E O REGISTRO TEM DE CONTAR A VERDADE (13/09/2026).
        # Sem isto o `.instancias.json` guarda a porta que foi PEDIDA: em 12/09
        # ficou dizendo que a 3000 era do `auto-waha-principal-pls`, que estava
        # na 3001 — e todo o pareamento seguiu falando com o container errado.
        log.warning('   a porta pedida (%s) estava ocupada; %s ficou na %s — '
                    'o pareamento e o registro seguem pela porta REAL.',
                    porta, nome_container, porta_real)
        reg = registro()
        if (reg.get(str(porta)) or {}).get('container') == nome_container:
            reg.pop(str(porta), None)
        linha = dict(reg.get(str(porta_real)) or {})
        linha.update({'api_key': chave, 'container': nome_container,
                      'sufixo': sufixo, 'sessao': sessao,
                      'cidade': pedido.get('cidade'),
                      'papel': pedido.get('papel'),
                      'painel': 'adm%s' % sufixo})
        reg[str(porta_real)] = linha
        salvar_registro(reg)
        porta = int(porta_real)
    if not esperar_waha(porta, chave):
        marcar(pid, 'falhou', mensagem='a instancia subiu mas a API nao respondeu')
        return False
    log.info('   instancia respondendo em http://localhost:%s', porta)

    # ---- 2. sessao ----
    avisar_etapa(pid, 'criando', 'criando a sessao %s' % sessao)
    if not criar_sessao(porta, chave, sessao, recriar=pedido.get('recriar')):
        marcar(pid, 'falhou', mensagem='nao consegui criar a sessao %s' % sessao)
        return False

    estado = esperar_estado(porta, chave, sessao,
                            ('WORKING', 'SCAN_QR_CODE', 'STARTING'), limite=200)
    log.info('   sessao em %s', estado)
    if estado == 'FAILED':
        # FAILED NAO PEDE CODIGO — PEDE SESSAO NOVA (13/09/2026, regra 15-A).
        #
        # Neste estado o WhatsApp ja desvinculou o aparelho do lado dele: a
        # sessao nao volta nem com restart nem com copia, e o `request-code`
        # devolve HTTP 500. Medido hoje as 19:00, com o dono esperando na tela:
        # o fluxo viu FAILED, seguiu em frente assim mesmo e queimou tres
        # tentativas de codigo.
        #
        # Parear E apagar e refazer do zero (regra 15-A) — o `send_ranking` ja
        # fazia isso no caminho dele; aqui faltava. Uma vez so: se voltar a
        # FAILED depois de refeita, o problema e outro e quem decide e gente.
        log.warning('   sessao em FAILED — apagando para refazer do zero '
                    '(regra 15-A); pedir codigo em FAILED so devolve HTTP 500.')
        avisar_etapa(pid, 'criando', 'refazendo a sessao do zero')
        try:
            requests.delete('%s/api/sessions/%s' % ('http://localhost:%d' % porta,
                                                    sessao),
                            headers={'X-Api-Key': chave}, timeout=30)
        except Exception as exc:
            log.warning('   nao consegui apagar a sessao em FAILED: %s', exc)
        time.sleep(3)
        if not criar_sessao(porta, chave, sessao):
            marcar(pid, 'falhou',
                   mensagem='a sessao estava em FAILED e nao consegui refaze-la')
            return False
        estado = esperar_estado(porta, chave, sessao,
                                ('WORKING', 'SCAN_QR_CODE', 'STARTING'),
                                limite=200)
        log.info('   sessao refeita — agora em %s', estado)
        if estado == 'FAILED':
            marcar(pid, 'falhou',
                   mensagem='a sessao voltou para FAILED logo depois de refeita '
                            '— o WhatsApp esta recusando este numero agora')
            return False
    if estado == 'WORKING':
        # WORKING SOZINHO NAO ENCERRA O PEDIDO (13/09/2026) — ver
        # `confirmar_conectada`. Se nao se prova a leitura, segue-se para o
        # pareamento, que e o que a pessoa pediu ao clicar em Conectar.
        ok_conexao, detalhe_conexao = confirmar_conectada(porta, chave, sessao)
        if ok_conexao:
            log.info('   ja estava conectada — %s', detalhe_conexao)
            marcar(pid, 'atendido', mensagem='ja estava conectada')
            avisar_etapa(pid, 'conectado', 'ja estava conectada')
            return True
        log.warning('   diz WORKING mas nao se provou de pe (%s) — seguindo '
                    'para o pareamento.', detalhe_conexao)

    # ---- 3. codigo ----
    avisar_etapa(pid, 'codigo', 'pedindo o codigo ao WhatsApp')
    codigo = pedir_codigo(porta, chave, sessao, telefone)
    if not codigo:
        marcar(pid, 'falhou', mensagem='o WhatsApp nao devolveu o codigo de pareamento')
        return False

    log.info('   codigo obtido: %s', codigo)
    # O site envia o e-mail ao receber o codigo (app/whatsapp.marcar).
    marcar(pid, 'atendido', codigo=codigo,
           mensagem='codigo gerado; aguardando o pareamento no celular')
    avisar_etapa(pid, 'aguardando', 'digite o codigo no celular %s' % telefone)

    # ---- 4. confirmar que conectou E que gravou ----
    estado = esperar_estado(porta, chave, sessao, ('WORKING',), limite=600)
    if estado != 'WORKING':
        # A TELA NAO PODE MENTIR DEPOIS QUE ACABOU (13/09/2026, pedido do dono).
        #
        # Aqui o fluxo saia com `return True` sem dizer nada a mais, e a tela
        # seguia mostrando "Esperando voce digitar o codigo" — para sempre, com
        # um codigo que o WhatsApp ja invalidou. Quem olha pensa que a conexao
        # ainda esta em curso enquanto o processo ja foi embora faz tempo.
        #
        # Agora o pedido e encerrado com o motivo em portugues, e o botao volta
        # a ser "Tentar de novo": codigo de pareamento tem validade curta, e
        # pedir outro e um clique.
        log.warning('   nao conectou dentro do tempo (estado=%s)', estado)
        marcar(pid, 'falhou',
               mensagem='o codigo foi gerado, mas o pareamento nao foi feito '
                        'em 10 minutos e o codigo perdeu a validade. Clique em '
                        'Conectar para pedir outro.')
        return False

    # A7 (18/09/2026): a prontidao nao reinicia sessao pareada ha < 10 min.
    try:
        import wahasessao
        wahasessao.marcar_pareada(sessao)
    except Exception:
        pass
    ok_conexao, detalhe_conexao = confirmar_conectada(porta, chave, sessao)
    if not ok_conexao:
        # Pareou e mesmo assim nao le: e' o erro de injecao (ERROS.md 4.50).
        # Dizer "conectado" aqui seria repetir a mentira que a tela ja contou
        # por dias.
        log.error('   pareou, mas nao se provou de pe: %s', detalhe_conexao)
        avisar_etapa(pid, 'aguardando',
                     'o WhatsApp aceitou o codigo, mas a sessao ainda nao le o '
                     'grupo — aguardando ela firmar')
        marcar(pid, 'falhou',
               mensagem='o pareamento foi aceito, mas a sessao nao consegue ler '
                        'o grupo (%s). Tente conectar de novo.' % detalhe_conexao)
        return False
    log.info('   conexao provada: %s', detalhe_conexao)
    arquivos = copia_gravada(sufixo, sessao)
    if arquivos:
        avisar_etapa(pid, 'conectado', 'conectado e com copia gravada')
        log.info('   CONECTADO, copia com %s arquivo(s)', arquivos)
    else:
        avisar_etapa(pid, 'conectado',
                     'conectado, mas SEM copia gravada — pode cair no reinicio')
        log.error('   CONECTADO mas SEM COPIA. Conferir o job RemoteAuth '
                  'Backup Sync desta sessao.')
    return True


def main():
    parser = argparse.ArgumentParser(description='Provisiona instancias WAHA sob demanda')
    parser.add_argument('--uma-vez', action='store_true',
                        help='atende a fila e sai (padrao)')
    parser.add_argument('--listar', action='store_true',
                        help='mostra as instancias ja provisionadas e sai')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%d/%m/%Y %H:%M:%S',
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(BASE / 'logs' /
                                      ('provisionar_%s.log' % time.strftime('%Y%m%d')),
                                      encoding='utf-8')])
    # LOG CANONICO (17/09/2026, item D1): cada linha deste script cai tambem
    # no livro semanal comum a Windows, WSL e site. Ver scripts/logcanon.py.
    try:
        import logcanon
        logcanon.ligar('SESSAO', 'provisionar')
    except Exception:
        pass

    if args.listar:
        for porta, dados in sorted(registro().items(), key=lambda kv: int(kv[0])):
            print('%s  %-22s %-16s %-11s %s' % (
                porta, dados.get('container'), dados.get('sessao'),
                dados.get('papel'), dados.get('cidade')))
        return 0

    if not API_TOKEN:
        log.error('MOVIMENTE_API_TOKEN vazio no .env — nao da para ler a fila.')
        return 1

    fila = buscar_fila()
    if not fila:
        log.info('Nenhum pedido de provisionamento.')
        return 0

    log.info('%d pedido(s) de provisionamento', len(fila))
    problemas = 0
    for pedido in fila:
        try:
            if not atender(pedido):
                problemas += 1
        except Exception as exc:
            log.exception('Pedido %s explodiu: %s', pedido.get('id'), exc)
            marcar(pedido.get('id'), 'falhou', mensagem='erro inesperado: %s' % exc)
            problemas += 1
    return 1 if problemas else 0


if __name__ == '__main__':
    sys.exit(main())
