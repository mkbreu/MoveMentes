# -*- coding: utf-8 -*-
"""Link postado no grupo que NAO virou quilometro — avisa o admin.

POR QUE ESTE MODULO EXISTE
Em 15/09/2026 uma pessoa compartilhou pelo aplicativo do Strava o link
`https://strava.app.link/TmnfZ90Ws6b` achando que postava a caminhada dela. O
link abre em `strava.com/athletes/<id>`: e o PERFIL, nao a atividade. O
sistema chamou aquilo de "shortlink nao resolvido" (etapa retentavel, ou seja,
tentaria para sempre) e nao avisou ninguem. Ela ficou fora do ranking em
silencio, e so se descobriu porque o dono reprocessou a conversa na mao.

Vale para qualquer link que NAO tem mais como virar km: link do Strava que nao
e atividade e atividade que o Strava respondeu em definitivo (apagada, privada,
sem distancia). Pendencia retentavel NAO entra aqui — a proxima rodada ainda
pode resolver sozinha, e avisar a cada rodada vira ruido.

O aviso e curto de proposito: link, motivo, quem postou e quando.
"""
import logging
from datetime import datetime

from app import correio, db

logger = logging.getLogger(__name__)

# UM AVISO POR LINK, NAO UM POR RODADA (17/09/2026, pedido do dono).
#
# O mesmo link de perfil gerou 4 e-mails (15/09 23:38, 16/09 05:33, 17/09
# 05:34 e 11:37) — medido em `processed_files` 189 a 192, sempre
# `strava.app.link/TmnfZ90Ws6b`. Ele volta ao TXT de proposito: quando uma
# rodada nao publica, a seguinte faz a "VARREDURA DE SEGURANCA" e rele ate 48 h
# para tras. Atividade repetida o banco descarta; o aviso nao tinha memoria.
# O dono ja tinha resolvido com o atleta na primeira vez.
#
# Agora o link avisado fica guardado por cidade e nao volta a gerar e-mail. A
# pendencia continua na tela do processamento — so o e-mail nao repete. A marca
# so e gravada DEPOIS de o e-mail sair: se o envio falhar, a proxima rodada
# tenta de novo, que e o certo.
_SCHEMA_PRONTO = False


def ensure_schema():
    global _SCHEMA_PRONTO
    if _SCHEMA_PRONTO:
        return
    with db.engine.begin() as conn:
        conn.execute(db.text("""
            CREATE TABLE IF NOT EXISTS link_sem_km_avisado (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade      VARCHAR(120) NOT NULL,
                url         TEXT NOT NULL,
                avisado_em  DATETIME
            )
        """))
        conn.execute(db.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_link_sem_km_unico "
            "ON link_sem_km_avisado(cidade, url)"))
    _SCHEMA_PRONTO = True


def _ja_avisados(cidade, urls):
    """Quais destes links ja foram avisados nesta cidade."""
    if not urls:
        return set()
    ensure_schema()
    marcadores = ', '.join(':u%d' % i for i in range(len(urls)))
    params = {'u%d' % i: u for i, u in enumerate(urls)}
    params['c'] = cidade
    linhas = db.session.execute(db.text(
        'SELECT url FROM link_sem_km_avisado WHERE cidade = :c AND url IN (%s)'
        % marcadores), params).fetchall()
    return {l[0] for l in linhas}


def marcar_avisados(cidade, urls, quando=None):
    """Guarda os links como avisados. Repetir nao duplica (indice unico)."""
    ensure_schema()
    quando = quando or datetime.now()
    for url in urls:
        db.session.execute(db.text(
            'INSERT OR IGNORE INTO link_sem_km_avisado (cidade, url, avisado_em) '
            'VALUES (:c, :u, :q)'), {'c': cidade, 'u': url, 'q': quando})
    db.session.commit()


def _quem(item):
    """Telefone de quem postou e, se der para saber, o nome do atleta."""
    telefone = (item.get('autor') or '').strip()
    if not telefone:
        return 'não identificado'
    try:
        from app.models import Atleta
        from app.telefone import normalizar
        alvo = normalizar(telefone) or telefone
        atleta = Atleta.query.filter_by(telefone_normalizado=alvo).first()
        if atleta:
            nome = atleta.nome_exibicao or atleta.nome_atual
            if nome:
                return '%s (%s)' % (nome, telefone)
    except Exception as exc:  # o nome e um extra: nunca impede o aviso
        logger.debug('Nao achei o atleta pelo telefone %s: %s', telefone, exc)
    return telefone


def montar_email(cidade, itens):
    assunto = 'Link sem km no grupo — %s (%d)' % (cidade, len(itens))

    linhas_html, linhas_txt = [], []
    for item in itens:
        quando = item.get('quando') or '—'
        url = item.get('url') or ''
        motivo = item.get('reason') or 'Sem motivo registrado.'
        linhas_html.append(
            '<tr>'
            '<td style="padding:6px 10px">%s</td>'
            '<td style="padding:6px 10px">%s</td>'
            '<td style="padding:6px 10px">%s</td>'
            '<td style="padding:6px 10px"><a href="%s">abrir</a></td>'
            '</tr>' % (quando, _quem(item), motivo, url)
        )
        linhas_txt.append('- %s | %s | %s | %s'
                          % (quando, _quem(item), motivo, url))

    corpo_html = """
    <div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#222">
      <p><b>%d link(es) postados em %s não viraram quilômetro</b> — e não vão
         virar sozinhos. Vale perguntar à pessoa.</p>
      <table style="border-collapse:collapse;width:100%%;margin:12px 0">
        <tr style="background:#f4f6f8">
          <th style="text-align:left;padding:6px 10px">Quando</th>
          <th style="text-align:left;padding:6px 10px">Quem postou</th>
          <th style="text-align:left;padding:6px 10px">Motivo</th>
          <th style="text-align:left;padding:6px 10px">Link</th>
        </tr>
        %s
      </table>
    </div>
    """ % (len(itens), cidade, ''.join(linhas_html))

    corpo_txt = ('%d link(es) postados em %s nao viraram quilometro e nao vao\n'
                 'virar sozinhos. Vale perguntar a pessoa.\n\n%s\n'
                 % (len(itens), cidade, '\n'.join(linhas_txt)))

    return assunto, corpo_html, corpo_txt


def avisar(cidade, itens):
    """Um e-mail com todos os links da rodada. Devolve quantos foram avisados."""
    itens = [i for i in (itens or []) if i.get('url')]
    if not itens:
        return 0

    # So o que NUNCA foi avisado nesta cidade — e cada link uma vez so, mesmo
    # que apareca repetido na mesma rodada. Se a consulta falhar, na duvida
    # avisa: repetir um e-mail e melhor que calar um link novo (regra 10).
    try:
        ja = _ja_avisados(cidade, sorted({i['url'] for i in itens}))
    except Exception as exc:
        db.session.rollback()
        logger.warning('Nao consegui ver quais links ja foram avisados: %s', exc)
        ja = set()
    vistos, novos = set(), []
    for i in itens:
        if i['url'] in ja or i['url'] in vistos:
            continue
        vistos.add(i['url'])
        novos.append(i)
    if not novos:
        logger.info('%d link(s) sem km em %s ja foram avisados antes — sem '
                    'e-mail novo.', len(itens), cidade)
        return 0
    itens = novos

    if not correio.configurado():
        logger.warning('%d link(s) sem km em %s, mas o e-mail nao esta '
                       'configurado: %s', len(itens), cidade,
                       ', '.join(i.get('url', '') for i in itens))
        return 0

    from app.alertas import destinatarios
    para = destinatarios(cidade)
    if not para:
        logger.warning('%d link(s) sem km em %s e nenhum destinatario de '
                       'e-mail.', len(itens), cidade)
        return 0

    assunto, corpo_html, corpo_txt = montar_email(cidade, itens)

    # correio.enviar devolve (ok, detalhe) e nunca levanta excecao: testar o
    # retorno cru daria verdadeiro ate quando o envio falhou.
    enviados, motivos = 0, []
    for email in para:
        ok, detalhe = correio.enviar(email, assunto, corpo_html, corpo_txt)
        if ok:
            enviados += 1
        else:
            motivos.append('%s: %s' % (email, detalhe))

    if not enviados:
        logger.warning('Nao consegui avisar sobre link sem km (%s). Links: %s',
                       '; '.join(motivos) or 'sem motivo',
                       ', '.join(i.get('url', '') for i in itens))
        return 0

    logger.info('Aviso de %d link(s) sem km enviado para %s.',
                len(itens), ', '.join(para))
    try:
        marcar_avisados(cidade, [i['url'] for i in itens])
    except Exception as exc:
        db.session.rollback()
        logger.warning('Aviso enviado, mas nao consegui guardar que foi avisado '
                       '(pode repetir na proxima rodada): %s', exc)
    return len(itens)
