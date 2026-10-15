# app/blueprints/athlete.py
# -*- coding: utf-8 -*-
"""
Dashboard publico do atleta: /ath/<strava_id>
Nao requer login. Exibe apenas dados publicos (km, atividades, status anticheat).
Seguranca:
  - strava_id validado como numerico (impede path traversal / injecao)
  - Dados exibidos sao todos publicos (scraped do Strava publico)
  - Nenhuma informacao de admin, senha, email ou outro atleta e exposta
  - html.escape aplicado a todos os valores interpolados no template via Jinja2 autoescaping
"""
import re
import os
import time
from flask import Blueprint, render_template, abort, request, jsonify, redirect, url_for, flash, current_app
from flask_login import current_user
from app import db, signing_keys
from app.models import Activity, FlagAnticheat, ControleDisciplinar, Atleta, MonthlyBackup
from sqlalchemy import extract, inspect, text, func
from datetime import datetime, timedelta
from uuid import uuid4
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

athlete_bp = Blueprint('athlete', __name__, url_prefix='/ath')

_STRAVA_ID_RE = re.compile(r'^\d{1,20}$')
_SPONSOR_SCHEMA_READY = False
_REVIEW_LINK_MAX_AGE_SECONDS = 90 * 24 * 60 * 60


def _slugify_city(cidade):
    # Translitera acentos p/ endereco limpo (mesma regra do ranking).
    import unicodedata
    texto = unicodedata.normalize('NFKD', (cidade or '').strip())
    texto = texto.encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'[^A-Za-z0-9]+', '-', texto).strip('-').lower() or 'cidade'


def _normalizar_nome_atleta(nome):
    return re.sub(r'\s+', ' ', (nome or '').strip()).lower()


def _can_manage_adjustment():
    return (
        current_user.is_authenticated and
        current_user.pode_administrar
    )


def _review_link_serializer():
    # Ver _ranking_links_serializer: assina com a chave atual, valida tambem
    # com a legada para os links de revisao ja enviados aos atletas.
    return URLSafeTimedSerializer(
        signing_keys(),
        salt='movimente-se-review-link',
    )


def gerar_link_revisao_publica(flag):
    """Gera URL assinada para o atleta justificar somente uma flag."""
    atleta = flag.atleta or Atleta.query.get(flag.atleta_id)
    if not atleta:
        return ''
    token = _review_link_serializer().dumps({
        'flag_id': int(flag.id),
        'strava_id': str(atleta.strava_id),
    })
    return url_for('athlete.revisao_publica', token=token, _external=True)


def _carregar_flag_link_publico(token):
    try:
        payload = _review_link_serializer().loads(
            token, max_age=_REVIEW_LINK_MAX_AGE_SECONDS
        )
    except SignatureExpired:
        abort(410)
    except BadSignature:
        abort(404)

    flag = FlagAnticheat.query.get_or_404(payload.get('flag_id'))
    atleta = Atleta.query.filter_by(
        strava_id=str(payload.get('strava_id') or '')
    ).first_or_404()
    if flag.atleta_id != atleta.id:
        abort(404)
    return flag, atleta


def _salvar_evidencia_atleta(flag, arquivo):
    if not arquivo or not arquivo.filename:
        return

    from werkzeug.utils import secure_filename
    from app.models import EvidenciaRevisao

    tipos_ok = {
        'image/jpeg', 'image/png', 'image/gif', 'image/webp',
        'application/pdf', 'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'text/plain',
    }
    mime = arquivo.content_type or ''
    arquivo.seek(0, 2)
    size = arquivo.tell()
    arquivo.seek(0)
    if mime not in tipos_ok or size > 10 * 1024 * 1024:
        raise ValueError('Evidencia invalida. Envie imagem, PDF, DOCX ou TXT de ate 10 MB.')

    pasta = os.path.join(current_app.config['UPLOAD_FOLDER'], 'evidencias', str(flag.id))
    os.makedirs(pasta, exist_ok=True)
    nome_seguro = secure_filename(arquivo.filename)
    if not nome_seguro:
        raise ValueError('Nome de arquivo invalido.')
    nome_final = f"{int(time.time())}_{nome_seguro}"
    caminho = os.path.join(pasta, nome_final)
    arquivo.save(caminho)
    db.session.add(EvidenciaRevisao(
        flag_id=flag.id,
        arquivo_nome=nome_seguro,
        arquivo_path=caminho,
        tipo_mime=mime,
        tamanho=size,
        enviado_por='atleta',
    ))


def _submeter_revisao(flag, atleta):
    """Regra compartilhada entre o dashboard e o link publico de revisao."""
    if flag.status in ('em_revisao', 'aprovada', 'rejeitada'):
        return False, 'Esta flag ja esta em status: ' + flag.status, None

    data_atividade = flag.atividade.data_hora if flag.atividade else None
    mes_ano = (
        data_atividade.strftime('%Y-%m') if data_atividade
        else datetime.now().strftime('%Y-%m')
    )
    cd = ControleDisciplinar.query.filter_by(
        atleta_id=atleta.id, mes_ano=mes_ano
    ).first()
    pedidos = (cd.pedidos_revisao or 0) if cd else 0
    if pedidos >= 3:
        return False, 'Limite de 3 pedidos de revisao por mes atingido.', None

    justificativa = request.form.get('justificativa', '').strip()
    try:
        _salvar_evidencia_atleta(flag, request.files.get('evidencia'))
    except ValueError as exc:
        return False, str(exc), None

    flag.status = 'em_revisao'
    flag.revisao_solicitada = True
    flag.solicitado_revisao_em = datetime.now()
    if justificativa:
        flag.justificativa_atleta = justificativa
    if cd:
        cd.pedidos_revisao = pedidos + 1
    else:
        db.session.add(ControleDisciplinar(
            atleta_id=atleta.id, mes_ano=mes_ano, pedidos_revisao=1
        ))
    db.session.commit()
    return True, '', 3 - (pedidos + 1)


def _manual_adjustment_redirect(strava_id, name_key=None, cidade=None):
    if request.form.get('return_to') == 'admin_adjustment':
        return redirect(url_for(
            'admin.ajuste_manual_atleta',
            strava_id=strava_id,
            name_key=name_key or None,
            cidade=cidade or None,
        ))
    return redirect(url_for('athlete.dashboard', strava_id=strava_id, name_key=name_key or None))


def _ensure_monthly_backup_sponsor_schema():
    global _SPONSOR_SCHEMA_READY
    if _SPONSOR_SCHEMA_READY:
        return

    insp = inspect(db.engine)
    if 'monthly_backup' not in set(insp.get_table_names()):
        _SPONSOR_SCHEMA_READY = True
        return

    with db.engine.begin() as conn:
        for stmt in (
            "ALTER TABLE monthly_backup ADD COLUMN patrocinador_nome_mes VARCHAR(200)",
            "ALTER TABLE monthly_backup ADD COLUMN patrocinador_logo_blob BLOB",
            "ALTER TABLE monthly_backup ADD COLUMN patrocinador_logo_mime VARCHAR(100)",
            "ALTER TABLE monthly_backup ADD COLUMN patrocinador_logo_filename VARCHAR(255)",
        ):
            try:
                conn.execute(text(stmt))
            except Exception as exc:
                msg = str(exc).lower()
                if 'duplicate column name' in msg or 'already exists' in msg:
                    continue
                raise

    _SPONSOR_SCHEMA_READY = True


def _tags_strava(strava_ids_grupo):
    """Uma tag por strava_id do grupo — a de quem postou o link mais recente
    vem marcada como destaque (mostrada maior/vermelha na pagina publica)."""
    recencia = dict(
        db.session.query(Activity.id_usuario_strava, func.max(Activity.data_hora))
        .filter(Activity.id_usuario_strava.in_(strava_ids_grupo), Activity.status != 'removida')
        .group_by(Activity.id_usuario_strava).all()
    )
    destaque = max(recencia, key=recencia.get) if recencia else None
    return [{'strava_id': sid, 'destaque': sid == destaque} for sid in strava_ids_grupo]


@athlete_bp.route('/<strava_id>')
def dashboard(strava_id):
    """
    Dashboard publico do atleta identificado pelo strava_id.
    Acessivel sem login. Dados exibidos: nome, km, atividades, status anticheat.
    """
    # Valida que strava_id e puramente numerico (evita path traversal e injecao)
    if not _STRAVA_ID_RE.match(strava_id):
        abort(404)

    # QUEM PEDIU RESERVA NAO TEM PAGINA PUBLICA (28/08/2026, regra do dono).
    # Vale so para visitante: admin logado continua enxergando, senao a
    # marcacao viraria uma forma de esconder atleta de quem administra.
    if not current_user.is_authenticated:
        try:
            reservado = db.session.query(Atleta.oculto_no_publico).filter_by(
                strava_id=str(strava_id)).scalar()
        except Exception:
            reservado = False
        if reservado:
            abort(404)
    _ensure_monthly_backup_sponsor_schema()
    name_key = _normalizar_nome_atleta(request.args.get('name_key'))

    now   = datetime.now()
    month = now.month
    year  = now.year
    mes_ano = f"{year}-{month:02d}"

    # Atleta mesclado (07/09/2026): resolve o grupo aqui, antes de tudo, para
    # que a busca de atividade ja considere todos os strava_id fundidos —
    # funciona a partir de qualquer id do grupo, sem redirect.
    atleta_url = Atleta.query.filter_by(strava_id=str(strava_id)).first()
    atleta_obj = Atleta.resolver_canonico(atleta_url) if atleta_url else None
    strava_ids_grupo = atleta_obj.strava_ids_do_grupo() if atleta_obj else [strava_id]

    # Busca atividades do atleta (todas as cidades — dashboard e pessoal)
    # Ordena por data decrescente para mostrar as mais recentes primeiro
    query = Activity.query.filter(Activity.id_usuario_strava.in_(strava_ids_grupo))
    if strava_id == '0' and name_key:
        linhas = [
            a for a in query.order_by(Activity.data_hora.desc()).all()
            if _normalizar_nome_atleta(a.nome_usuario) == name_key
        ]
    else:
        linhas = query.order_by(Activity.data_hora.desc()).all()

    if not linhas:
        abort(404)

    # A mesma atividade pode existir em mais de uma cidade (historico
    # consolidado entre grupos). Na pagina pessoal ela aparece e soma UMA
    # unica vez, listando as cidades onde esta registrada.
    grupos_atividade = {}
    ordem_atividades = []
    for a in linhas:
        chave = a.id_atividade or f'row-{a.id}'
        if chave not in grupos_atividade:
            grupos_atividade[chave] = []
            ordem_atividades.append(chave)
        grupos_atividade[chave].append(a)

    atividades = []
    cidades_por_atividade = {}
    for chave in ordem_atividades:
        grupo = grupos_atividade[chave]
        # Removida pelo anticheat em qualquer cidade: mostra o estado removido;
        # senao, a linha mais antiga (coleta original) representa a atividade.
        representante = next(
            (x for x in grupo if x.status == 'removida'),
            min(grupo, key=lambda x: x.id),
        )
        atividades.append(representante)
        cidades_por_atividade[representante.id] = sorted(
            {x.cidade for x in grupo if x.cidade}
        )

    nome = atividades[0].nome_usuario

    # KM do mes corrente por modalidade
    atividades_mes = [
        a for a in atividades
        if a.status != 'removida' and a.data_hora and a.data_hora.month == month and a.data_hora.year == year
    ]
    atividades_ano = [
        a for a in atividades
        if a.status != 'removida' and a.data_hora and a.data_hora.year == year
    ]
    atividades_removidas_mes = [
        a for a in atividades
        if a.status == 'removida' and a.data_hora and a.data_hora.month == month and a.data_hora.year == year
    ]

    # criado_em fica em UTC no banco; a diferenca now-utcnow devolve o fuso do
    # servidor sem depender de configuracao (Brasil nao tem mais horario de verao).
    fuso_local = datetime.now() - datetime.utcnow()

    def _activity_display(a):
        hora_inicio = a.data_hora.strftime('%H:%M') if a.data_hora else '-'
        if a.data_hora and a.tempo_minutos:
            hora_fim = (a.data_hora + timedelta(minutes=a.tempo_minutos)).strftime('%H:%M')
        else:
            hora_fim = '-'
        if a.criado_em:
            coletada_em = (a.criado_em + fuso_local).strftime('%d/%m/%y %H:%M')
        else:
            coletada_em = '-'
        return {
            'obj': a,
            'hora_inicio': hora_inicio,
            'hora_fim': hora_fim,
            'coletada_em': coletada_em,
            'cidades': ', '.join(cidades_por_atividade.get(a.id, [a.cidade or ''])),
            'acumulado': None,
        }

    def _montar_tabela(lista):
        """Monta as linhas em ordem CRESCENTE de data, com o acumulado por
        modalidade (soma corrente separada de corrida e ciclismo) e os totais
        de cada modalidade. Corrida e ciclismo nunca se somam."""
        ordenadas = sorted(
            lista, key=lambda a: a.data_hora or datetime.min
        )
        acumulado = {}
        linhas = []
        for a in ordenadas:
            display = _activity_display(a)
            if a.status != 'removida':
                mod = (a.modalidade or 'outro')
                acumulado[mod] = acumulado.get(mod, 0) + (a.distancia or 0)
                display['acumulado'] = round(acumulado[mod], 2)
            linhas.append(display)
        totais = {
            'corrida_caminhada': round(sum(
                a.distancia or 0 for a in lista
                if a.status != 'removida' and a.modalidade == 'corrida_caminhada'
            ), 2),
            'ciclismo': round(sum(
                a.distancia or 0 for a in lista
                if a.status != 'removida' and a.modalidade == 'ciclismo'
            ), 2),
        }
        return linhas, totais

    atividades_mes_display, totais_mes_tabela = _montar_tabela(atividades_mes)

    atividades_por_mes = []
    grupos = {}
    for a in atividades:
        if not a.data_hora:
            key = 'sem-data'
            label = 'Sem data'
        else:
            key = a.data_hora.strftime('%Y-%m')
            label = a.data_hora.strftime('%m/%Y')
        grupos.setdefault(key, {'key': key, 'label': label, 'itens': []})
        grupos[key]['itens'].append(a)
    for key in sorted(grupos.keys(), reverse=True):
        linhas_grupo, totais_grupo = _montar_tabela(grupos[key]['itens'])
        atividades_por_mes.append({
            'key': key,
            'label': grupos[key]['label'],
            'activities': linhas_grupo,
            'totais': totais_grupo,
            'tem_removida': any(a.status == 'removida' for a in grupos[key]['itens']),
        })

    def km_mes_mod(mod):
        return round(sum(a.distancia for a in atividades_mes if a.modalidade == mod), 2)

    km_corrida_mes  = km_mes_mod('corrida_caminhada')
    km_ciclismo_mes = km_mes_mod('ciclismo')
    km_corrida_ano = round(sum(a.distancia for a in atividades_ano if a.modalidade == 'corrida_caminhada'), 2)
    km_ciclismo_ano = round(sum(a.distancia for a in atividades_ano if a.modalidade == 'ciclismo'), 2)

    # ===== TRIMESTRE E SEMESTRE (P-137, 20/09/2026) =====
    # Blocos FIXOS do calendário, a mesma regra do ranking e da página pública:
    # jan-mar / abr-jun / jul-set / out-dez, e jan-jun / jul-dez. NÃO são "os
    # últimos 3 ou 6 meses" — em fevereiro o trimestre começa em janeiro.
    # Sai das atividades do ano que a página já tem na mão: nenhuma consulta
    # nova (o NAS tem 490 MB).
    def _km_bloco(mod, largura):
        bloco = (month - 1) // largura
        primeiro, ultimo = bloco * largura + 1, bloco * largura + largura
        return round(sum(a.distancia for a in atividades_ano
                         if a.modalidade == mod and a.data_hora
                         and primeiro <= a.data_hora.month <= ultimo), 2)

    km_corrida_trimestre = _km_bloco('corrida_caminhada', 3)
    km_ciclismo_trimestre = _km_bloco('ciclismo', 3)
    km_corrida_semestre = _km_bloco('corrida_caminhada', 6)
    km_ciclismo_semestre = _km_bloco('ciclismo', 6)
    # Qual bloco o visitante veio ver: o link do ranking do trimestre traz
    # `?periodo=trimestre`, e aí é ele que nasce em destaque.
    periodo_destaque = (request.args.get('periodo') or '').strip().lower()
    if periodo_destaque not in ('trimestre', 'semestre', 'ano'):
        periodo_destaque = ''

    # Total historico
    km_corrida_total  = round(sum(a.distancia for a in atividades if a.modalidade == 'corrida_caminhada'), 2)
    km_ciclismo_total = round(sum(a.distancia for a in atividades if a.modalidade == 'ciclismo'), 2)

    # Status anticheat do mes (simplificado para o publico). `atleta_obj` ja
    # foi resolvido para o cabeca do grupo, acima — usa `atleta_ids_do_grupo()`
    # para tambem enxergar flag/disciplinar gravados no atleta_id antigo,
    # antes de uma eventual mesclagem.
    cd         = None
    flags_mes  = []

    if atleta_obj:
        atleta_ids_grupo = atleta_obj.atleta_ids_do_grupo()
        cds_grupo = ControleDisciplinar.query.filter(
            ControleDisciplinar.atleta_id.in_(atleta_ids_grupo),
            ControleDisciplinar.mes_ano == mes_ano,
        ).all()
        if cds_grupo:
            class _ControleDisciplinarAgregado:
                pass
            cd = _ControleDisciplinarAgregado()
            cd.avisos = sum(c.avisos or 0 for c in cds_grupo)
            cd.remocoes = sum(c.remocoes or 0 for c in cds_grupo)
            cd.total_flags = sum(c.total_flags or 0 for c in cds_grupo)
            cd.atividades_suspeitas = sum(c.atividades_suspeitas or 0 for c in cds_grupo)
            cd.eliminado_do_ranking = any(c.eliminado_do_ranking for c in cds_grupo)
            cd.pedidos_revisao = sum(c.pedidos_revisao or 0 for c in cds_grupo)
            cd.pedidos_revisao_mes = sum(c.pedidos_revisao_mes or 0 for c in cds_grupo)
        # Flags do mes (somente tipo e nivel — sem descricao tecnica interna)
        flags_mes = (
            FlagAnticheat.query
            .filter(FlagAnticheat.atleta_id.in_(atleta_ids_grupo))
            .join(Activity, FlagAnticheat.atividade_id == Activity.id)
            .filter(
                extract('month', Activity.data_hora) == month,
                extract('year',  Activity.data_hora) == year,
            )
            .order_by(FlagAnticheat.id.desc())
            .all()
        )

    # Status resumido
    if cd and cd.eliminado_do_ranking:
        status_label = 'Eliminado do ranking este mes'
        status_class = 'danger'
        status_icon  = 'fa-ban'
    elif cd and (cd.remocoes or 0) > 0:
        status_label = 'Atividade(s) removida(s) este mes'
        status_class = 'warning'
        status_icon  = 'fa-exclamation-triangle'
    elif cd and (cd.total_flags or 0) > 0:
        status_label = 'Em analise'
        status_class = 'info'
        status_icon  = 'fa-flag'
    else:
        status_label = 'Participacao regular'
        status_class = 'success'
        status_icon  = 'fa-check-circle'

    # Cidades onde o atleta participa (todas as linhas, antes do dedup)
    cidades = sorted({a.cidade for a in linhas if a.cidade})
    sponsor_city = atividades[0].cidade if atividades and atividades[0].cidade else None
    sponsor_name = 'A definir'
    sponsor_logo_url = None
    if sponsor_city:
        sponsor_backup = MonthlyBackup.query.filter_by(cidade=sponsor_city, ano=year, mes=month).first()
        if sponsor_backup:
            sponsor_name = sponsor_backup.patrocinador_nome_mes or 'A definir'
            if sponsor_backup.patrocinador_logo_blob:
                sponsor_logo_url = url_for(
                    'ranking.public_sponsor_logo',
                    city_slug=_slugify_city(sponsor_city),
                    year=sponsor_backup.ano,
                    month=sponsor_backup.mes,
                )

    from app.blueprints.ranking import _public_page_context
    pub = _public_page_context(sponsor_city, year, month) if sponsor_city else None

    return render_template(
        'athlete/dashboard.html',
        pub=pub,
        nome=nome,
        strava_id=strava_id,
        tags_strava=_tags_strava(strava_ids_grupo),
        atividades=atividades,
        atividades_mes=atividades_mes,
        atividades_mes_display=atividades_mes_display,
        totais_mes_tabela=totais_mes_tabela,
        km_corrida_mes=km_corrida_mes,
        km_ciclismo_mes=km_ciclismo_mes,
        km_corrida_ano=km_corrida_ano,
        km_ciclismo_ano=km_ciclismo_ano,
        km_corrida_trimestre=km_corrida_trimestre,
        km_ciclismo_trimestre=km_ciclismo_trimestre,
        km_corrida_semestre=km_corrida_semestre,
        km_ciclismo_semestre=km_ciclismo_semestre,
        periodo_destaque=periodo_destaque,
        km_corrida_total=km_corrida_total,
        km_ciclismo_total=km_ciclismo_total,
        cd=cd,
        flags_mes=flags_mes,
        mes_ano=mes_ano,
        month=month,
        year=year,
        status_label=status_label,
        status_class=status_class,
        status_icon=status_icon,
        cidades=cidades,
        atividades_removidas_mes=atividades_removidas_mes,
        atividades_por_mes=atividades_por_mes,
        sponsor_name=sponsor_name,
        sponsor_logo_url=sponsor_logo_url,
        can_manage_adjustment=_can_manage_adjustment(),
        name_key=name_key,
        foto_url=(atleta_obj.foto_url if atleta_obj else None),
        atleta_arquivado=bool(getattr(atleta_obj, 'arquivado', False)) if atleta_obj else False,
        arquivado_motivo=(getattr(atleta_obj, 'arquivado_motivo', '') if atleta_obj else ''),
    )


@athlete_bp.route('/todos/<city_slug>')
def lista_publica(city_slug):
    """Lista pública de atletas de uma cidade (sem login, sem ajustar km).

    Dá acesso à página pública de cada atleta. Perfis arquivados ficam ocultos.
    """
    from app.models import Config, Atleta
    cidade = None
    # CIDADE INATIVA NAO TEM PAGINA PUBLICA (19/09/2026, regra do dono — P-135).
    from app import consolidado as _cons
    for _c in _cons.cidades_ativas():
        if _slugify_city(_c) == (city_slug or '').lower():
            cidade = _c
            break
    if not cidade:
        abort(404)

    linhas = db.session.execute(
        db.text(
            "SELECT a.id_usuario_strava, a.nome_usuario, at.foto_url, "
            "COALESCE(at.arquivado, 0) arquivado "
            "FROM activities a "
            "LEFT JOIN atletas at ON at.strava_id = a.id_usuario_strava "
            "WHERE a.cidade = :c AND a.status != 'removida' "
            "AND a.id_usuario_strava != '0' AND a.data_hora IS NOT NULL "
            "GROUP BY a.id_usuario_strava"
        ),
        {'c': cidade},
    ).fetchall()
    atletas = sorted(
        (
            {
                'sid': str(sid),
                'nome': (nome or 'Atleta').strip(),
                'foto': foto or '',
            }
            for sid, nome, foto, arquivado in linhas
            if not arquivado
        ),
        key=lambda a: a['nome'].lower(),
    )
    from app.blueprints.ranking import _public_page_context
    now = datetime.now()
    pub = _public_page_context(cidade, now.year, now.month)
    return render_template(
        'athlete/lista_publica.html',
        cidade=cidade, city_slug=city_slug, atletas=atletas, pub=pub,
    )


@athlete_bp.route('/<strava_id>/manual-adjustment', methods=['POST'])
def add_manual_adjustment(strava_id):
    if not _can_manage_adjustment():
        abort(403)
    if not _STRAVA_ID_RE.match(str(strava_id)):
        abort(404)

    name_key = _normalizar_nome_atleta(request.form.get('name_key'))
    modalidade = (request.form.get('modalidade') or '').strip()
    if modalidade not in ('corrida_caminhada', 'ciclismo'):
        flash('Modalidade invalida para ajuste manual.', 'danger')
        return _manual_adjustment_redirect(strava_id, name_key)

    try:
        distancia = float(str(request.form.get('distancia', '')).replace(',', '.'))
    except ValueError:
        distancia = 0
    if distancia == 0:
        flash('Informe uma distancia diferente de zero.', 'danger')
        return _manual_adjustment_redirect(strava_id, name_key)

    data_str = (request.form.get('data') or '').strip()
    try:
        data_hora = datetime.strptime(data_str, '%Y-%m-%d')
    except ValueError:
        data_hora = datetime.now()

    base_query = Activity.query.filter_by(id_usuario_strava=strava_id)
    if strava_id == '0' and name_key:
        atividades_base = [
            a for a in base_query.all()
            if _normalizar_nome_atleta(a.nome_usuario) == name_key
        ]
    else:
        atividades_base = base_query.all()
    if not atividades_base:
        abort(404)

    nome = atividades_base[0].nome_usuario
    cidade_form = (request.form.get('cidade') or '').strip()
    cidades_atleta = {a.cidade for a in atividades_base if a.cidade}
    if current_user.is_supremo and cidade_form in cidades_atleta:
        cidade = cidade_form
    else:
        cidade = current_user.cidade_ativa
        if not current_user.is_supremo and cidade not in cidades_atleta:
            abort(403)   # o atleta não tem nada nesta cidade: não se lança km para ele aqui
    atividades_mes_mod = [
        a for a in atividades_base
        if (
            a.cidade == cidade and a.status != 'removida' and
            a.modalidade == modalidade and a.data_hora and
            a.data_hora.year == data_hora.year and a.data_hora.month == data_hora.month
        )
    ]

    velocidades = [a.velocidade_media for a in atividades_mes_mod if a.velocidade_media and a.velocidade_media > 0]
    paces = [a.pace_min_km for a in atividades_mes_mod if a.pace_min_km and a.pace_min_km > 0]
    total_dist = sum(a.distancia or 0 for a in atividades_mes_mod)
    total_elev = sum(a.elevacao_total or 0 for a in atividades_mes_mod)

    if distancia > 0:
        velocidade_media = round(sum(velocidades) / len(velocidades), 2) if velocidades else None
        pace_min_km = round(sum(paces) / len(paces), 2) if modalidade == 'corrida_caminhada' and paces else None
        tempo_minutos = round((distancia / velocidade_media) * 60) if velocidade_media else None
        elevacao_total = round((total_elev / total_dist) * distancia, 1) if total_dist > 0 else None
    else:
        velocidade_media = None
        pace_min_km = None
        tempo_minutos = None
        elevacao_total = None

    ajuste = Activity(
        id_atividade='manual-{}-{}'.format(datetime.utcnow().strftime('%Y%m%d%H%M%S'), uuid4().hex[:8]),
        id_usuario_strava=str(strava_id),
        nome_usuario=nome,
        modalidade=modalidade,
        distancia=round(distancia, 3),
        distancia_raw=round(distancia * 1000, 3),
        data_hora=data_hora,
        cidade=cidade,
        id_admin_upload=current_user.id,
        arquivo_origem='ajuste_manual',
        link_original='',
        telefone_whatsapp=atividades_base[0].telefone_whatsapp,
        tempo_minutos=tempo_minutos,
        elevacao_total=elevacao_total,
        velocidade_media=velocidade_media,
        pace_min_km=pace_min_km,
        status='ativa',
    )
    db.session.add(ajuste)
    db.session.commit()
    if distancia > 0:
        flash('Ajuste manual de quilometragem adicionado.', 'success')
    else:
        flash('Desconto manual de quilometragem aplicado.', 'success')
    return _manual_adjustment_redirect(strava_id, name_key, cidade)


@athlete_bp.route('/review/<int:flag_id>', methods=['POST'])
def solicitar_revisao(flag_id):
    """
    Solicita revisao de uma flag anticheat.
    Requer que o strava_id seja passado como form field ou query param para validacao.
    Maximo de 3 pedidos de revisao por atleta por mes.
    """
    strava_id = request.form.get('strava_id') or request.args.get('strava_id')
    if not strava_id or not _STRAVA_ID_RE.match(str(strava_id)):
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=False, error='strava_id invalido'), 400
        flash('strava_id invalido', 'danger')
        return redirect(url_for('athlete.dashboard', strava_id=strava_id or '0'))

    flag = FlagAnticheat.query.get_or_404(flag_id)

    # Verify flag belongs to this athlete
    atleta = Atleta.query.filter_by(strava_id=str(strava_id)).first_or_404()
    if flag.atleta_id != atleta.id:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=False, error='Flag nao pertence a este atleta'), 403
        flash('Acesso negado', 'danger')
        return redirect(url_for('athlete.dashboard', strava_id=strava_id))

    ok, error, pedidos_restantes = _submeter_revisao(flag, atleta)
    if not ok:
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=False, error=error), 400
        flash(error, 'warning')
        return redirect(url_for('athlete.dashboard', strava_id=strava_id))

    if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(ok=True, pedidos_restantes=pedidos_restantes)

    flash('Pedido de revisao enviado com sucesso! Aguarde a analise do administrador.', 'success')
    return redirect(url_for('athlete.dashboard', strava_id=strava_id))


@athlete_bp.route('/review-link/<token>', methods=['GET', 'POST'])
def revisao_publica(token):
    """Pagina publica e limitada a uma flag para o atleta enviar justificativa."""
    flag, atleta = _carregar_flag_link_publico(token)
    if request.method == 'POST':
        ok, error, _ = _submeter_revisao(flag, atleta)
        if ok:
            flash('Pedido de revisao enviado com sucesso! Aguarde a analise do administrador.', 'success')
        else:
            flash(error, 'warning')
        return redirect(url_for('athlete.revisao_publica', token=token))

    return render_template(
        'athlete/review_flag.html',
        flag=flag,
        atleta=atleta,
        token=token,
    )


@athlete_bp.route('/evidencia/<int:ev_id>/<strava_id>')
def ver_evidencia_atleta(ev_id, strava_id):
    """
    Serve evidência de revisão para o atleta dono da flag.
    Valida que o strava_id corresponde ao atleta da flag antes de servir.
    """
    import os
    from flask import send_file
    from app.models import EvidenciaRevisao, Atleta as _Atleta

    if not _STRAVA_ID_RE.match(str(strava_id)):
        abort(404)

    ev = EvidenciaRevisao.query.get_or_404(ev_id)
    flag = ev.flag
    atleta = _Atleta.query.filter_by(strava_id=str(strava_id)).first_or_404()
    if flag.atleta_id != atleta.id:
        abort(403)
    if not os.path.exists(ev.arquivo_path):
        abort(404)
    return send_file(ev.arquivo_path, download_name=ev.arquivo_nome, as_attachment=False)
