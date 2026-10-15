# app/blueprints/conferencia.py
# -*- coding: utf-8 -*-
"""Conferencia publica de km: o atleta escolhe o proprio nome e o mes, e o
sistema compara o que esta no ranking com o que existe no Strava dele,
apontando educadamente os links que nao foram postados no grupo.

Desenho para o publico idoso: sem link para colar, botoes grandes, uma etapa
por vez. A consulta ao Strava roda em segundo plano (job) para nunca prender
um worker; o resultado fica em cache por 45 minutos por atleta+mes.
"""
import random
import re
import threading
import time
import uuid
from datetime import datetime

from flask import (
    Blueprint, render_template, request, redirect, url_for, jsonify,
    current_app, abort,
)

from app import db, jobstore, secretstore
from app.models import Activity, Atleta, Config
from app.blueprints.processing import (
    build_strava_throttle,
    fetch_athlete_avatar_url,
    fetch_athlete_month_summary,
    parse_strava_activity,
    ScrapePermanent,
    ScrapeTimeout,
    StravaAuthRequired,
    StravaListaVazia,
    StravaRateLimited,
)

conferencia_bp = Blueprint('conferencia', __name__, url_prefix='/conferencia')

# Frases sorteadas ao fim da conferencia. "ok" = nada faltando (o sistema
# estava certo); "falta" = achamos link(s) que o atleta esqueceu de postar.
_FRASES_TUDO_CERTO = [
    'Confirmado: seus km estão todos aqui. Mais um ponto pro sistema. 😎',
    'Nenhuma atividade perdida. O sistema passou no teste de novo. Placar: Sistema 1 x 0 Desconfiança.',
    'Tá tudo no lugar! Pode relaxar e ir treinar — a matemática está do seu lado.',
    'Zero km sumido. O sistema mandou avisar que continua trabalhando direitinho. 🤖💚',
    'Auditoria concluída: 100% dos seus km presentes. Selo de qualidade para o sistema e medalha pra você. 🏅',
    'Você veio conferir e a gente adorou — está tudo certo, obrigado por confiar!',
    'Revisamos km por km: está tudo certinho. Pode tirar print e mandar ver! 📸',
    'Seus km estão todos no lugar. O sistema continua invicto. 🏆',
    'Contas batendo! Se alguém disser que sumiu km, mostre esta tela. 😉',
    'Tudo certo por aqui! O único que precisa acelerar agora é você na próxima corrida. 🏃💨',
    'Nada faltando! O sistema tirou nota 10 de novo. Já virou rotina. 😄',
]
_FRASES_FALTA = [
    'Achamos o mistério! Tem atividade sua no Strava que não foi postada no grupo. '
    'Não foi o sistema — você correu tanto que espantou um link, e ele saiu correndo. 😄',
    'Boas notícias e um recadinho: seus km existem! Só faltou postar o link no grupo. '
    'Cola ele lá que a gente pega.',
    'Detetive de plantão: encontramos links escondidos no seu Strava. '
    'Poste todos no grupo e eles entram na próxima. 🕵️',
    'Calma, seus km não sumiram — só estão tímidos, esperando você postar o link no grupo. 😉',
    'O sistema está bem, viu? Só encontrou atividade que não veio parar no grupo. '
    'Manda o link que resolvemos.',
    'Pegadinha resolvida! Faltou o link de uma atividade no grupo. Posta lá, volte a correr e pronto. 💪',
    'Achamos! Não é bug: você andou muitos km e deixou link pra trás. '
    'Posta todos no grupo e eles entram automaticamente.',
    'Mistério dos km resolvido: o link ficou dormindo no seu Strava. Acorda ele e posta no grupo! ⏰',
    'Não foi o sistema, foi a correria do dia a dia: escapou um link. '
    'Posta no grupo que a gente cuida do resto. 🙂',
    'Seus km estão salvos no Strava, só faltou o convite pro grupo. Manda o link e bora! 🎉',
]

import logging
logger = logging.getLogger(__name__)

_STRAVA_ID_RE = re.compile(r'^\d{1,20}$')
_PERIODO_RE = re.compile(r'^(\d{4})-(\d{2})$')
# Uma conferencia por IP a cada N segundos (por worker) + teto de jobs ativos:
# protege o Strava e o proprio NAS de cliques repetidos.
_RATE_MIN_SECONDS = 20
_MAX_JOBS_ATIVOS = 2
_CACHE_TTL_SECONDS = 45 * 60
_MAX_DETALHES_FALTANTES = 6
# Folga ao comparar o total do Strava com a soma do ranking. O Strava arredonda
# o total do mes para 0,1 km e soma TODAS as modalidades — inclusive as que o
# Movimente-se nao conta. Diferenca pequena, portanto, nao e km sumido.
_TOLERANCIA_KM = 1.0
# Teto de links que o proprio atleta pode lancar pela conferencia por mes: o
# objetivo do grupo e POSTAR no grupo (mostrar atividade, incentivar os outros),
# entao a conferencia so cobre o esquecimento pontual, nao substitui o grupo.
_MAX_LANCAMENTOS = 3
_LANC_SCHEMA_READY = False
_ultimo_lancamento_ip = {}


def _ensure_lancamentos_schema():
    global _LANC_SCHEMA_READY
    if _LANC_SCHEMA_READY:
        return
    with db.engine.begin() as conn:
        conn.execute(db.text("""
            CREATE TABLE IF NOT EXISTS conferencia_lancamentos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                strava_id VARCHAR(50) NOT NULL,
                periodo VARCHAR(7) NOT NULL,
                activity_id VARCHAR(50),
                ip VARCHAR(64),
                criado_em DATETIME
            )
        """))
        conn.execute(db.text(
            "CREATE INDEX IF NOT EXISTS idx_conf_lanc "
            "ON conferencia_lancamentos(cidade, strava_id, periodo)"
        ))
    _LANC_SCHEMA_READY = True


def _lancamentos_count(cidade, sid, periodo):
    _ensure_lancamentos_schema()
    return db.session.execute(
        db.text("SELECT COUNT(*) FROM conferencia_lancamentos "
                "WHERE cidade=:c AND strava_id=:s AND periodo=:p"),
        {'c': cidade, 's': str(sid), 'p': periodo},
    ).scalar() or 0
_ultimo_por_ip = {}
_jobs_ativos = set()
_jobs_lock = threading.Lock()
# Namespace fixo para o nome deterministico do cache (uuid5).
_CACHE_NS = uuid.UUID('7c8f5a52-0000-4000-8000-6d6f76696d65')


def _slugify_city(cidade):
    # Translitera acentos p/ endereco limpo (mesma regra do ranking).
    import unicodedata
    texto = unicodedata.normalize('NFKD', (cidade or '').strip())
    texto = texto.encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^A-Za-z0-9]+', '-', texto).strip('-').lower() or 'cidade'


def _cidade_por_slug(slug):
    # CIDADE INATIVA NAO TEM PAGINA PUBLICA (19/09/2026, regra do dono — P-135).
    from app import consolidado as _cons
    for cidade in _cons.cidades_ativas():
        if _slugify_city(cidade) == (slug or '').lower():
            return cidade
    return None


def _feedback_url(cidade):
    """URL publica de feedback/pedido de revisao da cidade (mesma do ranking)."""
    try:
        from app.blueprints.ranking import _feedback_public_url
        return _feedback_public_url(cidade)
    except Exception:
        return f'/ranking/feedback/{_slugify_city(cidade)}'


def _nome_mes(mes):
    nomes = ['', 'Janeiro', 'Fevereiro', 'Março', 'Abril', 'Maio', 'Junho',
             'Julho', 'Agosto', 'Setembro', 'Outubro', 'Novembro', 'Dezembro']
    return nomes[mes] if 1 <= mes <= 12 else str(mes)


def _atletas_da_cidade(cidade):
    """[{sid, nome, meses:[YYYY-MM...], foto}] com o mes atual sempre disponivel."""
    linhas = db.session.execute(
        db.text(
            "SELECT a.id_usuario_strava, a.nome_usuario, "
            "GROUP_CONCAT(DISTINCT substr(a.data_hora, 1, 7)) meses, at.foto_url "
            "FROM activities a "
            "LEFT JOIN atletas at ON at.strava_id = a.id_usuario_strava "
            "WHERE a.cidade = :c AND a.status != 'removida' "
            "AND a.id_usuario_strava != '0' AND a.data_hora IS NOT NULL "
            "GROUP BY a.id_usuario_strava"
        ),
        {'c': cidade},
    ).fetchall()
    mes_atual = datetime.now().strftime('%Y-%m')
    atletas = []
    for sid, nome, meses, foto in linhas:
        lista_meses = sorted(set((meses or '').split(',')) | {mes_atual}, reverse=True)
        atletas.append({
            'sid': str(sid),
            'nome': (nome or 'Atleta').strip(),
            'meses': [m for m in lista_meses if m],
            'foto': foto or '',
        })
    atletas.sort(key=lambda a: a['nome'].lower())
    return atletas


def _cache_name(cidade, sid, periodo):
    return 'confcache_' + str(uuid.uuid5(_CACHE_NS, f'{cidade}|{sid}|{periodo}'))


def _cache_fresco(nome):
    payload = jobstore.read(nome, base_dir=jobstore.CACHE_DIR)
    if not payload:
        return None
    try:
        if time.time() - float(payload.get('cached_at') or 0) > _CACHE_TTL_SECONDS:
            return None
    except (TypeError, ValueError):
        return None
    return payload


def _ip_cliente():
    rota = request.access_route
    return (rota[0] if rota else request.remote_addr) or 'desconhecido'


@conferencia_bp.route('/')
def escolher_cidade():
    configs = Config.query.order_by(Config.cidade).all()
    if len(configs) == 1:
        return redirect(url_for(
            'conferencia.pagina', city_slug=_slugify_city(configs[0].cidade)))
    return render_template(
        'conferencia/cidades.html',
        cidades=[{'nome': c.cidade, 'slug': _slugify_city(c.cidade)} for c in configs],
    )


@conferencia_bp.route('/<city_slug>')
def pagina(city_slug):
    cidade = _cidade_por_slug(city_slug)
    if not cidade:
        abort(404)
    # ?atleta=<strava_id>&periodo=AAAA-MM abre a tela ja preenchida.
    # Existe por causa do aviso de atividade de outro mes: mandar "abra a
    # conferencia, ache a pessoa na lista, escolha o mes" e tres passos para
    # quem esta lendo e-mail no celular. Os valores so PRE-SELECIONAM — quem
    # confere e valida continua sendo o POST /conferir.
    sel_atleta = (request.args.get('atleta') or '').strip()
    if not _STRAVA_ID_RE.match(sel_atleta):
        sel_atleta = ''
    sel_periodo = (request.args.get('periodo') or '').strip()
    if not _PERIODO_RE.match(sel_periodo):
        sel_periodo = ''
    return render_template(
        'conferencia/conferencia.html',
        cidade=cidade,
        city_slug=city_slug,
        atletas=_atletas_da_cidade(cidade),
        mes_atual=datetime.now().strftime('%Y-%m'),
        sel_atleta=sel_atleta,
        sel_periodo=sel_periodo,
    )


@conferencia_bp.route('/<city_slug>/conferir', methods=['POST'])
def conferir(city_slug):
    cidade = _cidade_por_slug(city_slug)
    if not cidade:
        abort(404)
    sid = (request.form.get('atleta') or '').strip()
    periodo = (request.form.get('periodo') or '').strip()
    m = _PERIODO_RE.match(periodo)
    if not _STRAVA_ID_RE.match(sid) or not m:
        return render_template(
            'conferencia/erro.html', city_slug=city_slug,
            mensagem='Escolha o seu nome e o mês antes de conferir.'), 400
    ano, mes = int(m.group(1)), int(m.group(2))
    agora = datetime.now()
    if not (1 <= mes <= 12) or not (2009 <= ano <= agora.year) or \
            (ano == agora.year and mes > agora.month):
        return render_template(
            'conferencia/erro.html', city_slug=city_slug,
            mensagem='Esse mês ainda não existe no ranking.'), 400

    cache = _cache_name(cidade, sid, periodo)
    if _cache_fresco(cache):
        return redirect(url_for('conferencia.resultado', nome_cache=cache,
                                city_slug=city_slug))

    ip = _ip_cliente()
    ultimo = _ultimo_por_ip.get(ip, 0)
    if time.time() - ultimo < _RATE_MIN_SECONDS:
        return render_template(
            'conferencia/erro.html', city_slug=city_slug,
            mensagem='Calma! Aguarde alguns segundos e tente de novo.'), 429
    with _jobs_lock:
        if len(_jobs_ativos) >= _MAX_JOBS_ATIVOS:
            return render_template(
                'conferencia/erro.html', city_slug=city_slug,
                mensagem='Muitas conferências ao mesmo tempo. '
                         'Espere um minutinho e tente novamente.'), 429
        job_name = 'conf_' + str(uuid.uuid4())
        _jobs_ativos.add(job_name)
    _ultimo_por_ip[ip] = time.time()

    jobstore.write(job_name, {
        'stage': 'running',
        'message': 'Consultando o Strava com calma... isso leva alguns segundos.',
        'pct': 10,
        'ts': time.time(),
    })
    app = current_app._get_current_object()
    threading.Thread(
        target=_rodar_conferencia,
        args=(app, job_name, cidade, city_slug, sid, ano, mes, cache),
        daemon=True,
    ).start()
    return render_template(
        'conferencia/aguarde.html',
        city_slug=city_slug,
        job_name=job_name,
        nome_cache=cache,
    )


_JOB_STALE_SECONDS = 4 * 60


@conferencia_bp.route('/status/<job_name>')
def status(job_name):
    payload = jobstore.read(job_name)
    if not payload:
        return jsonify(stage='unknown', message='Conferência não encontrada.', pct=0)
    stage = payload.get('stage', 'running')
    # Job interrompido (ex.: reinicio do servidor no meio): avisa em vez de
    # deixar a pessoa esperando para sempre. Refazer e barato (ha cache).
    try:
        idade = time.time() - float(payload.get('ts') or 0)
    except (TypeError, ValueError):
        idade = None
    if stage == 'running' and (idade is None or idade > _JOB_STALE_SECONDS):
        return jsonify(
            stage='error',
            message='A conferência foi interrompida. Volte e tente de novo, por favor.',
            pct=0,
        )
    return jsonify(
        stage=stage,
        message=payload.get('message', ''),
        pct=payload.get('pct', 0),
    )


@conferencia_bp.route('/<city_slug>/resultado/<nome_cache>')
def resultado(city_slug, nome_cache):
    cidade = _cidade_por_slug(city_slug)
    if not cidade or not str(nome_cache).startswith('confcache_'):
        abort(404)
    payload = jobstore.read(nome_cache, base_dir=jobstore.CACHE_DIR)
    if not payload or payload.get('cidade') != cidade:
        return render_template(
            'conferencia/erro.html', city_slug=city_slug,
            mensagem='O resultado expirou. Faça a conferência novamente.'), 410
    # Estado do "lancar link faltante": quantos ja lancou e se ainda pode.
    ja_lancou = _lancamentos_count(cidade, payload.get('sid'), payload.get('periodo'))
    restantes = max(0, _MAX_LANCAMENTOS - ja_lancou)
    return render_template('conferencia/resultado.html',
                           r=payload, city_slug=city_slug, nome_cache=nome_cache,
                           lancar_restantes=restantes, lancar_max=_MAX_LANCAMENTOS)


@conferencia_bp.route('/<city_slug>/lancar', methods=['POST'])
def lancar_faltante(city_slug):
    """O proprio atleta lanca no ranking um link que esqueceu de postar.

    Trava: no maximo _MAX_LANCAMENTOS por atleta/mes. A atividade e raspada e
    so entra se for do proprio atleta e do mes conferido (nunca link de outra
    pessoa ou de outro periodo). Rate-limit por IP.
    """
    cidade = _cidade_por_slug(city_slug)
    if not cidade:
        abort(404)
    dados = request.get_json(silent=True) or request.form
    sid = (dados.get('sid') or '').strip()
    periodo = (dados.get('periodo') or '').strip()
    url = (dados.get('url') or '').strip()
    m = _PERIODO_RE.match(periodo)
    if not _STRAVA_ID_RE.match(sid) or not m or 'strava.com/activities/' not in url.lower():
        return jsonify(ok=False, error='Dados inválidos.'), 400
    ano, mes = int(m.group(1)), int(m.group(2))

    # Rate-limit leve por IP (dispara scraping).
    ip = _ip_cliente()
    if time.time() - _ultimo_lancamento_ip.get(ip, 0) < 8:
        return jsonify(ok=False, error='Calma! Aguarde alguns segundos.'), 429

    if _lancamentos_count(cidade, sid, periodo) >= _MAX_LANCAMENTOS:
        return jsonify(ok=False, limite=True,
                       error=f'Limite de {_MAX_LANCAMENTOS} links atingido. '
                             'Poste os demais no grupo — é lá que a turma vê seu esforço!'), 429

    _ultimo_lancamento_ip[ip] = time.time()
    from app.blueprints.upload import _process_single_strava_url, _invalidar_consolidacao
    try:
        item = _process_single_strava_url(
            url, cidade=cidade, admin_id=None,
            only_current_month=False, periodos={(ano, mes)},
        )
    except Exception as exc:
        db.session.rollback()
        logger.error('Erro ao lancar link faltante: %s', exc, exc_info=True)
        return jsonify(ok=False, error='Não foi possível processar agora. Tente de novo.'), 500

    if item.get('status') != 'salvo':
        # duplicado / fora do periodo / erro: nao conta no limite.
        return jsonify(ok=False, error=item.get('message') or 'Não foi possível lançar este link.'), 422

    m_act = re.search(r'/activities/(\d+)', url)
    activity_id = m_act.group(1) if m_act else ''

    # Confere se a atividade e mesmo do atleta conferido (nunca de terceiro).
    act = Activity.query.filter_by(cidade=cidade, id_atividade=activity_id).first()
    if act and str(act.id_usuario_strava) != str(sid):
        # Era treino em grupo de outra pessoa: desfaz para nao poluir o ranking.
        db.session.delete(act)
        db.session.commit()
        return jsonify(ok=False, error='Esse link é de uma atividade de outra pessoa.'), 422

    db.session.execute(
        db.text("INSERT INTO conferencia_lancamentos "
                "(cidade, strava_id, periodo, activity_id, ip, criado_em) "
                "VALUES (:c, :s, :p, :a, :ip, :em)"),
        {'c': cidade, 's': sid, 'p': periodo, 'a': activity_id,
         'ip': ip[:64], 'em': datetime.now()},
    )
    db.session.commit()
    _invalidar_consolidacao(cidade, {(ano, mes)})
    # Invalida o cache da conferencia para o resultado refletir o novo lancamento.
    jobstore.delete(_cache_name(cidade, sid, periodo), base_dir=jobstore.CACHE_DIR)
    restantes = max(0, _MAX_LANCAMENTOS - _lancamentos_count(cidade, sid, periodo))
    return jsonify(ok=True, restantes=restantes,
                   message='Link lançado! Ele já entra no ranking. 🎉')


def _atualizar_foto_atleta(sid, cookie, throttle):
    """Aproveita a sessao ativa para guardar/renovar a foto do atleta."""
    try:
        atleta = Atleta.query.filter_by(strava_id=str(sid)).first()
        if atleta and atleta.foto_url and atleta.foto_atualizada_em and \
                (datetime.utcnow() - atleta.foto_atualizada_em).days < 30:
            return
        foto = fetch_athlete_avatar_url(sid, cookie, throttle=throttle)
        if not foto:
            return
        if not atleta:
            atleta = Atleta(strava_id=str(sid))
            db.session.add(atleta)
        atleta.foto_url = foto
        atleta.foto_atualizada_em = datetime.utcnow()
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning('Foto do atleta %s nao atualizada: %r', sid, exc)


def _rodar_conferencia(app, job_name, cidade, city_slug, sid, ano, mes, cache):
    with app.app_context():
        def _st(message, pct):
            jobstore.write(job_name, {
                'stage': 'running', 'message': message, 'pct': pct,
                'ts': time.time(),
            })
        try:
            inicio = datetime(ano, mes, 1)
            fim = datetime(ano + 1, 1, 1) if mes == 12 else datetime(ano, mes + 1, 1)
            linhas = (
                Activity.query
                .filter(
                    Activity.cidade == cidade,
                    Activity.id_usuario_strava == str(sid),
                    Activity.data_hora >= inicio,
                    Activity.data_hora < fim,
                    Activity.status != 'removida',
                )
                .order_by(Activity.data_hora)
                .all()
            )
            nome_atleta = linhas[0].nome_usuario if linhas else ''
            if not nome_atleta:
                qualquer = (Activity.query
                            .filter_by(cidade=cidade, id_usuario_strava=str(sid))
                            .first())
                nome_atleta = qualquer.nome_usuario if qualquer else 'Atleta'
            db_ids = {str(a.id_atividade) for a in linhas}
            registradas = [
                {
                    'dia': a.data_hora.strftime('%d/%m'),
                    'km': round(a.distancia or 0, 2),
                    'modalidade': ('Pedalada' if a.modalidade == 'ciclismo'
                                   else 'Corrida/Caminhada'),
                    'url': a.link_original or
                           f'https://www.strava.com/activities/{a.id_atividade}',
                }
                for a in linhas
            ]
            total_km = round(sum(a.distancia or 0 for a in linhas), 2)

            _st('Lendo o seu mês no Strava...', 30)
            cookie = secretstore.get(secretstore.STRAVA_SESSION, scope=cidade)
            strava_ok = False
            erro_strava = ''
            faltantes = []
            faltantes_sem_detalhe = 0
            km_strava = None
            km_nao_explicado = None
            throttle = build_strava_throttle(mode='mes_atual', cidade=cidade)
            if not cookie:
                erro_strava = 'sessao'
            else:
                try:
                    # Uma requisicao traz as duas verdades: a LISTA (quais
                    # atividades existem) e o TOTAL que o proprio Strava mostra
                    # (quanto km). O total e o que impede esta tela de dizer
                    # "tudo certo" quando a lista falha — foi exatamente assim
                    # que sumiram 136 km de uma atleta sem ninguem perceber.
                    ids_strava, km_strava = fetch_athlete_month_summary(
                        sid, ano, mes, cookie, throttle=throttle,
                    )
                    strava_ok = True
                    # O feed mensal do perfil inclui atividades de TREINOS EM
                    # GRUPO (de outros atletas). So podemos acusar falta depois
                    # de confirmar, na pagina da atividade, que o dono e o
                    # proprio atleta conferido.
                    ids_faltantes = [i for i in ids_strava if str(i) not in db_ids]
                    detalhar = ids_faltantes[:_MAX_DETALHES_FALTANTES]
                    faltantes_sem_detalhe = len(ids_faltantes) - len(detalhar)
                    for pos, aid in enumerate(detalhar, start=1):
                        _st(f'Verificando atividade ({pos}/{len(detalhar)})...',
                            40 + int(50 * pos / max(len(detalhar), 1)))
                        url = f'https://www.strava.com/activities/{aid}'
                        try:
                            dados = parse_strava_activity(url, throttle=throttle)
                        except (ScrapePermanent, ScrapeTimeout, StravaRateLimited):
                            dados = None
                        if not dados:
                            # Sem confirmacao de dono nao se acusa falta.
                            faltantes_sem_detalhe += 1
                            continue
                        if str(dados.get('id_usuario_strava')) != str(sid):
                            continue  # treino em grupo: atividade de outra pessoa
                        item = {'url': url, 'dia': '', 'km': None, 'modalidade': ''}
                        if dados.get('data_hora'):
                            item['dia'] = dados['data_hora'].strftime('%d/%m')
                        if dados.get('distancia'):
                            item['km'] = round(dados['distancia'], 2)
                        item['modalidade'] = (
                            'Pedalada' if dados.get('modalidade') == 'ciclismo'
                            else 'Corrida/Caminhada'
                            if dados.get('modalidade') == 'corrida_caminhada'
                            else '')
                        faltantes.append(item)
                    # Total do Strava - o que ja esta no ranking - o que a
                    # lista acabou de explicar. O que sobra e km que existe la e
                    # ninguem aqui sabe de onde veio: nao acusa nada sozinho,
                    # mas proibe a tela de prometer que esta tudo certo.
                    if km_strava is not None:
                        km_confirmado = sum((f['km'] or 0) for f in faltantes)
                        km_nao_explicado = round(
                            km_strava - total_km - km_confirmado, 2)
                    _atualizar_foto_atleta(sid, cookie, throttle)
                except StravaListaVazia as exc:
                    # A pagina veio inteira e nada foi reconhecido: o Strava
                    # mudou o formato. Isso NUNCA pode virar "esta tudo certo".
                    erro_strava = 'formato'
                    logger.error('Conferencia de %s em %02d/%04d: %s',
                                 sid, mes, ano, exc)
                except StravaAuthRequired:
                    erro_strava = 'sessao'
                    secretstore.record_session_health(
                        cidade, False, 'Sessao recusada durante a conferencia publica.')
                except StravaRateLimited:
                    erro_strava = 'limite'
                except ScrapeTimeout:
                    erro_strava = 'rede'

            # Frase sorteada: so faz sentido quando conseguimos comparar com o
            # Strava. Com falta -> tom "achamos link"; sem falta -> "tudo certo".
            contas_batem = (km_nao_explicado is None
                            or km_nao_explicado <= _TOLERANCIA_KM)
            if strava_ok and faltantes:
                frase = random.choice(_FRASES_FALTA)
                frase_tipo = 'falta'
            elif strava_ok and contas_batem:
                frase = random.choice(_FRASES_TUDO_CERTO)
                frase_tipo = 'ok'
            else:
                # Sem faltante detalhado, mas com km sobrando no total: nao da
                # para prometer "tudo certo" nem para acusar falta. A tela
                # mostra a conta e oferece o pedido de revisao.
                frase = ''
                frase_tipo = 'duvida' if strava_ok else ''

            atleta_obj = Atleta.query.filter_by(strava_id=str(sid)).first()
            resultado = {
                'cidade': cidade,
                'cached_at': time.time(),
                'gerado_em': datetime.now().strftime('%d/%m/%Y %H:%M'),
                'sid': str(sid),
                'nome': nome_atleta,
                'foto_url': atleta_obj.foto_url if atleta_obj else None,
                'periodo_label': f'{_nome_mes(mes)}/{ano}',
                'periodo': f'{ano:04d}-{mes:02d}',
                'registradas': registradas,
                'total_km': total_km,
                'strava_ok': strava_ok,
                'km_strava': km_strava,
                'km_nao_explicado': km_nao_explicado,
                'contas_batem': contas_batem,
                'erro_strava': erro_strava,
                'faltantes': faltantes,
                'faltantes_sem_detalhe': faltantes_sem_detalhe,
                'frase': frase,
                'frase_tipo': frase_tipo,
                'feedback_url': _feedback_url(cidade),
            }
            jobstore.write(cache, resultado, base_dir=jobstore.CACHE_DIR)
            jobstore.write(job_name, {
                'stage': 'done', 'message': 'Conferência pronta!', 'pct': 100,
                'ts': time.time(),
            })
        except Exception as exc:
            logger.error('Erro na conferencia publica: %s', exc, exc_info=True)
            jobstore.write(job_name, {
                'stage': 'error',
                'message': 'Não conseguimos concluir agora. Tente de novo em alguns minutos.',
                'pct': 0, 'ts': time.time(),
            })
        finally:
            with _jobs_lock:
                _jobs_ativos.discard(job_name)
