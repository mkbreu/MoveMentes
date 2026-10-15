# -*- coding: utf-8 -*-
"""O mapa de navegacao do site — e o detector de tela orfa.

O PROBLEMA QUE ISTO RESOLVE (P-50)
Em 07/08/2026 o dono escreveu: *"foram tantas mudancas e reorganizacao de
pagina que dificultou o acesso"*, *"nao consigo encontrar o historico de
mensagens, avisos, automacoes"*, *"preciso encontrar tudo e ter tudo
disponivel"*. O levantamento achou **57 telas abriveis** e o painel mostrava
uma fracao. Tela que existe e ninguem alcanca e o mesmo que tela que nao
existe — so que ocupando espaco e dando a sensacao de que o sistema comeu
alguma coisa.

ESTE ARQUIVO E A UNICA LISTA DE NAVEGACAO DO SISTEMA
Painel (`/admin`), tela de cada assunto (`/admin/area/<chave>`) e sumario
(`/admin/indice`) leem daqui. Antes existiam duas listas — os cards escritos
a mao no painel e a descoberta automatica do sumario — e elas discordavam: o
sumario acusava orfa uma tela que o painel jurava mostrar. Uma lista so, e a
discordancia deixa de ser possivel.

Correcao de 09/08: o dono pediu **cards dentro de cards**, no estilo da
`/welcome` — *"o card todo e o botao"*, sem ter que mirar num botaozinho
"Abrir". Nivel 1 e o assunto, nivel 2 sao as telas daquele assunto. E por isso
que cada tela tem aqui um **nome curto** e um **icone**: no card cabe o nome,
nao o paragrafo. O paragrafo explicativo continua existindo — mas so no
sumario, que e a pagina que serve para explicar.

COMO A ORFANDADE E DECIDIDA
Uma tela esta alcancavel se **algum template** do site chama `url_for` para
ela. Nao ha adivinhacao: e a mesma coisa que o navegador faz. Menu, card,
link no meio de um texto — tudo conta, porque tudo e caminho de verdade.
Com os cards de assunto, `area.html` passa a apontar para todas as telas
catalogadas aqui — entao **tela catalogada nao fica mais orfa**, que era
exatamente o pedido. Sobra vermelho so para o que ninguem colocou em lugar
nenhum, que e quando o vermelho serve para alguma coisa.

E ai mora a diferenca que importa: **o vermelho deixou de ser "esqueci de
fazer o card" e passou a ser "esta tela nao esta no catalogo"**. Trocar de
lugar no painel agora e editar uma linha aqui; esquecer, o proprio sumario
avisa.

OS TRES NIVEIS DO SUMARIO
    1. Categoria      — "Cadastros", "Mensagens"…      (o card do painel)
    2. Tela           — as paginas daquele assunto      (dentro do card)
    3. Links da tela  — para onde AQUELA tela leva      (dentro de cada uma)

Uma tela do nivel 3 pode ser de outra categoria, e **esta tudo bem**: ela ja
aparece no nivel 2 da categoria dela. O nivel 3 responde "de onde da para
chegar aqui", nao "a quem isto pertence".
"""
import inspect
import logging
import os
import re

logger = logging.getLogger(__name__)


# ===================== O CATALOGO =====================
#
# Cada tela: (endpoint, nome curto para o card, icone, nivel de acesso).
#
# NIVEL `None` = `pode_administrar` (admin da cidade para cima). Nao e' "todo
# mundo": desde 20/09/2026 o visualizador e o patrocinador tambem entram no
# painel, e a tela que eles podem abrir diz `pode_ver` aqui. Quem le isto e'
# `NIVEL_PADRAO`, logo abaixo.
# O nivel e o do MODELO de usuario (`current_user.<nivel>`), nao um texto
# solto: 'is_master' e 'is_supremo' sao propriedades que ja existem. None =
# qualquer admin. Isto evita o card que abre e da "sem permissao" na cara.
#
# O nome curto e obrigatorio e e curto de proposito: card com paragrafo dentro
# vira parede de texto e ninguem le. A explicacao mora no sumario.

# O que vale quando a linha nao diz nivel (20/09/2026): administrar, nunca ver.
# Assim, esquecer o nivel numa tela nova FECHA, em vez de abrir.
NIVEL_PADRAO = 'pode_administrar'

CATEGORIAS = [
    {
        'chave': 'mensagens',
        'titulo': 'Mensagens e avisos',
        'icone': 'fa-comment-dots',
        'cor': 'success',
        'descricao': 'Tudo que o sistema envia: para o grupo, para o privado '
                     'e para os patrocinadores.',
        'no_painel': True,
        'telas': [
            ('admin.central_mensagens', 'Central de mensagens', 'fa-inbox', None),
            ('admin.postagem', 'Quem publica e quando', 'fa-list-ol', None),
            ('avisos.index', 'Avisos programados', 'fa-bullhorn', None),
            ('admin.mensagens_ciclo', 'Textos do ciclo', 'fa-file-lines', None),
            # Tela nova entra no catalogo, senao aparece em vermelho em
            # /admin/indice (convencao do projeto).
            ('admin.textos_site', 'Textos da página inicial', 'fa-pen-to-square', 'is_supremo'),
            ('admin.parametros_velocidade', 'Velocidade plausível', 'fa-gauge-high', None),
        ],
    },
    {
        'chave': 'medalhas',
        'titulo': 'Medalhas',
        'icone': 'fa-medal',
        'cor': 'primary',
        'descricao': 'Quem bateu a meta, as etiquetas para colar e o '
                     'fechamento do mês.',
        'no_painel': True,
        'telas': [
            ('admin.medalhistas', 'Medalhistas do mês', 'fa-medal', None),
            ('admin.etiquetas', 'Etiquetas para colar', 'fa-tags', None),
            ('admin.fechamento', 'Fechamento e backups', 'fa-lock', None),
        ],
    },
    {
        'chave': 'atletas',
        'titulo': 'Atletas',
        'icone': 'fa-person-running',
        'cor': 'primary',
        'descricao': 'Quem corre e pedala: cadastro, telefones, conferência '
                     'e revisões.',
        'no_painel': True,
        'telas': [
            ('admin.lista_atletas', 'Lista de atletas', 'fa-people-group', None),
            ('admin.telefones_atletas', 'Agenda telefônica', 'fa-address-book', None),
            ('admin.reviews', 'Revisões do anticheat', 'fa-shield-halved', None),
            ('admin.anticheat_config', 'Regras do anticheat', 'fa-sliders', None),
            ('conferencia.escolher_cidade', 'Conferência pública', 'fa-magnifying-glass', 'pode_ver'),
            ('admin.exportar_telefones', 'Baixar a agenda', 'fa-file-csv', None),
        ],
    },
    {
        'chave': 'ranking',
        'titulo': 'Ranking e patrocínio',
        'icone': 'fa-trophy',
        'cor': 'warning',
        'descricao': 'O ranking do mês, os patrocinadores e a prestação de contas.',
        'no_painel': True,
        'telas': [
            ('ranking.current', 'Ranking do mês', 'fa-trophy', 'pode_ver'),
            # 20/09/2026 (P-137): trimestre, semestre e ano, com o Copiar.
            # 25/09 (P-137-c): pela rota SEM argumento — `/periodo/<periodo>`
            # nao vira card (`_abrivel`), e o card nunca tinha aparecido.
            ('ranking.periodo_inicio', 'Ranking por período', 'fa-calendar-check', 'pode_ver'),
            ('ranking.patrocinadores', 'Patrocinadores', 'fa-handshake', None),
            ('config.prestacao', 'Prestação de contas', 'fa-receipt', 'pode_ver'),
            ('caixa.index', 'Livro-caixa', 'fa-book', 'pode_ver'),  # 19/09/2026 (item 73)
            ('caixa.fluxo', 'Fluxo de caixa', 'fa-arrow-right-arrow-left', 'pode_ver'),  # 24/09/2026
            ('pedidos.lista', 'Pedidos recebidos', 'fa-inbox', 'pode_administrar'),  # 25/09/2026
            ('execucao.index', 'O que o projeto fez', 'fa-clipboard-check', 'pode_ver'),
            ('relatorios.index', 'Relatórios', 'fa-file-lines', 'pode_ver'),
        ],
    },
    {
        'chave': 'upload',
        'titulo': 'Entrada de dados',
        'icone': 'fa-file-arrow-up',
        'cor': 'info',
        'descricao': 'Por onde os links do Strava entram no sistema.',
        'no_painel': True,
        'telas': [
            ('upload.index', 'Enviar a conversa', 'fa-file-arrow-up', None),
            ('upload.manual_links', 'Colar links na mão', 'fa-link', None),
            ('upload.importar_atleta', 'Importar pelo perfil', 'fa-user-clock', None),
        ],
    },
    {
        'chave': 'automacao',
        'titulo': 'Automação',
        'icone': 'fa-robot',
        'cor': 'info',
        'descricao': 'A máquina que publica sozinha: WhatsApp, Strava e os comandos.',
        'no_painel': True,
        'telas': [
            # 19/09/2026 (item 73): so o supremo mexe em horario, cookie,
            # token e endereco. Ao admin e ao master sobra o que eles usam para
            # tocar a cidade: parear e reconectar o WhatsApp.
            ('admin.painel_automayk', 'Painel da automação', 'fa-robot', 'is_supremo'),
            ('admin.whatsapp_sessao', 'Sessão do WhatsApp', 'fa-comments', None),
            ('admin.strava_sessao', 'Sessão do Strava', 'fa-plug', 'is_supremo'),
            ('admin.automacao', 'Token e endereços', 'fa-key', 'is_supremo'),
        ],
    },
    {
        'chave': 'cadastros',
        'titulo': 'Cadastros e configuração',
        'icone': 'fa-sliders',
        'cor': 'secondary',
        'descricao': 'Cidades, usuários, metas e as configurações da cidade.',
        'no_painel': True,
        'telas': [
            # 19/09/2026 (item 73): a rota e' master/aprovador — o card nao pode
            # prometer o que da 'sem permissao' na cara do admin.
            ('admin.manage_users', 'Usuários', 'fa-users', 'pode_aprovar'),
            ('admin.add_user', 'Adicionar usuário', 'fa-user-plus', 'pode_aprovar'),
            # 19/09/2026 (item 73): criar, ativar e renomear cidade e do
            # supremo — e ele quem abre cidade nova e nomeia o master dela.
            ('admin.manage_cities', 'Cidades', 'fa-city', 'is_supremo'),
            ('admin.add_city', 'Adicionar cidade', 'fa-plus', 'is_supremo'),
            ('admin.renomear_cidade', 'Renomear cidade', 'fa-arrow-right-arrow-left', 'is_master'),
            ('config.view_config', 'Configuração da cidade', 'fa-gear', None),
            ('config.edit_config', 'Editar a configuração', 'fa-pen', 'pode_aprovar'),
        ],
    },
    {
        'chave': 'manutencao',
        'titulo': 'Manutenção',
        'icone': 'fa-screwdriver-wrench',
        'cor': 'dark',
        'descricao': 'Backup, restauração e as coisas que se usam de vez em '
                     'quando — ou uma vez só.',
        'no_painel': True,
        'telas': [
            ('admin.manutencao', 'Backup e restauração', 'fa-database', 'is_supremo'),  # 19/09/2026
            # 19/09/2026 (item 73): saiu de "Automação" — comando de sistema e
            # manutencao, nao operacao da cidade.
            ('admin.comandos', 'Comandos do sistema', 'fa-terminal', 'is_supremo'),
            ('admin.reinstalacao', 'Kit de reinstalação', 'fa-toolbox', 'is_supremo'),
            ('admin.limpeza_copias_palmeiropolis', 'Limpeza de cópias',
             'fa-broom', 'is_supremo'),
        ],
    },
    {
        'chave': 'publicas',
        'titulo': 'Páginas públicas',
        'icone': 'fa-globe',
        'cor': 'success',
        'descricao': 'O que qualquer pessoa vê sem login — é por aqui que o '
                     'projeto deixa de ser um circuito fechado.',
        'no_painel': True,
        'telas': [
            ('ranking.consolidado_publico', 'Números do projeto', 'fa-chart-line', 'pode_ver'),
            ('adesao.inscrever', 'Inscrição pública', 'fa-user-plus', 'pode_ver'),
            ('adesao.termos', 'Termos de uso', 'fa-file-contract', 'pode_ver'),
            ('conferencia.escolher_cidade', 'Conferência pública', 'fa-magnifying-glass', 'pode_ver'),
            # 19/09/2026 (item 71): o feedback dos atletas e' pagina publica.
            ('admin.feedbacks', 'Feedback dos atletas', 'fa-comment-dots', 'pode_ver'),
        ],
    },
    {
        # Fora do painel: login, senha e perfil ja moram no menu de cima, ao
        # lado do nome do usuario. Card para eles seria repeticao.
        'chave': 'conta',
        'titulo': 'Sua conta',
        'icone': 'fa-user',
        'cor': 'secondary',
        'descricao': 'Login, senha e perfil.',
        'no_painel': False,
        'telas': [
            ('welcome_page', 'Início', 'fa-house', None),
            ('auth.profile', 'Meu perfil', 'fa-id-card', None),
            ('auth.change_password', 'Trocar a senha', 'fa-key', None),
            # 20/09/2026 (P-103): trocar o e-mail e pedir senha nova NAO sao
            # telas — sao formularios dentro de "Trocar a senha" e da tela de
            # entrar (correcao do dono: nao criar tela onde ja existe uma).
            ('auth.login', 'Entrar', 'fa-right-to-bracket', None),
            ('auth.logout', 'Sair', 'fa-right-from-bracket', None),
            ('auth.register', 'Criar conta', 'fa-user-plus', None),
        ],
    },
]

# Botao que EXECUTA em vez de abrir tela. Fica no card junto das telas do
# assunto, porque para quem usa nao ha diferenca: e mais uma coisa que se faz
# ali. A diferenca (POST, confirmacao) e problema do template.
ACOES = {
    'ranking': [
        {'endpoint': 'admin.recalcular_ranking',
         'nome': 'Reconsolidar o ranking',
         'icone': 'fa-calculator',
         'confirmacao': 'Reconsolidar o ranking do mês atual agora?',
         'nivel': None},
    ],
}

_SEM_CATEGORIA = {
    'chave': 'outras',
    'titulo': 'Ainda sem categoria',
    'icone': 'fa-circle-question',
    'cor': 'secondary',
    'descricao': 'Telas que existem e ainda não foram colocadas num assunto. '
                 'Não é erro — é lembrete de arrumar.',
    'no_painel': False,
    'telas': [],
}

# Telas que sao pedaco de outra, nao destino: nao entram no sumario nem contam
# como orfas. Sao POST-only, download interno, JSON, ou endereco antigo.
_NAO_SAO_TELAS = {
    'static', 'admin.etiquetas_folha', 'ranking.current_sponsor_logo',
    'config.get_pix_info', 'favicon',
    # O painel e de onde se PARTE, nao um destino do sumario. Ele aparecendo
    # numa categoria so daria uma volta em si mesmo. O sumario, idem: card
    # dele dentro dele mesmo e espelho de frente para espelho.
    'admin.dashboard', 'admin.indice_telas',
    # Nao sao telas: mandam para outra e pronto. `/` decide para onde levar
    # conforme quem esta logado; `/automayk` e o atalho curto do dominio da
    # automacao. Catalogar redirecionamento so faria o card levar a lugar
    # nenhum — ou de volta para onde a pessoa ja estava.
    'index', 'automayk',
    # `/login` so desvia para `/auth/login` — e o que as pessoas digitam
    # (19/09/2026: aparecia em vermelho no sumario como "sem tela").
    'login_atalho',
    # Formularios que vivem DENTRO de outra tela (20/09/2026, P-103).
    'auth.trocar_email', 'auth.esqueci_senha', 'auth.confirmar_email',
    # A tela de cada assunto e o proprio sumario em forma de card: catalogar
    # ela dentro do catalogo seria um espelho de frente para outro.
    'admin.area',
    # Dependem da chave pessoal de quem se inscreveu: nao ha card para elas.
    'adesao.pronto', 'adesao.meus_dados',
    # POST-only ou download: nao sao destino de card.
    'caixa.lancar', 'caixa.apagar', 'caixa.conciliar', 'caixa.comprovante',
    'caixa.exportar', 'caixa.editar', 'caixa.planilha',
    # POST-only, download ou documento que se chega pela tela de relatorios.
    'execucao.nova_acao', 'execucao.novo_acompanhamento',
    'execucao.atualizar_acompanhamento', 'execucao.nova_midia',
    'execucao.liberar_midia', 'execucao.apagar_midia',
    'execucao.material_da_acao', 'execucao.arquivo_da_midia',
    'relatorios.beneficiarios', 'relatorios.resultados',
    'relatorios.fotografico', 'relatorios.despesas',
    # Endereco que a TELA da sessao do WhatsApp consulta de poucos em poucos
    # segundos, para mostrar o pareamento ao vivo. Nao e' destino de card --
    # estava caindo em "sem categoria" e aparecendo em vermelho no sumario.
    'admin.whatsapp_andamento',
}

# O blueprint `api` inteiro fica de fora: ele existe para a automacao, nao
# para gente clicar. Sem esta regra, 20 enderecos de automacao apareceriam
# marcados em vermelho como "orfaos" — e estariam certos e inuteis ao mesmo
# tempo, porque link para eles no painel nao faz sentido nenhum. Alarme falso
# em sumario ensina a ignorar o vermelho, que e justamente o que ele serve
# para chamar atencao.
_BLUEPRINTS_DE_MAQUINA = ('api.',)

# O menu de cima vale como caminho, mas nao e "link DESTA tela": se contasse,
# toda pagina apareceria levando a todas as outras e o nivel 3 viraria ruido.
# `area.html` entra na mesma conta: ela aponta para tudo por construcao, entao
# contar os links dela no nivel 3 encheria a tela de repeticao.
_TEMPLATES_DE_MOLDURA = {'base.html', 'admin/area.html'}

_RENDER_RE = re.compile(r"render_template\(\s*['\"]([^'\"]+)['\"]")
_URLFOR_RE = re.compile(r"url_for\(\s*['\"]([A-Za-z_][\w]*\.[\w]+|[A-Za-z_][\w]*)['\"]")

# Migracao de dominio (07/09/2026): domínio velho escrito à mão num template
# vira link errado para sempre — nao se autocorrige como `app/enderecos.py`.
# Mesmo mecanismo do detector de orfã, mesmo vermelho: nao criar um jeito novo
# de mostrar problema quando o que ja existe serve.
_DOMINIO_VELHO_RE = re.compile(r'dominio-antigo\.exemplo\.com')


def _tem_dominio_velho(pasta, templates):
    """Algum template DESTA tela ainda escreve o domínio antigo à mão?"""
    for nome in templates:
        caminho = os.path.join(pasta, nome.replace('/', os.sep))
        try:
            with open(caminho, encoding='utf-8') as fh:
                if _DOMINIO_VELHO_RE.search(fh.read()):
                    return True
        except OSError:
            continue
    return False

_cache = {}


def _pasta_templates(app):
    return os.path.join(app.root_path, app.template_folder or 'templates')


def _templates_da_rota(func):
    """Que template(s) esta view desenha. Vazio quando nao da para saber."""
    try:
        return _RENDER_RE.findall(inspect.getsource(func))
    except (OSError, TypeError):
        return []


def _links_do_template(pasta, nome):
    """Endpoints que ESTE template aponta com url_for."""
    caminho = os.path.join(pasta, nome.replace('/', os.sep))
    try:
        with open(caminho, encoding='utf-8') as fh:
            return set(_URLFOR_RE.findall(fh.read()))
    except OSError:
        return set()


def _todos_os_links(pasta):
    """{endpoint} apontado por QUALQUER template — inclusive o menu.

    E isto que decide orfandade: se nenhum template do site chama `url_for`
    para a tela, nao existe caminho ate ela sem digitar o endereco na mao.
    """
    achados = set()
    for raiz, _dirs, arquivos in os.walk(pasta):
        for arquivo in arquivos:
            if not arquivo.endswith('.html'):
                continue
            caminho = os.path.join(raiz, arquivo)
            try:
                with open(caminho, encoding='utf-8') as fh:
                    achados |= set(_URLFOR_RE.findall(fh.read()))
            except OSError:
                continue
    return achados


def _titulo(func, endpoint):
    """A primeira linha do docstring da view. E onde o "o que isto faz" ja mora."""
    doc = (inspect.getdoc(func) or '').strip()
    if doc:
        return doc.split('\n')[0].strip()
    return endpoint.split('.')[-1].replace('_', ' ').capitalize()


def _abrivel(regra):
    """Da para clicar e abrir? Precisa aceitar GET e nao pedir parametro."""
    return 'GET' in (regra.methods or set()) and not regra.arguments


def _catalogo():
    """endpoint -> (nome curto, icone, nivel), como escrito la em cima."""
    fichas = {}
    for cat in CATEGORIAS:
        for endpoint, nome, icone, nivel in cat['telas']:
            fichas[endpoint] = (nome, icone, nivel)
    return fichas


def construir(app, recarregar=False):
    """O mapa inteiro, em tres níveis. Nunca levanta excecao.

    Devolve uma lista de categorias; cada uma com suas telas; cada tela com os
    links internos dela. `orfa=True` marca a tela sem nenhum caminho ate ela.
    """
    if _cache.get('arvore') is not None and not recarregar:
        return _cache['arvore']

    pasta = _pasta_templates(app)
    try:
        referenciados = _todos_os_links(pasta)
    except Exception as exc:
        logger.warning('Indice: nao consegui ler os templates: %s', exc)
        referenciados = set()

    fichas = _catalogo()

    # endpoint -> dados basicos
    telas = {}
    for regra in app.url_map.iter_rules():
        endpoint = regra.endpoint
        if endpoint in _NAO_SAO_TELAS or not _abrivel(regra):
            continue
        if endpoint.startswith(_BLUEPRINTS_DE_MAQUINA):
            continue
        if endpoint in telas:          # rota com dois enderecos: fica o 1o
            continue
        func = app.view_functions.get(endpoint)
        if func is None:
            continue
        nome, icone, nivel = fichas.get(endpoint, (None, 'fa-file', None))
        # ESTAR NO CATALOGO JA E TER CAMINHO.
        # `area.html` desenha um card para cada tela catalogada, mas monta o
        # endereco com `url_for(tela.endpoint)` — variavel, nao texto fixo. A
        # varredura so enxerga `url_for('nome-literal')`, entao ela nao ve
        # esses links e acusaria de escondida justamente a tela que ganhou
        # card. Aqui o catalogo conta como caminho porque **e** um caminho:
        # a pessoa clica no assunto e a tela esta la.
        telas[endpoint] = {
            'endpoint': endpoint,
            'url': str(regra.rule),
            'titulo': _titulo(func, endpoint),
            'nome': nome or endpoint.split('.')[-1].replace('_', ' ').capitalize(),
            'icone': icone,
            'nivel': nivel,
            'orfa': endpoint not in referenciados and endpoint not in fichas,
            'filhos': [],
            '_templates': [t for t in _templates_da_rota(func)
                           if t not in _TEMPLATES_DE_MOLDURA],
        }
        telas[endpoint]['dominio_velho'] = _tem_dominio_velho(
            pasta, telas[endpoint]['_templates'])

    # Nivel 3: para onde cada tela leva. So o template DELA, sem a moldura.
    for dados in telas.values():
        vistos, filhos = set(), []
        for template in dados['_templates']:
            for alvo in sorted(_links_do_template(pasta, template)):
                if alvo == dados['endpoint'] or alvo in vistos:
                    continue
                destino = telas.get(alvo)
                if destino is None:
                    continue
                vistos.add(alvo)
                filhos.append({'endpoint': alvo, 'url': destino['url'],
                               'titulo': destino['titulo'],
                               'nome': destino['nome']})
        dados['filhos'] = filhos
        dados.pop('_templates', None)

    # Nivel 1 e 2.
    colocados, arvore = set(), []
    for cat in CATEGORIAS:
        endpoints = [e for e, _n, _i, _v in cat['telas']]
        itens = [telas[e] for e in endpoints if e in telas]
        colocados.update(e for e in endpoints if e in telas)
        if itens:
            copia = dict(cat)
            copia['telas'] = itens
            copia['acoes'] = ACOES.get(cat['chave'], [])
            copia['orfas'] = sum(1 for i in itens if i['orfa'])
            copia['dominios_velhos'] = sum(1 for i in itens if i['dominio_velho'])
            arvore.append(copia)

    sobraram = [telas[e] for e in sorted(telas) if e not in colocados]
    if sobraram:
        copia = dict(_SEM_CATEGORIA)
        copia['telas'] = sobraram
        copia['acoes'] = []
        copia['orfas'] = sum(1 for i in sobraram if i['orfa'])
        copia['dominios_velhos'] = sum(1 for i in sobraram if i['dominio_velho'])
        arvore.append(copia)

    _cache['arvore'] = arvore
    return arvore


def categoria(app, chave):
    """Uma categoria só, com as telas dela — para `/admin/area/<chave>`."""
    for cat in construir(app):
        if cat['chave'] == chave:
            return cat
    return None


def painel(app):
    """As categorias que viram card no painel, na ordem escrita aqui."""
    try:
        return [c for c in construir(app) if c.get('no_painel')]
    except Exception as exc:
        logger.warning('Indice: nao consegui montar o painel: %s', exc)
        return []


def resumo(app):
    """(quantas telas, quantas órfãs) — para o card do painel."""
    try:
        arvore = construir(app)
    except Exception as exc:
        logger.warning('Indice: nao consegui montar o resumo: %s', exc)
        return 0, 0
    telas = sum(len(c['telas']) for c in arvore)
    return telas, sum(c['orfas'] for c in arvore)
