# -*- coding: utf-8 -*-
"""PUBLICAR O RANKING AGORA, com um clique no painel.

POR QUE ISTO EXISTE (15/08/2026, pedido do dono)

A publicacao acontece duas vezes por dia, em horario fixo, e a maquina da
automacao passa o resto do tempo desligada. Quando o dono quer publicar fora
de hora, o caminho era: ligar o notebook na mao, esperar, abrir o
`Movimente-se_Automacao.bat` e olhar uma janela preta. Quem nao esta em casa
nao publica.

Aqui o pedido fica GUARDADO no site (que nunca desliga) e a maquina o atende na
primeira vez que perguntar — e ela pergunta de 15 em 15 minutos, e tambem no
instante em que liga. Junto com o pedido sai o Wake-on-LAN: com a maquina
dormindo, o caminho inteiro leva de 1 a 3 minutos.

O QUE ELE NAO FAZ, DE PROPOSITO

Ele nao publica nada por conta propria. Quem publica e a pipeline, a MESMA de
sempre — com as mesmas travas (regra 6: sem km novos nao republica; regra 7: 2h
entre publicacoes; regra 8: leitura incompleta nao publica) e os mesmos avisos.
Um segundo caminho de publicacao seria um segundo conjunto de regras para
divergir do primeiro, e foi disso que nasceu metade dos incidentes deste
projeto.
"""
import json
import logging
from datetime import datetime, timedelta

from app import secretstore

logger = logging.getLogger(__name__)

# Quanto tempo um pedido continua valendo. Depois disso ele caduca: publicar
# um ranking que alguem pediu ontem, sem que a pessoa esteja esperando, e o
# tipo de surpresa que o grupo de 80 pessoas nao merece.
VALIDADE_MIN = 180


def pedir(cidade, por=None, forcar=False):
    """Registra o pedido e tenta acordar a maquina. Devolve (ok, mensagem)."""
    aberto = pendente(cidade)
    if aberto and not forcar:
        return False, ('Já existe um pedido em andamento, feito há %s. '
                       'Espere ele terminar.' % _humano(aberto['idade_seg']))

    dados = {
        'pedido_em': datetime.now().isoformat(timespec='seconds'),
        'por': (por or 'admin')[:120],
        'atendido_em': None,
        'forcar': bool(forcar),
    }
    secretstore.set_value(secretstore.RANKING_PEDIDO, json.dumps(dados),
                          scope=cidade, updated_by=por)
    logger.warning('RANKING PEDIDO AGORA em %s por %s (forcar=%s)',
                   cidade, por, forcar)

    # O PEDIDO PROTEGE A PROPRIA RODADA (17/09/2026). Medido: pedido as
    # 10:45:40, atendido as 10:45:58, e as 10:48:06 o Guardiao concluiu "fora
    # do horario e sem pedido em aberto" e derrubou os containers as 10:49:44 —
    # no meio da coleta (WAHA recusando conexao a partir de 10:51:09). A
    # protecao de 30 min existia, mas so na ROTA do botao: pedido que entrasse
    # por outro caminho ficava exposto. Agora ela vive aqui, onde todo pedido
    # passa. Chamar de novo na rota nao faz mal: a funcao nunca reduz um prazo.
    try:
        from app.blueprints.admin import _garantir_containers_de_pe
        _garantir_containers_de_pe(cidade, minutos_minimo=30, por=por or 'admin')
    except Exception as exc:
        logger.warning('Pedido de rodada: nao consegui proteger os containers '
                       'por 30 min (%s) — o Guardiao pode derrubar a rodada.', exc)

    # O Wake-on-LAN e best-effort: falhar aqui nao invalida o pedido, que fica
    # guardado e e atendido na proxima vez que a maquina falar com o site.
    aviso = ''
    try:
        from app import ligarmaquina, maquina
        situacao = (maquina.estado(cidade) or {}).get('situacao')
        if situacao == maquina.LIGADA:
            aviso = (' O computador já está de pé: a publicação começa na '
                     'próxima consulta dele, em até 15 minutos.')
        elif ligarmaquina.configurado(cidade):
            ligarmaquina.ligar(cidade)
            aviso = (' Mandei o sinal que liga o computador — ele costuma levar '
                     'de 1 a 3 minutos para ficar pronto, e aí publica.')
        else:
            aviso = (' Não há Wake-on-LAN cadastrado nesta cidade: se a máquina '
                     'estiver desligada, ligue-a na mão.')
    except Exception as exc:
        logger.warning('Pedido de rodada: nao consegui acordar a maquina: %s', exc)

    return True, ('Pedido registrado.%s Acompanhe aqui no painel — a publicação '
                  'segue as mesmas regras da automática: se os km não mudaram '
                  'ou a leitura vier incompleta, o ranking não sai (e você fica '
                  'sabendo por quê).' % aviso)


def pendente(cidade):
    """O pedido em aberto desta cidade, ou None. Nunca levanta excecao."""
    dados = _ler(cidade)
    if not dados or dados.get('atendido_em'):
        return None
    idade = _idade(dados.get('pedido_em'))
    if idade is None:
        return None
    if idade > VALIDADE_MIN * 60:
        # CADUCOU. Nao publica: quem pediu ha tres horas nao esta mais olhando.
        logger.info('Pedido de rodada de %s caducou (%s) — descartado.',
                    cidade, _humano(idade))
        marcar_atendido(cidade, por='caducou')
        return None
    dados['idade_seg'] = idade
    return dados


def marcar_atendido(cidade, por=None):
    """A maquina pegou o pedido — nao pode ser atendido duas vezes."""
    dados = _ler(cidade) or {}
    if not dados:
        return False
    dados['atendido_em'] = datetime.now().isoformat(timespec='seconds')
    dados['atendido_por'] = (por or 'automacao')[:80]
    secretstore.set_value(secretstore.RANKING_PEDIDO, json.dumps(dados),
                          scope=cidade, updated_by=por or 'automacao')
    logger.info('Pedido de rodada de %s atendido por %s', cidade, por)
    return True


def cancelar(cidade, por=None):
    secretstore.delete(secretstore.RANKING_PEDIDO, scope=cidade)
    logger.info('Pedido de rodada de %s cancelado por %s', cidade, por)


def estado(cidade):
    """Para a tela: o que dizer ao dono sobre o pedido dele, em português."""
    dados = _ler(cidade)
    if not dados:
        return {'tem': False}
    idade = _idade(dados.get('pedido_em'))
    atendido = dados.get('atendido_em')
    saida = {
        'tem': True,
        'pedido_em': dados.get('pedido_em'),
        'por': dados.get('por'),
        'ha_quanto_tempo': _humano(idade),
        'atendido': bool(atendido),
        'atendido_em': atendido,
        'em_aberto': bool(not atendido and idade is not None
                          and idade <= VALIDADE_MIN * 60),
    }
    if atendido:
        saida['frase'] = ('A máquina pegou o pedido e está publicando. '
                          'O resultado aparece no grupo (ou no número de teste).')
    elif saida['em_aberto']:
        saida['frase'] = ('Pedido feito há %s, esperando a máquina. Ela pergunta '
                          'de 15 em 15 minutos, e também no instante em que liga.'
                          % saida['ha_quanto_tempo'])
    else:
        saida['frase'] = 'Último pedido: %s.' % saida['ha_quanto_tempo']
    return saida


# ===== EXECUTAR PEDIDOS AGORA (17/09/2026, pedido do dono) =====
#
# "Manter de pé" e "Conectar" com o WhatsApp parado ESPERAM A MAQUINA: quem
# sobe os containers e o Guardiao (10 em 10 min) e quem atende a conexao e a
# tarefa da fila (15 em 15). Medido: clique 17/09 10:51:15 -> containers
# 10:58:03; conexao 14/09 23:26:57 -> 23:35:03. A tela mostrava "Automacao de
# pe" no instante do clique porque le o PRAZO gravado, nao os containers.
#
# Este pedido faz a consulta de minuto em minuto do Dell
# (`Movimente-se_PedidoImediato.ps1`) chamar o Guardiao e a fila NA HORA, em
# sequencia e sem atropelar o que ja estiver rodando. Nao publica ranking: e o
# "Publicar agora" sem a publicacao. Mesmo molde e mesma validade do pedido de
# rodada acima — um jeito so de guardar pedido para a maquina.

def pedir_execucao(cidade, por=None):
    """Registra o pedido e tenta acordar a maquina. Devolve (ok, mensagem)."""
    aberto = execucao_pendente(cidade)
    if aberto:
        return False, ('Já existe um pedido esperando a máquina, feito há %s. '
                       'Ela pega em até 1 minuto quando estiver ligada.'
                       % _humano(aberto['idade_seg']))
    dados = {
        'pedido_em': datetime.now().isoformat(timespec='seconds'),
        'por': (por or 'admin')[:120],
        'atendido_em': None,
    }
    secretstore.set_value(secretstore.EXECUCAO_PEDIDA, json.dumps(dados),
                          scope=cidade, updated_by=por)
    logger.warning('EXECUTAR PEDIDOS AGORA em %s por %s', cidade, por)
    aviso = ''
    try:
        from app import ligarmaquina, maquina
        situacao = (maquina.estado(cidade) or {}).get('situacao')
        if situacao == maquina.LIGADA:
            aviso = ' O computador está ligado: ele pega o pedido em até 1 minuto.'
        elif ligarmaquina.configurado(cidade):
            ligarmaquina.ligar(cidade)
            aviso = (' Mandei o sinal que liga o computador; quando ele acordar, '
                     'pega o pedido.')
    except Exception as exc:
        logger.warning('Executar pedidos: nao consegui acordar a maquina: %s', exc)
    return True, ('Pedido registrado.%s Anda na hora o que estava esperando a '
                  'máquina: containers pedidos, conexão de WhatsApp e mensagens '
                  'na fila — nada é publicado por causa dele.' % aviso)


def execucao_pendente(cidade):
    """O pedido de execucao em aberto, ou None. Nunca levanta excecao."""
    try:
        bruto = (secretstore.get(secretstore.EXECUCAO_PEDIDA, scope=cidade) or '').strip()
        dados = json.loads(bruto) if bruto else None
    except Exception:
        return None
    if not isinstance(dados, dict) or dados.get('atendido_em'):
        return None
    idade = _idade(dados.get('pedido_em'))
    if idade is None:
        return None
    if idade > VALIDADE_MIN * 60:
        marcar_execucao_atendida(cidade, por='caducou')
        return None
    dados['idade_seg'] = idade
    return dados


def marcar_execucao_atendida(cidade, por=None):
    """A maquina pegou o pedido de execucao — nao atende duas vezes."""
    try:
        bruto = (secretstore.get(secretstore.EXECUCAO_PEDIDA, scope=cidade) or '').strip()
        dados = json.loads(bruto) if bruto else None
    except Exception:
        dados = None
    if not isinstance(dados, dict):
        return False
    dados['atendido_em'] = datetime.now().isoformat(timespec='seconds')
    dados['atendido_por'] = (por or 'automacao')[:80]
    secretstore.set_value(secretstore.EXECUCAO_PEDIDA, json.dumps(dados),
                          scope=cidade, updated_by=por or 'automacao')
    logger.info('Pedido de execucao de %s atendido por %s', cidade, por)
    return True


def _ler(cidade):
    bruto = (secretstore.get(secretstore.RANKING_PEDIDO, scope=cidade) or '').strip()
    if not bruto:
        return None
    try:
        dados = json.loads(bruto)
        return dados if isinstance(dados, dict) else None
    except ValueError:
        return None


def _idade(carimbo):
    if not carimbo:
        return None
    try:
        return int((datetime.now() - datetime.fromisoformat(str(carimbo))).total_seconds())
    except ValueError:
        return None


def _humano(segundos):
    """"4 minutos", "2 horas e 10 minutos" — nunca "9000 seg"."""
    try:
        segundos = int(segundos or 0)
    except (TypeError, ValueError):
        return 'agora'
    if segundos < 60:
        return 'menos de um minuto'
    minutos, horas = (segundos // 60) % 60, segundos // 3600
    if horas and minutos:
        return '%d hora%s e %d minuto%s' % (horas, '' if horas == 1 else 's',
                                            minutos, '' if minutos == 1 else 's')
    if horas:
        return '%d hora%s' % (horas, '' if horas == 1 else 's')
    return '%d minuto%s' % (minutos, '' if minutos == 1 else 's')
