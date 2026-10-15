# app/blueprints/anticheat.py
# -*- coding: utf-8 -*-
"""
Motor anti-cheat do Movimente-se MVP 2.0.
5 detectores sincronos + estrutura para MATCH_KML assincrono (futuro).
Regras de punicao progressiva integradas.
"""
import logging
from datetime import datetime, timedelta
from app import db
from app.models import (
    Activity, FlagAnticheat, ControleDisciplinar, Atleta, Config, AnticheatOverride,
)

logger = logging.getLogger(__name__)

# ===== LIMITES VALIDADOS COM O USUARIO =====
LIMITES = {
    'velocidade_max_corrida':     15.0,   # km/h  (= 4:00 min/km)
    'velocidade_max_ciclismo':    45.0,   # km/h
    'pace_max_corrida_curta':     4.167,  # min/km para distancia < 5 km (= 4:10)
    'pace_max_corrida_longa':     5.0,    # min/km para distancia >= 5 km (= 5:00)
    'elevacao_max_pct':           50.0,   # % da distancia em metros
    'max_atividades_dia':         3,
    'max_atividades_semana':      5,
}

# A quarta atividade suspeita do mesmo atleta no mesmo mes fica fora do
# ranking, mas permanece salva e pode voltar a contar apos a revisao.
ATIVIDADES_SUSPEITAS_PARA_REMOCAO = 4


# ===== HELPERS =====

def _get_ou_criar_atleta(strava_id, nome, telefone=None):
    """Busca ou cria registro de Atleta pelo strava_id.

    O strava_id e a chave definitiva. Nome e telefone sao atualizados
    como snapshots — nunca sobrescrevem um telefone ja registrado.
    """
    from sqlalchemy.exc import IntegrityError
    atleta = Atleta.query.filter_by(strava_id=str(strava_id)).first()
    if not atleta:
        atleta = Atleta(
            strava_id=str(strava_id),
            nome_atual=nome,
            telefone_normalizado=telefone,
        )
        db.session.add(atleta)
        try:
            db.session.flush()
        except IntegrityError:
            # Telefone ja em uso por outro atleta — salvar sem telefone
            db.session.rollback()
            atleta = Atleta.query.filter_by(strava_id=str(strava_id)).first()
            if not atleta:
                atleta = Atleta(
                    strava_id=str(strava_id),
                    nome_atual=nome,
                    telefone_normalizado=None,
                )
                db.session.add(atleta)
                db.session.flush()
    else:
        # `nome_atual` e SNAPSHOT do Strava e continua sendo reescrito: e
        # ele que mostra o nome como esta la hoje. O que NAO se toca aqui e
        # o `nome_exibicao` -- aquele foi escolhido por gente, e um link
        # postado no grupo nao pode desfazer a escolha de alguem
        # (29/08/2026). Era esse o problema que fazia a correcao de nome
        # nao durar: a proxima atividade a apagava.
        if atleta.nome_atual != nome:
            atleta.nome_atual = nome
        # Associa telefone somente se ainda nao tiver um registrado
        if telefone and not atleta.telefone_normalizado:
            # Verificar se o telefone ja esta em uso por outro atleta
            existente = Atleta.query.filter_by(telefone_normalizado=telefone).first()
            if not existente:
                atleta.telefone_normalizado = telefone
    # Atividade nova sendo processada => o perfil voltou a ter uso: desarquiva
    # sozinho (o admin nao precisa reativar na mao) -- EXCETO quando a pessoa
    # pediu para sair de proposito (`saiu_a_pedido`, Fase 3 do plano das
    # melhorias). Duplicata esquecida volta sozinha; pedido de saida, nao --
    # senao um link novo desfaria a saida sem ninguem decidir isso.
    if getattr(atleta, 'arquivado', False) and not getattr(atleta, 'saiu_a_pedido', False) \
            and not getattr(atleta, 'atleta_canonico_id', None):
        atleta.arquivado = False
        atleta.arquivado_em = None
        atleta.arquivado_motivo = None
    # Atleta mesclado (07/09/2026): flag/disciplinar novos nascem direto no
    # cabeca do grupo, para nao precisar de agregacao retroativa depois.
    return Atleta.resolver_canonico(atleta)


def _get_ou_criar_disciplinar(atleta_id, mes_ano):
    """Busca ou cria ControleDisciplinar para atleta/mes."""
    cd = ControleDisciplinar.query.filter_by(
        atleta_id=atleta_id, mes_ano=mes_ano
    ).first()
    if not cd:
        cd = ControleDisciplinar(atleta_id=atleta_id, mes_ano=mes_ano)
        db.session.add(cd)
        db.session.flush()
    return cd


_FLAGS_QUE_DERRUBAM_VELOCIDADE = {'VELOCIDADE_ANORMAL', 'TEMPO_IMPOSSIVEL'}


def _registrar_flag(atleta_id, atividade_id, tipo_flag, nivel, descricao):
    """Registra flag no banco e retorna o objeto criado."""
    flag = FlagAnticheat(
        atleta_id    = atleta_id,
        atividade_id = atividade_id,
        tipo_flag    = tipo_flag,
        nivel        = nivel,
        descricao    = descricao,
        status       = 'aberta',
    )
    db.session.add(flag)
    # A velocidade fica de fora da tabela de "mais veloz" ate um admin dizer
    # que e' real (Fase 4 do plano das melhorias, 28/08/2026) -- NAO tira km
    # do ranking (regra 14), mexe so' na media de velocidade. E' o que
    # impede alguem de moto/carro liderar a tabela de corrida ou ciclismo.
    if tipo_flag in _FLAGS_QUE_DERRUBAM_VELOCIDADE:
        atividade = Activity.query.get(atividade_id)
        if atividade:
            atividade.velocidade_confiavel = False
    db.session.flush()
    return flag


def _override_atleta(strava_id):
    """Retorna o AnticheatOverride do atleta (por strava_id) ou None."""
    if not strava_id:
        return None
    try:
        return AnticheatOverride.query.filter_by(strava_id=str(strava_id)).first()
    except Exception:
        # Tabela pode ainda nao existir em bases muito antigas — nao punir.
        return None


def _periodo_limites(atividade):
    """Limites efetivos: override do atleta > config da cidade > historico.

    Cada limite e resolvido de forma independente, entao um override pode
    ajustar apenas o pace curto (caso do atleta rapido) e herdar o resto.
    """
    config = Config.query.filter_by(cidade=atividade.cidade).first()
    override = _override_atleta(getattr(atividade, 'id_usuario_strava', None))

    def _resolver(campo, base):
        ov = getattr(override, campo, None) if override else None
        if ov is not None:
            return ov
        cfg = getattr(config, campo, None) if config else None
        if cfg is not None:
            return cfg
        return base

    return {
        'pace_max_corrida_curta': _resolver(
            'pace_max_corrida_curta', LIMITES['pace_max_corrida_curta']),
        'pace_max_corrida_longa': _resolver(
            'pace_max_corrida_longa', LIMITES['pace_max_corrida_longa']),
        # Sem minimo por padrao: base None significa "nao flagar por velocidade baixa".
        'velocidade_min_ciclismo': _resolver('velocidade_min_ciclismo', None),
        'velocidade_max_ciclismo': _resolver(
            'velocidade_max_ciclismo', LIMITES['velocidade_max_ciclismo']),
    }


def _tolerancia_flags(cidade):
    """Quantas atividades suspeitas do mesmo tipo sao toleradas no mes (default 3)."""
    if not cidade:
        return 3
    config = Config.query.filter_by(cidade=cidade).first()
    valor = getattr(config, 'tolerancia_flags', None) if config else None
    try:
        valor = int(valor)
        if valor > 0:
            return valor
    except (TypeError, ValueError):
        pass
    return 3


def _inicio_periodo(mes_ano):
    year, month = (int(part) for part in mes_ano.split('-', 1))
    inicio = datetime(year, month, 1)
    if month == 12:
        return inicio, datetime(year + 1, 1, 1)
    return inicio, datetime(year, month + 1, 1)


def recalcular_controle_disciplinar(atleta_id, mes_ano):
    """Recalcula contadores por atividade suspeita, com tolerancia por TIPO de flag.

    Cada tipo de flag tolera N atividades no mes (Config.tolerancia_flags,
    padrao 3). A partir da atividade seguinte do mesmo tipo (a 4a, por padrao)
    aquela atividade sai do ranking. Uma atividade rejeitada pelo admin sai
    sempre. A ordem e cronologica para o resultado ser estavel mesmo em
    importacoes historicas fora de ordem.
    """
    inicio, fim = _inicio_periodo(mes_ano)
    flags = FlagAnticheat.query.join(Activity, FlagAnticheat.atividade_id == Activity.id).filter(
        FlagAnticheat.atleta_id == atleta_id,
        Activity.data_hora >= inicio,
        Activity.data_hora < fim,
    ).all()
    por_atividade = {}
    for flag in flags:
        if (flag.status or 'aberta') != 'aprovada':
            por_atividade.setdefault(flag.atividade_id, []).append(flag)
    atividades = sorted(
        (itens[0].atividade for itens in por_atividade.values()),
        key=lambda atividade: (atividade.data_hora or inicio, atividade.id or 0),
    )
    cidade = atividades[0].cidade if atividades else None
    limite = _tolerancia_flags(cidade)
    marcador = 'SUSPEITA_FORA_DO_RANKING'
    contadores = {}   # tipo_flag -> quantidade acumulada no mes
    removidas = 0
    for atividade in atividades:
        flags_atividade = por_atividade.get(atividade.id, [])
        rejeitada_admin = any((flag.status or '') == 'rejeitada' for flag in flags_atividade)
        tipos = {flag.tipo_flag for flag in flags_atividade}
        excede = False
        for tipo in tipos:
            contadores[tipo] = contadores.get(tipo, 0) + 1
            if contadores[tipo] > limite:
                excede = True
        strava_flags = atividade.strava_flags or ''
        if excede or rejeitada_admin:
            removidas += 1
            atividade.status = 'removida'
            if marcador not in strava_flags:
                atividade.strava_flags = (strava_flags + '|' + marcador).strip('|')
        elif marcador in strava_flags:
            atividade.status = 'ativa'
            atividade.strava_flags = '|'.join(
                item for item in strava_flags.split('|') if item != marcador
            )
    # MARCADOR ORFAO: A ATIVIDADE QUE PERDEU A FLAG TEM DE VOLTAR.
    #
    # Medido em 09/09/2026, ao apagar as 766 flags de GRUPO_SUSPEITO: a
    # atividade removida por estourar a tolerancia carrega o marcador, mas
    # depois de apagada a flag ela nao aparece mais em `por_atividade` -- o laco
    # acima nunca a visita, e o `elif marcador in strava_flags` que a devolveria
    # jamais roda. Resultado: ficaria removida para sempre por causa de uma
    # regra que deixou de existir. Eram 54 atividades e ~630 km de gente real.
    #
    # Aqui nao se julga nada: se a atividade tem o marcador e nao tem mais
    # nenhuma flag nao-aprovada no mes, o motivo da remocao acabou.
    atleta = Atleta.query.get(atleta_id)
    if atleta is not None:
        orfas = Activity.query.filter(
            Activity.id_usuario_strava == str(atleta.strava_id),
            Activity.data_hora >= inicio,
            Activity.data_hora < fim,
            Activity.strava_flags.like('%' + marcador + '%'),
        ).all()
        for atividade in orfas:
            if atividade.id in por_atividade:
                continue   # ainda tem flag: quem manda e o laco de cima
            atividade.strava_flags = '|'.join(
                item for item in (atividade.strava_flags or '').split('|')
                if item != marcador
            )
            if atividade.status == 'removida':
                atividade.status = 'ativa'
                logger.info('Atividade %s voltou ao ranking: a flag que a '
                            'removeu nao existe mais', atividade.id_atividade)

    atividades_suspeitas = len(atividades)
    cd = _get_ou_criar_disciplinar(atleta_id, mes_ano)
    cd.total_flags = atividades_suspeitas
    cd.atividades_suspeitas = atividades_suspeitas
    cd.remocoes = removidas
    cd.avisos = max(0, atividades_suspeitas - removidas)
    # A regra atual nao elimina o atleta inteiro: apenas a atividade suspeita
    # que estoura a tolerancia do seu tipo fica temporariamente fora do ranking.
    cd.eliminado_do_ranking = False
    return cd


def atividade_pode_voltar_ao_ranking(atividade):
    """Uma atividade so volta a contar quando nenhuma flag pendente a bloqueia."""
    bloqueios = {'aberta', 'em_revisao', 'rejeitada'}
    return not any((flag.status or 'aberta') in bloqueios for flag in atividade.flags_anticheat)


def _aplicar_punicao(atividade, atleta_id, mes_ano):
    """Aplica uma unica tentativa por atividade, independente das flags geradas."""
    recalcular_controle_disciplinar(atleta_id, mes_ano)
    punicao = 'removida' if atividade.status == 'removida' else 'aviso_leve'
    logger.info('Atividade suspeita %s recebeu %s em %s', atividade.id_atividade, punicao, mes_ano)
    return punicao


# ===== DETECTORES =====

def detect_velocidade_anormal(atividade):
    """
    Flag VELOCIDADE_ANORMAL:
    - Ciclismo: velocidade media fora da faixa configurada em km/h.
    Corrida/caminhada e avaliada por pace em detect_tempo_impossivel.
    """
    flags = []
    vel = atividade.velocidade_media
    if not vel or vel <= 0:
        return flags

    if atividade.modalidade == 'ciclismo':
        limites = _periodo_limites(atividade)
        minimo = limites['velocidade_min_ciclismo']
        maximo = limites['velocidade_max_ciclismo']
        abaixo_minimo = minimo is not None and vel < minimo
        acima_maximo = maximo is not None and vel > maximo
        if abaixo_minimo or acima_maximo:
            faixa = '{} a {} km/h'.format(
                '{:.1f}'.format(minimo) if minimo is not None else 'sem minimo',
                '{:.1f}'.format(maximo) if maximo is not None else 'sem maximo',
            )
            flags.append({
                'tipo': 'VELOCIDADE_ANORMAL',
                'nivel': 'medio',
                'descricao': (
                    f"Ciclismo: velocidade {vel:.1f} km/h fora da faixa {faixa}"
                ),
            })
    return flags


def detect_tempo_impossivel(atividade):
    """
    Flag TEMPO_IMPOSSIVEL:
    - Corrida < 5km: pace < 4:10 min/km
    - Corrida >= 5km: pace < 5:00 min/km
    - Ciclismo: detectado via velocidade_anormal
    """
    flags = []
    if atividade.modalidade != 'corrida_caminhada':
        return flags

    dist = atividade.distancia or 0
    if dist <= 0:
        return flags

    # Obter pace: diretamente ou calculado da velocidade
    pace = atividade.pace_min_km
    if pace is None or pace <= 0:
        vel = atividade.velocidade_media
        if vel and vel > 0:
            pace = 60.0 / vel
        else:
            return flags

    limites = _periodo_limites(atividade)
    limite = (
        limites['pace_max_corrida_curta']
        if dist < 5
        else limites['pace_max_corrida_longa']
    )

    # Pace MENOR = mais rapido = suspeito
    if pace < limite:
        flags.append({
            'tipo': 'TEMPO_IMPOSSIVEL',
            'nivel': 'medio',
            'descricao': (
                f"Pace {pace:.2f} min/km < limite {limite:.3f} min/km "
                f"para corrida de {dist:.1f} km"
            ),
        })
    return flags


def detect_elevacao_fake(atividade):
    """
    Flag ELEVACAO_FAKE:
    - Elevacao acumulada > 50% da distancia (em metros)
    """
    flags = []
    elev  = atividade.elevacao_total or 0
    dist  = atividade.distancia or 0
    if dist <= 0 or elev <= 0:
        return flags

    dist_m   = dist * 1000
    elev_pct = (elev / dist_m) * 100

    if elev_pct > LIMITES['elevacao_max_pct']:
        flags.append({
            'tipo': 'ELEVACAO_FAKE',
            'nivel': 'baixo',
            'descricao': (
                f"Elevacao {elev:.0f}m = {elev_pct:.1f}% da distancia "
                f"{dist_m:.0f}m (limite: {LIMITES['elevacao_max_pct']}%)"
            ),
        })
    return flags


def detect_padrao_suspeito(atividade):
    """
    Flag PADRAO_SUSPEITO:
    - Mais de 3 atividades do mesmo atleta no mesmo dia
    - Mais de 5 atividades do mesmo atleta na mesma semana
    """
    flags = []
    dt = atividade.data_hora
    if not dt:
        return flags

    dia_inicio  = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    dia_fim     = dia_inicio + timedelta(days=1)
    semana_ini  = dia_inicio - timedelta(days=dt.weekday())
    semana_fim  = semana_ini + timedelta(days=7)

    with db.session.no_autoflush:
        # Atividades no mesmo dia
        cnt_dia = Activity.query.filter(
            Activity.id_usuario_strava == atividade.id_usuario_strava,
            Activity.cidade            == atividade.cidade,
            Activity.data_hora         >= dia_inicio,
            Activity.data_hora         < dia_fim,
            Activity.id                != atividade.id,
        ).count()

    if cnt_dia >= LIMITES['max_atividades_dia']:
        flags.append({
            'tipo': 'PADRAO_SUSPEITO',
            'nivel': 'baixo',
            'descricao': (
                f"{cnt_dia + 1} atividades no mesmo dia ({dt.date()}) "
                f"- limite: {LIMITES['max_atividades_dia']}"
            ),
        })
        return flags  # Nao acumular flag de semana se ja flagou dia

    with db.session.no_autoflush:
        # Atividades na mesma semana
        cnt_sem = Activity.query.filter(
            Activity.id_usuario_strava == atividade.id_usuario_strava,
            Activity.cidade            == atividade.cidade,
            Activity.data_hora         >= semana_ini,
            Activity.data_hora         < semana_fim,
            Activity.id                != atividade.id,
        ).count()

    if cnt_sem >= LIMITES['max_atividades_semana']:
        flags.append({
            'tipo': 'PADRAO_SUSPEITO',
            'nivel': 'baixo',
            'descricao': (
                f"{cnt_sem + 1} atividades na mesma semana "
                f"(inicio {semana_ini.date()}) - limite: {LIMITES['max_atividades_semana']}"
            ),
        })
    return flags


def detect_marcacao_falsa(atividade):
    # Flag MARCACAO_FALSA (alto):
    # Detecta quando uma atividade e copia identica de outra no banco
    # (marcacao de outro usuario via Group Activity do Strava).
    # Criterio: fingerprint GPS identico = mesmo arquivo copiado (100% match).
    flags = []
    fp = atividade.gps_fingerprint
    if not fp:
        return flags  # Sem GPS, nao punir automaticamente

    with db.session.no_autoflush:
        duplicata = Activity.query.filter(
            Activity.gps_fingerprint   == fp,
            Activity.id                != atividade.id,
            Activity.id_usuario_strava != atividade.id_usuario_strava,
            Activity.cidade            == atividade.cidade,
        ).first()

    if duplicata:
        flags.append({
            'tipo':  'MARCACAO_FALSA',
            'nivel': 'alto',
            'descricao': (
                f"Rota GPS identica a atividade {duplicata.id_atividade} "
                f"de {duplicata.nome_usuario} — marcacao confirmada por GPS "
                f"(fingerprint: {fp[:8]}...)"
            ),
        })
    return flags


# DETECTOR "GRUPO SUSPEITO": REMOVIDO EM 09/09/2026, por decisao do dono.
#
# Ele marcava quem se exercitou no MESMO HORARIO com distancia parecida, e so
# quando a atividade nao tinha GPS. Isso nao e evidencia de nada: treinar junto
# e o comportamento normal de um grupo de corrida de cidade pequena -- era
# exatamente o publico do projeto que ele acusava. O resultado medido foram
# 762 flags em aberto (metade de TODAS as flags do sistema), que nenhuma pessoa
# revisaria nunca e que so serviam para enterrar as flags que importam.
#
# Quem prova marcacao falsa e `detect_marcacao_falsa`, comparando a IMPRESSAO
# DIGITAL DO TRAJETO (sha do GPS): trajeto identico e prova, mesmo horario nao.
# As 766 flags desse tipo foram apagadas na mesma sessao, depois desta remocao
# (a fonte seca antes do derivado -- regra 9).
#
# As chaves `grupo_janela_min` e `grupo_dist_pct` de LIMITES sairam junto:
# nasceram com esta funcao e nao tinham outro leitor.


def detect_veiculo_strava(atividade):
    """
    Flag VEICULO_STRAVA:
    Atividade marcada como invalida pelo proprio Strava (via strava_flags).
    """
    flags = []
    sf = (atividade.strava_flags or '').lower()
    keywords = ('flagged', 'invalid', 'veiculo', 'vehicle', 'hidden')
    if any(kw in sf for kw in keywords):
        flags.append({
            'tipo': 'VEICULO_STRAVA',
            'nivel': 'alto',
            'descricao': 'Atividade marcada como invalida/veiculo pelo Strava',
        })
    return flags



def detect_velocidade_parcial(atividade):
    import json as _j
    flags=[]
    if atividade.modalidade!='corrida_caminhada': return flags
    sj=getattr(atividade,'splits_json',None)
    if not sj: return flags
    try: splits=_j.loads(sj)
    except: return flags
    limite = _periodo_limites(atividade)['pace_max_corrida_curta']
    for i in {0,1,len(splits)-1}:
        if i<len(splits):
            p=splits[i].get('pace_min_km')
            if p and 0<p<limite:
                flags.append({'tipo':'VELOCIDADE_PARCIAL_SUSPEITA','nivel':'medio','descricao':f'Km {i+1}: pace {p:.2f} min/km < limite {limite:.3f} min/km'})
    return flags

# ===== PONTO DE ENTRADA =====

def process_activity_anticheat(atividade):
    """
    Executa todos os detectores sincronos em uma atividade recém-salva.
    Registra flags e aplica punicoes progressivas.
    Retorna lista de dicts com info de cada flag detectada.
    """
    todas_flags = []

    try:
        # Obter/criar Atleta — associa telefone do WhatsApp ao strava_id
        atleta = _get_ou_criar_atleta(
            strava_id=atividade.id_usuario_strava,
            nome=atividade.nome_usuario,
            telefone=atividade.telefone_whatsapp,
        )

        mes_ano = (
            atividade.data_hora.strftime('%Y-%m')
            if atividade.data_hora
            else datetime.now().strftime('%Y-%m')
        )

        # Rodar os 5 detectores sincronos
        detectores = [
            detect_velocidade_anormal,
            detect_tempo_impossivel,
            detect_velocidade_parcial,
            # detect_elevacao_fake,     # desativado: falsos positivos em relevos acidentados
            # detect_padrao_suspeito,   # desativado: atleta pode treinar todos os dias
            detect_marcacao_falsa,
            detect_veiculo_strava,
        ]

        for detector in detectores:
            try:
                flags_detectadas = detector(atividade)
                todas_flags.extend(flags_detectadas)
            except Exception as e:
                logger.error(f"Erro no detector {detector.__name__}: {e}", exc_info=True)

        # Registrar todas as flags, mas contabilizar apenas uma tentativa por
        # atividade. Uma corrida pode ter flag de pace total e parcial sem que
        # isso antecipe a quarta tentativa.
        for flag_info in todas_flags:
            tipo  = flag_info['tipo']
            nivel = flag_info['nivel']
            desc  = flag_info['descricao']

            _registrar_flag(atleta.id, atividade.id, tipo, nivel, desc)
        if todas_flags:
            punicao = _aplicar_punicao(atividade, atleta.id, mes_ano)
            for flag_info in todas_flags:
                flag_info['punicao'] = punicao

        db.session.flush()

    except Exception as e:
        logger.error(f"Erro geral no anticheat para atividade {atividade.id}: {e}", exc_info=True)

    return todas_flags
