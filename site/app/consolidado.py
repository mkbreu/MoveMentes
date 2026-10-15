# -*- coding: utf-8 -*-
"""Numeros consolidados do projeto -- a primeira agregacao que ATRAVESSA cidade.

Por que este arquivo existe (P-93, DOC E de 21/08/2026):
todo agregador que ja existia recebe `cidade` como parametro OBRIGATORIO, que
e' o certo para o ranking (regra 1: consulta sem filtro de cidade e' bug). A
pagina publica consolidada e' a unica excecao legitima -- ela existe para somar
o projeto inteiro -- entao ela mora aqui, separada, e nao dentro de `ranking.py`
onde alguem poderia copiar por engano uma consulta sem filtro.

DUAS DECISOES QUE MUDAM NUMERO, e por isso ficam escritas:

1. **Uma atividade conta UMA vez, mesmo em duas cidades.** A restricao unica do
   banco e' `(id_atividade, cidade)`, nao `id_atividade`: a mesma atividade PODE
   existir em duas cidades (athlete.py documenta isso). Somar ingenuamente
   contaria duas vezes no dia em que Parana entrar. Por isso todo total geral
   passa por `GROUP BY id_atividade`. O recorte POR CIDADE nao deduplica -- ali
   cada cidade mostra o numero dela -- e a pagina avisa que a soma das cidades
   pode passar do total.

2. **Tudo em SQL, nada em memoria.** O padrao antigo (`.all()` e soma em Python)
   carrega a tabela inteira; com 10 mil atividades num NAS de 490 MB isso e'
   risco. Aqui sao 6 consultas agregadas.

O que fica de fora, sempre (mesmo filtro de `_build_yearly_public_sections`):
atividade `removida` e atividade com flag de anticheat `rejeitada`. O
`excluir_do_anual` NAO entra aqui: ele e' punicao do ranking anual, nao correcao
de km (regra 14 -- flag nao tira km).
"""
import io
import json
import os
from datetime import date, datetime

from sqlalchemy import func, or_, case

from app import db
from app.models import Activity, Atleta, Config, FlagAnticheat, PatrocinadorMensal

# Rotulo de cada modalidade. Sao duas, e so estas duas (glossario).
MODALIDADES = {
    'corrida_caminhada': 'Corrida e caminhada',
    'ciclismo': 'Ciclismo',
}

PERIODOS = ('tudo', 'mes', 'trimestre', 'semestre', 'ano', 'livre')

ROTULO_PERIODO = {
    'tudo': 'Desde o começo',
    'mes': 'Este mês',
    'trimestre': 'Este trimestre',
    'semestre': 'Este semestre',
    'ano': 'Este ano',
    'livre': 'Período escolhido',
}


def ocultos():
    """strava_id de quem NAO deve aparecer na listagem por pessoa.

    Duas razoes, mesmo efeito na lista: pediu anonimato
    (`oculto_no_publico`) ou o perfil esta arquivado (`arquivado` -- Fase 3
    do plano das melhorias, 28/08/2026: ate aqui `ranking.py` e
    `consolidado.py` nao filtravam arquivado nenhum, e o atleta continuava
    aparecendo no ranking mensal e na pagina inicial mesmo arquivado).

    Os km continuam contando no total do projeto nos dois casos -- o pedido
    e' sair da LISTA, nao apagar o que foi feito. Quem quer os km fora do
    total tambem usa a opcao "arquivar e os quilometros" (`admin.arquivar_atleta`),
    que mexe em `Activity.status`, nao aqui.
    """
    try:
        return {
            str(sid) for (sid,) in db.session.query(Atleta.strava_id)
            .filter(or_(Atleta.oculto_no_publico.is_(True),
                       Atleta.arquivado.is_(True))).all() if sid
        }
    except Exception:
        return set()


def _km(valor):
    """Quilometro que vai para a tela: INTEIRO, sem casa decimal.

    Pedido do dono (28/08/2026): em quadro, tabela e card o numero sai exato e
    arredondado, sem virgula. O arredondamento e' feito AQUI, no dado, e nao na
    tela -- se cada celula arredondasse sozinha na exibicao, os anos exibidos
    somariam um numero diferente do total exibido, e a tabela da pagina inicial
    deixaria de fechar.
    """
    return int(round(float(valor or 0)))


def _base_valida():
    """Filtro comum: fora removida, fora flag rejeitada."""
    rejeitadas = db.session.query(FlagAnticheat.atividade_id).filter(
        FlagAnticheat.status == 'rejeitada')
    return [
        or_(Activity.status.is_(None), Activity.status != 'removida'),
        ~Activity.id.in_(rejeitadas),
    ]


def intervalo_do_periodo(periodo, de=None, ate=None, hoje=None):
    """Traduz o botao apertado em (inicio, fim) -- datas, nao textos.

    Devolve (None, None) para 'tudo'. `fim` e' exclusivo do dia seguinte para
    apanhar a atividade das 23h50 sem depender da hora gravada.
    """
    hoje = hoje or date.today()
    periodo = (periodo or 'tudo').strip().lower()
    if periodo not in PERIODOS:
        periodo = 'tudo'

    if periodo == 'tudo':
        return None, None, periodo
    if periodo == 'mes':
        return date(hoje.year, hoje.month, 1), hoje, periodo
    if periodo == 'ano':
        return date(hoje.year, 1, 1), hoje, periodo
    if periodo in ('trimestre', 'semestre'):
        # Marco FIXO do calendario, nao janela rolante -- pedido do dono
        # (28/08/2026): trimestre e' sempre jan-mar/abr-jun/jul-set/out-dez, e
        # semestre e' sempre jan-jun/jul-dez. Entrar em setembro nao pode
        # trazer junho (2o trimestre) para dentro do 3o.
        largura = 3 if periodo == 'trimestre' else 6
        bloco = (hoje.month - 1) // largura
        mes_inicio = bloco * largura + 1
        return date(hoje.year, mes_inicio, 1), hoje, periodo

    # livre: so vale com as duas pontas legiveis; qualquer coisa torta vira 'tudo'
    ini = _data(de)
    fim = _data(ate)
    if not ini or not fim:
        return None, None, 'tudo'
    if ini > fim:
        ini, fim = fim, ini
    return ini, fim, 'livre'


def _data(texto):
    try:
        return datetime.strptime((texto or '').strip(), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _filtros(inicio, fim, cidade=None):
    filtros = list(_base_valida())
    if cidade:
        filtros.append(Activity.cidade == cidade)
    if inicio:
        filtros.append(Activity.data_hora >= datetime.combine(inicio, datetime.min.time()))
    if fim:
        filtros.append(Activity.data_hora <= datetime.combine(fim, datetime.max.time()))
    return filtros


def _sem_repetir(filtros, *colunas, **kwargs):
    """Subconsulta com UMA linha por atividade (a defesa da dupla contagem).

    `MAX(distancia)` e' so a forma de trazer o km junto do GROUP BY: as linhas
    de uma mesma atividade em duas cidades tem a mesma distancia.

    `colunas` entram no GROUP BY; `agregados` (nomeado) sao funcoes de agregacao
    que viajam junto SEM entrar no GROUP BY -- o SQLite recusa agregado ali, e
    foi assim que a primeira versao desta funcao quebrou no banco de provas.
    """
    campos = [
        Activity.id_atividade.label('aid'),
        func.max(Activity.distancia).label('km'),
    ]
    campos.extend(colunas)
    campos.extend(kwargs.get('agregados') or ())
    return (
        db.session.query(*campos)
        .filter(*filtros)
        .group_by(Activity.id_atividade, *colunas)
        .subquery()
    )


def _chave_atleta():
    """Mesma chave do ranking: strava_id quando existe, senao o nome.

    Atleta mesclado (07/09/2026): antes de virar chave, o strava_id de quem
    foi fundido em outro perfil e' trocado pelo do cabeca do grupo, via CASE
    WHEN -- assim as tabelas da pagina inicial tambem somam o grupo como uma
    pessoa so. O mapa e' quase sempre vazio (mesclagem e' rara); so' entra
    CASE na consulta quando ha' de fato algum atleta mesclado.
    """
    sid_bruto = func.nullif(func.trim(Activity.id_usuario_strava), '')
    mapa = Atleta.mapa_strava_canonico()
    sid = case(mapa, value=sid_bruto, else_=sid_bruto) if mapa else sid_bruto
    return func.coalesce(sid, 'name:' + func.lower(func.trim(Activity.nome_usuario)))


def numeros(periodo='tudo', de=None, ate=None, cidade=None, hoje=None,
            limite_atletas=None):
    """Tudo que a pagina publica mostra, em 6 consultas agregadas."""
    inicio, fim, periodo = intervalo_do_periodo(periodo, de, ate, hoje)
    filtros = _filtros(inicio, fim, cidade)

    # 1. total geral, sem repetir atividade
    sub = _sem_repetir(filtros)
    _bruto, total_ativ = db.session.query(
        func.coalesce(func.sum(sub.c.km), 0.0), func.count()).one()

    # 2. quantos atletas e desde quando (aqui repetir nao atrapalha: e' DISTINCT)
    total_atletas = db.session.query(
        func.count(func.distinct(_chave_atleta()))).filter(*filtros).scalar() or 0
    primeira, ultima = db.session.query(
        func.min(Activity.data_hora), func.max(Activity.data_hora)
    ).filter(*filtros).one()

    # 3. por modalidade
    sub_mod = _sem_repetir(filtros, Activity.modalidade.label('mod'))
    por_modalidade = []
    for mod, km, qtd in db.session.query(
            sub_mod.c.mod, func.sum(sub_mod.c.km), func.count()
    ).group_by(sub_mod.c.mod).all():
        chave = (mod or '').lower()
        if chave not in MODALIDADES:
            continue
        por_modalidade.append({
            'id': chave,
            'titulo': MODALIDADES[chave],
            'km': _km(km),
            'atividades': qtd,
        })
    por_modalidade.sort(key=lambda x: x['km'], reverse=True)

    # O TOTAL E' A SOMA DO QUE ESTA NA TELA (28/08/2026). Somar o bruto e
    # arredondar no fim dava 183.177,5 enquanto as duas modalidades exibidas
    # somavam 183.177,6: a tabela da pagina inicial nao fechava por 100 metros.
    # Numa pagina feita para ser auditada, tabela que nao fecha e' uma pergunta
    # que ninguem quer responder -- entao existe UMA regra, aqui, e ela vale em
    # todas as telas: o total e' a soma das parcelas exibidas.
    total_km = _km(sum(m['km'] for m in por_modalidade))

    # 4. por cidade -- SEM deduplicar: cada cidade mostra o numero dela
    por_cidade = []
    for nome, km, qtd, atletas in db.session.query(
            Activity.cidade, func.sum(Activity.distancia), func.count(),
            func.count(func.distinct(_chave_atleta()))
    ).filter(*filtros).group_by(Activity.cidade).all():
        por_cidade.append({
            'cidade': nome,
            'km': _km(km),
            'atividades': qtd,
            'atletas': atletas,
        })
    por_cidade.sort(key=lambda x: x['km'], reverse=True)

    # 5. por ano
    ano_sql = func.strftime('%Y', Activity.data_hora)
    sub_ano = _sem_repetir(filtros, ano_sql.label('ano'))
    por_ano = [
        {'ano': ano, 'km': _km(km), 'atividades': qtd}
        for ano, km, qtd in db.session.query(
            sub_ano.c.ano, func.sum(sub_ano.c.km), func.count()
        ).group_by(sub_ano.c.ano).all() if ano
    ]
    por_ano.sort(key=lambda x: x['ano'])

    # 6. km por atleta no periodo filtrado
    sub_atl = _sem_repetir(
        filtros, _chave_atleta().label('chave'),
        agregados=[func.max(Activity.nome_usuario).label('nome')])
    consulta_atl = db.session.query(
        sub_atl.c.chave, func.max(sub_atl.c.nome), func.sum(sub_atl.c.km), func.count()
    ).group_by(sub_atl.c.chave).order_by(func.sum(sub_atl.c.km).desc())
    if limite_atletas:
        consulta_atl = consulta_atl.limit(limite_atletas)
    reservados = ocultos()
    atletas = []
    for chave, nome, km, qtd in consulta_atl.all():
        sid = str(chave or '')
        if sid in reservados:
            continue  # pediu para nao aparecer -- os km dela seguem no total
        atletas.append({
            'nome': _nome_limpo(nome),
            'strava_id': sid if sid.isdigit() else None,
            'km': _km(km),
            'atividades': qtd,
        })
    for i, item in enumerate(atletas, start=1):
        item['posicao'] = i

    # PERFIL REPETIDO NAO SE UNE SOZINHO (28/08/2026). Quatro pessoas aqui tem
    # DOIS strava_id: refizeram o perfil e o antigo ficou com km. Unir por nome
    # seria inventar regra -- duas pessoas podem ter o mesmo nome, e a
    # identidade do sistema vem do link do Strava (regra 4-A). Entao a pagina
    # CONTA e AVISA, em vez de esconder ou de juntar por conta propria.
    vistos = {}
    for item in atletas:
        vistos.setdefault(_chave_nome(item['nome']), []).append(item)
    repetidos = sum(len(v) - 1 for v in vistos.values() if len(v) > 1)

    return {
        'periodo': periodo,
        'periodo_rotulo': ROTULO_PERIODO.get(periodo, ''),
        'inicio': inicio,
        'fim': fim,
        'cidade': cidade,
        'total_km': _km(total_km),
        'total_atividades': total_ativ or 0,
        'total_atletas': total_atletas,
        'primeira_atividade': primeira,
        'ultima_atividade': ultima,
        'por_modalidade': por_modalidade,
        'por_cidade': por_cidade,
        'por_ano': por_ano,
        'atletas': atletas,
        'perfis_repetidos': repetidos,
        'origem': _origem(filtros),
    }


def _origem(filtros):
    """Quanto veio de link postado no grupo e quanto veio do histórico do Strava.

    A distincao existe no modelo (`fora_do_grupo`) e e' honesta perante quem
    audita: km importado do perfil do atleta e' km real, mas nao e' prova de
    participacao no grupo. Misturar os dois sem dizer e' o tipo de coisa que um
    analista pega.
    """
    sub = _sem_repetir(filtros, Activity.fora_do_grupo.label('fora'))
    do_grupo = importado = 0.0
    for fora, km in db.session.query(sub.c.fora, func.sum(sub.c.km)).group_by(sub.c.fora).all():
        if fora:
            importado += km or 0
        else:
            do_grupo += km or 0
    return {
        'link_no_grupo': _km(do_grupo),
        'historico_strava': _km(importado),
    }


def _nome_limpo(nome):
    """Nome INTEIRO, so tirando espaco a mais.

    A pagina publica nao encurta para primeiro+ultimo, como o ranking faz: com
    95 pessoas na mesma lista, "Helena Vieira Exemplo" e "Helena Exemplo" viram
    a mesma linha aos olhos de quem le, e numa pagina que existe para ser
    auditada isso e' pior que um nome comprido.
    """
    return ' '.join((nome or '').split()) or 'Atleta'


def _chave_nome(nome):
    """Nome sem acento e sem caixa -- so para CONTAR repeticao, nunca para unir."""
    import unicodedata
    limpo = unicodedata.normalize('NFKD', nome or '').encode('ascii', 'ignore').decode()
    return ' '.join(limpo.lower().split())


def arquivados():
    """strava_id de perfil ARQUIVADO -- so isto, nao `oculto_no_publico`.

    Usado onde o nome precisa sumir da lista mas o `oculto_no_publico` NAO
    pode (ex.: o ranking mensal que vai pro grupo -- quem pediu para nao
    aparecer no SITE continua aparecendo pelo nome ali, e' o proprio grupo;
    so o arquivado (perfil duplicado/sem uso) some de la tambem). Fase 3 do
    plano das melhorias, 28/08/2026.
    """
    try:
        return {
            str(sid) for (sid,) in db.session.query(Atleta.strava_id)
            .filter(Atleta.arquivado.is_(True)).all() if sid
        }
    except Exception:
        return set()


def _slug_cidade(cidade):
    """Mesma regra de `ranking._slugify_city` -- duplicada aqui de proposito
    (funcao pura, 3 linhas) para nao criar import cruzado entre os dois
    modulos. "Palmeiropolis" -> "palmeiropolis", "Minacu" -> "minacu"."""
    import re
    import unicodedata
    texto = unicodedata.normalize('NFKD', (cidade or '').strip())
    texto = texto.encode('ascii', 'ignore').decode('ascii')
    base = re.sub(r'[^A-Za-z0-9]+', '-', texto).strip('-').lower()
    return base or 'cidade'


def cidades_com_dado():
    """Cidades que TEM atividade -- nao as que existem no cadastro.

    A pagina publica nao pode sugerir tres cidades ativas enquanto duas
    estiverem vazias: e' exatamente o tipo de inconsistencia que reprova um
    projeto (DOC E, P-93).
    """
    com_dado = {
        c for (c,) in db.session.query(func.distinct(Activity.cidade))
        .filter(*_base_valida()).all() if c
    }
    todas = [c for (c,) in db.session.query(Config.cidade).all() if c]
    return sorted(com_dado), sorted(c for c in todas if c not in com_dado)


def cidades_ativas():
    """Cidades que a automacao pode tratar como ATIVAS (P-81, 20/08/2026).

    Regra do dono: cidade e' ativa se TEM ranking rodando (atividade real,
    mesmo criterio de `cidades_com_dado`) OU se um admin marcou `Config.ativa`
    manualmente -- para uma cidade nova, em implantacao, antes do primeiro
    link chegar. Sem essa segunda opcao, toda cidade nasceria "inativa" e
    precisaria de atividade para a automacao sequer tentar ligar a maquina.

    Usada por `vigia._cidades()` (Wake-on-LAN + alerta de "sem sinal") e por
    `adesao._cidades()` (a inscricao publica nao pode oferecer uma cidade sem
    grupo nem administrador rodando de verdade).
    """
    # SO O CAMPO MARCADO (19/09/2026, regra do dono — P-135). Antes valia "tem
    # dado OU marcada", e uma cidade DESMARCADA com dados antigos continuava
    # acionando automacao. Palmeiropolis foi marcada pelo dono antes desta
    # troca (estava desmarcada e so rodava por ter dados — medido 19/09).
    try:
        marcadas = {
            c for (c,) in db.session.query(Config.cidade)
            .filter(Config.ativa.is_(True)).all() if c
        }
    except Exception:
        marcadas = set()
    return sorted(marcadas)


# ---------------------------------------------------------------------------
# A FOTO DO DIA -- os numeros que a pagina inicial mostra
# ---------------------------------------------------------------------------
# Pedido do dono (28/08/2026): os numeros consolidados aparecem na PAGINA
# INICIAL, junto da tabela do historico, e se atualizam UMA VEZ POR DIA.
#
# Por que cache e nao consulta ao vivo: a pagina inicial e' publica e e' a mais
# aberta do site; as 6 agregacoes varrem 10 mil atividades. Num NAS de 490 MB,
# uma tela perguntando demais ja derrubou o site (`ERROS.md` §4.24). A foto sai
# UMA vez por dia e todo mundo le o mesmo arquivo.
#
# Quem refaz: quem chegar primeiro depois da virada do dia -- normalmente a
# rotina diaria do site, que acorda antes de qualquer visita, e por tabela o
# primeiro ranking do dia. Nao existe temporizador novo: se a foto e' de hoje,
# ninguem consulta nada.
_ARQUIVO_FOTO = 'consolidado_do_dia.json'

# FOTO GUARDADA TEM VERSAO (28/08/2026). Sem isto, mudar o FORMATO da foto
# (acrescentar a tabela anual, por exemplo) deixava a pagina inicial quebrada
# ate a virada do dia, porque o cache de hoje continuava valendo e nao tinha o
# campo novo. Mexeu no que a foto guarda? soma 1 aqui.
_VERSAO_FOTO = 11


def _caminho_foto():
    from flask import current_app
    pasta = os.path.join(str(current_app.config['TEMP_EXTRACT_FOLDER']), 'publico')
    os.makedirs(pasta, exist_ok=True)
    return os.path.join(pasta, _ARQUIVO_FOTO)


def _marca_do_banco():
    """Um numero que muda quando entra atividade nova. Custa quase nada.

    Pedido do dono (28/08/2026): *"a cada upload que for feito, ele ja deve ir
    aparecendo aqui de maneira automatica"*. Reconsultar as 6 agregacoes a cada
    visita nao cabe num NAS de 490 MB (foi tela perguntando demais que derrubou
    o site, `ERROS.md` §4.24). `MAX(id)` usa a chave primaria e responde na hora:
    mudou, a foto se refaz; nao mudou, ninguem consulta nada.

    O que ele NAO pega: atividade REMOVIDA por revisao (o id maximo nao muda) e
    atleta que pediu reserva. Para esses dois a foto se refaz na rotina diaria e
    no proprio botao que fez a mudanca.
    """
    # SO' O MAIOR ID NAO BASTA (09/09/2026, pedido do dono: "a pagina precisa
    # ser atualizada sempre que houver alguma modificacao nos km do banco").
    #
    # `MAX(id)` so' muda quando ENTRA atividade. Nao muda quando uma sai por
    # revisao, quando alguem corrige a quilometragem, nem quando o antitrapaca
    # marca atividade como nao confiavel -- e foi exatamente isso que aconteceu
    # em 09/09, quando 267 atividades foram regularizadas de uma vez e a home
    # continuou mostrando os numeros velhos.
    #
    # Agora a marca junta tres coisas baratas: o maior id (entrada), a
    # contagem (saida) e a soma dos km arredondada (correcao). Qualquer uma
    # que mude, a foto se refaz. Continua sendo UMA consulta, e so' roda quando
    # alguem abre a pagina -- o custo por dia segue proximo de zero, que e' o
    # requisito do NAS (490 MB).
    try:
        linha = db.session.query(
            func.max(Activity.id),
            func.count(Activity.id),
            func.round(func.coalesce(func.sum(Activity.distancia), 0), 1),
        ).filter(Activity.status != 'removida').one()
        return '%s-%s-%s' % (int(linha[0] or 0), int(linha[1] or 0), linha[2] or 0)
    except Exception:
        return 0


def foto_do_dia(forcar=False):
    """Numeros do projeto inteiro, recalculados so quando mudam.

    Devolve sempre um dicionario utilizavel: se o calculo falhar e existir foto
    velha, devolve a velha com `desatualizada=True` -- pagina publica com numero
    de ontem e' melhor que pagina publica quebrada.
    """
    hoje = date.today().isoformat()
    caminho = _caminho_foto()
    guardada = None
    try:
        with io.open(caminho, encoding='utf-8') as fh:
            guardada = json.load(fh)
    except Exception:
        guardada = None

    marca = _marca_do_banco()
    if (not forcar and guardada and guardada.get('dia') == hoje
            and guardada.get('versao') == _VERSAO_FOTO
            and guardada.get('marca') == marca):
        return guardada

    try:
        n = numeros('tudo', limite_atletas=10)
    except Exception:
        if guardada and guardada.get('versao') == _VERSAO_FOTO:
            guardada['desatualizada'] = True
            return guardada
        raise

    com_dado, sem_dado = cidades_com_dado()
    foto = {
        'dia': hoje,
        'versao': _VERSAO_FOTO,
        'marca': marca,
        # Data montada na mao, nao por strftime: com acento no formato o
        # strftime devolveu STRING VAZIA sob a localidade pt_BR do Windows, e a
        # pagina inicial ficou dizendo "atualizado em" e mais nada.
        'atualizada_em': '{:%d/%m/%Y} às {:%H:%M}'.format(datetime.now(), datetime.now()),
        'total_km': n['total_km'],
        'total_atividades': n['total_atividades'],
        'total_atletas': n['total_atletas'],
        'desde': n['primeira_atividade'].strftime('%d/%m/%Y') if n['primeira_atividade'] else None,
        'por_modalidade': n['por_modalidade'],
        'por_ano': n['por_ano'],
        'tabela': tabela_anual(),
        'classificacoes': classificacoes_da_capa(),
        'mais_velozes': [
            {'id': 'ciclismo', 'titulo': 'Mais velozes — ciclismo',
             'linhas': mais_velozes('ciclismo')},
            # CORRIDA **E CAMINHADA** (09/09/2026, pedido do dono): as pessoas
            # marcam corrida e caminham, ou o contrario. Separar deixava de
            # fora quem caminha e, pior, o filtro de "somente corrida" so'
            # aceitava ritmo abaixo de 7 min/km nas atividades antigas --
            # exatamente onde moram os fragmentos de GPS de 1.060 km/h.
            {'id': 'corrida', 'titulo': 'Mais velozes — corrida e caminhada',
             'linhas': mais_velozes('corrida_caminhada')},
        ],
        # DUAS PERGUNTAS DIFERENTES, DUAS TABELAS (09/09/2026, pedido do dono).
        # "Quem anda mais rapido no dia a dia" (acima, pela mediana) nao e' a
        # mesma coisa que "quem tem o melhor resultado" (aqui, o recorde). A
        # mediana procura o meio e apaga o pico -- que e' justamente o que
        # define o mais veloz.
        'recordes': [
            {'id': 'rec-ciclismo', 'titulo': 'Recordes — ciclismo',
             'linhas': recordistas('ciclismo')},
            {'id': 'rec-corrida', 'titulo': 'Recordes — corrida e caminhada',
             'linhas': recordistas('corrida_caminhada')},
        ],
        'patrocinadores': patrocinadores_publicos(),
        'esporte_gravado': cobertura_do_esporte(),
        # Contagem AGREGADA por sexo (09/09/2026). Fica junto dos numeros
        # gerais, perto do total, para o leitor conferir que fecha.
        'por_sexo': atividades_por_sexo(),
        'por_cidade': n['por_cidade'],
        'origem': n['origem'],
        'top': n['atletas'][:10],
        'perfis_repetidos': n.get('perfis_repetidos', 0),
        # Estrutura {'nome', 'slug'} desde 28/08/2026 (P-121): a home vira o
        # botao de cidade clicavel, apontando para a pagina que ja existe e
        # ja funciona por cidade (`ranking.consolidado_publico`).
        'cidades_com_dado': [{'nome': c, 'slug': _slug_cidade(c)} for c in com_dado],
        'cidades_em_implantacao': [{'nome': c, 'slug': _slug_cidade(c)} for c in sem_dado],
        'desatualizada': False,
    }
    try:
        with io.open(caminho, 'w', encoding='utf-8') as fh:
            json.dump(foto, fh, ensure_ascii=False)
    except Exception:
        pass  # numero na tela vale mais que cache gravado
    return foto


def refazer_foto_do_dia():
    """Chamada pela rotina diaria do site. Nunca levanta erro para cima."""
    try:
        foto_do_dia(forcar=True)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# A TABELA DA PAGINA INICIAL -- uma so, e ela fecha na ultima linha
# ---------------------------------------------------------------------------
# Pedido do dono (28/08/2026): a porta de entrada do projeto mostra UMA tabela
# com um ano por linha, uma coluna por modalidade, o total de cada ano e o
# TOTAL GERAL na linha final -- de modo que quem le veja os anos somarem no
# total, sem precisar de outra tela.
#
# O mes vigente entra tambem, mas como linha DESTACADA e fora da soma: ele ja
# esta dentro do ano corrente. Somar as duas coisas seria contar duas vezes, e
# a tabela deixaria de fechar -- que e' justamente o que ela existe para fazer.


def tabela_anual(cidade=None, hoje=None):
    """Ano a ano por modalidade, com a linha de total que fecha a conta."""
    hoje = hoje or date.today()
    filtros = _filtros(None, None, cidade)
    ano_sql = func.strftime('%Y', Activity.data_hora)
    sub = _sem_repetir(filtros, ano_sql.label('ano'), Activity.modalidade.label('mod'))

    linhas = {}
    for ano, mod, km, qtd in db.session.query(
            sub.c.ano, sub.c.mod, func.sum(sub.c.km), func.count()
    ).group_by(sub.c.ano, sub.c.mod).all():
        if not ano:
            continue
        chave = (mod or '').lower()
        if chave not in MODALIDADES:
            continue
        linha = linhas.setdefault(int(ano), {
            'ano': int(ano), 'total': 0.0, 'atividades': 0,
        })
        linha[chave] = _km(km)
        linha['total'] += linha[chave]
        linha['atividades'] += qtd

    anos = []
    for ano in sorted(linhas):
        linha = linhas[ano]
        for chave in MODALIDADES:
            linha.setdefault(chave, 0.0)
        linha['total'] = _km(linha['total'])
        linha['corrente'] = (ano == hoje.year)
        anos.append(linha)

    # A tabela fecha nos dois sentidos: cada ano e' a soma das suas modalidades,
    # e o total e' a soma dos anos. Mesma regra de `numeros()` -- o total e' a
    # soma do que esta na tela, nunca um bruto arredondado por fora.
    total = {'rotulo': 'Total do projeto', 'total': 0.0, 'atividades': 0}
    for chave in MODALIDADES:
        total[chave] = _km(sum(l[chave] for l in anos))
    total['total'] = _km(sum(total[c] for c in MODALIDADES))
    total['atividades'] = sum(l['atividades'] for l in anos)

    # o mes em andamento, para quem quer o numero de hoje sem abrir outra tela
    mes = numeros('mes', cidade=cidade, hoje=hoje, limite_atletas=1)
    linha_mes = {
        'rotulo': '{}/{}'.format(_MES_NOME.get(hoje.month, hoje.month), hoje.year),
        'total': mes['total_km'],
        'atividades': mes['total_atividades'],
        'atletas': mes['total_atletas'],
    }
    for chave in MODALIDADES:
        linha_mes[chave] = next(
            (m['km'] for m in mes['por_modalidade'] if m['id'] == chave), 0.0)

    return {
        'modalidades': [(c, MODALIDADES[c]) for c in ('corrida_caminhada', 'ciclismo')],
        'anos': anos,
        'total': total,
        'mes_corrente': linha_mes,
    }


_MES_NOME = {
    1: 'Janeiro', 2: 'Fevereiro', 3: 'Março', 4: 'Abril', 5: 'Maio',
    6: 'Junho', 7: 'Julho', 8: 'Agosto', 9: 'Setembro', 10: 'Outubro',
    11: 'Novembro', 12: 'Dezembro',
}


# ---------------------------------------------------------------------------
# AS CLASSIFICACOES DE ATLETA DA PAGINA INICIAL
# ---------------------------------------------------------------------------
# Pedido do dono (28/08/2026): alem da tabela de quilometros, a porta de entrada
# mostra QUEM andou mais -- no geral e por modalidade.
#
# SAO DUAS MODALIDADES, E SO DUAS. Cheguei a montar uma quarta lista separando
# CORRIDA de CAMINHADA por ritmo. O dono vetou, e a razao dele e' melhor que a
# minha: nao existe ritmo que separe as duas com honestidade -- quem marca
# "corrida" no Strava e faz 7:30 min/km alternou corrida e caminhada, e nao cabe
# a nos julgar isso. O ranking do projeto sempre foi um so para
# corrida+caminhada, e partir isso na tela criaria uma competicao que o projeto
# nao quer ter.
#
# O `esporte_strava` CONTINUA sendo gravado: e' dado que o Strava manda e que o
# codigo jogava fora ate hoje. Serve para relatorio interno e para o Ministerio,
# nunca para partir o ranking em dois.


ESCOPOS = {
    'geral': 'Todas as modalidades',
    'corrida_caminhada': 'Corrida e caminhada',
    'ciclismo': 'Ciclismo',
    'corrida': 'Somente corrida',
}

# Esportes que o Strava manda e que SAO corrida de verdade (Fase 4, 28/08/2026).
# Gravado desde 28/08/2026 -- atividade antiga fica sem isto (None) e cai no
# desempate por ritmo, contra o corte configuravel por cidade
# (`Config.pace_corte_corrida`).
_ESPORTES_DE_CORRIDA = ('Run', 'TrailRun', 'VirtualRun')


def _filtros_do_escopo(escopo):
    if escopo == 'ciclismo':
        return [Activity.modalidade == 'ciclismo']
    if escopo == 'corrida_caminhada':
        return [Activity.modalidade == 'corrida_caminhada']
    if escopo == 'corrida':
        # O corte e' POR CIDADE (`Config.pace_corte_corrida`) -- subquery
        # correlacionada pela cidade da PROPRIA atividade, pra uma pagina que
        # atravessa cidade (home, consolidado) respeitar o ajuste de cada uma.
        from sqlalchemy import select
        corte_da_cidade = (
            select(Config.pace_corte_corrida)
            .where(Config.cidade == Activity.cidade)
            .correlate(Activity)
            .scalar_subquery()
        )
        return [
            Activity.modalidade == 'corrida_caminhada',
            or_(
                Activity.esporte_strava.in_(_ESPORTES_DE_CORRIDA),
                db.and_(
                    Activity.esporte_strava.is_(None),
                    Activity.pace_min_km.isnot(None),
                    Activity.pace_min_km <= func.coalesce(corte_da_cidade, 7.0),
                ),
            ),
        ]
    return []


def classificacao(escopo='geral', cidade=None, limite=10, periodo='tudo',
                  de=None, ate=None, hoje=None):
    """Quem andou mais, no escopo pedido. Uma consulta agregada."""
    inicio, fim, _ = intervalo_do_periodo(periodo, de, ate, hoje)
    filtros = _filtros(inicio, fim, cidade) + _filtros_do_escopo(escopo)

    sub = _sem_repetir(
        filtros, _chave_atleta().label('chave'),
        agregados=[func.max(Activity.nome_usuario).label('nome')])
    consulta = db.session.query(
        sub.c.chave, func.max(sub.c.nome), func.sum(sub.c.km), func.count()
    ).group_by(sub.c.chave).order_by(func.sum(sub.c.km).desc())
    if limite:
        # folga: quem pediu reserva sai DEPOIS da consulta, e sem folga a lista
        # encurtaria em vez de trazer o proximo colocado
        consulta = consulta.limit(limite + 25)

    reservados = ocultos()
    # Selo da bicicleta (speed/mtb/gravel) tambem na tabela de km, nao so' na
    # de mais veloz -- pedido do dono (28/08/2026): quem anda de Speed cobre
    # mais km no mesmo tempo, a bicicleta explica parte do numero tambem
    # aqui, nao so' na velocidade.
    bicicletas = {}
    if escopo == 'ciclismo':
        try:
            bicicletas = {
                str(sid): tipo for sid, tipo in db.session.query(
                    Atleta.strava_id, Atleta.tipo_bicicleta
                ).filter(Atleta.strava_id.isnot(None)).all()
            }
        except Exception:
            bicicletas = {}

    linhas = []
    for chave, nome, km, qtd in consulta.all():
        sid = str(chave or '')
        if sid in reservados:
            continue
        linha = {
            'posicao': len(linhas) + 1,
            'nome': _nome_limpo(nome),
            'strava_id': sid if sid.isdigit() else None,
            'km': _km(km),
            'atividades': qtd,
        }
        if escopo == 'ciclismo':
            linha['tipo_bicicleta'] = bicicletas.get(sid) or 'mtb'
        linhas.append(linha)
        if limite and len(linhas) >= limite:
            break
    return linhas


# ---------------------------------------------------------------------------
# MAIS VELOZES -- reescrita em 09/09/2026 (o caso dos 109 km/h)
# ---------------------------------------------------------------------------
# A pagina inicial mostrava uma moradora com 109,6 km/h em corrida. Nao era
# fraude dela: uma das atividades tinha 1,18 km em 4 SEGUNDOS de tempo em
# movimento -- o GPS do celular pega um primeiro ponto longe antes de ficar
# preciso, e o Strava calcula em cima disso. Um unico fragmento desses
# desloca a media inteira.
#
# Regra de negocio nova, do dono: O QUE APARECE NA PAGINA PUBLICA TEM DE SER
# INFORMACAO CONFIAVEL. Um patrocinador em potencial que veja 109 km/h
# conclui, com razao, que a conta esta errada.
#
# Quatro mudancas, nesta ordem:
#  1. CORRIDA E CAMINHADA JUNTAS. A tabela usava o escopo "somente corrida",
#     que para atividade antiga (sem esporte gravado) so aceita ritmo abaixo
#     de 7 min/km -- ou seja, so as MAIS RAPIDAS. Os fragmentos de GPS
#     passavam e as caminhadas de verdade ficavam de fora: o filtro escolhia
#     justamente o lixo. Medido na mesma pessoa: 34 atividades e media 56,3
#     km/h no escopo antigo, contra 211 atividades e 13,5 km/h com tudo.
#  2. MEDIANA, NAO MEDIA. A mediana e o valor do meio: um outlier a 1.060
#     km/h desloca a media, mas nao desloca o meio.
#  3. FAIXA DE SANIDADE por modalidade, que tira tambem as quase-paradas
#     (abaixo de 1 km/h), que ninguem barrava e puxavam a media para baixo.
#  4. MINIMO DE ATIVIDADES LIMPAS, nao de atividades no total: e o que impede
#     alguem liderar com uma unica saida defeituosa.
#
# Flag ABERTA (ninguem olhou ainda) fica fora; flag APROVADA volta a contar --
# aprovar quer dizer "eu olhei, esta ok", e quem barra o absurdo e a faixa,
# nao o estado da flag. Decisao do dono em 09/09: assim ele aprova as mil
# flags em aberto sem punir o atleta que e rapido de verdade.
#
# NADA DISSO TIRA QUILOMETRO DE NINGUEM (regra 14): mexe so nesta tabela.

# Faixa plausivel por modalidade, em km/h. Fora dela nao entra na conta.
_FAIXA_VELOCIDADE = {
    'corrida_caminhada': (1.0, 25.0),
    'ciclismo':          (5.0, 50.0),
}

# ---------------------------------------------------------------------------
# TETO POR DISTANCIA -- porque um numero so' nao serve (09/09/2026)
# ---------------------------------------------------------------------------
# Quanto maior a distancia, menor a velocidade que um corpo sustenta. Um teto
# unico trata 5 km e 15 km igual, e foi por isso que uma atividade de 16,47 km
# a 3:28/km passou como legitima sem ser -- e que a tabela de recordes elegeu
# 23,4 km/h em 10,7 km, ACIMA do recorde mundial dos 10 km.
#
# O corte abaixo e' o RECORDE MUNDIAL de cada distancia: nao e' opiniao, e'
# limite fisico publico e verificavel. Ninguem num grupo de moradores de uma
# cidade pequena corre mais rapido que o campeao do mundo -- se o numero passa
# disso, o dado esta errado, ponto.
#
#   ate 5 km    12:35  Joshua Cheptegei (2020)     -> 23,8 km/h
#   10 km       26:24  Rhonex Kipruto  (2020)      -> 22,7 km/h
#   meia 21 km  57:30  Yomif Kejelcha  (2024)      -> 22,0 km/h
#   maratona  1:59:30  Sabastian Sawe  (2026)      -> 21,2 km/h
#
# ISTO E' TETO ABSOLUTO, NAO CRITERIO DE SUSPEITA. Serve para barrar o
# impossivel. O teto REALISTA para atleta amador -- que e' mais baixo e varia
# por pessoa -- fica na tela de parametros, por cidade e por atleta, e vence
# este aqui quando existir (decisao do dono: primeiro corrigir os erros,
# depois calibrar as metricas).
# Abaixo disto a atividade nao concorre a recorde (mas conta no ranking e na
# media, normalmente). Escolha do dono em 09/09: 2,2 km.
_DISTANCIA_MINIMA_RECORDE = 2.2

_TETO_POR_DISTANCIA = (
    # (ate quantos km, teto em km/h)
    (5.0,   23.8),
    (10.0,  22.7),
    (15.0,  22.2),      # entre 10 km e a meia, interpolado
    (21.1,  22.0),
    (999.0, 21.2),
)


# ---------------------------------------------------------------------------
# RECORDE MUNDIAL COMO REGUA (09/09/2026, pedido do dono)
# ---------------------------------------------------------------------------
# Numero solto nao diz nada a quem le: "17,2 km/h" e' rapido? e' devagar? A
# resposta so' aparece com uma regua ao lado, e a regua mais honesta e' o
# recorde do mundo -- publico, verificavel e que ninguem contesta.
#
# CORRIDA: recorde oficial da distancia mais proxima (World Athletics).
#   5 km     12:35  Joshua Cheptegei (2020)   23,8 km/h
#   10 km    26:24  Rhonex Kipruto   (2020)   22,7 km/h
#   meia     57:30  Yomif Kejelcha   (2024)   22,0 km/h
#   maratona 1:59:30 Sabastian Sawe  (2026)   21,2 km/h
#
# CICLISMO: recorde da hora da UCI -- Filippo Ganna, 56,792 km em 1 h (2022).
# ⚠️ E' em VELODROMO, com bicicleta de pista: e' teto absoluto, nao alvo justo
# para quem pedala em estrada de terra. O texto da tela diz isso, para a
# comparacao nao virar humilhacao de ninguem.
_RECORDES_MUNDIAIS = {
    'corrida_caminhada': (
        # (ate quantos km, km/h, quem, quando, prova)
        (5.0,   23.8, 'Joshua Cheptegei',  2020, '5 km'),
        (10.0,  22.7, 'Rhonex Kipruto',    2020, '10 km'),
        (15.0,  22.2, 'Rhonex Kipruto',    2020, '10 km'),
        (21.1,  22.0, 'Yomif Kejelcha',    2024, 'meia maratona'),
        (999.0, 21.2, 'Sabastian Sawe',    2026, 'maratona'),
    ),
    'ciclismo': (
        (999.0, 56.8, 'Filippo Ganna', 2022, 'recorde da hora (velódromo)'),
    ),
}


def _referencia_mundial(modalidade, km):
    """A régua para esta atividade: quem detém, quando, e quanto o atleta fez.

    Devolve dicionário pronto para a tela, ou {} quando não há referência.
    """
    tabela = _RECORDES_MUNDIAIS.get(modalidade) or ()
    try:
        km = float(km or 0)
    except (TypeError, ValueError):
        km = 0.0
    for limite, kmh, quem, ano, prova in tabela:
        if km <= limite:
            return {'kmh': kmh, 'quem': quem, 'ano': ano, 'prova': prova}
    return {}


def _quanto_do_recorde(velocidade, referencia):
    """Percentual do recorde mundial, inteiro. None quando não dá para dizer.

    Percentual é o jeito que mais gente entende de primeira -- melhor que
    "faltam X km/h", que exige a pessoa saber o recorde de cabeça.
    """
    try:
        alvo = float((referencia or {}).get('kmh') or 0)
        v = float(velocidade or 0)
    except (TypeError, ValueError):
        return None
    if alvo <= 0 or v <= 0:
        return None
    return int(round(100.0 * v / alvo))


def _teto_da_distancia(km):
    """Teto de velocidade plausivel para esta distancia, em km/h.

    Distancia ausente ou zero devolve o teto mais permissivo: na duvida nao se
    descarta atividade -- descartar por falta de dado seria punir o atleta por
    uma falha nossa de leitura.
    """
    try:
        km = float(km or 0)
    except (TypeError, ValueError):
        return _TETO_POR_DISTANCIA[0][1]
    if km <= 0:
        return _TETO_POR_DISTANCIA[0][1]
    for limite, teto in _TETO_POR_DISTANCIA:
        if km <= limite:
            return teto
    return _TETO_POR_DISTANCIA[-1][1]
# Quantas atividades LIMPAS o atleta precisa ter para aparecer.
_MINIMO_LIMPAS = 5
# Corte por dispersao robusta: distancias medianas em relacao a mediana.
# Usa o desvio absoluto mediano (MAD) e nao o desvio padrao de proposito --
# o desvio padrao e calculado a partir da media, entao o proprio outlier
# infla a regua que deveria medi-lo, e o valor absurdo acaba "cabendo".
_CORTE_MAD = 3.0


def _mediana(valores):
    """Valor do meio de uma lista. Lista vazia devolve None."""
    if not valores:
        return None
    ordenados = sorted(valores)
    meio = len(ordenados) // 2
    if len(ordenados) % 2:
        return ordenados[meio]
    return (ordenados[meio - 1] + ordenados[meio]) / 2.0


def _sem_outliers(valores):
    """Tira o que esta a mais de _CORTE_MAD desvios medianos do meio.

    Com poucos valores o MAD pode dar zero (todos iguais ou quase); nesse
    caso nao ha o que cortar e a lista volta inteira, em vez de sumir com
    tudo por divisao degenerada.
    """
    med = _mediana(valores)
    if med is None:
        return []
    desvios = [abs(v - med) for v in valores]
    mad = _mediana(desvios)
    if not mad:
        return list(valores)
    return [v for v in valores if abs(v - med) <= _CORTE_MAD * mad]


def _ritmo_por_km(velocidade_kmh):
    """Ritmo "6:12" a partir de km/h.

    O dono pediu os dois lado a lado: tem quem so entenda km/h e quem so
    entenda ritmo.
    """
    if not velocidade_kmh or velocidade_kmh <= 0:
        return None
    minutos = 60.0 / velocidade_kmh
    m = int(minutos)
    s = int(round((minutos - m) * 60))
    if s == 60:
        m, s = m + 1, 0
    return '%d:%02d' % (m, s)


def _nomes_corrigidos():
    """strava_id -> nome cadastrado por nos, quando existe.

    O dono corrige o nome na tela de atletas (o do Strava vem como a pessoa
    digitou, muitas vezes tudo em minusculo). E esse nome que deve aparecer
    na pagina publica; o do Strava so vale quando nao ha correcao.
    """
    try:
        return {str(sid): (nome or '').strip()
                for sid, nome in db.session.query(Atleta.strava_id, Atleta.nome_atual)
                .filter(Atleta.strava_id.isnot(None)).all()
                if sid and (nome or '').strip()}
    except Exception:
        return {}


def atividades_por_sexo(cidade=None):
    """Quantas atividades são de homens e quantas de mulheres.

    POR QUE É CONTAGEM AGREGADA, E NUNCA POR PESSOA (09/09/2026, dono)

    O sexo de cada atleta NÃO aparece em página pública. O que aparece aqui é
    só o total do grupo — quantas atividades vieram de homens e quantas de
    mulheres —, que é dado de alcance do projeto, útil para patrocinador e
    prestação de contas. Ninguém é identificado.

    O TOTAL SEMPRE FECHA. O dado é preenchido à mão (o Strava não publica
    sexo), então sempre haverá quem falte. Em vez de somar duas partes que não
    dão o todo — e deixar o leitor achando que a conta está errada —, mostramos
    a PORCENTAGEM sobre o que está preenchido, e dizemos quanto do total isso
    representa. Assim a página é honesta sobre o que sabe e o que não sabe.
    """
    from app.models import Atleta

    filtros = _filtros(None, None, cidade)
    try:
        sexos = {str(sid): (sx or '').upper()
                 for sid, sx in db.session.query(Atleta.strava_id, Atleta.sexo)
                 .filter(Atleta.strava_id.isnot(None)).all()}
    except Exception:
        return {}

    sub = _sem_repetir(filtros, _chave_atleta().label('chave'))
    try:
        linhas = db.session.query(sub.c.chave, func.count()).group_by(sub.c.chave).all()
    except Exception:
        return {}

    h = m = sem = 0
    for chave, quantas in linhas:
        s = sexos.get(str(chave or ''), '')
        if s == 'M':
            h += int(quantas or 0)
        elif s == 'F':
            m += int(quantas or 0)
        else:
            sem += int(quantas or 0)

    conhecido = h + m
    total = conhecido + sem
    if not conhecido:
        return {}
    return {
        'homens': h,
        'mulheres': m,
        'sem_informacao': sem,
        'total': total,
        'pct_homens': int(round(100.0 * h / conhecido)),
        'pct_mulheres': int(round(100.0 * m / conhecido)),
        # Quanto do total tem a informação: é isto que impede o leitor de
        # somar duas partes e estranhar não dar o todo.
        'pct_conhecido': int(round(100.0 * conhecido / total)) if total else 0,
    }


def _teto_do_dono(strava_id, cidade, modalidade, km):
    """O teto que vale para esta atividade, segundo a tela do dono.

    Ponte para `parametrosvel.teto_para`. Se algo falhar (tabela ainda nao
    criada, banco ocupado), devolve o teto FISICO de antes em vez de None:
    tabela publica sem trava nenhuma foi o incidente de 08/09 (regra 22).
    """
    try:
        from app import parametrosvel
        valor = parametrosvel.teto_para(strava_id, cidade, modalidade, km)
        if valor:
            return float(valor)
    except Exception:
        # SEM `logger` NESTE MODULO (conferido em 09/09): chamar um aqui
        # levantaria NameError DENTRO do except e derrubaria a pagina inicial.
        # O silencio e' seguro porque a linha de baixo garante a trava fisica.
        pass
    return _teto_da_distancia(km) if modalidade == 'corrida_caminhada' else None


def recordistas(escopo, cidade=None, limite=5):
    """A MELHOR atividade de cada pessoa, depois da limpeza — o recorde.

    POR QUE DUAS TABELAS, E NAO UMA (09/09/2026, pedido do dono)

    "Quem anda mais rapido no dia a dia" e "quem tem o melhor resultado" sao
    perguntas diferentes, e ate hoje estavam misturadas numa tabela so'. O
    atleta mais veloz de verdade nao e' quem repete a mesma media 500 vezes:
    e' quem tem UMA atividade que ninguem alcancou. A mediana, que responde bem
    a primeira pergunta, apaga exatamente essa — ela procura o meio, nao o pico.

    Entao: `mais_velozes` continua respondendo o dia a dia (mediana), e esta
    aqui responde o recorde. Cada uma com o seu nome na tela, para ninguem
    confundir.

    A LIMPEZA E' A MESMA, e ela importa mais aqui: numa tabela de recorde, o
    lixo do GPS SEMPRE vence. A atividade de 1,18 km em 4 segundos (1.060 km/h)
    seria a campea eterna. Por isso vale a faixa de sanidade e a marca do
    antitrapaca -- e, ainda assim, o numero fica CLICAVEL, para qualquer pessoa
    abrir a atividade no Strava e conferir. Transparencia e' a ultima trava:
    nenhum filtro automatico substitui um par de olhos quando o assunto e'
    dizer publicamente quem foi o mais rapido.
    """
    if escopo in ('corrida', 'corrida_caminhada'):
        modalidade = 'corrida_caminhada'
    elif escopo == 'ciclismo':
        modalidade = 'ciclismo'
    else:
        return []
    piso, teto = _FAIXA_VELOCIDADE[modalidade]

    filtros = _filtros(None, None, cidade) + [
        Activity.modalidade == modalidade,
        Activity.velocidade_media.isnot(None),
        Activity.velocidade_media > piso,
        Activity.velocidade_media <= teto,
        or_(Activity.velocidade_confiavel.is_(True),
            Activity.velocidade_confiavel.is_(None)),
    ]
    # Aqui NAO se agrupa por atleta no banco: precisamos da atividade inteira
    # (id, data, distancia) para poder linkar. Traz so' as mais rapidas.
    linhas = (
        db.session.query(
            _chave_atleta().label('chave'),
            Activity.nome_usuario, Activity.velocidade_media,
            Activity.id_atividade, Activity.data_hora, Activity.distancia,
        )
        .filter(*filtros)
        .order_by(Activity.velocidade_media.desc())
        .limit(400)          # folga para descartar ocultos e repetidos
        .all()
    )

    reservados = ocultos()
    corrigidos = _nomes_corrigidos()
    vistos, saida = set(), []
    for chave, nome, vel, id_atividade, quando, dist in linhas:
        sid = str(chave or '')
        if sid in reservados or sid in vistos:
            continue          # UMA linha por pessoa: e' o recorde DELA
        # DISTANCIA MINIMA PARA CONCORRER A RECORDE (09/09/2026, dono: 2,2 km).
        #
        # Nao e' o calculo que erra: a velocidade ja sai do `moving_time` em
        # SEGUNDOS (o `tempo_minutos` do banco e' so' para exibir). Medido em
        # 09/09 -- 1,71 km a 22,83 km/h confere com o tempo real de 4min29s.
        # O problema e' que o proprio tempo do Strava INCLUI o trecho em que o
        # GPS ainda estava impreciso: os primeiros 200 m entram com velocidade
        # que ninguem fez. Em atividade curta esse trecho e' uma fatia grande
        # do total e desloca tudo; em 10 km ele se dilui.
        #
        # Nenhum recalculo conserta isso, porque o dado de entrada ja vem
        # contaminado. O que resolve e' nao deixar distancia curta concorrer a
        # RECORDE -- ela continua contando no ranking e na media, como sempre.
        if float(dist or 0) < _DISTANCIA_MINIMA_RECORDE:
            continue
        # O TETO DEPENDE DA DISTANCIA (ver _TETO_POR_DISTANCIA). Numa tabela de
        # recorde o lixo de GPS sempre vence, entao aqui a trava e' mais dura
        # que na tabela do dia a dia.
        # A REGUA DO DONO MANDA, DENTRO DO LIMITE FISICO.
        #
        # Ate 09/09/2026 aqui so valia `_teto_da_distancia`, que e' o recorde
        # MUNDIAL -- teto tao alto que quase nada era barrado, e a tela de
        # "Velocidade plausivel" que o dono preenche nao tinha nenhum leitor
        # (achado na varredura de codigo orfao do mesmo dia). `teto_para` segue
        # a hierarquia atleta > cidade > sugestao e continua limitada pelo
        # recorde mundial, entao nenhum ajuste na tela pode liberar o
        # impossivel. Vale para ciclismo tambem, que antes nao tinha teto aqui.
        _limite = _teto_do_dono(sid, cidade, modalidade, dist)
        if _limite and float(vel or 0) > _limite:
            continue
        vistos.add(sid)
        saida.append({
            'posicao': len(saida) + 1,
            'nome': _nome_limpo(corrigidos.get(sid) or nome),
            'strava_id': sid if sid.isdigit() else None,
            'velocidade_media': round(float(vel or 0), 1),
            'ritmo': _ritmo_por_km(vel) if modalidade == 'corrida_caminhada' else None,
            'atividade_id': str(id_atividade or ''),
            'quando': quando.strftime('%d/%m/%Y') if quando else '',
            'distancia': round(float(dist or 0), 2),
            # A REGUA AO LADO DO NUMERO: sem ela, "17,2 km/h" nao diz nada a
            # quem nao corre. O recorde e' escolhido pela DISTANCIA daquela
            # atividade -- comparar 10 km com o recorde dos 5 km seria trapaça
            # na conta.
            'recorde': _referencia_mundial(modalidade, dist),
            'porcento_do_recorde': _quanto_do_recorde(
                vel, _referencia_mundial(modalidade, dist)),
        })
        if len(saida) >= limite:
            break
    return saida


def mais_velozes(escopo, cidade=None, limite=5, minimo_atividades=_MINIMO_LIMPAS):
    """Top N por velocidade TIPICA (mediana), corrida+caminhada ou ciclismo.

    Ver o cabecalho desta secao para o porque de cada trava.
    """
    if escopo in ('corrida', 'corrida_caminhada'):
        modalidade = 'corrida_caminhada'
    elif escopo == 'ciclismo':
        modalidade = 'ciclismo'
    else:
        return []
    piso, teto = _FAIXA_VELOCIDADE[modalidade]

    filtros = _filtros(None, None, cidade) + [
        Activity.modalidade == modalidade,
        Activity.velocidade_media.isnot(None),
        Activity.velocidade_media > 0,
        or_(Activity.velocidade_confiavel.is_(True),
            Activity.velocidade_confiavel.is_(None)),
    ]
    sub = _sem_repetir(
        filtros, _chave_atleta().label('chave'),
        agregados=[
            func.max(Activity.nome_usuario).label('nome'),
            func.max(Activity.velocidade_media).label('vel'),
        ])
    linhas_brutas = db.session.query(sub.c.chave, sub.c.nome, sub.c.vel).all()

    por_atleta = {}
    for chave, nome, vel in linhas_brutas:
        if vel is None:
            continue
        try:
            vel = float(vel)
        except (TypeError, ValueError):
            continue
        if vel < piso or vel > teto:
            continue                      # fora da faixa plausivel
        d = por_atleta.setdefault(str(chave or ''), {'nome': nome, 'vels': []})
        if nome:
            d['nome'] = nome
        d['vels'].append(vel)

    reservados = ocultos()
    corrigidos = _nomes_corrigidos()
    bicicletas = {}
    if modalidade == 'ciclismo':
        try:
            bicicletas = {
                str(sid): tipo for sid, tipo in db.session.query(
                    Atleta.strava_id, Atleta.tipo_bicicleta
                ).filter(Atleta.strava_id.isnot(None)).all()
            }
        except Exception:
            bicicletas = {}

    candidatos = []
    for sid, d in por_atleta.items():
        if sid in reservados:
            continue
        limpas = _sem_outliers(d['vels'])
        if len(limpas) < minimo_atividades:
            continue
        tipica = _mediana(limpas)
        if tipica is None:
            continue
        candidatos.append((tipica, len(limpas), sid, d['nome']))

    candidatos.sort(key=lambda item: item[0], reverse=True)

    linhas = []
    for tipica, quantas, sid, nome in candidatos[:limite]:
        linha = {
            'posicao': len(linhas) + 1,
            'nome': _nome_limpo(corrigidos.get(sid) or nome),
            'strava_id': sid if sid.isdigit() else None,
            'velocidade_media': round(tipica, 1),
            # RITMO SO' EM CORRIDA/CAMINHADA. Em ciclismo ninguem fala em
            # min/km -- "1:58/km" numa pedalada nao diz nada a ninguem.
            'ritmo': _ritmo_por_km(tipica) if modalidade == 'corrida_caminhada' else None,
            'atividades': quantas,
        }
        if modalidade == 'ciclismo':
            linha['tipo_bicicleta'] = bicicletas.get(sid) or 'mtb'
        linhas.append(linha)
    return linhas


def classificacoes_da_capa(limite=10, periodo='tudo'):
    """As quatro tabelas da pagina inicial, na ordem que o dono pediu.

    `periodo` (P-122, 28/08/2026): 'tudo' vem da foto em cache; qualquer
    outro periodo e' calculado NA HORA (nao entra no cache diario).
    """
    return [
        {'id': chave, 'titulo': ESCOPOS[chave],
         'linhas': classificacao(chave, limite=limite, periodo=periodo)}
        for chave in ('geral', 'corrida_caminhada', 'ciclismo', 'corrida')
    ]


def cobertura_do_esporte():
    """Quantas atividades ja tem o esporte do Strava gravado.

    Serve para a tela dizer a verdade: enquanto a maioria estiver sem, a
    separacao corrida/caminhada e' mista (dado onde ha, ritmo onde nao ha).
    """
    filtros = _base_valida() + [Activity.modalidade == 'corrida_caminhada']
    total = db.session.query(func.count()).select_from(Activity).filter(*filtros).scalar() or 0
    com = db.session.query(func.count()).select_from(Activity).filter(
        *(filtros + [Activity.esporte_strava.isnot(None),
                     func.trim(Activity.esporte_strava) != ''])).scalar() or 0
    return {'com': com, 'total': total,
            'porcento': round(100.0 * com / total, 1) if total else 0.0}


# ---------------------------------------------------------------------------
# QUEM APOIA -- a lista de patrocinadores da pagina inicial
# ---------------------------------------------------------------------------
# Pedido do dono (28/08/2026): no fim da porta de entrada, uma tabela com TODOS
# que ja apoiaram -- nome, logo em miniatura legivel, o mes, e o QUE cada um
# banca (medalhas, a nutricionista...). Quem apoia todo mes aparece marcado como
# continuo, uma linha so, em vez de repetir doze vezes.
#
# Nao aparece quem foi desmarcado na ficha (`mostrar_na_home`): apoiar nao
# obriga ninguem a virar vitrine.

_MES_CURTO = {1: 'jan', 2: 'fev', 3: 'mar', 4: 'abr', 5: 'mai', 6: 'jun',
              7: 'jul', 8: 'ago', 9: 'set', 10: 'out', 11: 'nov', 12: 'dez'}


def patrocinadores_publicos(cidade=None):
    """Um item por apoiador, com os meses em que apoiou."""
    consulta = PatrocinadorMensal.query
    if cidade:
        consulta = consulta.filter(PatrocinadorMensal.cidade == cidade)
    try:
        linhas = consulta.order_by(
            PatrocinadorMensal.ano.asc(), PatrocinadorMensal.mes.asc()).all()
    except Exception:
        return []

    por_nome = {}
    for p in linhas:
        # Quem manda e' a ficha do patrocinador (o catalogo); o registro do
        # mes serve de excecao. Qualquer um dos dois dizendo NAO tira da lista.
        if getattr(p, 'mostrar_na_home', True) is False:
            continue
        # `_ficha` ja resolve o catalogo por id OU pelo nome -- o `catalogo_id`
        # esta vazio em todo registro antigo (medido em 14/08), e depender so
        # dele deixaria a marcacao sem efeito, sem erro nenhum na tela.
        try:
            ficha = p._ficha
        except Exception:
            ficha = None
        if ficha is not None and getattr(ficha, 'mostrar_na_home', True) is False:
            continue
        nome = (p.nome_exibicao or p.patrocinador_nome or '').strip()
        if not nome:
            continue
        item = por_nome.setdefault(nome.lower(), {
            'nome': nome,
            'meses': [],
            'periodos': [],
            'continuo': False,
            'banca': set(),
            'logo_id': None,
            'cidades': set(),
        })
        item['periodos'].append((p.ano, p.mes))
        item['meses'].append('{}/{}'.format(_MES_CURTO.get(p.mes, p.mes), p.ano))
        if getattr(p, 'patrocinio_continuo', False):
            item['continuo'] = True
        oq = (getattr(p, 'o_que_banca', '') or '').strip()
        # "as medalhas" so entra se o texto livre ja nao disser isso -- senao a
        # linha saia "Patrocinio das medalhas · as medalhas".
        if getattr(p, 'banca_medalhas', False) and 'medalha' not in oq.lower():
            item['banca'].add('as medalhas')
        # `o_que_banca` guarda tanto "o que" quanto, em registro antigo, o
        # ENDERECO de retirada. Texto com "Av." ou "Rua" e' endereco: nao entra
        # na coluna do que a pessoa banca.
        if oq and not oq.lower().startswith(('av.', 'av ', 'rua', 'r.', 'pra')):
            item['banca'].add(oq)
        if p.patrocinador_logo_blob and not item['logo_id']:
            item['logo_id'] = p.id
        if p.cidade:
            item['cidades'].add(p.cidade)

    saida = []
    for item in por_nome.values():
        item['periodos'].sort()
        item['banca'] = sorted(item['banca'])
        item['cidades'] = sorted(item['cidades'])
        item['primeiro'] = item['periodos'][0] if item['periodos'] else None
        item['ultimo'] = item['periodos'][-1] if item['periodos'] else None
        item['qtd_meses'] = len(set(item['periodos']))
        saida.append(item)
    saida.sort(key=lambda x: (x['ultimo'] or (0, 0)), reverse=True)
    return saida
