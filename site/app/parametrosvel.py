# -*- coding: utf-8 -*-
"""Tetos de velocidade por cidade e por atleta — o que é plausível para quem.

POR QUE ISTO EXISTE (09/09/2026, pedido do dono)

Um teto único trata 5 km e 15 km igual, e trata todo mundo igual. Nenhuma das
duas coisas é verdade: quanto maior a distância menor a velocidade que o corpo
sustenta, e um atleta treinado faz com folga o que para outro seria impossível.

Sem isso, o antitrapaça só sabe dizer "acima de X é suspeito" — e aí, ou marca
gente honesta, ou deixa passar o que devia pegar. Com mais de mil flags em
aberto, o dono não vai revisar uma a uma: ele precisa de uma régua que decida
o óbvio e sobre a qual ele tenha controle.

A HIERARQUIA, do mais específico para o mais geral:

    1. teto do ATLETA     (ele treina, e o número dele é maior)
    2. teto da CIDADE     (a régua local, que o dono ajusta)
    3. recorde MUNDIAL    (limite físico — ninguém passa disso)

O recorde mundial nunca é ultrapassado por nenhum ajuste: é teto absoluto, não
opinião. O que o dono regula é *abaixo* disso — o que ele considera plausível
para o grupo dele.
"""
import logging
from datetime import datetime

from sqlalchemy import text as sa_text

from app import db

logger = logging.getLogger(__name__)

_PRONTO = False

# As faixas de distância, em km. O dono pediu estas quatro (09/09).
FAIXAS = (
    ('ate5',   0.0,   5.0,  'até 5 km'),
    ('5a10',   5.0,  10.0,  'de 5 a 10 km'),
    ('10a15', 10.0,  15.0,  'de 10 a 15 km'),
    ('acima15', 15.0, 9999.0, 'acima de 15 km'),
)

MODALIDADES = (
    ('corrida_caminhada', 'Corrida e caminhada'),
    ('ciclismo', 'Ciclismo'),
)

# Sugestão inicial, em km/h. NÃO é o recorde mundial (esse é o teto absoluto,
# em consolidado._TETO_POR_DISTANCIA): é o que um amador rápido faz sem que
# ninguém estranhe. O dono ajusta olhando a prévia de quantas atividades cada
# valor deixa de fora.
SUGESTAO = {
    'corrida_caminhada': {'ate5': 17.0, '5a10': 16.0, '10a15': 15.0, 'acima15': 14.0},
    'ciclismo':          {'ate5': 35.0, '5a10': 35.0, '10a15': 34.0, 'acima15': 33.0},
}


def ensure_schema():
    """Cria a tabela na primeira chamada. Idempotente (convenção do projeto)."""
    global _PRONTO
    if _PRONTO:
        return
    try:
        with db.engine.begin() as conn:
            conn.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS parametros_velocidade (
                    escopo     VARCHAR(20)  NOT NULL,
                    alvo       VARCHAR(100) NOT NULL,
                    modalidade VARCHAR(30)  NOT NULL,
                    faixa      VARCHAR(10)  NOT NULL,
                    teto       REAL         NOT NULL,
                    definido_por VARCHAR(120) DEFAULT '',
                    definido_em  DATETIME,
                    PRIMARY KEY (escopo, alvo, modalidade, faixa)
                )
            """))
        _PRONTO = True
    except Exception as exc:
        logger.warning('parametros_velocidade: não preparei a tabela (%s)', exc)


def faixa_da_distancia(km):
    """A chave da faixa em que esta distância cai."""
    try:
        km = float(km or 0)
    except (TypeError, ValueError):
        km = 0.0
    for chave, minimo, maximo, _rotulo in FAIXAS:
        if minimo < km <= maximo or (chave == 'ate5' and km <= maximo):
            return chave
    return FAIXAS[-1][0]


_CACHE = {}


def _limpar_cache():
    """Esvazia a memória curta. Chamado por `gravar` — valor salvo tem de valer
    na tela seguinte, sem esperar reinício."""
    _CACHE.clear()


def tetos(escopo, alvo, modalidade):
    """{faixa: teto} do que está gravado para este escopo. {} se nada.

    GUARDA EM MEMÓRIA porque quem chama mais é a tabela de recordes, uma vez
    por atleta: sem isto seriam duas consultas por atividade num NAS de 490 MB
    e 2 núcleos (o custo por dia é requisito do projeto). A memória é do
    processo e morre no reload; `gravar` a esvazia na hora.
    """
    chave = (escopo, str(alvo), modalidade)
    if chave in _CACHE:
        return _CACHE[chave]
    ensure_schema()
    try:
        linhas = db.session.execute(sa_text(
            'SELECT faixa, teto FROM parametros_velocidade '
            'WHERE escopo = :e AND alvo = :a AND modalidade = :m'),
            {'e': escopo, 'a': str(alvo), 'm': modalidade}).fetchall()
        _CACHE[chave] = {l[0]: float(l[1]) for l in linhas}
        return _CACHE[chave]
    except Exception:
        return {}   # NAO guarda falha em cache: o proximo pedido tenta de novo


def gravar(escopo, alvo, modalidade, faixa, teto, por=''):
    """Guarda um teto. `teto` vazio ou zero apaga (volta a valer o mais geral)."""
    ensure_schema()
    try:
        if not teto:
            db.session.execute(sa_text(
                'DELETE FROM parametros_velocidade WHERE escopo = :e AND '
                'alvo = :a AND modalidade = :m AND faixa = :f'),
                {'e': escopo, 'a': str(alvo), 'm': modalidade, 'f': faixa})
        else:
            db.session.execute(sa_text(
                'INSERT INTO parametros_velocidade (escopo, alvo, modalidade, '
                'faixa, teto, definido_por, definido_em) VALUES '
                '(:e, :a, :m, :f, :t, :p, :d) '
                'ON CONFLICT(escopo, alvo, modalidade, faixa) DO UPDATE SET '
                'teto = :t, definido_por = :p, definido_em = :d'),
                {'e': escopo, 'a': str(alvo), 'm': modalidade, 'f': faixa,
                 't': float(teto), 'p': (por or '')[:120], 'd': datetime.now()})
        db.session.commit()
        _limpar_cache()
        return True
    except Exception as exc:
        db.session.rollback()
        logger.error('parametros_velocidade: não gravei (%s)', exc)
        return False


def teto_para(strava_id, cidade, modalidade, km):
    """O teto que vale para ESTA atividade, seguindo a hierarquia.

    Atleta vence cidade; cidade vence o padrão; e o recorde mundial, que é
    limite físico, nunca é ultrapassado por nenhum ajuste.
    """
    from app.consolidado import _teto_da_distancia

    faixa = faixa_da_distancia(km)
    do_atleta = tetos('atleta', strava_id, modalidade).get(faixa)
    if do_atleta:
        candidato = do_atleta
    else:
        da_cidade = tetos('cidade', cidade or '', modalidade).get(faixa)
        candidato = da_cidade or SUGESTAO.get(modalidade, {}).get(faixa)

    if not candidato:
        return None
    if modalidade == 'corrida_caminhada':
        # O RECORDE MUNDIAL É TETO ABSOLUTO: nenhum ajuste passa dele.
        return min(float(candidato), _teto_da_distancia(km))
    return float(candidato)


def quantas_ficam_de_fora(cidade, modalidade, teto_por_faixa):
    """Quantas atividades cairiam fora com estes tetos — a prévia da tela.

    É o que transforma "17 km/h parece razoável" em decisão informada: o dono
    vê que 17 tira 3 atividades e 15 tira 40 antes de salvar qualquer coisa.
    """
    from app.models import Activity

    ensure_schema()
    try:
        linhas = db.session.query(
            Activity.velocidade_media, Activity.distancia
        ).filter(
            Activity.modalidade == modalidade,
            Activity.velocidade_media.isnot(None),
            Activity.velocidade_media > 0,
            Activity.status != 'removida',
            *( [Activity.cidade == cidade] if cidade else [] )
        ).all()
    except Exception:
        return {'fora': 0, 'total': 0}

    fora = 0
    for vel, dist in linhas:
        limite = teto_por_faixa.get(faixa_da_distancia(dist))
        if limite and float(vel or 0) > float(limite):
            fora += 1
    return {'fora': fora, 'total': len(linhas)}
