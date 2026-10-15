# -*- coding: utf-8 -*-
"""A atualizacao de perfis que termina sozinha, sem ninguem clicar.

POR QUE EXISTE (P-128, pedido do dono em 06-07/09/2026). O botao "Atualizar
fotos" parou em 55 de 77 porque bateu o teto do quarto de hora. Ele avisava
direito, mas o trabalho ficava pela metade -- e o clique seguinte parava no
mesmo lugar. Um botao que nunca termina o servico nao e' um botao, e' um pedido
de desculpas.

O que o dono pediu, na frase dele: "deve obedecer e acompanhar todo o fluxo de
requisicoes ao Strava, porem, tudo deve funcionar automatico sem clique para a
frequencia definida e mesmo assim seguir o fluxo de requisicoes permitidas".

Entao: NAO se pede mais requisicao por minuto, e NAO se fura teto nenhum. O que
muda e' que o trabalho **guarda de onde parou** e alguem o **retoma sozinho**
quando a janela virar. E, passada a frequencia definida, ele **recomeca
sozinho** -- ninguem precisa lembrar de clicar.

Quem chama e' o vigia, que ja passa de 15 em 15 minutos: nao ha temporizador
novo. Se houver cota, ele anda; se nao houver, ele volta na proxima passada.
"""
import json
import os
import time

_RAIZ = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'temp_extract', 'perfis')

# UMA PASSADA POR MES, a partir do dia 1 (15/09/2026, regra do dono). Quem
# manda na frequencia e' `vencida()`, que compara o MES DE CALENDARIO da
# ultima conclusao com o de hoje. Este mapa ficou como registro do que valia
# antes (nome a cada 7 dias, foto a cada 30) e nao e' mais consultado.
DIAS_ENTRE_PASSADAS = {'foto': 30, 'nome': 7}

# Quantos atletas por passada do vigia. Baixo de proposito: o objetivo e'
# terminar sem NUNCA disputar espaco com o ranking ou com uma importacao.
POR_PASSADA = 12

# So anda com esta folga em TODAS as janelas de cota. Abaixo disso, espera --
# atualizar foto nunca pode ser o motivo de o ranking nao sair.
FOLGA_MINIMA = 0.35


def _caminho(cidade, o_que):
    # NOME DE ARQUIVO SEM ACENTO (17/09/2026). O Python embutido no uWSGI roda
    # sem idioma definido e le nomes de arquivo como ASCII: "Palmeirópolis_
    # nome.json" dava UnicodeEncodeError no site, a leitura voltava vazia e a
    # atualizacao recomecava a cada 15 min (medido: concluiu 23:46, recomecou
    # 23:51; replica fiel do ambiente provou o erro no "ó"). O processo dos
    # trabalhos, que grava, lida bem com acento — por isso so a leitura falhava.
    # `isalnum()` sozinho deixava o acento passar; aqui ele sai antes.
    import unicodedata
    base = unicodedata.normalize('NFKD', '%s_%s' % (cidade, o_que))
    base = base.encode('ascii', 'ignore').decode('ascii')
    seguro = ''.join(c for c in base if c.isalnum() or c in '-_')
    return os.path.join(_RAIZ, '%s.json' % seguro) if seguro else None


def _ler(cidade, o_que):
    caminho = _caminho(cidade, o_que)
    if not caminho:
        return {}
    try:
        with open(caminho, 'r') as f:
            return json.load(f) or {}
    except FileNotFoundError:
        return {}                        # nunca rodou: o normal da 1a vez
    except Exception as exc:
        # (Sem `os.path.exists` antes, de proposito: ele tambem engole erro de
        # codificacao do nome — responde "nao existe" — e o diagnostico
        # abaixo nunca apareceria.)
        # ERRO DE LEITURA NAO E "NUNCA RODOU" (17/09/2026). Este except
        # devolvia {} calado, e {} vira "nunca rodou" em `vencida()` — a
        # passada recomeca a cada 15 min. Em 17/09 Palmeiropolis concluiu as
        # 23:46 e recomecou as 23:51. A linha abaixo diz o motivo real.
        import logging
        logging.getLogger(__name__).warning(
            'perfilauto: nao consegui ler %s (%s: %s)', caminho,
            type(exc).__name__, exc)
        return {}


def _gravar(cidade, o_que, dados):
    caminho = _caminho(cidade, o_que)
    if not caminho:
        return
    try:
        if not os.path.isdir(_RAIZ):
            os.makedirs(_RAIZ)
        tmp = caminho + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(dados, f)
        os.replace(tmp, caminho)
    except Exception:
        # Sem estado, o pior caso e' voltar ao comportamento antigo (so' no
        # clique). Nunca deixar isto derrubar a atualizacao.
        pass


def guardar_pendentes(cidade, o_que, sids, motivo=''):
    """Anota de onde continuar. Chamado quando o teto interrompe a passada."""
    _gravar(cidade, o_que, {
        'pendentes': [str(s) for s in (sids or [])],
        'motivo': motivo,
        'em': time.time(),
        'iniciada_em': _ler(cidade, o_que).get('iniciada_em') or time.time(),
    })


def marcar_concluida(cidade, o_que):
    """A passada acabou: zera a fila e carimba a data, que e' o que segura a
    frequencia ate a proxima."""
    _gravar(cidade, o_que, {'pendentes': [], 'concluida_em': time.time()})


def pendentes(cidade, o_que):
    return list(_ler(cidade, o_que).get('pendentes') or [])


def vencida(cidade, o_que, agora=None):
    """Passou tempo suficiente para comecar uma passada nova?

    O prazo conta desde a ULTIMA CONCLUSAO, nao desde o ultimo comeco: assim
    uma passada longa (que atravessou varios tetos) nao dispara outra logo
    atras.
    """
    agora = agora or time.time()
    dados = _ler(cidade, o_que)
    if dados.get('pendentes'):
        return True                      # tem trabalho parado: continua
    ultima = float(dados.get('concluida_em') or 0)
    if not ultima:
        return True                      # nunca rodou
    # UMA VEZ POR MES, A PARTIR DO DIA 1 (15/09/2026, regra do dono). Antes
    # eram 7 dias (nome) e 30 dias (foto) contados da ultima conclusao, o que
    # espalhava as passadas por qualquer dia do mes. A conta e por MES DE
    # CALENDARIO, nao por 30 dias corridos: a primeira passada do mes vale, e
    # se a maquina estiver fora do ar no dia 1 o mes nao se perde.
    ref = time.localtime(ultima)
    hoje = time.localtime(agora)
    return (hoje.tm_year, hoje.tm_mon) != (ref.tm_year, ref.tm_mon)


def ha_folga():
    """Sobra cota para adiantar isto sem atrapalhar o que importa?"""
    try:
        from app.ratepacer import SharedPacer
        from app import stravasinal
        if stravasinal.bloqueado_ate():
            return False
        pacer = SharedPacer('strava')
        tetos, uso = pacer.tetos(), pacer.uso()
        for chave in ('q15', 'hora', 'dia'):
            teto = int(tetos.get(chave) or 0)
            if teto <= 0:
                continue
            if (teto - int(uso.get(chave) or 0)) < teto * FOLGA_MINIMA:
                return False
        return True
    except Exception:
        return False


def situacao(cidade, o_que):
    """Para a tela: quanto falta e desde quando."""
    dados = _ler(cidade, o_que)
    return {
        'faltam': len(dados.get('pendentes') or []),
        'motivo': dados.get('motivo') or '',
        'concluida_em': dados.get('concluida_em'),
        'iniciada_em': dados.get('iniciada_em'),
    }
