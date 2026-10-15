# -*- coding: utf-8 -*-
"""A maquina da automacao esta ligada? E, se esta, esta prestando?

TRES ESTADOS, NAO DOIS — e a diferenca entre eles e tudo.

  desligada  -> o NORMAL. A maquina da automacao nao fica ligada de proposito:
                energia custa, e o projeto e filantropico. A BIOS liga sozinha
                as 21:45 e o `Dormir.ps1` desliga no fim. Tratar "desligada"
                como falha encheria o admin de alarme falso todo dia, e alarme
                falso ensina a ignorar alarme.
  ligada     -> de pe e respondendo. Da para publicar.
  quebrada   -> LIGADA e sem prestar. Este e o unico que merece alerta, e e
                justamente o que ninguem enxergava: em 06/08 a sessao caiu as
                11h10 com a maquina ligada e so a rodada da noite descobriria.

POR QUE ISTO MORA NO SITE E NAO NA MAQUINA
Porque uma sentinela rodando de N em N minutos DENTRO do Dell nao resolve o
caso em que o Dell nao esta de pe — e ainda cobra memoria de uma maquina que
tem 8 GB para segurar Chromium (400 a 700 MB por sessao WEBJS). O site ja tem
um vigia rodando de 15 em 15 min no DS215j, que e sempre ligado. Ele so
precisava de um endereco para onde olhar.

O QUE TORNOU ISTO POSSIVEL
O WAHA ja esta exposto na rede local (portproxy 0.0.0.0:3000 no Windows), e o
site mora na mesma rede. Nao precisa de tunel — tunel serve para entrar pela
internet, e aqui os dois estao do mesmo lado da porta.
"""
import logging
import socket
from urllib.parse import urlparse

from app import secretstore

logger = logging.getLogger(__name__)

# Curto de proposito: a pergunta e "esta ligada?", e maquina desligada nao
# responde nunca. Esperar 10s por um "nao" trava a tela do admin a toa.
TIMEOUT_PORTA = 2.5
TIMEOUT_HTTP = 6

LIGADA = 'ligada'
DESLIGADA = 'desligada'
QUEBRADA = 'quebrada'
SEM_ENDERECO = 'sem_endereco'

_ROTULO = {
    LIGADA: 'Ligada e respondendo',
    DESLIGADA: 'Desligada',
    QUEBRADA: 'Ligada, mas com problema',
    SEM_ENDERECO: 'Endereço não cadastrado',
}


def endereco(cidade=None):
    """(host, porta) da maquina da automacao, ou (None, None).

    Sai do endereco do WAHA que a cidade ja tem cadastrado: se o site fala com
    aquele endereco para publicar, e o mesmo endereco que responde se a maquina
    esta de pe. Cadastrar um segundo campo so para isto seria mais uma coisa
    para o admin manter em dia — e desencontrada, mais uma para dar errado.
    """
    url = secretstore.get(secretstore.WAHA_URL, scope=cidade)
    if not url:
        return None, None
    try:
        partes = urlparse(url if '://' in url else 'http://' + url)
    except Exception:
        return None, None
    host = partes.hostname
    if not host or host in ('localhost', '127.0.0.1', '::1'):
        # Endereco que so existe sentado na propria maquina nao serve para o
        # site perguntar nada. Melhor dizer "nao sei" que responder errado.
        return None, None
    return host, (partes.port or (443 if partes.scheme == 'https' else 80))


def porta_responde(host, porta, timeout=TIMEOUT_PORTA):
    """A porta aceita conexao? E o teste mais barato de 'esta ligada'."""
    try:
        with socket.create_connection((host, porta), timeout=timeout):
            return True
    except Exception:
        return False


def porta_tem_waha(host, porta, timeout=TIMEOUT_PORTA):
    """E o WAHA que atende nesta porta? (nao so 'alguem aceita conexao')

    ACEITAR CONEXAO NAO E ESTAR OCUPADA (09/09/2026 -- o caso da 3002).
    O `expor_waha_na_rede.ps1` cria um atalho de rede (portproxy) para as
    portas 3000, 3001 e 4533 SEMPRE, mesmo quando nao existe container atras
    delas. Esse atalho completa o aperto de mao TCP e so' falha depois, ao
    tentar repassar -- entao `porta_responde()` dizia "ocupada" para uma porta
    vazia. Foi assim que o pedido do suplente pulou a 3001, que estava LIVRE,
    e nasceu um segundo WAHA da mesma cidade na 3002, contra a regra de um
    container por cidade.

    Aqui a pergunta e' outra: quem responde fala a lingua do WAHA? Um 200 e'
    o WAHA aberto; um 401/403 e' o WAHA pedindo a chave -- os dois provam que
    ele esta la. Atalho vazio devolve conexao cortada ou tempo esgotado, e
    isso conta como LIVRE.

    Na duvida devolve None (nao sei), e quem chama trata "nao sei" como
    ocupada -- nunca se inventa porta (regra que nasceu do zumbi da 3001).
    """
    import requests
    url = 'http://%s:%s/api/sessions' % (host, porta)
    try:
        resp = requests.get(url, timeout=timeout)
    except requests.exceptions.RequestException:
        # Conexao cortada/recusada/estourou o prazo: ninguem do WAHA ali.
        return False
    except Exception as exc:
        logger.debug('porta_tem_waha: %s falhou de forma inesperada (%s)',
                     url, type(exc).__name__)
        return None
    if resp.status_code in (200, 401, 403):
        return True
    return False


def camadas(cidade=None):
    """O topo da tela Sessão WhatsApp, por camada, perguntado AO VIVO.

    18/09/2026 (plano A3, Pendente 2). Nada daqui vem do Guardião nem da Fila:
      * Windows — a porta 135 é do próprio Windows (medido 18/09: abre do NAS
        em 55 ms e não depende do WSL);
      * WSL — alguém responde HTTP na 4533 (outro servico) ou na porta do WAHA. O
        atalho do Windows (portproxy) aceita a conexão mesmo com o WSL caído,
        mas não devolve resposta nenhuma — por isso a pergunta é HTTP, não TCP;
      * containers — o WAHA de CADA cidade que tem container registrado.
    Windows desligado: não pergunta mais nada (resposta em ~1,5 s, não 10 s).
    """
    import requests
    from sqlalchemy import text as sa_text
    from app import db
    host, porta = endereco(cidade)
    res = {'windows': {'ligado': False, 'ip': host},
           'wsl': {'respondendo': False,
                   'ip': secretstore.get(WSL_IP, scope=cidade)},
           'containers': []}
    try:
        linhas = db.session.execute(sa_text(
            "SELECT cidade, container, host, porta FROM instancias_waha "
            "WHERE container IS NOT NULL AND TRIM(container) <> '' "
            "GROUP BY cidade, container ORDER BY cidade")).fetchall()
    except Exception as exc:
        logger.warning('camadas: nao li os containers registrados: %s', exc)
        linhas = []
    if not host or _parada_combinada(cidade):
        # Avisou que ia parar: perguntar a porta acorda o Dell pelo Wi-Fi
        # (item 22, confirmado 18/09). Tudo fica como parado.
        res['containers'] = [{'cidade': cid, 'nome': cont, 'porta': p,
                              'operacional': False}
                             for cid, cont, _h, p in linhas]
        return res
    res['windows']['ligado'] = porta_responde(host, 135, timeout=1.5)
    if res['windows']['ligado']:
        for p in (4533, porta):
            try:
                requests.get('http://%s:%s/' % (host, p), timeout=2)
                res['wsl']['respondendo'] = True
                break
            except Exception:
                continue
    for cid, cont, h, p in linhas:
        ok = False
        if res['wsl']['respondendo'] and h and p:
            ok = bool(porta_tem_waha(h, int(p), timeout=2))
        res['containers'].append({'cidade': cid, 'nome': cont,
                                  'porta': p, 'operacional': ok})
    return res


# IP do WSL, contado pela máquina em `/api/instancias` (18/09/2026, A3).
WSL_IP = 'wsl_ip'


# Quanto tempo o carimbo do Windows vale. A tarefa dele passa de 15 em 15 min;
# 25 minutos cobrem uma passada perdida sem transformar maquina realmente
# desligada em "quebrada" — que seria alarme falso, e alarme falso ensina a
# ignorar alarme.
JANELA_SINAL_WINDOWS_MIN = 25


# Quanto tempo o aviso de "vou parar agora" continua valendo. Cobre a parada
# limpa inteira (containers + WSL + desligar) com folga larga.
JANELA_PARADA_COMBINADA_MIN = 40


def _parada_combinada(cidade):
    """A máquina avisou que ia parar, e foi há pouco?

    17/08/2026 (plano attach-tidy-globe, C4) — "AVISEI QUE IA PARAR" NAO E O
    MESMO QUE "PAROU". O `PararLimpo.ps1`/`Dormir.ps1` avisam o site ANTES de
    tentar (regra do proprio script), mas os dois podem abortar depois disso
    — por exemplo, sem cópia fresca da sessão (regra 17). Sem este
    autocancelamento, a porta muda de responder, o site conclui "parada
    combinada, nada a fazer" e um problema real (máquina ligada, sem
    prestar) ficava escondido pelos 40 minutos inteiros da janela.
    ➜ O desempate é o mesmo carimbo do Windows que ja existe
    (`WINDOWS_VISTO_EM`, regra `_windows_de_pe`): se ele foi atualizado
    DEPOIS do aviso de parada, a maquina continuou dando sinal de vida — ou
    seja, avisou que ia parar e nao parou. Cancela o grace period e deixa a
    logica normal (QUEBRADA/DESLIGADA) decidir.
    """
    from datetime import datetime, timedelta
    carimbo = secretstore.get(secretstore.MAQUINA_PARANDO_EM, scope=cidade)
    if not carimbo:
        return False
    try:
        quando = datetime.fromisoformat(str(carimbo))
    except ValueError:
        return False
    if (datetime.now() - quando) > timedelta(minutes=JANELA_PARADA_COMBINADA_MIN):
        return False
    visto = secretstore.get(secretstore.WINDOWS_VISTO_EM, scope=cidade)
    if visto:
        try:
            if datetime.fromisoformat(str(visto)) > quando:
                # Windows deu sinal DEPOIS do aviso de parada — nao parou.
                return False
        except ValueError:
            pass
    return True


def _windows_de_pe(cidade):
    """O computador da automação deu sinal de vida há pouco?

    O sinal vem do proprio Windows (a tarefa da fila chamando o site), nao do
    WSL — e essa e a graca: ele continua respondendo quando o WSL cai.
    """
    from datetime import datetime, timedelta
    carimbo = secretstore.get(secretstore.WINDOWS_VISTO_EM, scope=cidade)
    if not carimbo:
        return False
    try:
        visto = datetime.fromisoformat(str(carimbo))
    except ValueError:
        return False
    return (datetime.now() - visto) <= timedelta(minutes=JANELA_SINAL_WINDOWS_MIN)


def estado(cidade=None):
    """{situacao, rotulo, host, porta, detalhe} — sem levantar excecao.

    Nao alerta ninguem: so responde. Quem decide alertar e o vigia (que so
    reclama de QUEBRADA) e a tela do painel (que mostra os tres).
    """
    host, porta = endereco(cidade)
    if not host:
        return {'situacao': SEM_ENDERECO, 'rotulo': _ROTULO[SEM_ENDERECO],
                'host': None, 'porta': None,
                'detalhe': 'Cadastre o endereço do WhatsApp da cidade para o '
                           'site saber onde olhar.'}

    # QUEM ACORDAVA O DELL ERA ESTA PERGUNTA (18/09/2026, item 22 — CONFIRMADO).
    # A testemunha abaixo casou ao segundo com o evento 1 do Windows (placa
    # Wi-Fi 7260): 00:22:30, 00:37:40 e 06:10:59 de 18/09 — 3 de 3 perguntas sem
    # resposta acordaram a máquina, 7 a 11 min depois de ela dormir. A placa
    # fica armada (regra do dono); quem para de bater na porta é o site:
    # enquanto vale o aviso "vou parar", não se pergunta nada.
    if _parada_combinada(cidade):
        return {'situacao': DESLIGADA, 'rotulo': _ROTULO[DESLIGADA],
                'host': host, 'porta': porta,
                'detalhe': 'A máquina avisou que ia parar — desligamento '
                           'combinado, nada a fazer (não pergunto para não '
                           'acordá-la).'}

    if not porta_responde(host, porta):
        # TESTEMUNHA DO ACORDAR PELO WI-FI (17/09/2026). Em 17/09 o Dell
        # suspenso acordou as 16:09:14 pela placa Wi-Fi sem Wake-on-LAN do site,
        # e este relogio (vigia, de 15 em 15 min) pergunta a porta do Dell antes
        # de olhar se ele avisou que ia dormir. Hipotese, nao causa: esta linha
        # guarda a hora de cada pergunta que ficou sem resposta, para comparar
        # com a hora em que o Windows acordou (evento 1, Power-Troubleshooter).
        logger.info('maquina: perguntei a porta %s:%s e ninguem respondeu', host, porta)
        # DESLIGADA NAO E A UNICA EXPLICACAO PARA A PORTA MUDA (14/08/2026).
        #
        # A porta 3000 e do WAHA, que mora dentro do WSL. Se o WSL cai — e ele
        # cai — o computador continua LIGADO e a porta para de responder do
        # mesmo jeito. Ate hoje o site concluia "esta dormindo", e dormindo nao
        # gera alerta (de proposito, regra 21-G): o resultado foi o log dizendo
        # "3 mensagem(ns) esperando ha 56 min, mas a maquina esta dormindo —
        # normal" com o notebook ligado na frente do dono.
        #
        # O desempate e o carimbo que o proprio WINDOWS deixa de 15 em 15 min
        # ao buscar a fila (`WINDOWS_VISTO_EM`). Ele nao depende do WSL.
        # PARADA COMBINADA NAO E FALHA (15/08/2026).
        #
        # O carimbo do Windows vale 25 minutos. Quem desliga a maquina as 22:55
        # deixa esse carimbo fresco: a porta para de responder e o site conclui
        # "ligada e sem prestar" — foi o e-mail das 23:01 de 14/08, sobre uma
        # maquina que o dono tinha acabado de desligar de proposito.
        #
        # Agora o proprio Dell avisa antes de parar (`POST /api/maquina/parando`,
        # chamado pelo Dormir.ps1). Enquanto esse aviso estiver quente, porta
        # muda e' DESLIGADA, que e o normal e nao alerta ninguem.
        if _parada_combinada(cidade):
            return {'situacao': DESLIGADA, 'rotulo': _ROTULO[DESLIGADA],
                    'host': host, 'porta': porta,
                    'detalhe': 'A máquina avisou que ia parar e parou — '
                               'desligamento combinado, nada a fazer.'}
        if _windows_de_pe(cidade):
            return {'situacao': QUEBRADA, 'rotulo': _ROTULO[QUEBRADA],
                    'host': host, 'porta': porta,
                    'detalhe': 'O computador da automação está LIGADO (deu '
                               'sinal há poucos minutos), mas o WhatsApp dele '
                               'não responde. Costuma ser o WSL que caiu e '
                               'derrubou o WhatsApp junto.'}
        return {'situacao': DESLIGADA, 'rotulo': _ROTULO[DESLIGADA],
                'host': host, 'porta': porta,
                'detalhe': 'A máquina da automação está desligada — que é o '
                           'normal fora do horário de publicar.'}

    # Ligada. Agora a pergunta que interessa: ela esta PRESTANDO?
    ok, detalhe = _waha_saudavel(cidade, host, porta)
    if ok:
        return {'situacao': LIGADA, 'rotulo': _ROTULO[LIGADA],
                'host': host, 'porta': porta, 'detalhe': detalhe}
    return {'situacao': QUEBRADA, 'rotulo': _ROTULO[QUEBRADA],
            'host': host, 'porta': porta, 'detalhe': detalhe}


def _motor_da_sessao(base, cabecalho, nome):
    """'CONNECTED', outro estado, ou None quando nao deu para perguntar.

    Motor ausente na resposta e o sintoma de `WORKING` com o Chromium morto
    (06/08): o WAHA devolve `engine: {"engine":"WEBJS"}` **sem** `state`.
    """
    import requests
    if not nome:
        return None
    try:
        resp = requests.get('%s/api/sessions/%s' % (base, nome),
                            headers=cabecalho, timeout=TIMEOUT_HTTP)
        if resp.status_code != 200:
            return None
        motor = (resp.json() or {}).get('engine') or {}
    except Exception:
        return None
    if not isinstance(motor, dict):
        return None
    # '' (e nao None) quando o WAHA respondeu e o estado simplesmente nao veio:
    # ai sabemos que perguntamos e que ele nao esta conectado.
    return motor.get('state') or ''


def _waha_saudavel(cidade, host, porta):
    """(ok, detalhe). Pergunta ao WAHA se ha sessao WORKING + CONNECTED.

    `WORKING` sozinho nao basta e nunca bastou: em 06/08 uma sessao ficou
    `WORKING` com o Chromium morto e devolvia HTTP 500 ao ler o grupo.
    """
    import requests
    base = 'http://%s:%s' % (host, porta)
    chave = secretstore.get(secretstore.WAHA_API_KEY, scope=cidade) or ''
    cabecalho = {'X-Api-Key': chave} if chave else {}
    try:
        resp = requests.get('%s/api/sessions' % base, params={'all': 'true'},
                            headers=cabecalho, timeout=TIMEOUT_HTTP)
    except Exception as exc:
        return False, 'A máquina está ligada, mas o WhatsApp não respondeu (%s).' \
                      % str(exc)[:80]
    if resp.status_code == 401:
        return False, 'A máquina está ligada, mas a chave de acesso ao WhatsApp ' \
                      'não confere.'
    if resp.status_code != 200:
        return False, 'A máquina está ligada, mas o WhatsApp respondeu HTTP %s.' \
                      % resp.status_code
    try:
        sessoes = resp.json() or []
    except ValueError:
        return False, 'A máquina está ligada, mas o WhatsApp respondeu algo ' \
                      'que não dá para entender.'
    if not sessoes:
        return False, 'A máquina está ligada, mas não há sessão do WhatsApp — ' \
                      'precisa parear.'

    for item in sessoes:
        if not isinstance(item, dict):
            continue
        if item.get('status') != 'WORKING':
            continue
        # A LISTA NAO TRAZ O MOTOR. Medido em 06/08 neste WAHA (2026.7.1): o
        # /api/sessions devolve status, `me` e `assignedWorker`, e nenhum
        # `engine`. Concluir "quebrada" daqui acusaria em falso toda sessao
        # saudavel — e alarme falso ensina a ignorar alarme. Entao pergunta.
        motor = _motor_da_sessao(base, cabecalho, item.get('name'))
        if motor == 'CONNECTED':
            return True, 'Sessão "%s" conectada.' % item.get('name')
        if motor is None:
            # Nao deu para perguntar. NAO SEI nao e ESTA QUEBRADA: melhor calar
            # do que acusar sem saber.
            return True, ('Sessão "%s" está WORKING; não deu para confirmar o '
                          'motor.' % item.get('name'))

    nomes = ', '.join('%s (%s)' % (s.get('name'), s.get('status'))
                      for s in sessoes if isinstance(s, dict))
    return False, ('A máquina está ligada, mas nenhuma sessão está pronta: %s. '
                   'Do jeito que está, o ranking não sai.' % nomes)
