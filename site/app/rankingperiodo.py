# -*- coding: utf-8 -*-
"""Qual ranking sai NESTA passada: o do mês ou o do período (P-137-b).

A DECISÃO DO DONO QUE ORIENTA TUDO (20/09/2026): numa passada sai **o ranking
mensal OU o do período, nunca os dois**. Duas tabelas grandes seguidas no grupo
viram parede de texto, e ninguém lê a segunda.

POR QUE ISTO VIVE NO SITE, E NÃO NA AUTOMAÇÃO
A automação pergunta `GET /api/ranking/latest` e publica o que vier. Então
basta o site responder com o ranking do período quando for o dia dele: a
pipeline não muda uma linha — ela nem precisa saber que existe ranking de
período. É o caminho mais curto e o único que não encosta em código
consolidado (regra 26 do `CLAUDE.md`).

ESTE MÓDULO SÓ DECIDE. Não envia, não fala com o WAHA, não toca em sessão.

O QUE MANDA, NA ORDEM
  1. **Dia 1º com o mês zerado** — o mensal não vai ao grupo (regra 5) e ficaria
     um silêncio esquisito justo na virada. Sai o período premiado; se nenhum
     estiver marcado, sai o **anual**, que está sempre atualizado.
  2. **Troféu marcado** — sem troféu não há patrocinador para o prêmio, e sem
     prêmio o ranking de período não vai ao grupo.
  3. **É dia?** — a frequência que a cidade escolheu (semanal no trimestral,
     mensal no semestral, porque semestre é período longo).
  4. **É hora?** — só depois do ÚLTIMO horário da agenda daquela cidade. O
     horário vem de `publicador.agenda`, nunca escrito aqui: mudou na tela,
     mudou aqui.
  5. **Já saiu hoje?** — uma vez por dia, no máximo.
"""
import logging
from datetime import datetime

from app import secretstore

logger = logging.getLogger(__name__)

# Chaves do cofre, por cidade. Uma cidade nunca alcança a outra (regra 1).
FREQ_TRIMESTRE = 'ranking_periodo_freq_trimestre'
FREQ_SEMESTRE = 'ranking_periodo_freq_semestre'
SERVIDO = 'ranking_periodo_servido'          # 'AAAA-MM-DD|periodo'

# Segunda=0 ... Domingo=6 (o mesmo que `datetime.weekday()`).
DIAS_DA_SEMANA = {
    '1': (5,),                 # sábado
    '2': (2, 5),               # quarta e sábado
    '3': (0, 2, 5),            # segunda, quarta e sábado
    'diario': tuple(range(7)),
}
# No semestral a conta é por MÊS: período longo não precisa de ranking semanal.
DIAS_DO_MES = {
    '4': (7, 14, 21, 28),
    '6': (5, 10, 15, 20, 25, 28),
    '8': (4, 8, 12, 16, 20, 24, 28, 0),   # 0 = o último dia do mês
}
FREQ_PADRAO = {'trimestre': '1', 'semestre': '4'}

# A janela depois do último ranking do dia. O vigia passa de 15 em 15 min,
# então ela precisa ser maior que isso — senão um dia a passada cai fora e o
# ranking do período simplesmente não sai.
JANELA_MIN = 25


def frequencia(cidade, periodo):
    """Com que frequência o ranking daquele período sai nesta cidade."""
    if periodo == 'trimestre':
        bruto = (secretstore.get(FREQ_TRIMESTRE, scope=cidade) or '').strip()
        return bruto if bruto in DIAS_DA_SEMANA else FREQ_PADRAO['trimestre']
    if periodo == 'semestre':
        bruto = (secretstore.get(FREQ_SEMESTRE, scope=cidade) or '').strip()
        return bruto if bruto in DIAS_DO_MES else FREQ_PADRAO['semestre']
    return ''        # anual não tem envio periódico


def definir_frequencia(cidade, periodo, valor, por=''):
    """Grava a frequência. Devolve True quando o valor é aceitável."""
    valor = (valor or '').strip()
    if periodo == 'trimestre' and valor in DIAS_DA_SEMANA:
        return secretstore.set_value(FREQ_TRIMESTRE, valor, scope=cidade,
                                     updated_by=por)
    if periodo == 'semestre' and valor in DIAS_DO_MES:
        return secretstore.set_value(FREQ_SEMESTRE, valor, scope=cidade,
                                     updated_by=por)
    return False


def _ultimo_dia_do_mes(quando):
    import calendar
    return calendar.monthrange(quando.year, quando.month)[1]


def e_dia_de_enviar(cidade, periodo, agora):
    """Hoje é dia de mandar o ranking daquele período nesta cidade?"""
    freq = frequencia(cidade, periodo)
    if periodo == 'trimestre':
        return agora.weekday() in DIAS_DA_SEMANA.get(freq, ())
    if periodo == 'semestre':
        dias = DIAS_DO_MES.get(freq, ())
        # O 0 da tabela quer dizer "o último dia do mês", seja ele 28 ou 31.
        dias = tuple(_ultimo_dia_do_mes(agora) if d == 0 else d for d in dias)
        return agora.day in dias
    return False


def ultimo_horario_do_dia(cidade):
    """(hora, minuto) do último ranking do dia, lido da agenda da cidade.

    None quando a cidade não tem agenda — e aí a passada não segura nada.
    """
    try:
        from app.publicador import agenda
        minutos = []
        for linha in agenda(cidade) or ():
            if not getattr(linha, 'ativo', True):
                continue
            try:
                h, m = str(linha.horario).split(':')
                minutos.append(int(h) * 60 + int(m))
            except (ValueError, AttributeError):
                continue
        if not minutos:
            return None
        ultimo = max(minutos)
        return ultimo // 60, ultimo % 60
    except Exception as exc:
        logger.debug('rankingperiodo: sem agenda para %s (%s)', cidade, exc)
        return None


def na_janela_do_ultimo_ranking(cidade, agora):
    """Já passou o último horário da agenda, e ainda estamos na janela?"""
    ultimo = ultimo_horario_do_dia(cidade)
    if ultimo is None:
        return True          # sem agenda cadastrada, não é isto que segura
    alvo = ultimo[0] * 60 + ultimo[1]
    atual = agora.hour * 60 + agora.minute
    return alvo <= atual <= alvo + JANELA_MIN


def ja_serviu_hoje(cidade, agora):
    """O período já foi servido hoje nesta cidade? Devolve qual, ou ''."""
    bruto = (secretstore.get(SERVIDO, scope=cidade) or '').strip()
    if '|' not in bruto:
        return ''
    dia, periodo = bruto.split('|', 1)
    return periodo if dia == agora.strftime('%Y-%m-%d') else ''


def marcar_servido(cidade, periodo, agora=None):
    """Registra que o ranking daquele período já saiu hoje.

    MARCAR AO SERVIR, e não depois de publicar: o site não fica sabendo se a
    publicação deu certo. Se ela falhar, o dia não repete — o contrário seria
    arriscar mensagem dobrada no grupo, que é pior e não tem desfazer.
    """
    agora = agora or datetime.now()
    return secretstore.set_value(
        SERVIDO, '%s|%s' % (agora.strftime('%Y-%m-%d'), periodo),
        scope=cidade, updated_by='rankingperiodo')


def _mes_zerado(cidade, agora):
    """O mês corrente ainda não tem km nenhum nesta cidade?"""
    try:
        from app.blueprints.ranking import build_periodo_ranking_data
        dados = build_periodo_ranking_data(cidade, 'mes', agora)
        total = 0.0
        for secao in ('corrida_caminhada', 'ciclismo'):
            total += float((dados.get(secao) or {}).get('total_km') or 0)
        return total <= 0
    except Exception as exc:
        logger.warning('rankingperiodo: não consegui ver o mês de %s (%s)',
                       cidade, exc)
        return False


def periodo_premiado(cidade):
    from app.blueprints.ranking import periodo_premiado as _premiado
    return _premiado(cidade)


def periodo_da_passada(cidade, agora=None):
    """Qual ranking sai agora: ('trimestre'|'semestre'|'ano'|None, motivo).

    None quer dizer "o do mês, como sempre".
    """
    agora = agora or datetime.now()
    try:
        ja = ja_serviu_hoje(cidade, agora)
        if ja:
            return None, 'o ranking do %s já saiu hoje' % ja

        premiado = periodo_premiado(cidade)

        # 1. A VIRADA DO MÊS. No dia 1º o mensal está zerado e não vai ao grupo
        # (regra 5). Em vez do silêncio, sai o período — e, sem período
        # marcado, o anual, que está sempre atualizado.
        if agora.day == 1 and _mes_zerado(cidade, agora):
            escolhido = premiado or 'ano'
            return escolhido, ('dia 1º com o mês ainda zerado — sai o ranking '
                               '%s' % escolhido)

        # 2. Sem troféu não há prêmio, e sem prêmio o ranking do período não
        # vai ao grupo (regra do dono).
        if not premiado:
            return None, 'nenhum período premiado nesta cidade'
        if premiado == 'ano':
            return None, 'o anual não tem envio periódico'

        # 3. É dia?
        if not e_dia_de_enviar(cidade, premiado, agora):
            return None, ('hoje não é dia do ranking %s (frequência %s)'
                          % (premiado, frequencia(cidade, premiado)))

        # 4. É hora? Só depois do último ranking do dia.
        if not na_janela_do_ultimo_ranking(cidade, agora):
            return None, 'ainda não é o último ranking do dia'

        return premiado, 'é o dia e a hora do ranking %s' % premiado
    except Exception as exc:
        # FALHA AQUI NÃO PODE DERRUBAR O RANKING DO MÊS: na dúvida, sai o
        # mensal, que é o de sempre.
        logger.error('rankingperiodo (%s): %s', cidade, exc, exc_info=True)
        return None, 'erro ao decidir: %s' % exc
