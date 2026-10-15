# -*- coding: utf-8 -*-
"""Atividade de OUTRO mes que chegou no grupo — o admin precisa saber.

POR QUE ESTE MODULO EXISTE
Em 05/08/2026, ao meio-dia, uma atleta postou no grupo o link de uma caminhada
de 104,99 km feita em 31/07. O job estava processando AGOSTO, o filtro de
periodo descartou a atividade — corretamente, ela nao e de agosto — e o unico
registro que sobrou foi uma linha de log. Julho fechou sete horas depois, sem
esses 105 km. Ninguem foi avisado. A conferencia de agosto respondeu "tudo
certo", porque para agosto estava mesmo.

O erro nao foi o filtro: foi DESCARTAR EM SILENCIO. Km que some sem deixar
rastro e o pior desfecho do sistema inteiro — nao da nem para descobrir depois.

A partir daqui, todo link que chega e nao entra no ranking do mes processado
por ser de outro mes fica REGISTRADO aqui, e o admin recebe um e-mail com o
numero na mao. A decisao continua sendo dele (o mes pode estar fechado, e
fechamento congela — regra 9), mas agora ele decide sabendo.

E frequente ser gente com vergonha de postar um feito grande. Isso e exatamente
o que o grupo existe para incentivar; perder em silencio e o contrario disso.
"""
import logging
from datetime import datetime

from app import db
from app import correio

logger = logging.getLogger(__name__)

_SCHEMA_PRONTO = False

# Quanto tempo esperar antes de reavisar a MESMA atividade. Existe para o caso
# de o admin nao ter agido ainda: o lembrete volta, mas nao toda rodada.
_REAVISO_HORAS = 72


def ensure_schema():
    global _SCHEMA_PRONTO
    if _SCHEMA_PRONTO:
        return
    with db.engine.begin() as conn:
        conn.execute(db.text("""
            CREATE TABLE IF NOT EXISTS atividade_fora_periodo (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade         VARCHAR(120) NOT NULL,
                activity_id    VARCHAR(40)  NOT NULL,
                strava_id      VARCHAR(40),
                atleta         VARCHAR(160),
                modalidade     VARCHAR(40),
                distancia      FLOAT,
                data_atividade DATETIME,
                periodo_job    VARCHAR(20),
                link           TEXT,
                criado_em      DATETIME,
                avisado_em     DATETIME,
                situacao       VARCHAR(20) DEFAULT 'novo'
            )
        """))
        # A chave e (cidade, atividade): a mesma atividade reaparece a cada
        # coleta enquanto o link estiver na janela, e nao pode virar uma linha
        # nova (nem um e-mail novo) por rodada.
        conn.execute(db.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_fora_periodo_unico "
            "ON atividade_fora_periodo(cidade, activity_id)"))
    _SCHEMA_PRONTO = True


def registrar(cidade, activity_id, periodo_job, dados, link=''):
    """Guarda a atividade descartada. True quando ela e novidade.

    Nunca levanta excecao para cima: isto e um registro paralelo, e derrubar o
    processamento de um mes inteiro por causa dele seria trocar um problema
    pequeno por um grande.
    """
    try:
        ensure_schema()
        dt = dados.get('data_hora')
        existe = db.session.execute(db.text(
            "SELECT id FROM atividade_fora_periodo "
            "WHERE cidade=:c AND activity_id=:a"),
            {'c': cidade, 'a': str(activity_id)}).first()
        if existe:
            return False
        db.session.execute(db.text(
            "INSERT INTO atividade_fora_periodo "
            "(cidade, activity_id, strava_id, atleta, modalidade, distancia, "
            " data_atividade, periodo_job, link, criado_em, situacao) "
            "VALUES (:c, :a, :s, :n, :m, :d, :dt, :p, :l, :agora, 'novo')"),
            {'c': cidade, 'a': str(activity_id),
             's': str(dados.get('id_usuario_strava') or ''),
             'n': (dados.get('nome_usuario') or 'Desconhecido')[:160],
             'm': dados.get('modalidade') or '',
             'd': float(dados.get('distancia') or 0),
             'dt': dt, 'p': periodo_job, 'l': link,
             'agora': datetime.now()})
        db.session.commit()
        logger.info('Atividade %s (de %s) registrada como fora do periodo %s.',
                    activity_id, dt.strftime('%m/%Y') if dt else '?', periodo_job)
        return True
    except Exception as exc:
        db.session.rollback()
        logger.warning('Nao consegui registrar a atividade %s fora do periodo: %s',
                       activity_id, exc)
        return False


def pendentes(cidade, limite=50):
    """As que ainda esperam decisao do admin."""
    try:
        ensure_schema()
        return db.session.execute(db.text(
            "SELECT * FROM atividade_fora_periodo "
            "WHERE cidade=:c AND situacao <> 'resolvido' "
            "ORDER BY data_atividade DESC LIMIT :n"),
            {'c': cidade, 'n': limite}).mappings().all()
    except Exception as exc:
        logger.warning('Nao consegui listar as atividades fora do periodo: %s', exc)
        return []


def _por_avisar(cidade):
    """Novas, mais as ja avisadas ha bastante tempo que ninguem resolveu."""
    from datetime import timedelta
    corte = datetime.now() - timedelta(hours=_REAVISO_HORAS)
    return db.session.execute(db.text(
        "SELECT * FROM atividade_fora_periodo "
        "WHERE cidade=:c AND situacao <> 'resolvido' "
        "AND (avisado_em IS NULL OR avisado_em < :corte) "
        "ORDER BY data_atividade DESC"),
        {'c': cidade, 'corte': corte}).mappings().all()


def _link_para_resolver(base, cidade, item):
    """O link que JA ABRE a tela certa, com atleta e mes escolhidos.

    Regra 21-E: mensagem que pede acao leva o link que faz a acao. Mandar "abra
    a conferencia, ache a pessoa, escolha o mes" e empurrar tres passos para
    quem esta lendo e-mail no celular.
    """
    from app.blueprints.conferencia import _slugify_city
    dt = item.get('data_atividade')
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt)
        except ValueError:
            dt = None
    periodo = dt.strftime('%Y-%m') if dt else ''
    return '%s/conferencia/%s?atleta=%s&periodo=%s' % (
        base.rstrip('/'), _slugify_city(cidade),
        item.get('strava_id') or '', periodo)


def _quando(item):
    """A data da atividade como datetime, venha ela do SQLite como texto ou nao."""
    dt = item.get('data_atividade')
    if isinstance(dt, str):
        try:
            return datetime.fromisoformat(dt)
        except ValueError:
            return None
    return dt


def montar_email(cidade, itens, base):
    """(assunto, html, texto) — sem tocar em banco, para poder ser testado."""
    linhas_html, linhas_txt = [], []
    for item in itens:
        dt = _quando(item)
        quando = dt.strftime('%d/%m/%Y') if dt else '?'
        km = float(item.get('distancia') or 0)
        modal = 'Pedalada' if item.get('modalidade') == 'ciclismo' else 'Corrida/Caminhada'
        acao = _link_para_resolver(base, cidade, item)
        linhas_html.append(
            '<tr>'
            '<td style="padding:8px 12px;border-bottom:1px solid #eee">%s</td>'
            '<td style="padding:8px 12px;border-bottom:1px solid #eee"><b>%s</b></td>'
            '<td style="padding:8px 12px;border-bottom:1px solid #eee">%.2f km</td>'
            '<td style="padding:8px 12px;border-bottom:1px solid #eee">%s</td>'
            '<td style="padding:8px 12px;border-bottom:1px solid #eee">'
            '<a href="%s">Abrir no Strava</a></td>'
            '<td style="padding:8px 12px;border-bottom:1px solid #eee">'
            '<a href="%s"><b>Conferir e lançar</b></a></td>'
            '</tr>' % (quando, item.get('atleta') or '?', km, modal,
                       item.get('link') or '#', acao))
        linhas_txt.append('  %s — %s — %.2f km (%s)\n     Strava: %s\n     Resolver: %s'
                          % (quando, item.get('atleta') or '?', km, modal,
                             item.get('link') or '-', acao))

    assunto = ('MoveMentes — %d atividade(s) de outro mês chegaram no grupo'
               % len(itens))
    corpo_html = """
    <div style="font-family:Segoe UI,Arial,sans-serif;color:#222">
      <h2 style="margin:0 0 4px">Atividade de outro mês no grupo</h2>
      <p style="color:#555;margin:0 0 16px">Cidade: <b>%s</b></p>
      <p>Alguém postou no grupo o link de uma atividade <b>feita em outro mês</b>.
         Ela <u>não entrou</u> no ranking do mês corrente — e está certo que não
         tenha entrado, porque o que vale é a data da atividade, não a data em
         que o link foi postado.</p>
      <p><b>A decisão é sua.</b> Se o mês daquela atividade ainda estiver aberto,
         dá para lançar. Se já fechou, o fechamento congelou aquele mês — aí a
         escolha é premiar por fora ou deixar como está.</p>
      <table style="border-collapse:collapse;width:100%%;margin:16px 0">
        <tr style="background:#f4f6f8">
          <th style="text-align:left;padding:8px 12px">Data</th>
          <th style="text-align:left;padding:8px 12px">Atleta</th>
          <th style="text-align:left;padding:8px 12px">Distância</th>
          <th style="text-align:left;padding:8px 12px">Tipo</th>
          <th style="text-align:left;padding:8px 12px">Atividade</th>
          <th style="text-align:left;padding:8px 12px">Ação</th>
        </tr>
        %s
      </table>
      <p style="color:#666;font-size:13px">
        Muita gente deixa de postar um feito grande por vergonha. É justamente
        isso que o grupo existe para incentivar — por isso este aviso.
      </p>
    </div>
    """ % (cidade, ''.join(linhas_html))

    corpo_txt = ('Atividade de outro mes no grupo — %s\n\n'
                 'Alguem postou o link de uma atividade feita em OUTRO MES. Ela\n'
                 'nao entrou no ranking do mes corrente, e esta certo: vale a data\n'
                 'da atividade, nao a data do post.\n\n%s\n\n'
                 'Se o mes daquela atividade ainda estiver aberto, da para lancar.\n'
                 'Se ja fechou, o fechamento congelou o mes.\n'
                 % (cidade, '\n'.join(linhas_txt)))

    return assunto, corpo_html, corpo_txt


def avisar_pendentes(cidade):
    """Um e-mail com tudo que chegou de outro mes. Devolve quantas foram avisadas."""
    try:
        ensure_schema()
        itens = _por_avisar(cidade)
    except Exception as exc:
        logger.warning('Nao consegui apurar as atividades fora do periodo: %s', exc)
        return 0
    if not itens:
        return 0

    if not correio.configurado():
        logger.warning('%d atividade(s) fora do periodo em %s, mas o e-mail nao '
                       'esta configurado — ficam registradas.', len(itens), cidade)
        return 0

    from app.alertas import destinatarios, _base_url
    para = destinatarios(cidade)
    if not para:
        logger.warning('%d atividade(s) fora do periodo em %s e nenhum '
                       'destinatario de e-mail.', len(itens), cidade)
        return 0

    assunto, corpo_html, corpo_txt = montar_email(cidade, itens, _base_url())

    # correio.enviar devolve (ok, detalhe) — nunca levanta excecao. Testar o
    # retorno cru daria verdadeiro sempre, inclusive quando o envio falhou.
    enviados, motivos = 0, []
    for email in para:
        ok, detalhe = correio.enviar(email, assunto, corpo_html, corpo_txt)
        if ok:
            enviados += 1
        else:
            motivos.append('%s: %s' % (email, detalhe))
    if not enviados:
        logger.warning('Nao consegui enviar o aviso de atividade fora do '
                       'periodo (%s). As atividades continuam registradas.',
                       '; '.join(motivos) or 'sem motivo')
        return 0

    try:
        agora = datetime.now()
        for item in itens:
            db.session.execute(db.text(
                "UPDATE atividade_fora_periodo SET avisado_em=:a, "
                "situacao='avisado' WHERE id=:i"),
                {'a': agora, 'i': item['id']})
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning('Aviso enviado, mas nao consegui marcar como avisado: %s', exc)

    logger.info('Aviso de %d atividade(s) fora do periodo enviado para %s.',
                len(itens), ', '.join(para))
    return len(itens)
