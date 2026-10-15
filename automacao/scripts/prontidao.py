#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PRONTIDAO — dizer, etapa por etapa, se da para confiar nesta instancia.

O PROBLEMA QUE ISTO RESOLVE
Ate aqui a automacao julgava prontidao por RELOGIO: "o container subiu ha 300
segundos, entao deve estar pronto". Relogio erra dos dois lados. Erra para
menos quando a maquina esta lenta e 300s nao bastaram — e ai a rodada atravessa
o processo e monta ranking sobre leitura incompleta. Erra para mais quando tudo
ja estava de pe ha horas — e ai a rodada espera a toa (medido em 06/08 01:40:
164 segundos de vigilia sobre uma leitura que nunca mudou).

O certo e PERGUNTAR. Cada etapa aqui tem um sinal concreto, e a rodada segue no
instante em que a ultima responde OK.

AS ETAPAS, NA ORDEM EM QUE ELAS FICAM PRONTAS
  1. container  o container esta rodando (e saudavel, quando ha healthcheck)
  2. http       a API responde (GET /ping)
  3. saude      GET /health diz "up" e GET /api/server/status da o UPTIME
  4. sessao     a sessao esta WORKING **e** CONNECTED (WORKING sozinho nao basta)
  5. copia      ha RemoteAuth gravado no Postgres — conectado nao e gravado
  6. grupo      da para LER o grupo. Publicar e ler nao sao a mesma coisa: o
                /sendText funciona assim que o socket conecta, e a leitura so
                depois que o banco local do WhatsApp Web se completa. Foi essa
                confusao que fez o robo publicar o ranking sem enxergar o link
                que estava no grupo (01/08).

TRES RESPOSTAS, NAO DUAS
Cada etapa devolve ok=True (passou), ok=False (reprovou) ou ok=None (NAO SEI).
"Nao sei" nunca reprova — e a diferenca entre "o docker disse que o container
caiu" e "nao consegui falar com o docker". Confundir os dois ja quase apagou uma
sessao recuperavel neste projeto.

O QUE ISTO NAO FAZ
Nao diz que a sincronizacao do historico terminou: esse indicador nao existe no
WAHA nem no whatsapp-web.js. O que este modulo entrega e o RELOGIO CERTO para
essa espera (o uptime do proprio WAHA, etapa 3) e a prova de que ler ja funciona
(etapa 6). Quem decide quando a leitura esta estavel continua sendo o
coletar_com_sincronizacao do send_ranking.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wahasessao


TIMEOUT_HTTP = 10

# Ordem em que as etapas sao mostradas e conferidas.
ETAPAS = ('container', 'http', 'saude', 'sessao', 'copia', 'grupo')

# Etapas que NAO reprovam a instancia quando falham. A copia e a mais
# importante da lista: sessao recem-pareada fica alguns minutos sem cópia
# gravada (o WAHA grava de 60 em 60 segundos) e nem por isso esta impedida de
# trabalhar. Reprovar aqui seria trocar "publica hoje e grava daqui a pouco"
# por "nao publica".
AVISO_APENAS = ('container', 'copia')


def duracao(segundos):
    """Tempo em portugues, nunca em conta de dividir.

    "840 min" obriga quem le a dividir por 60 no meio da madrugada para
    descobrir que sao 14 horas. Nos contamos em base 10 e a hora e base 60 —
    quem tem que fazer essa conversao e o codigo, nao a pessoa. Vale para log,
    e-mail, WhatsApp e tela: se o numero vai ser lido por gente, vem daqui.
    """
    try:
        s = int(round(float(segundos)))
    except (TypeError, ValueError):
        return '?'
    if s < 0:
        s = 0
    if s < 60:
        return '%d segundo%s' % (s, '' if s == 1 else 's')

    def _junta(a, qa, b, qb):
        parte = '%d %s%s' % (qa, a, '' if qa == 1 else 's')
        if qb:
            parte += ' e %d %s%s' % (qb, b, '' if qb == 1 else 's')
        return parte

    minutos = s // 60
    if minutos < 60:
        return _junta('minuto', minutos, 'segundo', 0)
    horas, resto_min = divmod(minutos, 60)
    if horas < 24:
        return _junta('hora', horas, 'minuto', resto_min)
    dias, resto_h = divmod(horas, 24)
    return _junta('dia', dias, 'hora', resto_h)


def _passo(nome, ok, detalhe):
    return {'etapa': nome, 'ok': ok, 'detalhe': detalhe}


def _cabecalho(chave):
    return {'X-Api-Key': chave} if chave else {}


# ==================== 1. CONTAINER ====================

def estado_do_container(nome):
    """(rodando, saude, erro). rodando=None significa NAO SEI.

    saude e None quando a imagem nao declara healthcheck — que e o caso das
    imagens do WAHA hoje. Ausencia de healthcheck nao e reprovacao: e ausencia
    de informacao, e quem responde por isso sao as etapas 2 e 3.
    """
    if not nome:
        return None, None, 'sem nome de container'
    formato = '{{.State.Running}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}'
    try:
        r = subprocess.run(['docker', 'inspect', nome, '--format', formato],
                           capture_output=True, text=True, timeout=30)
    except Exception as exc:
        return None, None, str(exc)
    if r.returncode != 0:
        return None, None, (r.stderr or '').strip()[:120] or 'docker recusou'
    bruto = (r.stdout or '').strip()
    if '|' not in bruto:
        return None, None, 'resposta inesperada: %r' % bruto[:60]
    rodando, saude = bruto.split('|', 1)
    return rodando.strip() == 'true', (saude.strip() or None), None


def _etapa_container(nome):
    rodando, saude, erro = estado_do_container(nome)
    if rodando is None:
        return _passo('container', None, 'nao deu para perguntar ao docker (%s)'
                      % (erro or 'sem motivo'))
    if not rodando:
        return _passo('container', False, 'o container "%s" nao esta rodando' % nome)
    if saude and saude != 'healthy':
        return _passo('container', False, 'container "%s" com saude "%s"' % (nome, saude))
    return _passo('container', True,
                  'container "%s" rodando%s' % (nome, ' e saudavel' if saude else ''))


# ==================== 2 e 3. A API E O UPTIME ====================

def uptime_do_waha(base, chave, timeout=TIMEOUT_HTTP):
    """Ha quantos SEGUNDOS este WAHA esta de pe, ou None se nao der.

    E o relogio certo para julgar sincronizacao — melhor que o `docker inspect`
    que estava aqui antes por tres motivos: quem sincroniza e o processo, nao o
    container; funciona sem acesso ao docker; e vale para WAHA remoto, que e
    para onde este projeto vai quando sair do WSL.

    O WAHA responde uptime em MILISSEGUNDOS (medido em 2026.7.1); aqui vira
    segundo, que e a unidade do resto do codigo.
    """
    try:
        resp = requests.get('%s/api/server/status' % base.rstrip('/'),
                            headers=_cabecalho(chave), timeout=timeout)
        if resp.status_code != 200:
            return None
        dados = resp.json() or {}
    except Exception:
        return None
    bruto = dados.get('uptime')
    if not isinstance(bruto, (int, float)):
        return None
    return float(bruto) / 1000.0


def _etapa_http(base, chave):
    try:
        resp = requests.get('%s/ping' % base.rstrip('/'),
                            headers=_cabecalho(chave), timeout=TIMEOUT_HTTP)
    except Exception as exc:
        return _passo('http', False, 'a API nao respondeu (%s)' % str(exc)[:80])
    if resp.status_code != 200:
        return _passo('http', False, 'a API respondeu HTTP %s' % resp.status_code)
    return _passo('http', True, 'a API respondeu')


def _etapa_saude(base, chave):
    """/health + uptime. Devolve (passo, uptime_seg).

    /health do WAHA CORE hoje so confere espaco em disco — ele diz "o servidor
    esta vivo", NAO "a sessao esta pronta". Por isso ele vale como etapa, e a
    prontidao de verdade fica nas etapas 4 e 6.
    """
    uptime = uptime_do_waha(base, chave)
    try:
        resp = requests.get('%s/health' % base.rstrip('/'),
                            headers=_cabecalho(chave), timeout=TIMEOUT_HTTP)
        dados = resp.json() if resp.status_code in (200, 503) else {}
    except Exception as exc:
        return _passo('saude', None,
                      'nao deu para perguntar a saude (%s)' % str(exc)[:80]), uptime
    situacao = (dados or {}).get('status')
    if situacao != 'ok':
        quebrado = ', '.join((dados or {}).get('error', {}).keys()) or 'sem detalhe'
        return _passo('saude', False, 'servidor com problema: %s' % quebrado), uptime
    if uptime is None:
        return _passo('saude', True, 'servidor saudavel (sem uptime)'), None
    return _passo('saude', True,
                  'servidor saudavel, de pe ha %s' % duracao(uptime)), uptime


# ==================== 4. A SESSAO ====================

def _etapa_sessao(base, chave, preferida, env):
    """(passo, nome, status). O STATUS VIAJA JUNTO (20/08/2026).

    Ate hoje so o nome saia daqui, e o status ficava preso dentro do texto do
    detalhe. Por causa disso `_consertar` decidia sem saber se a sessao estava
    em `FAILED` — e reiniciava, contra a regra 15-A, apagando a copia boa.
    """
    nome, status, estado, quantas = wahasessao.resolver(
        base=base, chave=chave, preferida=preferida, env=env)
    if quantas is None:
        return _passo('sessao', None, 'a instancia nao respondeu a lista'), None, None
    if not quantas:
        return _passo('sessao', False, 'instancia vazia — precisa parear'), None, None
    if not nome:
        return _passo('sessao', False, 'nenhuma sessao utilizavel'), None, status
    if not wahasessao.pronta(status, estado):
        return _passo('sessao', False, 'a sessao "%s" esta %s/%s (precisa de '
                      'WORKING + CONNECTED)' % (nome, status, estado or '-')), nome, status
    return _passo('sessao', True, 'sessao "%s" WORKING + CONNECTED' % nome), nome, status


# ==================== 5. A COPIA ====================

def _etapa_copia(sessao, env):
    if not sessao:
        return _passo('copia', None, 'sem sessao para conferir')
    arquivos, quando, erro = wahasessao.copia_da_sessao(sessao, env)
    if arquivos is None:
        return _passo('copia', None, 'nao deu para conferir a copia (%s)'
                      % (erro or 'sem motivo'))
    if not arquivos:
        # Conectado e gravado sao coisas diferentes: sem copia, esta sessao
        # morre no proximo reinicio e volta pedindo QR Code.
        return _passo('copia', False, 'a sessao "%s" NAO tem copia gravada — '
                      'ela nao sobrevive a um reinicio' % sessao)
    return _passo('copia', True, 'copia gravada%s'
                  % (' em %s' % quando if quando else ''))


# ==================== 6. LER O GRUPO ====================

def _etapa_grupo(base, chave, sessao, grupo):
    """A prova de que LER funciona — nao so publicar."""
    if not grupo:
        return _passo('grupo', None, 'sem grupo configurado para conferir')
    if not sessao:
        return _passo('grupo', None, 'sem sessao para conferir')
    try:
        resp = requests.get(
            '%s/api/%s/chats/%s/messages' % (base.rstrip('/'), sessao, grupo),
            params={'limit': 1, 'downloadMedia': 'false'},
            headers=_cabecalho(chave), timeout=30)
    except Exception as exc:
        return _passo('grupo', False, 'nao deu para ler o grupo (%s)' % str(exc)[:80])
    if resp.status_code != 200:
        return _passo('grupo', False,
                      'ler o grupo devolveu HTTP %s' % resp.status_code)
    return _passo('grupo', True, 'o grupo respondeu — da para ler')


def _sessao_que_le(base, chave, falhou, grupo):
    """A primeira OUTRA sessao pronta da instancia que consegue ler o grupo.

    Pergunta so as sessoes WORKING + CONNECTED (as demais nem teriam como ler)
    e so faz leitura (`limit=1`) — nunca reinicia nem apaga nada.
    """
    sessoes = wahasessao.listar(base, chave) or []
    for item in sessoes:
        nome = item.get('name')
        if not nome or wahasessao.mesmo_nome(nome, falhou):
            continue
        status, estado = wahasessao.estado_de(item)
        if status == 'WORKING' and not estado:
            completo = wahasessao.detalhar(base, chave, nome)
            if completo:
                _, estado = wahasessao.estado_de(completo)
        if not wahasessao.pronta(status, estado):
            continue
        if _etapa_grupo(base, chave, nome, grupo)['ok']:
            return nome
    return None


# ==================== O RELATORIO ====================

def checar(base, chave, preferida=None, grupo=None, container=None, env=None):
    """Passa por todas as etapas e devolve o relatorio.

    Nao para na primeira falha de proposito: quem le o log quer ver o quadro
    inteiro, nao a primeira reclamacao.
    """
    env = env if env is not None else wahasessao.carregar_env()
    passos = [_etapa_container(container)]

    passo_http = _etapa_http(base, chave)
    passos.append(passo_http)

    uptime = None
    sessao = None
    status_sessao = None
    if passo_http['ok']:
        passo_saude, uptime = _etapa_saude(base, chave)
        passos.append(passo_saude)
        passo_sessao, sessao, status_sessao = _etapa_sessao(base, chave, preferida, env)
        passos.append(passo_sessao)
        passos.append(_etapa_copia(sessao, env))
        if passo_sessao['ok']:
            passo_grupo = _etapa_grupo(base, chave, sessao, grupo)
            if passo_grupo['ok'] is False:
                # SO A LEITURA PROVA QUE ESTA CONECTADA (17/09/2026, regra do
                # dono). WORKING + CONNECTED e o estado do worker, nao da pagina:
                # em 17/09 a principal dizia "conectada" e devolvia HTTP 500 a
                # cada leitura desde 16:10. A outra sessao da mesma instancia lia
                # normalmente. Aqui se testa a leitura nas OUTRAS sessoes prontas;
                # a primeira que le vira a sessao da rodada — nos dois sentidos.
                outra = _sessao_que_le(base, chave, sessao, grupo)
                if outra:
                    passos[-1] = _etapa_copia(outra, env)
                    passo_grupo = _passo(
                        'grupo', True,
                        'a sessao "%s" nao leu o grupo (%s); a sessao "%s" leu — '
                        'a rodada segue por ela' % (sessao, passo_grupo['detalhe'], outra))
                    sessao = outra
            # RECEM-PAREADA NAO PUBLICA ANTES DA RECARGA (18/09/2026, E-C).
            # Ver `wahasessao.pagina_firmando`. Havendo outra sessao que le e ja
            # recarregou, a rodada segue por ela; nao havendo, espera a recarga
            # (ate 6 min, so neste caso) em vez de mandar e falhar no meio.
            if passo_grupo['ok'] and wahasessao.pagina_firmando(sessao, container, env):
                outra = _sessao_que_le(base, chave, sessao, grupo)
                if outra and not wahasessao.pagina_firmando(outra, container, env):
                    passos[-1] = _etapa_copia(outra, env)
                    passo_grupo = _passo(
                        'grupo', True,
                        'a sessao "%s" foi pareada agora e a pagina ainda vai '
                        'recarregar; a sessao "%s" le — a rodada segue por ela'
                        % (sessao, outra))
                    sessao = outra
                else:
                    fim = time.time() + 360
                    while time.time() < fim and wahasessao.pagina_firmando(sessao, container, env):
                        time.sleep(15)
                    if wahasessao.pagina_firmando(sessao, container, env):
                        passo_grupo = _passo(
                            'grupo', False,
                            'a sessao "%s" foi pareada agora e a pagina nao '
                            'recarregou em 6 min — enviar agora falharia' % sessao)
            passos.append(passo_grupo)
        else:
            # Sem sessao pronta, tentar ler o grupo so produz um 4xx confuso no
            # log e esconde a causa real, que a etapa anterior ja nomeou.
            passos.append(_passo('grupo', None, 'a sessao ainda nao esta pronta'))
    else:
        for etapa in ('saude', 'sessao', 'copia', 'grupo'):
            passos.append(_passo(etapa, None, 'a API nao respondeu'))

    impedem = [p for p in passos
               if p['ok'] is False and p['etapa'] not in AVISO_APENAS]
    return {'ok': not impedem,
            'passos': passos,
            'uptime_seg': uptime,
            'sessao': sessao,
            # `status` existe para `_consertar` poder recusar o restart em
            # FAILED (regra 15-A) — sem ele a decisao saia so da copia.
            'status': status_sessao,
            'impedem': [p['etapa'] for p in impedem]}


def _reiniciar_sessao(base, chave, sessao, timeout=30):
    """(ok, detalhe) — o unico conserto automatico que este modulo faz.

    REINICIAR NAO E APAGAR. A sessao volta do zip gravado no Postgres, sem QR
    Code e sem ninguem com o celular na mao. Apagar seria o contrario: trocaria
    "reinicia e volta" por "precisa de um humano" (regra 15-A).
    """
    if not sessao:
        return False, 'nao ha sessao para reiniciar'
    try:
        resp = requests.post('%s/api/sessions/%s/restart' % (base.rstrip('/'), sessao),
                             headers=_cabecalho(chave), timeout=timeout)
    except Exception as exc:
        return False, str(exc)[:100]
    if resp.status_code not in (200, 201, 204):
        return False, 'HTTP %s' % resp.status_code
    return True, 'reiniciada'


def _consertar(base, chave, rel, ja_tentados, log=None, env=None):
    """Tenta UMA correcao para o que esta impedindo. None quando nao ha o que fazer.

    AS REGRAS (a 4a esta junto do codigo, onde o gatilho e decidido).

    1. UM CONSERTO POR PROBLEMA, NUNCA EM LACO. Sem esse limite a espera vira
       eterna — tenta, falha, tenta de novo, e ninguem fica sabendo. Conserto
       que nao resolveu na primeira nao resolve na decima; dali em diante quem
       resolve e uma pessoa.

    2. QUEM DECIDE QUAL CONSERTO E A COPIA, NAO O ESTADO. Reiniciar so devolve
       a sessao se existir o RemoteAuth gravado no Postgres — e dele que ela
       volta, sem QR. Sem copia, reiniciar nao conserta nada: gasta o prazo,
       atrasa o alerta e termina no mesmo lugar, com a diferenca de que agora e
       tarde. Sem copia o unico caminho e parear, e parear exige gente com o
       celular na mao — entao o "conserto" certo e chamar essa pessoa.

    3. TER COPIA NAO GARANTE QUE REINICIAR RESOLVE — e reiniciar nao e de
       graca. Achado em 09/08, lendo o log do WAHA depois de uma noite em
       FAILED: quando o WhatsApp derruba o vinculo do aparelho, o restart faz
       o whatsapp-web.js chamar `logout()` -> `deleteRemoteSession()`, ou
       seja, **pede para APAGAR esta copia**. Naquela noite ela sobreviveu por
       sorte (a exclusao morreu num timeout do banco). O sintoma e
       reconhecivel: a sessao vai para STARTING, chega a CONNECTED por uns
       segundos e cai em FAILED. Voltou a cair assim depois de um restart? O
       conserto automatico acabou — dali em diante e parear, com gente.
       Por isso a pipeline guarda `restart_falhou_em` e nao repete o pedido a
       cada rodada: duas rodadas por dia pedindo para apagar a copia e apagar
       a copia de proposito, so devagar.
    """
    # 4. O GRUPO ILEGIVEL TAMBEM PEDE ESTE CONSERTO (19/08/2026).
    #
    #    Ate hoje a linha abaixo era `if 'sessao' not in rel['impedem']`, e por
    #    causa dela o unico conserto automatico que existe nunca era tentado no
    #    caso que a propria prontidao foi feita para pegar: o da regra 16-F, em
    #    que o status mente. Em 18/08 as 05:30 o relatorio era
    #
    #        [OK] sessao  "principal-pls" WORKING + CONNECTED
    #        [X ] grupo   ler o grupo devolveu HTTP 500
    #
    #    Quem impedia era `grupo`, entao `_consertar` devolvia None — vinte
    #    vezes seguidas, ate o prazo acabar, e a rodada abortou as 05:33. As
    #    05:41 o Guardiao subiu a stack por conta propria, o Chromium voltou, e
    #    as 06:45 o laudo ja dizia "[OK] grupo". O conserto certo existia e foi
    #    feito pelo componente errado, 8 minutos tarde demais para o ranking.
    #
    #    Detectar sem consertar nao serve: a etapa `grupo` le o grupo de verdade
    #    justamente porque nenhum campo de status pega o Chromium morto. Agora
    #    ela aciona a mesma acao (reiniciar a sessao), com as mesmas travas — so
    #    com copia gravada (16-E-bis) e uma vez por rodada (16-E).
    impedem = set(rel.get('impedem') or ())
    if not (impedem & {'sessao', 'grupo'}) or 'sessao' in ja_tentados:
        return None
    # A chave e a ACAO ("reiniciar a sessao"), nao a etapa: `sessao` e `grupo`
    # levam ao mesmo conserto, e ele nao pode ser tentado duas vezes por rodada
    # so porque duas etapas reclamaram.
    ja_tentados.add('sessao')
    sessao = rel.get('sessao')
    if not sessao:
        if log:
            log('   Nao ha conserto automatico: a instancia esta sem sessao. '
                'Parear exige alguem com o celular.')
        return {'etapa': 'sessao', 'acao': 'nenhum conserto automatico',
                'ok': False, 'detalhe': 'instancia sem sessao — precisa parear'}

    # 5. EM `FAILED` NAO SE REINICIA — NEM COM COPIA (20/08/2026).
    #
    #    A regra 15-A do CLAUDE.md ja dizia, e o `executar.sh` ja obedecia
    #    ("NAO RELIGAR AQUI"). Aqui nao obedecia: a decisao saia so da COPIA
    #    (regra 16-E-bis) e o status nem era olhado. O preco, medido em
    #    19/08/2026:
    #
    #        19:37:07  o Dormir gravou a copia e fez a parada limpa
    #        19:38:03  a maquina reiniciou
    #        19:45:29  o WhatsApp derrubou o aparelho sozinho (LOGOUT sem reqId)
    #        19:50:18  aqui: "sessao FAILED | copia gravada OK"
    #        19:50:19  "Tentando consertar: reiniciando a sessao"
    #        19:50:22  POST /api/sessions/principal-pls/restart
    #        19:50:41  logged out -> RemoteAuth.deleteRemoteSession
    #
    #    O restart nao trouxe a sessao de volta (nunca traz, em FAILED) e ainda
    #    pediu para APAGAR a copia boa que o Dormir tinha acabado de gravar —
    #    justamente a que permitiria repor sem o celular. Duas rodadas por dia
    #    fazendo isso e apagar a copia de proposito, so devagar.
    #
    #    Em FAILED o caminho e repor a copia guardada em arquivo (regra 17) e,
    #    se nao houver, chamar gente. O alerta diz isso.
    status = (rel.get('status') or '').upper()
    if status == 'FAILED':
        if log:
            log('   A sessao "%s" esta em FAILED: o WhatsApp desvinculou o '
                'aparelho. Reiniciar nao traz de volta e ainda pede para apagar '
                'a copia — NAO vou reiniciar.' % sessao)
        return {'etapa': 'sessao', 'acao': 'nenhum conserto automatico',
                'ok': False,
                'detalhe': 'a sessao "%s" esta em FAILED — reiniciar nao resolve '
                           'e apagaria a copia; e preciso repor a copia guardada '
                           'ou parear de novo' % sessao}

    # 6. PAREADA HA MENOS DE 10 MIN NAO SE REINICIA (18/09/2026, plano A7).
    #    Em 12/09 um reinicio logo depois do pareamento desfez o pareamento.
    #    Sessao recem-pareada ainda esta sincronizando: espera, nao conserta.
    _ha = wahasessao.pareada_ha(sessao)
    # E-D (18/09/2026): o carimbo so existe quando o `provisionar` pareou;
    # quando o SITE atende o Conectar (pedidos 60 e 61), nao ha carimbo. O
    # sinal do proprio WAHA (`pagina_firmando`) vale para os dois caminhos.
    if wahasessao.pagina_firmando(sessao, env=env) is True:
        _ha = 0
    if _ha is not None and 0 <= _ha < 600:
        if log:
            log('   A sessao "%s" foi pareada ha %d min: NAO vou reiniciar '
                '(ela ainda esta firmando).' % (sessao, _ha // 60))
        return {'etapa': 'sessao', 'acao': 'nenhum conserto automatico',
                'ok': False,
                'detalhe': 'a sessao "%s" foi pareada ha menos de 10 min — '
                           'reiniciar agora desfaria o pareamento' % sessao}

    arquivos, quando, erro = wahasessao.copia_da_sessao(sessao, env)
    if arquivos is None:
        # NAO SEI nao e NAO TEM. Na duvida, reiniciar e o movimento barato e
        # reversivel; e apagar continua fora de questao (regra 15-A).
        if log:
            log('   Nao consegui conferir a copia (%s) — vou reiniciar assim '
                'mesmo, que e barato e nao apaga nada.' % (erro or 'sem motivo'))
    elif not arquivos:
        if log:
            log('   A sessao "%s" NAO tem copia gravada: reiniciar nao traz ela '
                'de volta. Isto so se resolve pareando de novo.' % sessao)
        return {'etapa': 'sessao', 'acao': 'nenhum conserto automatico',
                'ok': False,
                'detalhe': 'a sessao "%s" nao tem copia gravada — reiniciar nao '
                           'resolve, precisa parear' % sessao}

    if log:
        log('   Tentando consertar: reiniciando a sessao "%s"%s...'
            % (sessao, ' (copia de %s)' % quando if quando else ''))
    ok, detalhe = _reiniciar_sessao(base, chave, sessao)
    if log:
        log('   %s' % ('Reinicio pedido — vendo se ela volta.' if ok
                       else 'O reinicio falhou (%s).' % detalhe))
    return {'etapa': 'sessao', 'acao': 'reiniciar a sessao', 'ok': ok,
            'detalhe': detalhe}


def esperar(base, chave, preferida=None, grupo=None, container=None, env=None,
            limite_seg=300, intervalo_seg=10, log=None, consertar=True):
    """Repete `checar` ate tudo passar ou o prazo acabar, tentando consertar.

    TRES DESFECHOS, E O PRAZO E SAGRADO
      1. ficou pronto        -> segue a rodada
      2. estava quebrado e o conserto resolveu -> segue a rodada
      3. acabou o prazo      -> PARA e avisa, dizendo o que travou e o que
                                ja foi tentado

    O prazo existe para o desfecho 3 acontecer. Esperar tambem faz parte do
    processo — sincronizacao e reconexao levam tempo de verdade — mas espera
    sem prazo nao e paciencia, e um sistema mudo: ninguem publica, ninguem
    reclama, e o mes passa. Melhor um alerta cedo demais que silencio.
    """
    fim = time.time() + max(limite_seg, 0)
    volta = 0
    ja_tentados = set()
    consertos = []
    while True:
        volta += 1
        rel = checar(base, chave, preferida, grupo, container, env)
        if log:
            for p in rel['passos']:
                log('   [%s] %-9s %s' % (_marca(p['ok']), p['etapa'], p['detalhe']))
        rel['voltas'] = volta
        rel['consertos'] = consertos
        if rel['ok'] or time.time() >= fim:
            return rel

        if consertar:
            feito = _consertar(base, chave, rel, ja_tentados, log, env)
            if feito:
                consertos.append(feito)

        if log:
            log('   Ainda falta: %s. Nova conferencia em %s...'
                % (', '.join(rel['impedem']), duracao(intervalo_seg)))
        time.sleep(min(intervalo_seg, max(fim - time.time(), 0)))


def _marca(ok):
    return 'OK ' if ok is True else ('X  ' if ok is False else '?  ')


def resumo(rel):
    """Uma linha por etapa, para log e para mensagem de alerta."""
    return '\n'.join('[%s] %-9s %s' % (_marca(p['ok']), p['etapa'], p['detalhe'])
                     for p in rel['passos'])


# ==================== CLI ====================

def main():
    import argparse
    p = argparse.ArgumentParser(description='Prontidao das instancias WAHA.')
    p.add_argument('--esperar', type=int, metavar='SEG',
                   help='esperar ate SEG segundos por tudo pronto')
    p.add_argument('--json', action='store_true')
    args = p.parse_args()

    env = wahasessao.carregar_env()
    grupo = env.get('GROUP_ID') or env.get('WHATSAPP_GROUP_ID')
    containers = {'principal': env.get('WAHA_CONTAINER',
                                       'auto-waha-principal-pls'),
                  'suplente': env.get('WAHA_CONTAINER_SUPLENTE',
                                      'auto-waha-suplente-pls')}
    saida, tudo_ok = [], True
    for inst in wahasessao.instancias(env):
        alvo = dict(inst)
        args_comuns = dict(preferida=env.get('WAHA_SESSION'), grupo=grupo,
                           container=containers.get(inst['papel']), env=env)
        if args.esperar:
            rel = esperar(inst['base'], inst['chave'], limite_seg=args.esperar,
                          log=(None if args.json else print), **args_comuns)
        else:
            rel = checar(inst['base'], inst['chave'], **args_comuns)
        alvo['relatorio'] = rel
        saida.append(alvo)
        tudo_ok = tudo_ok and rel['ok']
        if not args.json:
            print('===== %s (%s) =====' % (inst['papel'], inst['base']))
            print(resumo(rel))
            print('-> %s\n' % ('PRONTA' if rel['ok']
                               else 'NAO PRONTA: ' + ', '.join(rel['impedem'])))
    if args.json:
        print(json.dumps(saida, ensure_ascii=False, default=str))
    return 0 if tudo_ok else 1


if __name__ == '__main__':
    sys.exit(main())
