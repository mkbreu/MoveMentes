# -*- coding: utf-8 -*-
"""API reservada a automacao (WAHA no computador da automacao).

Autenticacao: header ``X-Automation-Token``. O token e gerado na tela
Painel > Automacao e fica no cofre (instance/secrets.json), guardado por
cidade — e o proprio token que diz a qual cidade o request pertence, entao
o orquestrador nao precisa enviar cidade nenhuma. As variaveis de ambiente
AUTOMATION_API_TOKEN + AUTOMATION_CIDADE continuam aceitas como alternativa
para operacao por env.

Endpoints (todos JSON):
- GET  /api/health              -> disponibilidade do app + banco
- POST /api/upload              -> TXT/ZIP da conversa; inicia job assincrono
- GET  /api/upload/<job_id>     -> polling do job (stage done/error/cancelled)
- GET  /api/ranking/latest      -> ranking do periodo + texto + patrocinadores
- GET  /api/ranking/annual      -> ranking anual consolidado
"""
import hmac
import json
import os
import uuid
import zipfile
from datetime import datetime

from flask import Blueprint, current_app, g, jsonify, request, url_for
from sqlalchemy import text as sa_text
from werkzeug.utils import secure_filename

from app import db, secretstore
from app.models import Config, MonthlyBackup
from app.blueprints.ranking import (
    _nomes_patrocinadores, _nomes_patrocinadores_do_ano,
    _patrocinadores_adicionais, _patrocinadores_periodo, _sponsor_principal,
    _slugify_city, _legenda_completa, apoio_da_vez,
    build_yearly_ranking_data, format_yearly_ranking_text,
    rebuild_monthly_backup,
)
from app.blueprints.upload import _job_progress, _process_saved_zip, _read_progress_file, _set_progress

api_bp = Blueprint('api', __name__, url_prefix='/api')


def _cidade_api():
    return getattr(g, 'automation_cidade', '') or ''


def _candidatos_token():
    """Pares (token, cidade) aceitos, do cofre e do ambiente."""
    candidatos = []
    cidade_env = (os.environ.get('AUTOMATION_CIDADE') or '').strip()
    token_env = (os.environ.get('AUTOMATION_API_TOKEN') or '').strip()
    if token_env:
        candidatos.append((token_env, cidade_env))
    for escopo, valor in secretstore.entries(secretstore.AUTOMATION_TOKEN):
        candidatos.append((valor, (escopo or cidade_env or '').strip()))
    return candidatos


# Rotas que o token do ORQUESTRADOR alcanca. Sao so as da fila de vinculacao
# do WhatsApp: nenhum dado de atleta, ranking ou cidade passa por aqui.
_ROTAS_DO_ORQUESTRADOR = {'api.waha_pendentes', 'api.waha_marcar', 'api.health'}


@api_bp.before_request
def _validar_token_api():
    recebido = request.headers.get('X-Automation-Token', '')
    g.automation_global = False

    # A IMAGEM DA FILA PASSA COM ASSINATURA NA URL, SEM CABECALHO (14/08/2026).
    #
    # Quem baixa esta imagem e o WAHA, e ele baixa sozinho: nos so passamos a
    # URL. Nao ha como mandar `X-Automation-Token` nesse pedido. Ate hoje ele
    # levava 401, entregava o JSON de erro ao WhatsApp como se fosse foto, e a
    # mensagem inteira morria em HTTP 500 — foi o que travou a fila a noite
    # toda de 13 para 14/08.
    #
    # A protecao continua existindo, so mudou de lugar: assinatura da
    # SECRET_KEY amarrada ao id da mensagem (`filamensagens.token_imagem`).
    # Sem ela, o 401 de sempre.
    # A JANELA DE PUBLICACAO PASSA SEM TOKEN (20/09/2026, P-139). Quem pergunta
    # e' o Agendador do Windows ANTES de ligar o WSL — e o token mora dentro do
    # WSL. A resposta e' so "e' hora?" e qual horario esta valendo: menos
    # informacao do que qualquer um tira olhando a hora do ranking no grupo.
    if request.endpoint == 'api.janela_publica':
        g.automation_cidade = ''
        return None

    if request.endpoint == 'api.fila_imagem':
        from app import filamensagens as _fila
        try:
            esperado = _fila.token_imagem(request.view_args.get('mensagem_id'))
        except Exception:
            esperado = None
        if esperado and hmac.compare_digest(
                request.args.get('t', ''), esperado):
            g.automation_cidade = ''
            return None

    global_token = (secretstore.get(secretstore.AUTOMATION_TOKEN_GLOBAL) or '').strip()
    if global_token and hmac.compare_digest(recebido, global_token):
        if request.endpoint not in _ROTAS_DO_ORQUESTRADOR:
            return jsonify(
                error='Este token e do orquestrador: ele so acessa a fila de '
                      'vinculacao do WhatsApp. Use o token da cidade para o resto.'
            ), 403
        g.automation_global = True
        g.automation_cidade = ''
        return None

    candidatos = _candidatos_token()
    if not candidatos:
        return jsonify(error='Token de automacao nao configurado. '
                             'Gere um em Painel > Automacao.'), 503
    cidade_autorizada = None
    for valor, cidade in candidatos:
        if hmac.compare_digest(recebido, valor):
            cidade_autorizada = cidade
            break
    if cidade_autorizada is None:
        return jsonify(error='Token de automacao invalido.'), 401
    if not cidade_autorizada:
        return jsonify(error='Cidade da automacao nao definida para este token. '
                             'Gere o token em Painel > Automacao ou defina AUTOMATION_CIDADE.'), 503
    g.automation_cidade = cidade_autorizada


@api_bp.route('/health')
def health():
    """Checagem leve de disponibilidade, para a automacao validar antes do fluxo."""
    banco = 'ok'
    try:
        db.session.execute(sa_text('SELECT 1'))
    except Exception as exc:
        banco = 'erro: ' + type(exc).__name__
    cidade = _cidade_api()
    payload = {
        'status': 'ok' if banco == 'ok' else 'degraded',
        'cidade': cidade,
        'banco': banco,
        'hora_servidor': datetime.now().isoformat(timespec='seconds'),
    }

    # SESSAO RECEM-PAREADA NAO SERVE PARA COLETA (P-38, regra 18).
    # Vai aqui porque a pipeline JA chama /api/health na etapa 1, antes de
    # ler o grupo — dizer isso depois da coleta nao adiantaria nada. Quem
    # guarda a regra e o site; a automacao pergunta em vez de manter uma
    # segunda copia dela (mesma ideia do /api/alerta/acao).
    if cidade:
        try:
            from app import whatsapp as wa
            payload['pareamento'] = wa.pareamento_recente(cidade)
        except Exception as exc:
            current_app.logger.warning(
                'Nao consegui informar o pareamento de %s: %s', cidade, exc)

    return jsonify(payload), (200 if banco == 'ok' else 503)


def _periodo_api():
    value = (request.values.get('periodo') or '').strip()
    if not value:
        now = datetime.now()
        return now.year, now.month
    try:
        year_s, month_s = value.split('-', 1)
        year, month = int(year_s), int(month_s)
    except (ValueError, AttributeError):
        raise ValueError('Periodo deve estar no formato AAAA-MM.')
    if year < 2009 or not 1 <= month <= 12:
        raise ValueError('Periodo invalido.')
    return year, month


def _sponsors_payload(cidade, year, month):
    """O patrocinador DA VEZ (07/09/2026), pronto para o WAHA.

    ANTES mandava TODOS os patrocinadores do periodo juntos -- e' por isso
    que o WhatsApp recebia 3 logos na mesma publicacao (o rodizio por peso
    so' filtrava o TEXTO do ranking, nunca a lista de imagens). Agora usa o
    mesmo `apoio_da_vez` que decide o texto: texto e logo sempre saem do
    mesmo sorteio, nunca separados. Lista vazia quando ninguem esta
    escalado para esta rodada (nao ha' patrocinador nenhum no periodo).

    `nome` e o nome de EXIBICAO (o que o publico ve); `nome_interno` vai
    junto para quem precisa casar com o cadastro. O item traz a imagem
    (logo_url, None se este patrocinador nao tem logo cadastrada -- quem
    envia ja sabe mandar so' o texto nesse caso) e a legenda pronta
    (legenda_completa) para virar mensagem.
    """
    items = []
    vencedor = apoio_da_vez(cidade, year, month)
    for sponsor in ([vencedor] if vencedor else []):
        if sponsor.is_principal:
            logo = url_for('ranking.public_sponsor_logo',
                           city_slug=_slugify_city(cidade), year=year, month=month,
                           _external=True)
        else:
            logo = url_for('ranking.public_extra_sponsor_logo',
                           sponsor_id=sponsor.id, _external=True)
        items.append({
            'nome': sponsor.exibicao,
            'nome_interno': sponsor.patrocinador_nome,
            'categoria': sponsor.categoria or 'adicional',
            'telefone': sponsor.telefone,
            'bordao': sponsor.bordao,
            'legenda': sponsor.legenda,
            'legenda_completa': _legenda_completa(sponsor),
            'tem_logo': bool(sponsor.patrocinador_logo_blob),
            'logo_url': logo if sponsor.patrocinador_logo_blob else None,
        })
    return items


@api_bp.route('/config/envio')
def config_envio():
    """PARA ONDE A CIDADE PUBLICA AGORA: grupo ou número de teste (P-27).

    Quem manda é o site, não o `.env` do Dell — assim o admin liga e desliga o
    modo teste por um botão, sem editar arquivo em máquina nenhuma.

    FALHA FECHADA, DE PROPÓSITO. Se esta rota não responder, a pipeline
    **mantém o ensaio**: o pior desfecho de uma falha de comunicação tem de ser
    "não publicou no grupo", nunca "publicou sem querer". Não existe
    despublicar (regra 16-C), e o grupo tem 80 pessoas (regra 16-J).
    """
    import re
    cidade = _cidade_api()
    if not cidade:
        return jsonify({'erro': 'cidade nao identificada'}), 401
    from app import secretstore
    ligado = (secretstore.get(secretstore.MODO_TESTE, scope=cidade) or '').strip().lower()
    destino = (secretstore.get(secretstore.MODO_TESTE_DESTINO, scope=cidade) or '').strip()

    # NUNCA CONFIGURADO NAO E "DESLIGADO" — e `null`, e a pipeline mantem o
    # `.env`. Descoberto testando, em 14/08: com o cofre vazio esta rota
    # devolvia `false`, a pipeline limpava o DESTINO_ENSAIO e a rodada seguinte
    # publicaria no grupo de 80 pessoas sem ninguem ter pedido. Sair do ensaio
    # tem de ser um clique deliberado, nunca a ausencia de um registro.
    if ligado not in ('sim', 'nao'):
        modo = None
    else:
        modo = (ligado == 'sim')

    return jsonify({
        'cidade': cidade,
        'modo_teste': modo,
        'destino_teste': re.sub(r'\D', '', destino),
        # GRUPO DE TESTE: em ensaio, o que e de grupo vai para ca em vez de
        # virar mensagem privada. Vazio = comportamento antigo.
        'grupo_teste': (secretstore.get(secretstore.MODO_TESTE_GRUPO,
                                        scope=cidade) or '').strip(),
        'grupo_real': (secretstore.get(secretstore.WHATSAPP_GRUPO,
                                       scope=cidade) or '').strip(),
        'hora_servidor': datetime.now().isoformat(timespec='seconds'),
    })


@api_bp.route('/upload', methods=['POST'])
def upload_automacao():
    """Recebe TXT (ou ZIP) preparado pela automacao e inicia o processamento assincrono."""
    arquivo = request.files.get('arquivo')
    if not arquivo or not arquivo.filename:
        return jsonify(error='Campo arquivo obrigatorio.'), 400
    try:
        year, month = _periodo_api()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    ext = os.path.splitext(secure_filename(arquivo.filename))[1].lower()
    if ext not in ('.txt', '.zip'):
        return jsonify(error='Envie um TXT ou ZIP contendo TXT.'), 400
    cidade = _cidade_api()
    job_id = str(uuid.uuid4())
    folder = current_app.config['UPLOAD_FOLDER_ZIP']
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f'automacao_{job_id}.zip')
    try:
        if ext == '.zip':
            arquivo.save(path)
        else:
            content = arquivo.read()
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as package:
                package.writestr('conversa.txt', content)
        _set_progress(job_id, 'upload', 'Arquivo recebido pela automacao.', 10, os.path.basename(path), cidade)
        # ESTE E' O CAMINHO DO RANKING (27/08/2026, regra do dono). A saida do
        # ranking e' prioridade: o upload de atleta nao pode causar falha nela,
        # entao este trabalho ATRAVESSA a trava de volume. Ele conta o que
        # gasta, mas nao e' barrado. Se o STRAVA barrar, ai e' bloqueio de
        # verdade -- identificado, comprovado e avisado, e o ranking nao sai
        # (regra 8).
        return _process_saved_zip(path, os.path.basename(path), cidade, None, False, job_id,
                                  periodos={(year, month)}, throttle_profile='periodo_especifico',
                                  prioridade='ranking')
    except Exception as exc:
        db.session.rollback()
        return jsonify(error=f'Falha ao iniciar processamento: {exc}'), 500


@api_bp.route('/upload/<job_id>')
def upload_status_automacao(job_id):
    job = _job_progress.get(job_id) or _read_progress_file(job_id)
    if not job or job.get('cidade') != _cidade_api():
        return jsonify(error='Job nao encontrado.'), 404
    return jsonify(job)


def _resposta_do_periodo(cidade, periodo, motivo):
    """A resposta com o ranking do trimestre/semestre/ano, no MESMO formato.

    Mesmo formato de proposito: a automacao ja sabe ler `ranking`,
    `texto_whatsapp` e `mensagens`, e as travas dela (ranking zerado, impressao
    digital dos km) continuam valendo sobre este conteudo.

    Devolve None quando nao consegue montar — e ai o ranking do mes sai como
    sempre, que e' o desfecho seguro.
    """
    from app.blueprints.ranking import (
        build_periodo_ranking_data, format_periodo_ranking_text,
        format_periodo_partes, _nomes_patrocinadores_do_ano,
        link_publico_do_periodo, ROTULO_PERIODO,
    )
    from app import rankingperiodo
    agora = datetime.now()
    try:
        dados = build_periodo_ranking_data(cidade, periodo, agora)
        vazio = all(float((dados.get(s) or {}).get('total_km') or 0) <= 0
                    for s in ('corrida_caminhada', 'ciclismo'))
        if vazio:
            # RANKING VAZIO NAO VAI AO GRUPO (regra 5). Nem se ofereceu: a
            # automacao barraria, mas ai o dia ja teria sido marcado como
            # servido e o mensal nao sairia tambem.
            current_app.logger.warning(
                'ranking do periodo (%s/%s): sem km, fica com o mensal',
                cidade, periodo)
            return None
        patrocinadores = _nomes_patrocinadores_do_ano(cidade, agora.year)
        config = Config.query.filter_by(cidade=cidade).first()
        # O LINK ABRE NO PERIODO DAQUELE RANKING (pedido do dono): quem clica
        # num ranking do trimestre quer ver o trimestre dele, nao o mes.
        # E E' PUBLICO (P-137-c, 25/09/2026): era `/ranking/periodo/<p>`,
        # que pede senha — o atleta caia na tela de login.
        painel = None
        try:
            painel = link_publico_do_periodo(cidade, periodo, agora)
        except Exception as exc:
            current_app.logger.warning(
                'ranking do periodo: sem link publico (%s)', exc)
            painel = None
        texto = format_periodo_ranking_text(dados, patrocinadores,
                                            dashboard_url=painel)
        mensagens = []
        try:
            mensagens = format_periodo_partes(dados, patrocinadores,
                                              dashboard_url=painel)
        except Exception as exc:
            current_app.logger.warning(
                'ranking do periodo: nao consegui partir em mensagens (%s)', exc)
        if not mensagens:
            mensagens = [texto]
        rankingperiodo.marcar_servido(cidade, periodo, agora)
        try:
            secretstore.set_value(secretstore.RANKING_SERVIDO_EM,
                                  agora.isoformat(timespec='seconds'),
                                  scope=cidade, updated_by='api/ranking-periodo')
        except Exception:
            pass
        current_app.logger.warning(
            'RANKING DO PERIODO servido para a automacao: %s / %s (%s)',
            cidade, periodo, motivo)
        return jsonify(
            cidade=cidade,
            periodo='%04d-%02d' % (agora.year, agora.month),
            tipo_ranking=periodo,
            rotulo=ROTULO_PERIODO.get(periodo, periodo),
            ranking=dados,
            texto_whatsapp=texto,
            mensagens=mensagens,
            patrocinadores=_sponsors_payload(cidade, agora.year, agora.month),
        )
    except Exception as exc:
        current_app.logger.error(
            'ranking do periodo (%s/%s) falhou: %s', cidade, periodo, exc,
            exc_info=True)
        return None


@api_bp.route('/ranking/latest')
def ranking_latest():
    try:
        year, month = _periodo_api()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    cidade = _cidade_api()
    # Cidade do token pode nao existir mais (ex.: cidade renomeada e token
    # antigo). Sem esta guarda, rebuild_monthly_backup levantaria RuntimeError
    # e a API responderia 500 em vez de um erro claro.
    if not Config.query.filter_by(cidade=cidade).first():
        return jsonify(
            error=f'A cidade do token ("{cidade}") nao existe mais. '
                  'Gere um token novo em Painel > Automacao.'
        ), 409
    # ===== O RANKING DESTA PASSADA E' O DO PERIODO? (P-137-b, 20/09/2026) =====
    # Decisao do dono: numa passada sai o mensal OU o do periodo, nunca os
    # dois. Quem escolhe e' o site, aqui — a automacao publica o que vier e
    # nao muda uma linha. `periodo_da_passada` nunca levanta excecao: na
    # duvida devolve None, e o ranking do mes sai como sempre.
    try:
        from app import rankingperiodo
        _periodo, _motivo = rankingperiodo.periodo_da_passada(cidade)
    except Exception as _exc:
        _periodo, _motivo = None, 'erro: %s' % _exc
        current_app.logger.error('ranking do periodo: %s', _exc)
    if _periodo:
        resposta = _resposta_do_periodo(cidade, _periodo, _motivo)
        if resposta is not None:
            return resposta

    backup = MonthlyBackup.query.filter_by(cidade=cidade, ano=year, mes=month).first()
    if not backup:
        rebuild_monthly_backup(cidade, month, year)
        backup = MonthlyBackup.query.filter_by(cidade=cidade, ano=year, mes=month).first()
    if not backup:
        return jsonify(error='Ranking indisponivel.'), 404
    config = Config.query.filter_by(cidade=cidade).first()

    # O ranking inteiro numa mensagem so vira um paredao no WhatsApp (aparece
    # "Ler mais" e quem esta no fim da lista nem e visto). Para a automacao vao
    # tambem as 3 partes prontas: corrida, ciclismo e fechamento. O
    # texto_whatsapp continua igual, para quem ainda copia e cola.
    mensagens = []
    try:
        from app.blueprints.ranking import (
            format_ranking_partes, _get_or_create_short_slug,
            _append_query_param, BASE_URL_ATUAL,
        )
        dados_ranking = backup.get_ranking_data()
        if config and dados_ranking:
            try:
                slug = _get_or_create_short_slug(cidade, year, month)
                painel = _append_query_param(f'{BASE_URL_ATUAL()}/r/{slug}', 'include_sponsor', '0')
            except Exception:
                painel = None
            mensagens = format_ranking_partes(dados_ranking, config, dashboard_url=painel)
    except Exception as exc:
        current_app.logger.warning('Falha ao montar o ranking em partes: %s', exc)

    # ANOTA QUE O RANKING FOI ENTREGUE (P-139, 20/09/2026): e' isto que diz
    # ao vigia que a rodada daquela janela ja aconteceu, e por isso ele nao
    # pede uma segunda por cima. Falhar aqui nao pode atrapalhar a resposta.
    try:
        secretstore.set_value(secretstore.RANKING_SERVIDO_EM,
                              datetime.now().isoformat(timespec='seconds'),
                              scope=cidade, updated_by='api/ranking-latest')
    except Exception as _exc:
        current_app.logger.debug('nao consegui anotar a entrega do ranking: %s', _exc)

    return jsonify(
        cidade=cidade, periodo=f'{year:04d}-{month:02d}', ranking=backup.get_ranking_data(),
        texto_whatsapp=backup.texto_whatsapp or '',
        mensagens=mensagens,
        patrocinadores=_sponsors_payload(cidade, year, month),
    )


@api_bp.route('/strava-sessao', methods=['GET', 'POST'])
def strava_sessao_api():
    """Gestao remota da sessao do Strava pela automacao.

    GET: situacao atual (configurado + ultima verificacao). Nunca devolve o valor.
    POST {"cookie": "..."}: valida e grava um cookie novo para a cidade do token.
    A propria aplicacao ja rotaciona o cookie a cada uso autenticado; este
    endpoint existe para a automacao reinjetar uma sessao nova quando ela for
    revogada de vez (logout/troca de senha).
    """
    cidade = _cidade_api()
    from app.blueprints.processing import (
        STRAVA_SESSION_COOKIE_NAME, validate_strava_session_cookie,
    )
    if request.method == 'GET':
        dados = secretstore.info(secretstore.STRAVA_SESSION, scope=cidade)
        saude = secretstore.session_health(cidade)
        return jsonify(
            cidade=cidade,
            configurado=bool(dados.get('configurado')),
            impressao=dados.get('impressao') or '',
            atualizado_em=dados.get('atualizado_em'),
            saude=saude,
        )

    corpo = request.get_json(silent=True) or {}
    valor = (corpo.get('cookie') or '').strip()
    if '=' in valor and valor.lower().startswith(STRAVA_SESSION_COOKIE_NAME.lower()):
        valor = valor.split('=', 1)[1]
    valor = valor.split(';', 1)[0].strip().strip('"').strip("'")
    if not valor:
        return jsonify(error='Campo "cookie" obrigatorio.'), 400
    ok, mensagem, _athlete_id = validate_strava_session_cookie(valor)
    if not ok:
        secretstore.record_session_health(cidade, False, mensagem)
        return jsonify(error=f'Cookie recusado pelo Strava: {mensagem}'), 422
    if not secretstore.set_value(secretstore.STRAVA_SESSION, valor,
                                 scope=cidade, updated_by='automacao'):
        return jsonify(error='Falha ao gravar no cofre.'), 500
    secretstore.record_session_health(cidade, True, mensagem)
    return jsonify(ok=True, mensagem=mensagem)


@api_bp.route('/parabens')
def parabens_pendentes():
    """Parabens de meta batida do periodo, prontos para o WAHA disparar.

    Retorna cada medalhista com a mensagem pronta, a data em que bateu a meta
    (ordenado por ela) e se ja foi enviado. O orquestrador dispara os pendentes
    e confirma via POST /api/parabens/marcar para nao repetir.
    """
    try:
        year, month = _periodo_api()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    cidade = _cidade_api()
    from app.blueprints.ranking import (
        _ensure_schema_once, process_ranking, _datas_meta_batida,
        _build_goal_messages, _nomes_patrocinadores,
    )
    from app.models import Activity
    from sqlalchemy import extract as _extract
    _ensure_schema_once()
    config = Config.query.filter_by(cidade=cidade).first()
    if not config:
        return jsonify(error='Cidade sem configuracao.'), 404
    activities = Activity.query.filter(
        Activity.cidade == cidade,
        _extract('month', Activity.data_hora) == month,
        _extract('year', Activity.data_hora) == year,
    ).all()
    ranking_data = process_ranking(activities, config, month, year)
    datas_meta = _datas_meta_batida(activities, config, month, year)
    itens = _build_goal_messages(
        ranking_data, cidade=cidade, year=year, month=month, datas_meta=datas_meta,
    )
    somente_pendentes = request.args.get('pendentes', '1') not in ('0', 'false', 'no')
    itens = [it for it in itens if not (somente_pendentes and it['enviado'])]

    # Telefone do atleta para o WAHA marcar a pessoa (@) na mensagem de
    # parabens. A chave do ranking e o strava_id quando ele e valido; quando
    # nao e ("name:<slug>") nao ha como cruzar e o parabens sai sem marcacao.
    telefones = {}
    chaves = [it['chave_atleta'] for it in itens
              if it['chave_atleta'] and not str(it['chave_atleta']).startswith('name:')]
    if chaves:
        from app.models import Atleta
        for atleta in Atleta.query.filter(Atleta.strava_id.in_(chaves)).all():
            if atleta.telefone_normalizado:
                telefones[str(atleta.strava_id)] = atleta.telefone_normalizado

    payload = []
    for it in itens:
        telefone = telefones.get(str(it['chave_atleta']))
        payload.append({
            'chave_atleta': it['chave_atleta'],
            'nome': it['nome'],
            'modalidade': it['section_id'],
            'modalidade_label': it['modalidade'],
            'mensagem': it['message'],
            'telefone': telefone,
            'data_meta': it['data_meta'].isoformat() if it['data_meta'] else None,
            'enviado': it['enviado'],
        })
    return jsonify(cidade=cidade, periodo=f'{year:04d}-{month:02d}', parabens=payload)


@api_bp.route('/parabens/marcar', methods=['POST'])
def parabens_marcar():
    """Confirma que um parabens foi enviado (idempotente)."""
    try:
        year, month = _periodo_api()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    cidade = _cidade_api()
    dados = request.get_json(silent=True) or {}
    chave = (dados.get('chave_atleta') or '').strip()
    modalidade = (dados.get('modalidade') or '').strip()
    if not chave or modalidade not in ('corrida_caminhada', 'ciclismo'):
        return jsonify(error='chave_atleta e modalidade (corrida_caminhada|ciclismo) obrigatorios.'), 400
    from app.blueprints.ranking import _ensure_schema_once
    _ensure_schema_once()
    try:
        db.session.execute(
            sa_text("INSERT OR REPLACE INTO parabens_enviados "
                    "(cidade, ano, mes, chave_atleta, modalidade, enviado_em, enviado_por) "
                    "VALUES (:c, :a, :m, :k, :mod, :em, :por)"),
            {'c': cidade, 'a': year, 'm': month, 'k': chave, 'mod': modalidade,
             'em': datetime.now(), 'por': 'automacao'},
        )
        db.session.commit()
        return jsonify(ok=True)
    except Exception as exc:
        db.session.rollback()
        return jsonify(error=f'Falha ao marcar: {exc}'), 500


_ESTADO_READY = False
_ESTADO_CAMPOS = {
    'last_ts': int,             # timestamp da ultima mensagem ja processada
    'last_id': str,
    'ranking_hash': str,        # impressao dos km da ultima publicacao
    'ranking_enviado_em': float,  # epoch do ultimo ranking publicado
}


def _ensure_estado_schema():
    """Cria a tabela do estado da automacao na primeira chamada."""
    global _ESTADO_READY
    if _ESTADO_READY:
        return
    with db.engine.begin() as conn:
        conn.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS automacao_estado (
                cidade VARCHAR(100) PRIMARY KEY,
                last_ts INTEGER NOT NULL DEFAULT 0,
                last_id VARCHAR(200) DEFAULT '',
                ranking_hash VARCHAR(64) DEFAULT '',
                ranking_enviado_em REAL NOT NULL DEFAULT 0,
                atualizado_por VARCHAR(60) DEFAULT '',
                atualizado_em DATETIME
            )
        """))
        # visto_em e SINAL DE VIDA, nao gravacao de estado. A diferenca importa:
        # uma rodada que nao teve novidade (ranking igual, grupo parado) nao
        # grava campo nenhum — entao "atualizado_em" fica velho mesmo com a
        # automacao funcionando. O vigia (P-35) leria isso como "a maquina nao
        # acordou" e mandaria alerta falso. Medido em 05/08: a rodada das 22:00
        # rodou e o estado continuou marcado 17:00.
        colunas = [linha[1] for linha in
                   conn.execute(sa_text('PRAGMA table_info(automacao_estado)'))]
        if 'visto_em' not in colunas:
            conn.execute(sa_text(
                'ALTER TABLE automacao_estado ADD COLUMN visto_em DATETIME'))

        # ATE QUANDO A AUTOMACAO FICA DE PE (09/09/2026, pedido do dono).
        #
        # Isto NAO e' credencial, entao nao mora no cofre: o cofre guarda o que
        # nao pode ir para o Git, e virou lixeira de estado. Fica aqui, junto do
        # resto do estado da automacao, que ja e' por cidade e entra no backup.
        #
        # NAO CONFUNDIR COM `manter_ligada`, que e' outra coisa: aquele mantem
        # o COMPUTADOR ligado; este mantem os CONTAINERS da aplicacao de pe. O
        # outro servico nao entra na conta -- ele fica de pe enquanto a maquina
        # estiver ligada, porque alguem pode estar usando-o enquanto
        # trabalha, e isso nao tem hora.
        #
        # Prazo em epoch; 0 = nada pedido. Valor especial -1 = "ate eu mandar
        # parar" (sem prazo), para o operador poder trabalhar sem relogio.
        if 'containers_ate' not in colunas:
            conn.execute(sa_text(
                'ALTER TABLE automacao_estado ADD COLUMN containers_ate REAL NOT NULL DEFAULT 0'))
        if 'containers_por' not in colunas:
            conn.execute(sa_text(
                "ALTER TABLE automacao_estado ADD COLUMN containers_por VARCHAR(120) DEFAULT ''"))
    _ESTADO_READY = True


def _janela_containers(cidade):
    """Ate quando os containers da aplicacao devem ficar de pe, e por quem.

    Devolve sempre dicionario (vazio quando nada foi pedido), para poder ser
    despejado na resposta sem quebrar quem nao conhece os campos.
    """
    from datetime import datetime as _dt
    try:
        linha = db.session.execute(sa_text(
            'SELECT containers_ate, containers_por FROM automacao_estado '
            'WHERE cidade = :c'), {'c': cidade}).fetchone()
        if not linha or not linha[0]:
            return {}
        ate = float(linha[0])
        if ate == -1:
            return {'containers_ate': -1, 'containers_por': linha[1] or '',
                    'containers_ativo': True, 'containers_texto': 'ate mandarem parar'}
        agora = _dt.now().timestamp()
        if ate <= agora:
            return {'containers_ate': ate, 'containers_por': linha[1] or '',
                    'containers_ativo': False, 'containers_texto': 'prazo vencido'}
        faltam = int((ate - agora) / 60)
        return {'containers_ate': ate, 'containers_por': linha[1] or '',
                'containers_ativo': True,
                'containers_texto': 'faltam %d minuto(s)' % faltam}
    except Exception:
        return {}


def _copiado_manual(cidade):
    """Quando o ranking foi copiado na tela para colar no grupo, e por quem.

    Devolve sempre um dicionario (vazio se nunca houve copia), para poder ser
    despejado direto na resposta sem quebrar quem nao conhece os campos.
    """
    from app import secretstore
    try:
        bruto = (secretstore.get('ranking_copiado', scope=cidade) or '').strip()
        if not bruto:
            return {}
        dados = json.loads(bruto)
        return {'copiado_em': dados.get('copiado_em') or '',
                'copiado_por': dados.get('por') or '',
                'copiado_periodo': dados.get('periodo') or ''}
    except Exception:
        return {}


@api_bp.route('/automacao/estado', methods=['GET', 'POST'])
def automacao_estado():
    """Memoria compartilhada entre os ambientes que rodam a automacao.

    O Dell faz dual boot: ora publica pelo WSL/Windows, ora pelo Xpenology.
    Cada um tem sessao propria do WhatsApp (e assim deve ser — reaproveitar a
    mesma sessao nos dois faz o WhatsApp derrubar o vinculo), mas os dois
    precisam concordar sobre DUAS coisas para nao publicarem em duplicidade:

      - last_ts / last_id  -> ate onde a conversa ja foi processada
      - ranking_hash       -> impressao dos km da ultima publicacao
      - ranking_enviado_em -> quando o ranking saiu pela ultima vez

    Guardar isso aqui (DS215j, sempre ligado) dispensa sincronizar banco ou
    arquivo entre os sistemas: quem liga primeiro pergunta, quem publica avisa.

    GET  -> estado atual da cidade do token
    POST -> grava os campos enviados (merge; os ausentes ficam como estao)
    """
    _ensure_estado_schema()
    cidade = _cidade_api()

    if request.method == 'GET':
        linha = db.session.execute(
            sa_text('SELECT last_ts, last_id, ranking_hash, ranking_enviado_em, '
                    'atualizado_por, atualizado_em FROM automacao_estado WHERE cidade = :c'),
            {'c': cidade},
        ).fetchone()
        if not linha:
            return jsonify(cidade=cidade, last_ts=0, last_id='', ranking_hash='',
                           ranking_enviado_em=0, atualizado_por='', atualizado_em=None,
                           novo=True)
        return jsonify(
            cidade=cidade, last_ts=linha[0], last_id=linha[1] or '',
            ranking_hash=linha[2] or '', ranking_enviado_em=linha[3] or 0,
            atualizado_por=linha[4] or '', atualizado_em=str(linha[5]) if linha[5] else None,
            novo=False,
            # PUBLICACAO MANUAL TAMBEM CONTA (09/09/2026). Quem copia o ranking
            # na tela e cola no grupo publicou de verdade -- mas isso nao mexia
            # em `ranking_enviado_em`, e o lembrete do Windows acordava o dono
            # dizendo que o ranking nao tinha saido horas depois de ele mesmo
            # ter publicado. Vai como campo PROPRIO, e nao somado ao da
            # automacao: copiar e' intencao, publicar e' fato, e quem confunde
            # os dois troca um alarme falso por um silencio falso.
            **_copiado_manual(cidade),
            **_janela_containers(cidade),
        )

    corpo = request.get_json(silent=True) or {}
    valores = {}
    for campo, tipo in _ESTADO_CAMPOS.items():
        if campo not in corpo:
            continue
        try:
            valores[campo] = tipo(corpo[campo])
        except (TypeError, ValueError):
            return jsonify(error=f'Campo "{campo}" invalido.'), 400

    origem = (corpo.get('origem') or 'automacao')[:60]

    # "Estive aqui." A automacao carimba isto no comeco de toda rodada, tenha
    # ela o que gravar ou nao. E o unico jeito de o site distinguir "a maquina
    # nao acordou" de "a maquina rodou e nao havia novidade".
    if corpo.get('heartbeat'):
        try:
            db.session.execute(
                sa_text('INSERT INTO automacao_estado (cidade, atualizado_por, '
                        'atualizado_em, visto_em) VALUES (:c, :por, :em, :em) '
                        'ON CONFLICT(cidade) DO UPDATE SET visto_em = :em, '
                        'atualizado_por = :por'),
                {'c': cidade, 'por': origem, 'em': datetime.now()})
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            return jsonify(error=f'Falha ao registrar o sinal de vida: {exc}'), 500
        if not valores:
            return jsonify(ok=True, cidade=cidade, visto=True)

    if not valores:
        return jsonify(error='Nada a gravar. Campos aceitos: '
                             + ', '.join(_ESTADO_CAMPOS)), 400
    try:
        db.session.execute(
            sa_text('INSERT INTO automacao_estado (cidade, atualizado_por, atualizado_em) '
                    'VALUES (:c, :por, :em) ON CONFLICT(cidade) DO NOTHING'),
            {'c': cidade, 'por': origem, 'em': datetime.now()},
        )
        valores['visto_em'] = datetime.now()
        sets = ', '.join(f'{campo} = :{campo}' for campo in valores)
        params = dict(valores)
        params.update({'c': cidade, 'por': origem, 'em': datetime.now()})
        db.session.execute(
            sa_text(f'UPDATE automacao_estado SET {sets}, atualizado_por = :por, '
                    'atualizado_em = :em WHERE cidade = :c'),
            params,
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        return jsonify(error=f'Falha ao gravar estado: {exc}'), 500
    return jsonify(ok=True, cidade=cidade, gravado=sorted(valores))


_ANTICHEAT_SCHEMA = False


def _ensure_anticheat_avisos():
    global _ANTICHEAT_SCHEMA
    if _ANTICHEAT_SCHEMA:
        return
    with db.engine.begin() as conn:
        conn.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS anticheat_avisados (
                flag_id INTEGER PRIMARY KEY,
                cidade VARCHAR(100),
                avisado_em DATETIME NOT NULL
            )
        """))
    _ANTICHEAT_SCHEMA = True


@api_bp.route('/anticheat/resumo')
def anticheat_resumo():
    """Flags do anticheat que o admin ainda nao viu, com o contexto que ele
    precisa para decidir sem abrir o sistema.

    Traz, por atleta: quantas flags ele ja acumulou no total e no mes, o link
    da atividade, a velocidade que disparou o alerta e — o mais importante —
    se os km JA ENTRARAM no ranking. Por padrao entram: so saem se o admin
    rejeitar na tela de revisoes.
    """
    _ensure_anticheat_avisos()
    cidade = _cidade_api()
    novos = request.args.get('novos', '1') not in ('0', 'false', 'no')

    linhas = db.session.execute(sa_text("""
        SELECT f.id, f.tipo_flag, f.nivel, f.status, f.descricao, f.criada_em,
               at.nome_atual, at.strava_id, at.telefone_normalizado,
               ac.id_atividade, ac.data_hora, ac.distancia,
               ac.velocidade_media, ac.cidade
        FROM flag_anticheat f
        JOIN atletas at ON at.id = f.atleta_id
        JOIN activities ac ON ac.id = f.atividade_id
        WHERE ac.cidade = :c AND f.status = 'aberta'
        ORDER BY f.criada_em DESC
        LIMIT 60
    """), {'c': cidade}).fetchall()

    if novos:
        ja = {r[0] for r in db.session.execute(sa_text(
            'SELECT flag_id FROM anticheat_avisados')).fetchall()}
        linhas = [l for l in linhas if l[0] not in ja]

    itens = []
    for l in linhas:
        strava_id = str(l[7] or '')
        total = db.session.execute(sa_text("""
            SELECT COUNT(*) FROM flag_anticheat f
            JOIN atletas a ON a.id = f.atleta_id WHERE a.strava_id = :s
        """), {'s': strava_id}).scalar() or 0
        abertas = db.session.execute(sa_text("""
            SELECT COUNT(*) FROM flag_anticheat f
            JOIN atletas a ON a.id = f.atleta_id
            WHERE a.strava_id = :s AND f.status = 'aberta'
        """), {'s': strava_id}).scalar() or 0
        itens.append({
            'flag_id': l[0], 'tipo': l[1], 'nivel': l[2], 'status': l[3],
            'descricao': l[4], 'criada_em': str(l[5]) if l[5] else None,
            'atleta': l[6], 'chave_atleta': strava_id,
            'telefone': l[8],
            'atividade_id': l[9],
            'atividade_url': f'https://www.strava.com/activities/{l[9]}' if l[9] else None,
            'data_atividade': str(l[10]) if l[10] else None,
            'distancia': l[11], 'velocidade_media': l[12],
            # A regra do projeto: flag NAO tira do ranking. So a rejeicao
            # manual do admin tira. Por isso "contando" e sempre True aqui.
            'km_contando_no_ranking': (l[3] != 'rejeitada'),
            'flags_total_do_atleta': total,
            'flags_abertas_do_atleta': abertas,
        })
    return jsonify(cidade=cidade, total=len(itens), flags=itens,
                   painel=f'{request.host_url.rstrip("/")}/admin/reviews')


@api_bp.route('/anticheat/marcar', methods=['POST'])
def anticheat_marcar():
    """Confirma que o admin ja foi avisado destas flags."""
    _ensure_anticheat_avisos()
    corpo = request.get_json(silent=True) or {}
    ids = corpo.get('flag_ids') or []
    if not isinstance(ids, list) or not ids:
        return jsonify(error='flag_ids (lista) obrigatorio.'), 400
    agora = datetime.now()
    try:
        for flag_id in ids:
            db.session.execute(sa_text(
                'INSERT OR REPLACE INTO anticheat_avisados (flag_id, cidade, avisado_em) '
                'VALUES (:f, :c, :em)'),
                {'f': int(flag_id), 'c': _cidade_api(), 'em': agora})
        db.session.commit()
        return jsonify(ok=True, marcadas=len(ids))
    except Exception as exc:
        db.session.rollback()
        return jsonify(error=f'Falha ao marcar: {exc}'), 500


@api_bp.route('/avisos/pendentes')
def avisos_pendentes():
    """Avisos vencidos, com a lista de quem recebe e o texto ja montado.

    A automacao chama isto a cada rodada, envia o que vier e confirma em
    /api/avisos/marcar. Quem nao tem telefone nao entra na lista — aparece no
    painel para o admin completar.
    """
    from app.blueprints import avisos as mod
    cidade = _cidade_api()
    mod.ensure_schema()
    agora = datetime.now()
    com_ranking = request.args.get('com_ranking') in ('1', 'true', 'yes')

    pendentes = []
    for resumo in mod.listar_avisos(cidade):
        if not resumo['ativo']:
            continue
        aviso = mod.carregar_aviso(resumo['id'], cidade)
        if not aviso:
            continue
        vencido, motivo = mod.aviso_vencido(aviso, agora)
        if not vencido:
            continue
        if aviso['frequencia'] == 'com_ranking' and not com_ranking:
            continue

        alvos = []
        for pessoa in mod.destinatarios(cidade, aviso['publico'], aviso['publico_params']):
            if not pessoa.get('telefone'):
                continue
            if mod.ja_enviado_hoje(aviso['id'], pessoa['telefone']):
                continue
            alvos.append({
                'chave_atleta': pessoa['chave'],
                'nome': pessoa['nome'],
                'telefone': pessoa['telefone'],
                'chat_id': f"{pessoa['telefone']}@c.us",
                'mensagem': mod.montar_mensagem(aviso, pessoa, cidade),
            })
        if not alvos:
            continue
        pendentes.append({
            'id': aviso['id'], 'codigo': aviso['codigo'], 'titulo': aviso['titulo'],
            'publico': aviso['publico'],
            'imagem_url': (url_for('api.aviso_imagem', aviso_id=aviso['id'],
                                   _external=True) if aviso['imagem_mime'] else None),
            'destinatarios': alvos,
        })
    return jsonify(cidade=cidade, quando=agora.isoformat(timespec='seconds'),
                   avisos=pendentes)


@api_bp.route('/avisos/<int:aviso_id>/imagem')
def aviso_imagem(aviso_id):
    """Imagem do aviso, para o WAHA baixar por URL."""
    from flask import Response
    linha = db.session.execute(sa_text(
        'SELECT imagem_blob, imagem_mime FROM avisos WHERE id = :i AND cidade = :c'),
        {'i': aviso_id, 'c': _cidade_api()}).fetchone()
    if not linha or not linha[0]:
        return jsonify(error='Aviso sem imagem.'), 404
    return Response(linha[0], mimetype=linha[1] or 'image/png')


@api_bp.route('/avisos/marcar', methods=['POST'])
def avisos_marcar():
    """Confirma o envio de um aviso a uma pessoa (idempotente)."""
    from app.blueprints import avisos as mod
    corpo = request.get_json(silent=True) or {}
    aviso_id = corpo.get('aviso_id')
    telefone = mod.normalizar_telefone(corpo.get('telefone'))
    if not aviso_id or not telefone:
        return jsonify(error='aviso_id e telefone obrigatorios.'), 400
    ok = mod.registrar_envio(
        int(aviso_id), telefone, corpo.get('chave_atleta'),
        status=(corpo.get('status') or 'ok')[:20], detalhe=corpo.get('detalhe'))
    return (jsonify(ok=True) if ok
            else (jsonify(error='Falha ao registrar.'), 500))


@api_bp.route('/ranking/annual')
def ranking_annual():
    year = request.args.get('ano', type=int) or datetime.now().year
    cidade = _cidade_api()
    nomes = _nomes_patrocinadores_do_ano(cidade, year)
    data = build_yearly_ranking_data(cidade, year)
    return jsonify(cidade=cidade, ano=year, ranking=data, texto_whatsapp=format_yearly_ranking_text(data, nomes))


# ===================== FILA DE VINCULACAO DO WHATSAPP =====================
# O servidor da aplicacao fica sempre ligado; a maquina que roda o WAHA passa
# o dia desligada. Quando ela sobe, pergunta aqui se alguem pediu para
# vincular um numero, cria a sessao daquela cidade, pega o codigo de
# pareamento e manda pelo WhatsApp do responsavel. E isto que permite ao admin
# de outra cidade se virar sozinho.

@api_bp.route('/waha/sessao')
def waha_sessao():
    """Nome da sessao desta cidade no WAHA — a automacao nunca chuta 'default'."""
    from app import whatsapp as wa
    cidade = _cidade_api()
    if not cidade:
        return jsonify(error='Este token nao esta ligado a uma cidade.'), 400
    return jsonify(
        cidade=cidade,
        sessao=wa.nome_sessao(cidade),
        slug=wa.slug_cidade(cidade),
        numero_responsavel=wa.numero_responsavel(cidade),
    )


@api_bp.route('/maquina/parando', methods=['POST'])
def maquina_parando():
    """A maquina avisa que vai parar AGORA — parada combinada, nao falha.

    POR QUE (15/08/2026)
    O site decide entre "dormindo" e "quebrada" pelo carimbo que o Windows
    deixa a cada 15 minutos. Quem desliga a maquina as 22:55 deixa o carimbo
    fresco: a porta 3000 emudece e o site conclui "ligada e sem prestar". Foi o
    e-mail das 23:01 de 14/08, sobre um desligamento que o dono fez de
    proposito.

    Quem chama e o `Movimente-se_Dormir.ps1`, na parada limpa, antes de
    derrubar os containers.
    """
    cidade = _cidade_api()
    if not cidade:
        return jsonify(error='Este token nao esta ligado a uma cidade.'), 400
    dados = request.get_json(silent=True) or {}
    motivo = (dados.get('motivo') or 'parada combinada')[:120]
    try:
        secretstore.set_value(secretstore.MAQUINA_PARANDO_EM,
                              datetime.now().isoformat(timespec='seconds'),
                              scope=cidade, updated_by='dell-parada-limpa')
    except Exception as exc:
        current_app.logger.warning('Nao consegui registrar a parada de %s: %s',
                                   cidade, exc)
        return jsonify(error='nao consegui registrar'), 500
    current_app.logger.info('Parada combinada de %s: %s', cidade, motivo)
    return jsonify(ok=True, janela_min=40)


@api_bp.route('/maquina/pode-dormir')
def maquina_pode_dormir():
    """A maquina pergunta antes da parada limpa: da para desligar agora?

    POR QUE (16/08/2026)
    A parada limpa roda as 06:00 e as 16:00, no relogio. Um pareamento pedido as
    15:55 morria no meio: o WAHA descia com a sessao esperando o codigo, e quem
    estava com o celular na mao via a tela parar sem explicacao. Pior que nao
    parear e parear pela metade — a sessao meio feita e a que some no reinicio
    (regra 15-C).

    O QUE ELA NAO FAZ, DE PROPOSITO: nao segura a maquina por qualquer motivo.
    So o pareamento em aberto adia, e adia por pouco — quem decide o resto do
    ciclo continua sendo o `Dormir.ps1`. Sem resposta do site, ele segue o
    combinado de hoje e desliga: transformar "site fora do ar" em "maquina
    ligada a noite inteira" seria trocar um problema por outro sem ninguem
    pedir.
    """
    from app import whatsapp as wa
    cidade = _cidade_api()
    if not cidade:
        return jsonify(error='Este token nao esta ligado a uma cidade.'), 400

    # MANTER LIGADA (17/08/2026) — alavanca manual no painel, ao lado do modo
    # teste (item 4.1-b do plano do MVP). Vem ANTES do pareamento porque e um
    # pedido deliberado do dono, sem prazo — diferente da espera do
    # pareamento, que solta sozinha em 20 min. So o proprio dono desliga a
    # alavanca de volta.
    manter = (secretstore.get(secretstore.MANTER_LIGADA, scope=cidade)
              or '').strip().lower() == 'sim'
    if manter:
        return jsonify(dormir=False, manter_ligada=True,
                       motivo='a maquina foi marcada para ficar ligada no painel')
    # "ATE EU MANDAR PARAR" E ORDEM, NAO SUGESTAO (17/09/2026, regra do dono).
    # A parada das 06:00 e das 16:00 ignorava o "Manter de pe — ate eu mandar
    # parar": em 17/09 as 16:00:03 desceu tudo e suspendeu a maquina, e a
    # subida de volta (16:09) quebrou a sessao principal. Com o prazo -1, a
    # parada agendada se comporta como com "manter ligada": nao toca em nada.
    # O "Desligar a maquina" clicado pelo dono continua valendo — aquele botao
    # zera o prazo ANTES de pedir a parada (admin.whatsapp_sessao,
    # desligar_maquina), entao aqui ele ja chega sem o -1.
    if (_janela_containers(cidade) or {}).get('containers_ate') == -1:
        return jsonify(dormir=False, manter_ligada=True,
                       motivo='a automacao foi marcada para ficar de pe ate '
                              'mandarem parar')

    # NAO DESLIGA EM CIMA DE UMA PUBLICACAO (23/08/2026). Achado depois de um
    # pedido de desligar manual: nada impedia ele de cair minutos antes do
    # WoL da proxima rodada, disputando com o proprio despertar. A agenda
    # (`/admin/postagem`) e a MESMA fonte que decide ranking, parabens,
    # avisos e promocoes -- entao mudar o horario na tela muda esta trava
    # junto, sem precisar mexer em codigo. A antecedencia e a mesma do
    # Wake-on-LAN (vigia.py) de proposito: se o WoL sai H-15, desligar dentro
    # desses 15 minutos e o mesmo risco que motivou o WoL existir.
    try:
        from app import publicador
        from app.vigia import _ANTECEDENCIA_WOL_MIN
        agora = datetime.now()
        minuto_atual = agora.hour * 60 + agora.minute
        for item in publicador.agenda(cidade):
            if not item.ativo:
                continue
            try:
                h, m = str(item.horario).split(':')
                alvo = int(h) * 60 + int(m)
            except (ValueError, AttributeError):
                continue
            falta = alvo - minuto_atual
            if falta < 0:
                falta += 24 * 60   # horario de amanha (ex: 05:30 visto as 23h50)
            if falta <= _ANTECEDENCIA_WOL_MIN:
                return jsonify(
                    dormir=False, manter_ligada=False,
                    motivo=('tem uma publicacao agendada para %s, daqui a %d '
                            'minuto(s) -- desligar agora atrapalharia essa rodada'
                            % (item.horario, falta)))
    except Exception as exc:
        current_app.logger.warning('pode-dormir: nao consegui olhar a agenda: %s', exc)
        # sem certeza, nao segura a maquina (mesma regra do resto da funcao)

    try:
        aberto = wa.pareamento_aberto(cidade)
    except Exception as exc:
        current_app.logger.warning('pode-dormir: nao consegui olhar a fila: %s', exc)
        return jsonify(dormir=True, manter_ligada=False,
                       motivo='nao consegui conferir a fila de '
                              'pareamento — siga o combinado')
    if aberto.get('tem'):
        return jsonify(
            dormir=False, manter_ligada=False,
            motivo=('tem %d pareamento(s) de WhatsApp em andamento, pedido ha %s '
                    '— desligar agora derruba a sessao pela metade'
                    % (aberto['quantos'], aberto['ha_quanto_tempo'])))
    return jsonify(dormir=True, manter_ligada=False, motivo='nada em andamento')


@api_bp.route('/instancias', methods=['GET', 'POST'])
def instancias_waha():
    """A maquina conta quais instancias WAHA ela tem — endereco, porta e chave.

    POR QUE ESTA ROTA EXISTE (14/08/2026)
    Ate hoje o endereco e a chave do WAHA eram DIGITADOS na tela, um por
    cidade. So que a cidade tem duas instancias com chaves diferentes: com a
    chave da errada, o WAHA devolve 401, o site conclui "automacao quebrada" e
    manda e-mail de 15 em 15 minutos sobre um WhatsApp que esta publicando
    normalmente. Pior: sem listar as sessoes, o site nao descobre o nome real
    (`principal-pls`), cai no slug e o botao Conectar pede uma instancia nova
    em cima de uma que ja existe — a P-64 inteira, nascida de um 401.

    Quem sabe a verdade e a maquina. Aqui ela conta, e o site para de adivinhar.
    """
    from app import whatsapp as wa
    cidade = _cidade_api()
    if not cidade:
        return jsonify(error='Este token nao esta ligado a uma cidade.'), 400

    if request.method == 'GET':
        return jsonify(cidade=cidade, instancias=wa.instancias(cidade))

    dados = request.get_json(silent=True) or {}
    itens = dados.get('instancias')
    if not isinstance(itens, list) or not itens:
        return jsonify(error='Mande instancias=[{container, porta, chave, '
                             'sessao, papel, host, status, motor}].'), 400
    gravadas, avisos = wa.registrar_instancias(
        cidade, itens, host=(dados.get('host') or '').strip() or None,
        por='maquina-da-automacao')

    # PEDIDO DE APAGAR SESSÃO FEITO COM O WHATSAPP PARADO (18/09/2026, A2/E2):
    # a máquina acabou de contar que subiu — é a hora de cumprir.
    try:
        recado = wa.cumprir_apagar_pendente(cidade)
        if recado:
            avisos.append(recado)
    except Exception as exc:
        current_app.logger.warning('Pedido guardado de apagar sessão falhou: %s', exc)

    # IP DO WSL (18/09/2026, A3): o topo da tela mostra cada camada com o IP.
    wsl_ip = (dados.get('wsl_ip') or '').strip()
    if wsl_ip and len(wsl_ip) <= 45:
        from app import maquina
        if secretstore.get(maquina.WSL_IP, scope=cidade) != wsl_ip:
            secretstore.set_value(maquina.WSL_IP, wsl_ip, scope=cidade,
                                  updated_by='maquina-da-automacao')

    # O ID DO GRUPO VEM JUNTO (15/08/2026). Sem ele o site nao consegue
    # entregar mensagem de grupo por conta propria: `_chat_id` nao tem como
    # transformar a palavra "grupo" num destino, e todo envio ao grupo fica
    # esperando o ciclo da maquina — ate 15 minutos depois do clique.
    grupo = (dados.get('grupo') or '').strip()
    if grupo and grupo.endswith('@g.us'):
        atual = (secretstore.get(secretstore.WHATSAPP_GRUPO, scope=cidade) or '').strip()
        if atual != grupo:
            secretstore.set_value(secretstore.WHATSAPP_GRUPO, grupo, scope=cidade,
                                  updated_by='maquina-da-automacao')
            avisos.append('o grupo desta cidade é %s' % grupo)
    return jsonify(ok=True, gravadas=gravadas, avisos=avisos)


@api_bp.route('/waha/pendentes')
def waha_pendentes():
    """Pedidos de vinculacao esperando a maquina do WAHA subir.

    Com o token do orquestrador devolve os de TODAS as cidades; com o token de
    uma cidade, so os dela.
    """
    from app import whatsapp as wa
    pendentes = wa.fila_pendente()
    if not getattr(g, 'automation_global', False):
        cidade = _cidade_api()
        pendentes = [p for p in pendentes if p['cidade'] == cidade]
    return jsonify(pendentes=pendentes, total=len(pendentes))


@api_bp.route('/faxina/pendente', methods=['POST'])
def faxina_pendente():
    """Entrega o pedido de faxina da cidade (ou null) e marca a entrega.

    Uma entrega a cada 15 min: se a maquina cair no meio, a passada seguinte
    tenta de novo, mas duas passadas proximas nao executam a mesma faxina.
    POST de proposito: marcar e escrita.
    """
    from app import whatsapp as wa
    cidade = _cidade_api()
    if not cidade:
        return jsonify(error='Este token nao esta ligado a uma cidade.'), 400
    # COPIAS DE SESSAO REFEITA (19/09/2026, item 62): entregues e esquecidas
    # aqui — apagar zip antigo de uma sessao repetido nao faz mal. O sufixo da
    # cidade vai junto: a maquina recusa sessao de outra cidade.
    copias = wa.copias_a_limpar(cidade)
    casa = wa.container_da_cidade(cidade) or {}
    sufixo = (casa.get('container') or '').split('auto-waha-', 1)[-1] \
        if (casa.get('container') or '').startswith('auto-waha-') else ''
    if copias:
        secretstore.delete(wa._COPIAS_A_LIMPAR, scope=cidade)
    pedido = wa.faxina_pedida(cidade)
    if pedido:
        entregue = pedido.get('entregue_em')
        if entregue and (datetime.now() - datetime.fromisoformat(entregue)).total_seconds() < 15 * 60:
            return jsonify(faxina=None, copias=copias, sufixo=sufixo,
                           motivo='ja entregue ha menos de 15 min')
        pedido['entregue_em'] = datetime.now().isoformat(timespec='seconds')
        secretstore.set_value(wa._FAXINA_PEDIDA, json.dumps(pedido), scope=cidade,
                              updated_by='maquina-da-automacao')
    return jsonify(faxina=pedido, copias=copias, sufixo=sufixo)


@api_bp.route('/versoes', methods=['POST'])
def versoes_relato():
    """A maquina conta as versoes instaladas e publicadas (19/09/2026, item 67)."""
    from app import whatsapp as wa
    cidade = _cidade_api()
    if not cidade:
        return jsonify(error='Este token nao esta ligado a uma cidade.'), 400
    dados = request.get_json(silent=True) or {}
    if not isinstance(dados.get('waha'), dict):
        return jsonify(error='Mande o relato do versoes.py.'), 400
    secretstore.set_value(wa._VERSOES, json.dumps(dados)[:20000], scope=cidade,
                          updated_by='maquina-da-automacao')
    return jsonify(ok=True)


@api_bp.route('/versoes/pedido', methods=['POST'])
def versoes_pedido():
    """Entrega (uma vez) a troca de versao do WAHA pedida pelo supremo."""
    from app import whatsapp as wa
    cidade = _cidade_api()
    if not cidade:
        return jsonify(error='Este token nao esta ligado a uma cidade.'), 400
    pedido = wa.troca_de_versao_pedida(cidade)
    if pedido:
        secretstore.delete(wa._VERSAO_PEDIDA, scope=cidade)
    return jsonify(trocar_waha=(pedido or {}).get('tag'))


@api_bp.route('/versoes/resultado', methods=['POST'])
def versoes_resultado():
    """A maquina conta como foi a troca de versao."""
    from app import whatsapp as wa
    cidade = _cidade_api()
    if not cidade:
        return jsonify(error='Este token nao esta ligado a uma cidade.'), 400
    linhas = (request.get_json(silent=True) or {}).get('linhas')
    if not isinstance(linhas, list):
        return jsonify(error='Mande linhas=[...].'), 400
    secretstore.set_value(wa._VERSAO_RESULTADO, json.dumps({
        'em': datetime.now().strftime('%d/%m %H:%M'),
        'linhas': [str(x)[:300] for x in linhas][:40]}), scope=cidade,
        updated_by='maquina-da-automacao')
    return jsonify(ok=True)


@api_bp.route('/faxina/resultado', methods=['POST'])
def faxina_resultado():
    """A maquina conta o que a faxina da cidade fez (18/09/2026, A10 + item 5)."""
    from app import whatsapp as wa
    cidade = _cidade_api()
    if not cidade:
        return jsonify(error='Este token nao esta ligado a uma cidade.'), 400
    dados = request.get_json(silent=True) or {}
    linhas = dados.get('linhas')
    if not isinstance(linhas, list):
        return jsonify(error='Mande linhas=[...].'), 400
    wa.registrar_faxina(cidade, linhas)
    return jsonify(ok=True)


@api_bp.route('/waha/marcar', methods=['POST'])
def waha_marcar():
    """A automacao conta o que fez com um pedido da fila."""
    from app import whatsapp as wa
    dados = request.get_json(silent=True) or request.form or {}
    try:
        pedido_id = int(dados.get('id') or 0)
    except (TypeError, ValueError):
        pedido_id = 0
    situacao = (dados.get('situacao') or '').strip()
    if not pedido_id or situacao not in (wa.EM_ANDAMENTO, wa.ATENDIDO,
                                         wa.FALHOU, wa.CANCELADO):
        return jsonify(error='Informe id e situacao (em_andamento, atendido, '
                             'falhou ou cancelado).'), 400

    # Token de cidade so mexe no que e da propria cidade.
    if not getattr(g, 'automation_global', False):
        alvo = db.session.execute(sa_text(
            'SELECT cidade FROM sessoes_whatsapp WHERE id = :i'), {'i': pedido_id}
        ).fetchone()
        if not alvo or alvo[0] != _cidade_api():
            return jsonify(error='Pedido nao encontrado.'), 404

    ok = wa.marcar(pedido_id, situacao,
                   codigo=(dados.get('codigo') or None),
                   mensagem=(dados.get('mensagem') or None))
    return (jsonify(ok=True) if ok
            else (jsonify(error='Nao consegui registrar.'), 500))


@api_bp.route('/waha/etapa', methods=['POST'])
def waha_etapa():
    """O agente conta em que passo do provisionamento esta.

    Existe separado do /waha/marcar porque etapa nao e desfecho: a rodada
    continua. Sem isto o admin fica olhando uma tela parada por minutos sem
    saber se esta andando ou travou — e apertar o botao de novo no meio do
    provisionamento e o que mais deixa container pela metade.
    """
    from app import whatsapp as wa
    dados = request.get_json(silent=True) or request.form or {}
    try:
        pedido_id = int(dados.get('id') or 0)
    except (TypeError, ValueError):
        pedido_id = 0
    etapa = (dados.get('etapa') or '').strip()
    if not pedido_id or etapa not in wa.ETAPA_LIMITE:
        return jsonify(error='Informe id e etapa (%s).'
                             % ', '.join(wa.ETAPA_LIMITE)), 400

    if not getattr(g, 'automation_global', False):
        alvo = db.session.execute(sa_text(
            'SELECT cidade FROM sessoes_whatsapp WHERE id = :i'), {'i': pedido_id}
        ).fetchone()
        if not alvo or alvo[0] != _cidade_api():
            return jsonify(error='Pedido nao encontrado.'), 404

    ok = wa.anotar_etapa(pedido_id, etapa, mensagem=(dados.get('mensagem') or None))
    return (jsonify(ok=True, etapa=etapa) if ok
            else (jsonify(error='Nao consegui registrar a etapa.'), 500))


@api_bp.route('/mensagens/ciclo')
def mensagens_ciclo():
    """Mensagens da virada do mes prontas para o grupo.

    `sugerida=true` marca as que fazem sentido HOJE (primeiros dias do mes).
    A automacao decide enviar; aqui so sai texto.
    """
    from app.mensagens import mensagens_do_ciclo
    cidade = _cidade_api()
    try:
        itens = mensagens_do_ciclo(cidade)
    except Exception as exc:
        current_app.logger.error('Falha ao montar mensagens do ciclo: %s', exc, exc_info=True)
        return jsonify(error='Nao consegui montar as mensagens.'), 500
    return jsonify(cidade=cidade, mensagens=itens,
                   sugeridas=[m for m in itens if m['sugerida']])


@api_bp.route('/fechamento')
def fechamento_api():
    """Situacao do fechamento de um periodo (a foto oficial do mes)."""
    from app import fechamento as fech
    try:
        year, month = _periodo_api()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    cidade = _cidade_api()
    dados = fech.info(cidade, year, month)
    return jsonify(
        cidade=cidade, periodo=f'{year:04d}-{month:02d}',
        fechado=bool(dados), fechamento=dados,
        ultimos=fech.listar(cidade, limite=6),
    )


@api_bp.route('/vigilancia')
def vigilancia():
    """Tudo que a automacao precisa saber para avisar antes de virar problema.

    Uma chamada, uma foto: banco, jobs travados, sessao do Strava, meses que
    ficaram sem fechar, backups atrasados, fila de vinculacao do WhatsApp e
    avisos vencidos. `alertas` ja vem com o texto pronto para mandar ao
    responsavel — o orquestrador so decide se manda.
    """
    from datetime import timedelta
    from app import fechamento as fech
    from app import backupauto, whatsapp as wa
    from app.models import MonthlyBackup

    cidade = _cidade_api()
    agora = datetime.now()
    alertas = []
    dados = {'cidade': cidade, 'hora_servidor': agora.isoformat(timespec='seconds')}

    # ---- banco ----
    try:
        db.session.execute(sa_text('SELECT 1'))
        dados['banco'] = 'ok'
    except Exception as exc:
        dados['banco'] = 'erro: ' + type(exc).__name__
        alertas.append('O banco de dados nao respondeu. O site pode estar fora.')

    # ---- sessao do Strava ----
    try:
        saude = secretstore.session_health(cidade) or {}
        dados['strava'] = {
            'configurado': bool(secretstore.get(secretstore.STRAVA_SESSION, scope=cidade)),
            'ok': saude.get('ok'),
            'mensagem': saude.get('message') or '',
        }
        if dados['strava']['configurado'] and saude.get('ok') is False:
            alertas.append('A sessao do Strava expirou: a conferencia de km e a '
                           'importacao por perfil param ate recolar o cookie.')
        if not dados['strava']['configurado']:
            alertas.append('Sem cookie do Strava configurado nesta cidade.')
    except Exception as exc:
        dados['strava'] = {'erro': type(exc).__name__}

    # ---- meses vencidos ainda abertos ----
    try:
        abertos = []
        ano, mes = fech._mes_anterior(agora.year, agora.month)
        for _ in range(6):
            if (fech._ja_passou_a_carencia(ano, mes, agora)
                    and not fech.esta_fechado(cidade, ano, mes)
                    and MonthlyBackup.query.filter_by(cidade=cidade, ano=ano, mes=mes).first()):
                abertos.append(f'{ano:04d}-{mes:02d}')
            ano, mes = fech._mes_anterior(ano, mes)
        dados['meses_abertos'] = abertos
        if abertos:
            alertas.append('Mes(es) vencido(s) ainda sem fechar: ' + ', '.join(abertos))
    except Exception as exc:
        dados['meses_abertos'] = []
        current_app.logger.debug('vigilancia/fechamento: %s', exc)

    # ---- backups ----
    try:
        situacao_bkp = backupauto.situacao()
        ultimo = (situacao_bkp.get('config') or [{}])[0].get('quando')
        dados['backup'] = {
            'copias_config': len(situacao_bkp.get('config') or []),
            'copias_codigo': len(situacao_bkp.get('codigo') or []),
            'ultima_config': str(ultimo) if ultimo else None,
        }
        if ultimo and (agora - ultimo) > timedelta(days=15):
            alertas.append('O ultimo backup do banco tem mais de 15 dias.')
        if not situacao_bkp.get('config'):
            alertas.append('Nenhum backup do banco foi gerado ainda.')
    except Exception as exc:
        dados['backup'] = {'erro': type(exc).__name__}

    # ---- fila de vinculacao do WhatsApp ----
    try:
        fila = [p for p in wa.fila_pendente() if p['cidade'] == cidade]
        dados['fila_whatsapp'] = len(fila)
        if fila:
            alertas.append(f'{len(fila)} pedido(s) de vinculacao de WhatsApp esperando.')
        dados['sessao_whatsapp'] = wa.nome_sessao(cidade)
    except Exception as exc:
        current_app.logger.debug('vigilancia/whatsapp: %s', exc)

    # ---- jobs de upload travados ----
    try:
        from app.blueprints.upload import _job_progress
        travados = [j for j in (_job_progress or {}).values()
                    if isinstance(j, dict) and j.get('stage') not in
                    ('done', 'error', 'cancelled', None)]
        dados['jobs_em_andamento'] = len(travados)
    except Exception:
        dados['jobs_em_andamento'] = None

    # ---- avisos vencidos ----
    try:
        from app.blueprints.avisos import listar_avisos, aviso_vencido
        vencidos = [a for a in listar_avisos(cidade) if aviso_vencido(a, agora)]
        dados['avisos_vencidos'] = len(vencidos)
    except Exception as exc:
        current_app.logger.debug('vigilancia/avisos: %s', exc)

    dados['alertas'] = alertas
    dados['status'] = 'ok' if not alertas else 'atencao'
    if dados.get('banco') != 'ok':
        dados['status'] = 'erro'
    return jsonify(dados), (200 if dados['status'] != 'erro' else 503)


# ===================== FILA DE MENSAGENS =====================
# O site fica ligado o tempo todo; a maquina do WhatsApp nao. Toda mensagem
# que uma tela dispara entra nesta fila e sai quando a automacao subir.

# Ultima vez que a agenda automatica rodou para cada cidade. A automacao bate
# nesta rota de 15 em 15 minutos; montar o ciclo inteiro a cada batida seria
# desperdicio no DS215j. Uma vez por hora e de sobra para uma mensagem que so
# existe uma vez por mes.
_ULTIMA_AGENDA = {}
_INTERVALO_AGENDA = 3600


def _atualizar_agenda(cidade):
    """Enfileira o que e do dia ANTES de entregar a fila.

    E aqui que a automacao e o site se encontram: quem liga a maquina as
    10h nao precisa que o site tenha acordado na hora certa — a fila e
    montada no momento em que alguem vem buscar.
    """
    import time as _t
    from app import agendamensagens
    agora = _t.time()
    if agora - _ULTIMA_AGENDA.get(cidade, 0) < _INTERVALO_AGENDA:
        return
    _ULTIMA_AGENDA[cidade] = agora
    try:
        novas, _rep, _sem = agendamensagens.rodar(cidade, por='automacao')
        if novas:
            current_app.logger.warning(
                'Agenda de mensagens: %s nova(s) na fila de %s', novas, cidade)
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning('Agenda de mensagens falhou em %s: %s', cidade, exc)


@api_bp.route('/rodada/pendente')
def rodada_pendente():
    """SO OLHA: tem pedido de "Publicar agora" esperando? (16/09/2026)

    PUBLICAR AGORA E AGORA, NAO NO PROXIMO QUARTO DE HORA (regra do dono).
    Ate 15/09 o unico caminho do pedido era `/api/fila/pendentes`, que a
    maquina consulta de 15 em 15 minutos. Medido naquele dia: clique as
    23:25:31, pedido atendido as 23:35:05 — 9 min 34 s de espera pura, com a
    maquina ligada e os containers de pe.

    Esta rota existe para ser perguntada de MINUTO em minuto, e por isso e' a
    mais barata possivel: le o pedido e responde. NAO marca atendido, NAO
    carimba o sinal do Windows, NAO mexe na agenda — quem faz tudo isso
    continua sendo `/api/fila/pendentes`, um caminho so, como sempre.
    """
    cidade = _cidade_api()
    if not cidade:
        return jsonify(error='Este token nao esta ligado a uma cidade.'), 400
    tem = execucao = False
    try:
        from app import rodada as _rodada
        tem = bool(_rodada.pendente(cidade))
        # "Executar pedidos agora" (17/09/2026) viaja na mesma pergunta: ainda
        # so leitura, ainda sem gravar nada por minuto.
        execucao = bool(_rodada.execucao_pendente(cidade))
    except Exception as exc:
        current_app.logger.warning('Nao consegui ver o pedido de rodada: %s', exc)
    return jsonify(rodada_pedida=tem, execucao_pedida=execucao)


@api_bp.route('/execucao/atendida', methods=['POST'])
def execucao_atendida():
    """A maquina pegou o "Executar pedidos agora" (17/09/2026).

    Rota propria em vez de marcar dentro de `/api/fila/pendentes`: aquela rota
    carimba o Windows, roda a agenda e marca a rodada — efeitos demais para uma
    confirmacao. Esta grava UMA vez por clique, nunca por minuto.
    """
    cidade = _cidade_api()
    if not cidade:
        return jsonify(error='Este token nao esta ligado a uma cidade.'), 400
    from app import rodada as _rodada
    ok = _rodada.marcar_execucao_atendida(cidade, por='pedido-imediato')
    return jsonify(ok=bool(ok))


@api_bp.route('/fila/pendentes')
def fila_pendentes():
    """Mensagens esperando para sair. So as da cidade do token."""
    from app import filamensagens
    cidade = _cidade_api()
    if not cidade:
        return jsonify(error='Este token nao esta ligado a uma cidade.'), 400
    # O WINDOWS ACABOU DE FALAR CONOSCO (14/08/2026). Quem chama esta rota de
    # 15 em 15 minutos e a tarefa da fila, do lado do Windows, ANTES de tocar no
    # WSL. Entao ela responde mesmo com o WSL caido — que e justamente o caso em
    # que o site dizia "a maquina esta dormindo" e se calava, com a maquina
    # ligada e tres mensagens de medalha paradas.
    try:
        secretstore.set_value(secretstore.WINDOWS_VISTO_EM,
                              datetime.now().isoformat(timespec='seconds'),
                              scope=cidade, updated_by='tarefa-da-fila')
    except Exception as exc:
        current_app.logger.debug('Nao consegui carimbar o sinal do Windows: %s', exc)
    _atualizar_agenda(cidade)

    # ALGUEM PEDIU O RANKING AGORA? (15/08/2026)
    #
    # Esta rota e a unica coisa que a maquina pergunta ao site de 15 em 15
    # minutos — e tambem no instante em que ela liga. Por isso o pedido do
    # botao "Publicar o ranking agora" viaja aqui: nao precisou de tarefa nova
    # no Windows nem de porta aberta para dentro do Dell.
    #
    # Marcado como atendido JA: se a rodada falhar, o pedido nao volta sozinho
    # — repetir publicacao sem alguem pedindo e o desfecho que nao se desfaz.
    rodada = False
    try:
        from app import rodada as _rodada
        if _rodada.pendente(cidade):
            rodada = True
            _rodada.marcar_atendido(cidade, por='fila-da-maquina')
    except Exception as exc:
        current_app.logger.warning('Nao consegui ver o pedido de rodada: %s', exc)

    # ALGUEM ESTA ESPERANDO UM PAREAMENTO? (16/08/2026 — o "Conectar nunca
    # funcionou de verdade")
    #
    # Quem atende pedido de pareamento e o `provisionar.py`, e ele so roda
    # dentro do `executar.sh`. Fora dos horarios do ranking, quem chama o
    # `executar.sh` e a tarefa da fila do Windows — e ela SAI SEM FAZER NADA
    # quando a fila esta vazia e ninguem pediu ranking. Resultado: o clique em
    # Conectar com a maquina desligada acordava a maquina e ficava esperando o
    # ranking das 05:30. O contador viaja aqui porque esta rota ja e a unica
    # coisa que a maquina pergunta de 15 em 15 minutos — e no instante em que
    # ela liga. Nao precisou de tarefa nova nem de porta aberta para o Dell.
    pareamento = False
    try:
        from app import whatsapp as _wa
        pareamento = bool(_wa.pareamento_aberto(cidade).get('tem'))
    except Exception as exc:
        current_app.logger.warning('Nao consegui ver a fila de pareamento: %s', exc)

    itens = filamensagens.pendentes(cidade=cidade)
    for item in itens:
        if item['tem_imagem']:
            # A ASSINATURA VAI NA URL porque quem baixa e o WAHA, e ele nao
            # manda cabecalho nenhum. Sem ela, 401 -> o WhatsApp recebe JSON no
            # lugar da foto -> HTTP 500 e a mensagem morre (14/08/2026).
            item['imagem_url'] = url_for(
                'api.fila_imagem', mensagem_id=item['id'],
                t=filamensagens.token_imagem(item['id']), _external=True)
        else:
            item['imagem_url'] = None

    # O BOTAO "DESLIGAR A MAQUINA" (17/08/2026, item G do plano do MVP).
    #
    # Nao existe SSH nem peca nova (foi o que barrou isto na P-45): o botao so
    # grava um pedido no cofre, e esta rota — a UNICA coisa que a maquina
    # pergunta de 15 em 15 minutos — e quem entrega. O `Movimente-se_Fila.ps1`
    # ve `desligar_pedido=true` e chama o `Movimente-se_Dormir.ps1`, que ja
    # sabe fazer a parada limpa inteira (copia conferida, containers na
    # ordem, WSL, so entao a energia).
    #
    # RESPEITA O QUE JA PROTEGE: pareamento em andamento e fila com mensagem
    # adiam — nao cancelam — o pedido. Ele continua valido (ate o prazo de 30
    # min) e a proxima passada, com a fila drenada, tenta de novo.
    desligar = False
    try:
        pedido_em = secretstore.get(secretstore.PEDIDO_DESLIGAR_MAQUINA, scope=cidade)
        if pedido_em:
            quando = datetime.fromisoformat(str(pedido_em))
            if (datetime.now() - quando).total_seconds() <= 30 * 60:
                desligar = not pareamento and len(itens) == 0
            else:
                secretstore.delete(secretstore.PEDIDO_DESLIGAR_MAQUINA, scope=cidade)
    except Exception as exc:
        current_app.logger.warning('Nao consegui ver o pedido de desligar: %s', exc)

    # FAXINA DA CIDADE (18/09/2026, A10 + item 5): aqui so se AVISA que ha
    # pedido (a tarefa da fila roda o `--so-fila` por causa disso). Quem
    # entrega e marca e `/api/faxina/pendente` — esta rota ja marca outras
    # coisas como atendidas e nao pode ser chamada duas vezes.
    faxina = False
    try:
        from app import whatsapp as _wa
        # copias de sessao refeita (19/09/2026, item 62) seguem o mesmo caminho
        faxina = (bool(_wa.faxina_pedida(cidade)) or bool(_wa.copias_a_limpar(cidade))
                  or bool(_wa.troca_de_versao_pedida(cidade)))
    except Exception as exc:
        current_app.logger.warning('Nao consegui ver o pedido de faxina: %s', exc)

    return jsonify(cidade=cidade, mensagens=itens, total=len(itens),
                   # `true` = rode a pipeline completa nesta passada, nao so a
                   # fila. Quem obedece e o `Movimente-se_Fila.ps1`.
                   rodada_pedida=rodada,
                   # `true` = tem gente esperando o codigo de pareamento. O
                   # `Movimente-se_Fila.ps1` roda o `executar.sh --so-fila`
                   # mesmo com a fila vazia, porque e ele que leva o
                   # `provisionar.py` junto.
                   pareamento_pedido=pareamento,
                   # `true` = o painel pediu para desligar agora e nada esta
                   # segurando (item G).
                   desligar_pedido=(desligar and not faxina),
                   # `true` = ha faxina pedida (18/09/2026, A10 + item 5); o
                   # `executar.sh` chama o `faxina_cidade.py`, que busca o
                   # pedido em `/api/faxina/pendente`.
                   faxina_pedida=faxina)


@api_bp.route('/fila/<int:mensagem_id>/imagem')
def fila_imagem(mensagem_id):
    """A imagem anexada a uma mensagem da fila.

    Entra por dois caminhos: o token da automacao no cabecalho (quando quem
    pede e a pipeline) ou a assinatura `?t=` na URL (quando quem pede e o
    WAHA, que nao tem como mandar cabecalho). Os dois sao conferidos no
    `before_request` — aqui ja se sabe que o pedido e legitimo.
    """
    from app import filamensagens
    from flask import Response
    blob, mime = filamensagens.imagem_de(mensagem_id, cidade=_cidade_api())
    if not blob:
        return jsonify(error='Sem imagem.'), 404
    return Response(blob, mimetype=mime)


@api_bp.route('/fila/marcar', methods=['POST'])
def fila_marcar():
    """A automacao conta o que fez com a mensagem.

    Falha nao some da fila: volta para pendente ate o teto de tentativas, para
    uma queda de rede no meio nao engolir a mensagem.
    """
    from app import filamensagens
    dados = request.get_json(silent=True) or request.form or {}
    try:
        mensagem_id = int(dados.get('id') or 0)
    except (TypeError, ValueError):
        mensagem_id = 0
    situacao = (dados.get('situacao') or '').strip()
    if not mensagem_id or situacao not in (filamensagens.ENVIADA,
                                           filamensagens.FALHOU,
                                           filamensagens.CANCELADA):
        return jsonify(error='Informe id e situacao (enviada, falhou ou cancelada).'), 400
    ok = filamensagens.marcar(mensagem_id, situacao,
                              detalhe=(dados.get('detalhe') or None),
                              cidade=_cidade_api())
    return (jsonify(ok=True) if ok else (jsonify(error='Nao consegui registrar.'), 500))


@api_bp.route('/agenda/agora')
def agenda_agora():
    """O que a automacao deve fazer NESTE momento, e de qual WhatsApp.

    Substitui os horarios fixos do Agendador do Windows: quem manda agora e a
    configuracao da cidade, feita na tela Publicacao no grupo.
    """
    from app import publicador
    cidade = _cidade_api()
    if not cidade:
        return jsonify(error='Este token nao esta ligado a uma cidade.'), 400
    slot = publicador.horario_de_agora(cidade)
    return jsonify(
        cidade=cidade,
        dentro_da_janela=bool(slot),
        horario=(slot.horario if slot else None),
        enviar=({
            'ranking': bool(slot.envia_ranking),
            'patrocinadores': bool(slot.envia_patrocinadores),
            'parabens': bool(slot.envia_parabens),
            'avisos': bool(slot.envia_avisos),
            'promocoes': bool(slot.envia_promocoes),
        } if slot else {}),
        remetentes={
            'ranking': publicador.candidatos_envio(cidade, 'ranking'),
            'avisos': publicador.candidatos_envio(cidade, 'avisos'),
        },
        responsavel_alertas=publicador.responsavel_alertas(cidade),
        agenda=[{
            'horario': i.horario, 'ativo': bool(i.ativo),
            'ranking': bool(i.envia_ranking),
            'patrocinadores': bool(i.envia_patrocinadores),
            'parabens': bool(i.envia_parabens),
            'avisos': bool(i.envia_avisos),
            'promocoes': bool(i.envia_promocoes),
        } for i in publicador.agenda(cidade)],
    )


@api_bp.route('/janela/<cidade>')
def janela_publica(cidade):
    """É hora de publicar nesta cidade? (P-139, 20/09/2026)

    SEM TOKEN, de propósito: quem pergunta é o Agendador do Windows, ANTES de
    ligar o WSL — e o token mora dentro do WSL. Pedir o token aqui obrigaria a
    ligar a distro inteira a cada 15 minutos.

    Devolve só o suficiente para decidir: se agora é a janela e qual horário da
    agenda está valendo. Nada de telefone, remetente ou a agenda inteira.
    """
    from app import publicador
    from app.models import Config as _Config
    cidade = (cidade or '').strip()
    if not cidade or not _Config.query.filter_by(cidade=cidade).first():
        return jsonify(error='cidade desconhecida'), 404
    try:
        slot = publicador.horario_de_agora(cidade)
    except Exception as exc:
        current_app.logger.warning('janela publica (%s): %s', cidade, exc)
        # NA DUVIDA, NAO AUTORIZA: quem pergunta tem a janela de seguranca
        # dele para cair (falha fechada).
        return jsonify(cidade=cidade, dentro_da_janela=False,
                       erro='nao consegui ler a agenda'), 200
    return jsonify(cidade=cidade, dentro_da_janela=bool(slot),
                   horario=(slot.horario if slot else None),
                   ranking=bool(slot.envia_ranking) if slot else False)


@api_bp.route('/alerta/acao')
def alerta_acao():
    """Qual botao e qual link resolvem esta etapa.

    Existe para o agente no Dell nao precisar guardar uma copia da lista de
    telas. Duas listas em maquinas diferentes divergem — foi assim que o nome da
    sessao do WhatsApp passou meses errado em um arquivo e certo em outro. Aqui
    o site e o dono, e quem precisa pergunta.
    """
    from app import alertas
    etapa = (request.args.get('etapa') or '').strip()
    acao = alertas.acao_da_etapa(etapa)
    return jsonify(etapa=etapa, botao=acao['botao'], url=acao['url'],
                   primeiro_passo=acao['primeiro_passo'],
                   explica=acao['explica'])


@api_bp.route('/alerta', methods=['POST'])
def alerta():
    """A automacao avisa que falhou, e o SITE manda o e-mail.

    POR QUE PASSA PELO SITE. O e-mail sai pelo Microsoft Graph, cujas
    credenciais moram no cofre do Synology — a maquina que fica ligada o tempo
    todo. A pipeline roda no Dell e nao tem (nem deve ter) essas credenciais.
    Entao o Dell nao manda e-mail: ele pede.

    E o melhor efeito colateral desse desenho: se a rodada falhou porque o Dell
    esta com problema, quem avisa e a outra maquina. Um alerta que depende da
    peca quebrada nao e alerta.

    Corpo: {etapa, erro, log: [linhas], origem}
    """
    from app import alertas
    dados = request.get_json(silent=True) or request.form or {}
    # APELIDOS: o Movimente-se_Dormir.ps1 (parada limpa no Windows) fala
    # "assunto"/"detalhe" desde 03/08, quando esta rota ainda nem existia — ele
    # vinha falhando calado. Aceitar os dois nomes e mais barato que fazer duas
    # maquinas concordarem sobre vocabulario, e nao quebra quem ja chama assim.
    etapa = (dados.get('etapa') or dados.get('assunto') or '').strip()
    erro = (dados.get('erro') or dados.get('detalhe') or '').strip()
    if not etapa and not erro:
        return jsonify(error='Informe pelo menos etapa/assunto ou erro/detalhe.'), 400

    linhas = dados.get('log') or []
    if isinstance(linhas, str):
        linhas = linhas.splitlines()
    if not isinstance(linhas, list):
        linhas = []

    # so_registrar: o agente ja avisou por outro canal (WhatsApp cruzado) e so
    # quer o fato gravado. Sem isso, o historico ficaria com buraco justamente
    # nas falhas que foram resolvidas rapido.
    resultado = alertas.registrar_falha(
        cidade=_cidade_api(),
        etapa=etapa or 'desconhecida',
        erro=erro,
        linhas_log=[str(l) for l in linhas][-60:],
        origem=(dados.get('origem') or '').strip(),
        canal=(dados.get('canal') or '').strip() or None,
        detalhe_canal=(dados.get('detalhe_canal') or '').strip(),
        so_registrar=bool(dados.get('so_registrar')),
    )
    # 200 mesmo quando o e-mail nao sai: quem chamou precisa saber o desfecho
    # para tentar outro canal, e nao tratar isso como "a API caiu".
    return jsonify(ok=bool(resultado.get('email')),
                   registrado=bool(resultado.get('registrado')),
                   repetido=bool(resultado.get('repetido')),
                   destinatarios=resultado.get('destinatarios') or [],
                   detalhe=resultado.get('detalhe') or '')
