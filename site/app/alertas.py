# app/alertas.py
# -*- coding: utf-8 -*-
"""Alerta de falha da automacao: registra sempre, avisa por e-mail.

POR QUE ISTO EXISTE
Em 02/08/2026 o ranking das 22h nao saiu e so foi notado por acaso, no dia
seguinte. A falha estava no log — e log ninguem le a tempo. Pior: o unico canal
de alerta era o WhatsApp, que e justamente a peca que costuma estar quebrada
quando a pipeline falha. Avisar pelo WhatsApp que o WhatsApp caiu nao funciona.

O e-mail resolve isso porque nao depende de nada que a rodada usa: sai do
Synology (que fica ligado o tempo todo) pelo Microsoft Graph. Se o Dell dormiu,
se o WAHA morreu, se a sessao caiu — o e-mail sai do mesmo jeito.

REGISTRAR E AVISAR SAO COISAS DIFERENTES
Toda falha entra na tabela, sempre. O e-mail e que tem freio: a mesma etapa nao
gera outro e-mail antes de _JANELA_MINUTOS. Sem esse freio, uma falha que se
repete a cada 15 minutos vira 96 e-mails por dia — e alerta demais ensina a
ignorar alerta, que e o mesmo que nao avisar.
"""
import logging
from datetime import datetime, timedelta

from app import db, correio
from app.models import Usuario

logger = logging.getLogger(__name__)

# Quanto tempo a mesma etapa fica sem gerar novo e-mail.
_JANELA_MINUTOS = 60
# Quantas linhas do log vao no corpo. 30 e o que a pendencia P-04 pediu: cabe
# num e-mail e quase sempre contem a causa.
_MAX_LINHAS_LOG = 30

_SCHEMA_PRONTO = False


def ensure_schema():
    """Cria a tabela se nao existir. Idempotente, roda a cada uso."""
    global _SCHEMA_PRONTO
    if _SCHEMA_PRONTO:
        return
    with db.engine.begin() as conn:
        conn.execute(db.text("""
            CREATE TABLE IF NOT EXISTS alertas_automacao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                etapa VARCHAR(120) NOT NULL,
                erro TEXT,
                origem VARCHAR(120),
                canal VARCHAR(40),
                destinatarios TEXT,
                situacao VARCHAR(30),
                detalhe TEXT,
                criado_em DATETIME
            )
        """))
        conn.execute(db.text(
            "CREATE INDEX IF NOT EXISTS idx_alertas_cidade_etapa "
            "ON alertas_automacao(cidade, etapa, criado_em)"
        ))
    _SCHEMA_PRONTO = True


def destinatarios(cidade):
    """E-mails que devem receber o alerta desta cidade, na ordem de quem responde.

    Ordem: quem publica primeiro (`ordem_envio` 1 = principal, 2 = suplente),
    depois os demais administradores por nivel.

    O supremo da PROPRIA cidade entra normalmente — em Palmeirópolis ele e quem
    publica (`ordem_envio` 1), e tirar o operador da lista de alertas seria o
    contrario do que a P-04 quer. A regra "o supremo e o ultimo recurso"
    (regra 21) trata de nao arrastar o supremo para a cidade DOS OUTROS: por
    isso ele so entra em cidade alheia quando la nao ha ninguem — melhor
    incomodar o supremo do que ninguem ficar sabendo.
    """
    ensure_schema()
    try:
        pessoas = Usuario.query.filter(Usuario.cidade == cidade).all()
    except Exception as exc:
        logger.warning('Nao consegui listar destinatarios de %s: %s', cidade, exc)
        return []

    def _vale(u):
        return bool(u.email) and u.ativo and u.pode_administrar

    def _ordem(u):
        # ordem_envio 0/None significa "nao publica": vai para o fim da fila.
        return ((u.ordem_envio or 99), -u.nivel, u.id)

    da_cidade = sorted([u for u in pessoas if _vale(u)], key=_ordem)
    if da_cidade:
        return [u.email for u in da_cidade]

    # Cidade sem nenhum administrador com e-mail: ai sim o supremo, de onde
    # quer que ele seja, e o ultimo recurso.
    try:
        todos = Usuario.query.filter(Usuario.ativo.is_(True)).all()
    except Exception:
        return []
    return [u.email for u in todos if u.is_supremo and u.email]


def _repetido(cidade, etapa):
    """Ja mandamos e-mail desta mesma etapa ha pouco tempo?"""
    limite = datetime.now() - timedelta(minutes=_JANELA_MINUTOS)
    achado = db.session.execute(db.text(
        "SELECT criado_em FROM alertas_automacao "
        "WHERE cidade=:c AND etapa=:e AND canal='email' AND situacao='enviado' "
        "AND criado_em >= :d ORDER BY id DESC LIMIT 1"
    ), {'c': cidade, 'e': etapa, 'd': limite}).first()
    return bool(achado)


def _base_url():
    """Endereco publico do site. O alerta NUNCA aponta para localhost.

    O alerta antigo mandava abrir http://localhost:<porta>/dashboard — endereco
    que so existe sentado na maquina da automacao. Quem recebe no celular, ou o
    suplente na casa dele, nao tem o que fazer com isso. Regra que fica:
    endereco que o destinatario nao consegue abrir vale o mesmo que endereco
    nenhum.
    """
    try:
        from app.enderecos import base_url
        return base_url()
    except Exception:
        from app.enderecos import PADRAO
        return PADRAO


# CADA FALHA PEDE UMA ACAO DIFERENTE.
# Um alerta que so diz "deu erro" empurra o trabalho de descobrir o que fazer
# para quem esta com o celular na mao, no fim da noite. Aqui cada etapa ja vem
# com o botao certo e uma frase dizendo o que acontece ao clicar.
#
# O caso da sessao merece explicacao no proprio e-mail: a maquina da automacao
# passa o dia desligada, e o admin nao tem como saber que o botao do site LIGA
# a maquina por Wake-on-LAN e guarda o pedido numa fila. Sem essa frase, ele le
# "reconectar o WhatsApp", pensa "a maquina esta desligada, nao adianta", e nao
# clica — que e o mesmo que nao ter alerta.
_ACAO_PADRAO = {
    'botao': 'Abrir o MoveMentes',
    'rota': '/admin',
    'primeiro_passo': 'Conferir o grupo: o ranking pode nao ter sido publicado.',
    'explica': '',
}

_ACOES = {
    # 27/08/2026 — ETAPA NOVA ENTRA AQUI NA MESMA SESSAO (regra 21-J): etapa
    # sem acao cadastrada vira e-mail generico, e generico e o mesmo que vazio.
    # 29/08/2026 — mesmo remedio, nome honesto: quem falhou foi o WhatsApp,
    # nao a conferencia. A chave antiga fica logo abaixo para nao deixar
    # alerta antigo sem acao.
    'WhatsApp perdeu link (a conferencia trouxe)': {
        'botao': 'Ver a sessao do WhatsApp',
        'rota': '/admin/whatsapp-sessao',
        'primeiro_passo': 'O WhatsApp deixou de entregar link, e a '
                          'conferencia foi buscar no Strava. Os km ja estao '
                          'no ranking — ninguem ficou sem.',
        'explica': 'Abra o grupo e olhe o horario do ultimo ranking: se so '
                   'chegou a logo dos patrocinadores, sem o ranking, o sinal '
                   'e forte — vale desconectar e conectar o WhatsApp de novo '
                   'por esta tela. Se voltar a acontecer nos proximos dias, '
                   'e caso de refazer o pareamento.',
    },
    'Conferencia recuperou link': {
        'botao': 'Ver a sessao do WhatsApp',
        'rota': '/admin/whatsapp-sessao',
        'primeiro_passo': 'A conferencia trouxe link que a coleta do grupo nao pegou.',
        'explica': 'Isso costuma querer dizer que o WhatsApp parou de entregar '
                   'mensagem nova. Abra o grupo e olhe o horario do ultimo '
                   'ranking: se so chegou a logo dos patrocinadores, sem o '
                   'ranking, o sinal e forte — vale desconectar e conectar o '
                   'WhatsApp de novo por esta tela. Os km ja foram recuperados; '
                   'ninguem ficou sem.',
    },
    'Sessao WhatsApp': {
        'botao': 'Reconectar o WhatsApp',
        'rota': '/admin/whatsapp-sessao',
        'primeiro_passo': 'O WhatsApp da automacao precisa ser reconectado.',
        'explica': 'Pode clicar mesmo com o computador da automacao desligado: '
                   'o proprio botao manda o sinal que LIGA a maquina pela rede e '
                   'guarda o seu pedido. Quando ela subir, o pedido e atendido '
                   'sozinho e voce e avisado. Nao precisa estar perto dela.',
    },
    'API Health': {
        'botao': 'Abrir o MoveMentes',
        'rota': '/admin',
        'primeiro_passo': 'A automacao nao conseguiu falar com o site.',
        'explica': 'Se o site abrir normalmente para voce, foi queda passageira '
                   'de rede e a proxima rodada resolve sozinha.',
    },
    'Coleta': {
        'botao': 'Ver a automacao',
        'rota': '/admin/automayk',
        'primeiro_passo': 'Nao deu para ler o grupo com seguranca, entao NADA foi '
                          'publicado — nenhum km se perdeu, so ficou para depois.',
        'explica': 'Isso costuma ser sincronizacao do WhatsApp Web ainda em '
                   'andamento. Se repetir na proxima rodada, ai sim vale olhar.',
    },
    'Upload': {
        'botao': 'Ver a automacao',
        'rota': '/admin/automayk',
        'primeiro_passo': 'O site recusou o arquivo da conversa.',
        'explica': '',
    },
    'Processamento': {
        'botao': 'Ver a automacao',
        'rota': '/admin/automayk',
        'primeiro_passo': 'A leitura dos links do Strava nao terminou.',
        'explica': '',
    },
    'Envio': {
        'botao': 'Abrir a Central de Mensagens',
        'rota': '/admin/central',
        'primeiro_passo': 'O ranking foi montado mas NAO chegou ao grupo.',
        'explica': 'O texto ja esta pronto: da para reenviar pela Central sem '
                   'refazer nada.',
    },
    # AS ETAPAS DO VIGIA FALTAVAM AQUI (14/08/2026). Sem entrada, todas caiam no
    # padrao "Conferir o grupo: o ranking pode nao ter sido publicado" — que nao
    # tem nada a ver com fila parada, e mandava o admin olhar o lugar errado.
    # Foi por isso que os e-mails da madrugada de 13 para 14/08 "nao diziam
    # nada": diziam o texto padrao, 40 vezes seguidas.
    'Fila de mensagens': {
        'botao': 'Abrir a Central de Mensagens',
        'rota': '/admin/central',
        'primeiro_passo': 'Mensagens que voce mandou enviar nao sairam. O '
                          'ranking nao tem nada a ver com isto.',
        'explica': 'Na aba "Na fila" voce ve uma por uma: para quem ia, quantas '
                   'vezes tentou e o motivo da ultima falha. Nada precisa ser '
                   'refeito — o que esta la sai assim que o caminho voltar.',
    },
    'Automacao sem sinal': {
        'botao': 'Ver a automacao',
        'rota': '/admin/automayk',
        'primeiro_passo': 'O computador da automacao nao acordou no horario.',
        'explica': 'Se ele estiver desligado na tomada ou sem energia, so ligar '
                   'resolve. A rodada seguinte recupera o que ficou para tras: '
                   'nenhum km se perde, so atrasa.',
    },
    'Automacao quebrada': {
        'botao': 'Reconectar o WhatsApp',
        'rota': '/admin/whatsapp-sessao',
        'primeiro_passo': 'O computador esta ligado, mas o WhatsApp dele nao '
                          'esta pronto para publicar.',
        'explica': 'Pode clicar mesmo longe da maquina: o botao guarda o pedido '
                   'e liga o computador pela rede.',
    },
    'Sessao do Strava': {
        'botao': 'Abrir a Configuracao',
        'rota': '/config',
        'primeiro_passo': 'O Strava recusou o acesso: a conferencia de km e a '
                          'importacao por perfil ficam paradas ate recolar o '
                          'cookie da sessao.',
        'explica': 'O ranking do que ja foi lido continua funcionando; o que '
                   'para e a leitura de coisa nova no Strava.',
    },
    'Sessão sem cópia': {
        'botao': 'Abrir a Sessão do WhatsApp',
        'rota': '/admin/whatsapp-sessao',
        'primeiro_passo': 'A sessão do WhatsApp está conectada, mas sem cópia '
                          'de segurança gravada — do jeito que está, ela morre '
                          'no próximo reinício da máquina e pede o celular de '
                          'novo (não é hoje, mas vale olhar sem pressa).',
        'explica': 'O ranking de agora não é afetado — isto é sobre o que '
                   'acontece na PRÓXIMA vez que a máquina reiniciar.',
    },
    'Vigia sem endereco': {
        'botao': 'Abrir a Sessao do WhatsApp',
        'rota': '/admin/whatsapp-sessao',
        'primeiro_passo': 'Falta cadastrar o endereco do WhatsApp desta cidade, '
                          'entao o site nao consegue vigiar a automacao.',
        'explica': 'Enquanto isso faltar, nenhum aviso sobre a maquina vai '
                   'funcionar — inclusive os que avisariam de uma falha real.',
    },
}

# Etapas em que faz sentido dizer "o ranking pode nao ter saido". As do vigia
# NAO entram: fila parada e sessao do Strava nao tem relacao nenhuma com a
# publicacao, e mandar o admin conferir o grupo a toa gasta a confianca dele no
# alerta seguinte.
_ETAPAS_DO_RANKING = {'Sessao WhatsApp', 'API Health', 'Coleta', 'Upload',
                      'Processamento', 'Envio', 'Automacao sem sinal'}


def acao_da_etapa(etapa):
    """(botao, url, primeiro_passo, explica) para esta etapa."""
    acao = dict(_ACAO_PADRAO)
    acao.update(_ACOES.get((etapa or '').strip(), {}))
    acao['url'] = _base_url() + acao['rota']
    return acao


def _corpo(cidade, etapa, erro, linhas_log, origem, quando):
    """(html, texto) do e-mail. Escrito para o admin, nao para o dev."""
    log_txt = '\n'.join(linhas_log or [])
    acao = acao_da_etapa(etapa)
    texto = (
        'A automacao do MoveMentes falhou.\n\n'
        'Cidade: %s\nQuando: %s\nEtapa: %s\nErro: %s\nMaquina: %s\n\n'
        'O QUE FAZER\n'
        '%s\n\n'
        '%s\n%s\n\n'
        '%s'
        'Ultimas linhas do log:\n%s\n'
    ) % (cidade, quando, etapa, erro, origem or '-',
         acao['primeiro_passo'], acao['botao'].upper() + ':', acao['url'],
         (acao['explica'] + '\n\n') if acao['explica'] else '',
         log_txt or '(sem log)')

    def _e(s):
        return (str(s or '').replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;'))

    # O CABECALHO SEGUE A ETAPA (14/08/2026). Dizer "o ranking pode nao ter
    # sido publicado" num aviso de fila parada manda o admin conferir o grupo,
    # ver o ranking la, concluir que o e-mail estava errado — e ignorar o
    # proximo. Alarme que fala do lugar errado gasta a mesma confianca que
    # alarme falso.
    if (etapa or '').strip() in _ETAPAS_DO_RANKING:
        chamada = ('<p>O ranking pode <strong>nao ter sido publicado</strong>. '
                   'Vale conferir o grupo antes de qualquer coisa.</p>')
    else:
        chamada = ('<p>Isto <strong>nao</strong> afeta o ranking que ja foi '
                   'publicado. O que precisa da sua atencao esta abaixo.</p>')

    html = (
        '<div style="font-family:Segoe UI,Arial,sans-serif;font-size:15px;color:#222">'
        '<h2 style="color:#b23c17;margin:0 0 12px">A automacao pediu atencao</h2>'
        + chamada +
        '<table cellpadding="6" style="border-collapse:collapse;font-size:15px">'
        '<tr><td valign="top"><strong>Cidade</strong></td><td>%s</td></tr>'
        '<tr><td valign="top"><strong>Quando</strong></td><td>%s</td></tr>'
        '<tr><td valign="top"><strong>Etapa</strong></td><td>%s</td></tr>'
        # PRE-WRAP AQUI E O QUE FAZ O DETALHE APARECER. O texto do vigia vem com
        # "O QUE FAZER, nesta ordem: 1) ... 2) ..." em varias linhas; numa celula
        # de tabela comum o HTML come as quebras e tudo vira um paragrafo unico e
        # ilegivel. Era esta a queixa "o e-mail nao da nenhum detalhe": o detalhe
        # estava la, amassado.
        '<tr><td valign="top"><strong>Detalhe</strong></td>'
        '<td style="white-space:pre-wrap;line-height:1.5">%s</td></tr>'
        '<tr><td valign="top"><strong>Maquina</strong></td><td>%s</td></tr>'
        '</table>'
        '<div style="margin:18px 0;padding:16px;background:#f0f7ff;'
        'border-left:4px solid #2b6cb0;border-radius:6px">'
        '<p style="margin:0 0 12px"><strong>O que fazer:</strong> %s</p>'
        '%s'
        '<a href="%s" style="display:inline-block;background:#2b6cb0;'
        'color:#fff;text-decoration:none;padding:12px 22px;border-radius:8px;'
        'font-weight:bold;font-size:15px">%s</a>'
        '</div>'
        '<p style="margin-top:16px"><strong>Ultimas linhas do log:</strong></p>'
        '<pre style="background:#f4f4f4;border:1px solid #ddd;padding:10px;'
        'border-radius:6px;font-size:12px;white-space:pre-wrap">%s</pre>'
        '<p style="color:#666;font-size:13px">Voce recebe este aviso porque '
        'administra esta cidade no MoveMentes. Enquanto a falha se repetir, '
        'um novo e-mail sai no maximo a cada %d minutos.</p>'
        '</div>'
    ) % (_e(cidade), _e(quando), _e(etapa), _e(erro), _e(origem or '-'),
         _e(acao['primeiro_passo']),
         ('<p style="margin:0 0 14px;color:#444">%s</p>' % _e(acao['explica'])
          if acao['explica'] else ''),
         acao['url'], _e(acao['botao']),
         _e(log_txt or '(sem log)'), _JANELA_MINUTOS)
    return html, texto


def registrar_falha(cidade, etapa, erro, linhas_log=None, origem=None,
                    canal=None, detalhe_canal='', so_registrar=False):
    """Registra a falha e, se for o caso, manda o e-mail.

    ``so_registrar=True`` quer dizer: **outro canal ja avisou** (o WhatsApp
    cruzado da tarefa 24). A falha entra no historico do mesmo jeito, mas sem
    e-mail — porque quem precisava saber ja soube, e e-mail repetido de algo ja
    resolvido ensina a ignorar e-mail.

    Registrar e avisar sao coisas diferentes: o registro acontece SEMPRE.

    NUNCA levanta excecao: avisar que algo deu errado nao pode ser mais uma
    coisa que da errado. Se o e-mail nao sair, a linha fica marcada como
    'falhou' com o motivo junto — dai da para descobrir depois por que ninguem
    foi avisado.
    """
    ensure_schema()
    cidade = (cidade or '').strip()
    etapa = (str(etapa or 'desconhecida')).strip()[:120]
    erro = str(erro or '')[:4000]
    linhas = [l for l in (linhas_log or []) if str(l).strip()][-_MAX_LINHAS_LOG:]
    quando = datetime.now().strftime('%d/%m/%Y %H:%M')

    resultado = {'registrado': False, 'email': False, 'repetido': False,
                 'destinatarios': [], 'detalhe': ''}
    canal = (canal or 'email').strip() or 'email'

    def _gravar(canal, situacao, detalhe, para):
        try:
            db.session.execute(db.text(
                "INSERT INTO alertas_automacao "
                "(cidade, etapa, erro, origem, canal, destinatarios, situacao, "
                " detalhe, criado_em) VALUES "
                "(:c, :e, :err, :o, :canal, :dest, :sit, :det, :em)"
            ), {'c': cidade, 'e': etapa, 'err': erro, 'o': (origem or '')[:120],
                'canal': canal, 'dest': ', '.join(para or [])[:500],
                'sit': situacao, 'det': str(detalhe or '')[:500],
                'em': datetime.now()})
            db.session.commit()
            resultado['registrado'] = True
        except Exception as exc:
            db.session.rollback()
            logger.error('Falha ao registrar alerta: %s', exc)

    if so_registrar:
        # Outro canal ja avisou: guarda o fato e sai. Sem e-mail, sem freio.
        _gravar(canal, 'enviado', detalhe_canal or 'avisado por outro canal', [])
        resultado['detalhe'] = detalhe_canal or 'avisado por outro canal'
        return resultado

    try:
        if _repetido(cidade, etapa):
            resultado['repetido'] = True
            resultado['detalhe'] = ('ja houve e-mail desta etapa nos ultimos %d min'
                                    % _JANELA_MINUTOS)
            _gravar('email', 'silenciado', resultado['detalhe'], [])
            return resultado
    except Exception as exc:
        logger.debug('Nao consegui checar repeticao: %s', exc)

    para = destinatarios(cidade)
    resultado['destinatarios'] = para
    if not para:
        resultado['detalhe'] = 'nenhum administrador com e-mail nesta cidade'
        _gravar('email', 'sem_destinatario', resultado['detalhe'], [])
        return resultado

    html, texto = _corpo(cidade, etapa, erro, linhas, origem, quando)
    assunto = '[MoveMentes] Falha na automacao — %s (%s)' % (cidade or '?', etapa)
    try:
        ok, detalhe = correio.enviar(para, assunto, html, texto)
    except Exception as exc:
        ok, detalhe = False, '%s: %s' % (type(exc).__name__, exc)

    resultado['email'] = bool(ok)
    resultado['detalhe'] = str(detalhe or '')
    _gravar('email', 'enviado' if ok else 'falhou', detalhe, para)
    if not ok:
        logger.error('ALERTA NAO ENTREGUE por e-mail (%s / %s): %s',
                     cidade, etapa, detalhe)
    return resultado


def ultimos(cidade=None, limite=50):
    """Historico de alertas, do mais novo para o mais antigo. Para a tela."""
    ensure_schema()
    sql = ("SELECT id, cidade, etapa, erro, origem, canal, destinatarios, "
           "situacao, detalhe, criado_em FROM alertas_automacao ")
    params = {'lim': int(limite)}
    if cidade:
        sql += "WHERE cidade = :c "
        params['c'] = cidade
    sql += "ORDER BY id DESC LIMIT :lim"
    try:
        linhas = db.session.execute(db.text(sql), params).fetchall()
    except Exception as exc:
        logger.warning('Nao consegui ler o historico de alertas: %s', exc)
        return []
    campos = ('id', 'cidade', 'etapa', 'erro', 'origem', 'canal',
              'destinatarios', 'situacao', 'detalhe', 'criado_em')
    return [dict(zip(campos, linha)) for linha in linhas]
