# -*- coding: utf-8 -*-
"""Fechamento de mes — a foto que nao muda mais.

POR QUE ESTE MODULO EXISTE
O ranking e vivo: se alguem posta um link atrasado em agosto referente a
julho, o ranking de julho muda. Isso e certo para os km, mas erra em tudo
que ja virou compromisso com gente de verdade:

  - a lista de medalhistas ja foi mandada para a grafica;
  - o patrocinador de julho ja apareceu no grupo com aquele nome e aquela logo;
  - a prestacao de contas do mes ja foi publicada.

Fechar o mes grava essa foto. Depois disso o mes tem uma versao oficial:
medalhistas congelados, patrocinador congelado, texto do ranking congelado.
Os km continuam sendo somados e aparecendo na pagina do atleta e no anual —
o que para de mudar e o resultado OFICIAL daquele mes.

QUANDO FECHA
Sozinho, alguns dias depois do mes virar (DIAS_DE_CARENCIA), para dar tempo
de chegar link atrasado. Da para fechar na mao antes disso, e o master
supremo pode reabrir se precisar corrigir algo.
"""
import hashlib
import json
import logging
from datetime import datetime

from sqlalchemy import text as sa_text

from app import db

logger = logging.getLogger(__name__)

# Dias apos o fim do mes antes do fechamento automatico. Sao os links que
# chegam atrasados: quem correu no dia 31 as vezes so posta no dia 2.
DIAS_DE_CARENCIA = 5

_SCHEMA_PRONTO = False


def ensure_schema():
    global _SCHEMA_PRONTO
    if _SCHEMA_PRONTO:
        return
    with db.engine.begin() as conn:
        conn.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS fechamento_mes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                ano INTEGER NOT NULL,
                mes INTEGER NOT NULL,
                fechado_em DATETIME NOT NULL,
                fechado_por VARCHAR(120),
                automatico INTEGER NOT NULL DEFAULT 0,
                total_atletas INTEGER DEFAULT 0,
                total_medalhas INTEGER DEFAULT 0,
                km_total REAL DEFAULT 0,
                patrocinador VARCHAR(200),
                impressao_digital VARCHAR(64),
                resumo_json TEXT,
                CONSTRAINT _fechamento_uc UNIQUE (cidade, ano, mes)
            )
        """))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_fechamento_cidade "
            "ON fechamento_mes (cidade, ano, mes)"))
    _SCHEMA_PRONTO = True


def esta_fechado(cidade, ano, mes):
    ensure_schema()
    linha = db.session.execute(sa_text(
        "SELECT 1 FROM fechamento_mes WHERE cidade = :c AND ano = :a AND mes = :m"
    ), {'c': cidade, 'a': ano, 'm': mes}).fetchone()
    return bool(linha)


def info(cidade, ano, mes):
    """Dados do fechamento, ou None se o mes ainda esta aberto."""
    ensure_schema()
    linha = db.session.execute(sa_text("""
        SELECT fechado_em, fechado_por, automatico, total_atletas,
               total_medalhas, km_total, patrocinador, impressao_digital
        FROM fechamento_mes WHERE cidade = :c AND ano = :a AND mes = :m
    """), {'c': cidade, 'a': ano, 'm': mes}).fetchone()
    if not linha:
        return None
    return {
        'fechado_em': linha[0], 'fechado_por': linha[1],
        'automatico': bool(linha[2]), 'total_atletas': linha[3],
        'total_medalhas': linha[4], 'km_total': linha[5],
        'patrocinador': linha[6], 'impressao_digital': linha[7],
    }


def listar(cidade, limite=24):
    ensure_schema()
    linhas = db.session.execute(sa_text("""
        SELECT ano, mes, fechado_em, fechado_por, automatico, total_atletas,
               total_medalhas, km_total, patrocinador
        FROM fechamento_mes WHERE cidade = :c
        ORDER BY ano DESC, mes DESC LIMIT :l
    """), {'c': cidade, 'l': limite}).fetchall()
    return [{
        'ano': r[0], 'mes': r[1], 'fechado_em': r[2], 'fechado_por': r[3],
        'automatico': bool(r[4]), 'total_atletas': r[5], 'total_medalhas': r[6],
        'km_total': r[7], 'patrocinador': r[8],
    } for r in linhas]


def _mes_anterior(ano, mes):
    return (ano - 1, 12) if mes == 1 else (ano, mes - 1)


def _proximo_mes(ano, mes):
    return (ano + 1, 1) if mes == 12 else (ano, mes + 1)


def _ja_passou_a_carencia(ano, mes, agora=None):
    """O mes acabou ha tempo suficiente para nao chegar mais link atrasado?"""
    agora = agora or datetime.now()
    ano_seg, mes_seg = _proximo_mes(ano, mes)
    inicio_seguinte = datetime(ano_seg, mes_seg, 1)
    return (agora - inicio_seguinte).days >= DIAS_DE_CARENCIA


def fechar_mes(cidade, ano, mes, fechado_por=None, automatico=False, refazer=False):
    """Grava a foto oficial do mes. Idempotente.

    Ordem importa: primeiro reconstroi o ranking (para a foto sair com os km
    mais recentes), depois congela medalhistas, depois grava o registro.
    Devolve o dicionario do fechamento, ou None se nao havia o que fechar.
    """
    ensure_schema()
    from app.models import Config, MonthlyBackup
    from app.blueprints.ranking import rebuild_monthly_backup, _sponsor_nome_periodo
    from app.blueprints.avisos import congelar_medalhistas

    if esta_fechado(cidade, ano, mes) and not refazer:
        return info(cidade, ano, mes)

    if not Config.query.filter_by(cidade=cidade).first():
        logger.warning('Fechamento ignorado: cidade %s nao existe mais', cidade)
        return None

    try:
        rebuild_monthly_backup(cidade, mes, ano)
    except Exception as exc:
        db.session.rollback()
        logger.warning('Fechamento de %s %s/%s: rebuild falhou (%s); '
                       'seguindo com o que ja estava gravado', cidade, mes, ano, exc)

    backup = MonthlyBackup.query.filter_by(cidade=cidade, ano=ano, mes=mes).first()
    if not backup:
        logger.info('Nada a fechar em %s %s/%s (mes sem ranking)', cidade, mes, ano)
        return None

    try:
        congelar_medalhistas(cidade, ano, mes)
    except Exception as exc:
        db.session.rollback()
        logger.warning('Nao consegui congelar medalhistas de %s %s/%s: %s',
                       cidade, mes, ano, exc)

    dados = backup.get_ranking_data() or {}
    km_total = 0.0
    for secao in ('corrida_caminhada', 'ciclismo'):
        km_total += float((dados.get(secao) or {}).get('total_km') or 0)
    impressao = hashlib.sha256(
        (backup.json_ranking or '').encode('utf-8')).hexdigest()
    patrocinador = _sponsor_nome_periodo(cidade, ano, mes)

    resumo = {
        'total_atletas': backup.total_atletas or 0,
        'total_medalhas': backup.total_medalhas or 0,
        'km_total': round(km_total, 2),
        'patrocinador': patrocinador,
    }

    db.session.execute(sa_text("""
        INSERT OR REPLACE INTO fechamento_mes
            (cidade, ano, mes, fechado_em, fechado_por, automatico,
             total_atletas, total_medalhas, km_total, patrocinador,
             impressao_digital, resumo_json)
        VALUES (:c, :a, :m, :quando, :quem, :auto, :atl, :med, :km, :pat, :fp, :js)
    """), {
        'c': cidade, 'a': ano, 'm': mes, 'quando': datetime.now(),
        'quem': fechado_por, 'auto': 1 if automatico else 0,
        'atl': resumo['total_atletas'], 'med': resumo['total_medalhas'],
        'km': resumo['km_total'], 'pat': patrocinador, 'fp': impressao,
        'js': json.dumps(resumo, ensure_ascii=False),
    })
    db.session.commit()
    logger.warning('Mes fechado: %s %s/%s (%s atletas, %s medalhas, %s km, '
                   'patrocinador "%s")', cidade, mes, ano,
                   resumo['total_atletas'], resumo['total_medalhas'],
                   resumo['km_total'], patrocinador)
    return info(cidade, ano, mes)


def reabrir_mes(cidade, ano, mes, por=None):
    """Desfaz o fechamento. So o master supremo chama isto.

    Serve para corrigir um mes que fechou com dado errado. Os medalhistas
    congelados continuam onde estao ate alguem fechar de novo.
    """
    ensure_schema()
    db.session.execute(sa_text(
        "DELETE FROM fechamento_mes WHERE cidade = :c AND ano = :a AND mes = :m"
    ), {'c': cidade, 'a': ano, 'm': mes})
    db.session.commit()
    logger.warning('Mes REABERTO por %s: %s %s/%s', por, cidade, mes, ano)


def fechar_meses_pendentes(agora=None):
    """Fecha sozinho todo mes vencido que ainda esta aberto.

    Roda no boot e no ticker. Olha ate 13 meses para tras para que uma
    instalacao que ficou desligada um tempo se acerte sozinha.
    """
    ensure_schema()
    from app.models import Config, MonthlyBackup

    agora = agora or datetime.now()
    fechados = []
    for (cidade,) in db.session.query(Config.cidade).all():
        ano, mes = _mes_anterior(agora.year, agora.month)
        # Percorre os 13 meses SEM atalho. Ja tentamos parar no primeiro mes
        # fechado, mas isso deixava passar um mes reaberto para correcao: ele
        # ficava aberto para sempre, porque a varredura parava antes de chegar
        # nele. Sao 13 consultas baratas, 4 vezes por dia.
        for _ in range(13):
            if not _ja_passou_a_carencia(ano, mes, agora) or esta_fechado(cidade, ano, mes):
                ano, mes = _mes_anterior(ano, mes)
                continue
            existe = MonthlyBackup.query.filter_by(
                cidade=cidade, ano=ano, mes=mes).first()
            if existe:
                try:
                    if fechar_mes(cidade, ano, mes, fechado_por='automatico',
                                  automatico=True):
                        fechados.append((cidade, ano, mes))
                except Exception as exc:
                    db.session.rollback()
                    logger.error('Falha ao fechar %s %s/%s: %s',
                                 cidade, mes, ano, exc, exc_info=True)
            ano, mes = _mes_anterior(ano, mes)
    if fechados:
        logger.warning('Fechamento automatico concluiu %s mes(es): %s',
                       len(fechados), fechados)
    return fechados
