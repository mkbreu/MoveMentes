# -*- coding: utf-8 -*-
"""Sessao do WhatsApp por cidade — nome, fila de pareamento e estado.

POR QUE ESTE MODULO EXISTE

1. NOME DA SESSAO. Cada cidade e um aparelho vinculado independente dentro do
   WAHA, com banco proprio (waha_webjs_<sessao>). Se todas usarem "default",
   duas cidades disputam a mesma sessao e o WhatsApp derruba o vinculo. O
   nome certo e o slug da cidade. Instalacoes antigas ja rodam com "default":
   o nome fica no cofre e a troca e uma decisao explicita do operador, feita
   na tela — trocar sozinho derrubaria a sessao que hoje funciona.

2. FILA DE PAREAMENTO. O servidor da aplicacao fica sempre ligado, mas a
   maquina que roda o WAHA passa o dia desligada. Quando o admin de uma
   cidade pede para vincular o numero dele, o pedido entra na fila; quando a
   maquina sobe, a automacao consome a fila, cria a sessao, pega o codigo de
   pareamento e manda pelo WhatsApp do responsavel. E isto que torna o
   sistema descentralizado: o admin de outra cidade nunca precisa falar com
   o dono do servidor.

3. FRONTEIRA DE CIDADE. Nenhuma funcao aqui aceita "sessao" vinda do
   navegador. A sessao SEMPRE sai da cidade do usuario logado — senao trocar
   um nome na URL daria acesso a conversa da cidade vizinha.
"""
import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timedelta

from sqlalchemy import text as sa_text

from app import db, secretstore
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_SCHEMA_PRONTO = False

# Situacoes de um pedido de vinculacao.
PENDENTE = 'pendente'
EM_ANDAMENTO = 'em_andamento'
ATENDIDO = 'atendido'
FALHOU = 'falhou'
CANCELADO = 'cancelado'


def ensure_schema():
    global _SCHEMA_PRONTO
    if _SCHEMA_PRONTO:
        return
    with db.engine.begin() as conn:
        conn.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS sessoes_whatsapp (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                sessao VARCHAR(80) NOT NULL,
                telefone VARCHAR(24),
                situacao VARCHAR(20) NOT NULL DEFAULT 'pendente',
                pedido_por VARCHAR(120),
                pedido_em DATETIME NOT NULL,
                atendido_em DATETIME,
                codigo VARCHAR(16),
                codigo_em DATETIME,
                mensagem VARCHAR(300),
                recriar INTEGER NOT NULL DEFAULT 0
            )
        """))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_sessoes_whatsapp_situacao "
            "ON sessoes_whatsapp (situacao, pedido_em)"))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_sessoes_whatsapp_cidade "
            "ON sessoes_whatsapp (cidade)"))

        # Colunas do PROVISIONAMENTO, acrescentadas depois. ALTER TABLE em vez
        # de recriar: a tabela ja existe em producao com pedidos dentro.
        # O SQLite nao tem "ADD COLUMN IF NOT EXISTS", entao a gente pergunta.
        existentes = {linha[1] for linha in
                      conn.execute(sa_text("PRAGMA table_info(sessoes_whatsapp)"))}
        novas = {
            # Dono da sessao. Cada admin tem o proprio WhatsApp: a sessao
            # carrega cidade E pessoa (ver publicador._sessao_do_usuario).
            'usuario_id': 'INTEGER',
            # principal | suplente | reserva — vem da ordem_envio do usuario.
            'papel': 'VARCHAR(20)',
            # Porta da instancia WAHA desta sessao. Uma instancia por admin: a
            # chave de uma nao abre a outra, e o painel de uma nao lista a
            # sessao da outra. 0 = ainda nao alocada.
            'porta': 'INTEGER',
            # Nome do container e do worker, para o agente saber o que subir.
            'instancia': 'VARCHAR(60)',
            # Em que passo do provisionamento esta, para a tela contar ao vivo.
            'etapa': 'VARCHAR(40)',
            'etapa_em': 'DATETIME',
            # E-mail que recebe o codigo. Cai para o do usuario que pediu.
            'email': 'VARCHAR(160)',
        }
        for coluna, tipo in novas.items():
            if coluna not in existentes:
                conn.execute(sa_text(
                    'ALTER TABLE sessoes_whatsapp ADD COLUMN %s %s' % (coluna, tipo)))

        # O QUE A MAQUINA TEM DE FATO — uma linha por instancia WAHA.
        #
        # POR QUE PRECISOU EXISTIR (14/08/2026)
        # O cofre guarda UM endereco e UMA chave por CIDADE, e a cidade tem
        # DUAS instancias com chaves diferentes (principal na 3000, suplente na
        # 3001). Com um campo so, o site acertava no maximo uma delas — e nesse
        # dia estava com a chave errada: o WAHA respondia HTTP 401, o site
        # concluia "automacao quebrada" e mandava e-mail de 15 em 15 minutos
        # sobre um WhatsApp que estava CONNECTED, publicando normalmente.
        #
        # O estrago nao parava no e-mail: sem conseguir listar as sessoes, o
        # site nao descobria que a sessao real se chama `principal-pls`, caia no
        # slug e o botao Conectar pedia `principal-palmeiropolis` na porta 3001
        # (que e do suplente). Era a P-64 inteira, nascida de um 401.
        #
        # Quem preenche isto e a propria maquina, por POST /api/instancias —
        # nao ha campo para ninguem digitar errado.
        conn.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS instancias_waha (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                papel VARCHAR(20),
                container VARCHAR(80) NOT NULL,
                sessao VARCHAR(80),
                host VARCHAR(120),
                porta INTEGER,
                chave VARCHAR(200),
                status VARCHAR(30),
                motor VARCHAR(30),
                numero VARCHAR(30),
                alcancavel INTEGER NOT NULL DEFAULT 1,
                visto_em DATETIME
            )
        """))
        # A CHAVE PASSOU A SER A SESSAO, NAO O CONTAINER (08/09/2026).
        #
        # O indice antigo era `(cidade, container)`, e isso embutia a regra
        # velha "um container por NUMERO": dois papeis nunca dividiam
        # container, entao o container servia de identidade. Com a fusao
        # (um container por CIDADE, uma sessao por numero dentro dele) essa
        # premissa cai -- `principal-pls` e `suplente-pls` passam a viver no
        # MESMO `auto-waha-principal-pls`, e o indice antigo recusaria a
        # segunda linha.
        #
        # A identidade certa sempre foi a SESSAO: e ela que carrega o numero,
        # e o nome dela entra ate no nome do banco no Postgres
        # (`waha_{namespace}_{sessao}`). Container e' onde ela mora hoje, e
        # onde ela mora pode mudar.
        #
        # O indice velho e' derrubado de proposito: deixa-lo vivo manteria a
        # recusa, e `CREATE ... IF NOT EXISTS` nao troca a definicao de um
        # indice que ja existe.
        conn.execute(sa_text("DROP INDEX IF EXISTS idx_instancias_waha_cont"))
        conn.execute(sa_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_instancias_waha_sessao "
            "ON instancias_waha (cidade, sessao)"))

        # O RASTRO DE CADA PEDIDO — uma linha por passo (P-75).
        #
        # `sessoes_whatsapp.etapa` e UMA COLUNA SOBRESCRITA: guarda o passo de
        # agora e perde o anterior. No log de 14/08 o passo `subindo` durou UM
        # SEGUNDO (14:22:39 -> 14:22:40); uma tela que pergunta de 3 em 3
        # segundos nunca o veria, e quem abrisse depois so encontraria
        # "falhou", sem saber ate onde tinha ido.
        conn.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS sessoes_whatsapp_passos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id INTEGER NOT NULL,
                etapa VARCHAR(40),
                mensagem VARCHAR(300),
                em DATETIME NOT NULL
            )
        """))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_passos_pedido "
            "ON sessoes_whatsapp_passos (pedido_id, id)"))
    _SCHEMA_PRONTO = True


# ===================== ETAPAS DO PROVISIONAMENTO =====================
# A tela mostra estas etapas em ordem. Cada uma tem um limite de tempo proprio:
# ligar a maquina e lento, criar container e medio, pedir codigo e rapido.
#
# O PRAZO DE "LIGANDO" ERA 7 MINUTOS E ACUSAVA EM FALSO (16/08/2026). Ligar do
# zero e Wake-on-LAN + POST do Windows + WSL + Docker + WAHA saudavel: de 3 a 6
# minutos medidos, e mais um tanto quando a maquina esta desligada de verdade
# (S5) em vez de dormindo. A tela avisava "passou do tempo" no meio de um
# pareamento que estava indo bem — e aviso que erra ensina a ignorar aviso
# (regra 21-J). 20 minutos e o mesmo prazo que o site usa para insistir.
ETAPAS = [
    ('ligando',      'Ligando o computador da automação',       1200),
    ('subindo',      'Subindo a instância do WhatsApp',          300),
    ('criando',      'Criando a sessão',                         180),
    ('codigo',       'Pedindo o código de pareamento',           120),
    ('aguardando',   'Esperando você digitar o código',          600),
    ('conectado',    'Conectado',                                  0),
]
ETAPA_TITULO = {chave: titulo for chave, titulo, _ in ETAPAS}
ETAPA_LIMITE = {chave: limite for chave, _, limite in ETAPAS}

# Faixa de portas das instancias WAHA: a MENOR LIVRE A PARTIR DE 3000, em ordem
# crescente.
#
# ANTES COMECAVA EM 3001 e isso custou caro (11/08/2026). A 3000 era tratada
# como "instalacao antiga, fora da alocacao" — mas a 3000 e onde mora o
# PRINCIPAL, por ter sido o primeiro a ser criado. Comecando em 3001, o pedido
# do principal recebia a porta do SUPLENTE: a tela mostrava o numero
# DD9XXXXXXXX (principal) em "auto-waha-3001", que na vida real e a instancia
# do DD9XXXXXXXX. E o provisionamento subia um container novo em cima da porta
# ocupada — foi assim que nasceu o `auto-waha-3001` orfao, que ficou
# reiniciando 854 vezes em dois dias.
#
# Regra do dono (11/08): a 3000 nao e privilegio de ninguem. Se quem esta la
# cair e outra instancia ocupar a vaga, a proxima procura 3001, 3002...
PORTA_INICIAL = 3000
PORTA_FINAL = 3099


def proxima_porta_livre(cidade=None):
    """Menor porta livre a partir de 3000. Reaproveita buraco deixado por exclusao.

    Se a 3001 e a 3003 estao ocupadas e a 3002 foi liberada, devolve 3002 —
    e o que o operador pediu: nao ir empurrando o numero para cima a cada
    cidade que entra e sai.

    "LIVRE" PRECISA SIGNIFICAR LIVRE DE VERDADE (11/08/2026)
    O banco sozinho nao sabe: um pedido que terminou em `falhou` some da conta,
    e a porta dele continua ocupada por um container de pe. Foi assim que o
    `auto-waha-3001` orfao nasceu em cima da porta do suplente e ficou
    reiniciando 854 vezes. Agora, alem do banco, a porta e TESTADA na maquina
    da automacao: se alguem responde ali, nao esta livre.

    O teste so roda quando a cidade tem endereco cadastrado — sem endereco nao
    ha onde perguntar, e ai vale so o banco (comportamento antigo).
    """
    ensure_schema()
    usadas = set()
    for (p,) in db.session.execute(sa_text(
            "SELECT porta FROM sessoes_whatsapp "
            "WHERE porta IS NOT NULL AND porta > 0 "
            "AND situacao NOT IN (:c, :f)"),
            {'c': CANCELADO, 'f': FALHOU}).fetchall():
        usadas.add(int(p))

    host = None
    try:
        from app import maquina
        host, _ = maquina.endereco(cidade)
    except Exception as exc:
        logger.debug('proxima_porta_livre: sem host para testar (%s)', exc)

    # SEM ENDERECO PARA TESTAR, NAO SE INVENTA PORTA (14/08/2026).
    #
    # O teste de porta e o que separa "livre" de "eu nao alcanco". Sem ele
    # sobra so o banco — e o banco nao sabe de container que ficou de pe depois
    # de um pedido marcado como `falhou`. Foi assim que a 3001, que e do
    # suplente, foi dada como livre: ela nao tinha ponte de rede, ninguem
    # respondia de fora, e o site concluiu que estava vaga. O `docker run`
    # falhou em "programming external connectivity" e deixou um container
    # quebrado reiniciando 72 vezes.
    #
    # Melhor recusar e mandar conferir do que chutar um numero: chutar aqui
    # custa um container zumbi e uma tarde de diagnostico.
    if not host:
        logger.warning('proxima_porta_livre: sem endereco da maquina para '
                       'testar as portas — nao vou adivinhar.')
        return None

    from app import maquina
    for porta in range(PORTA_INICIAL, PORTA_FINAL + 1):
        if porta in usadas:
            continue
        try:
            # PERGUNTA SE E' O WAHA, nao se "alguem aceita conexao"
            # (09/09/2026). O atalho de rede que o expor_waha_na_rede.ps1
            # cria responde ao TCP mesmo sem container atras: a 3001 estava
            # LIVRE e foi dada como ocupada, e o suplente nasceu na 3002 --
            # um segundo WAHA da mesma cidade, contra a regra de um container
            # por cidade. `porta_tem_waha` so' diz "ocupada" quando quem
            # atende fala a lingua do WAHA.
            ocupada = maquina.porta_tem_waha(host, porta)
            if ocupada is None:
                # "Nao sei" continua valendo como ocupada.
                logger.debug('proxima_porta_livre: nao consegui saber se a %s '
                             'tem WAHA — tratando como ocupada.', porta)
                continue
        except Exception as exc:
            logger.debug('proxima_porta_livre: teste da %s falhou (%s)',
                         porta, exc)
            # NAO SEI NAO E LIVRE. Seguir em frente aqui e exatamente o erro
            # que produziu o zumbi da 3001.
            continue
        if ocupada:
            logger.info('Porta %s ja responde em %s — pulando.', porta, host)
            continue
        return porta
    return None


# ===================== NOME DA SESSAO =====================

def slug_cidade(cidade):
    """Slug da cidade, sem acento — mesmo criterio das rotas publicas."""
    texto = unicodedata.normalize('NFKD', (cidade or '').strip())
    texto = texto.encode('ascii', 'ignore').decode('ascii')
    base = re.sub(r'[^A-Za-z0-9]+', '-', texto).strip('-').lower()
    return base or 'cidade'


def token_qr(pedido_id):
    """Token curto e sem segredo dentro, para o link do QR no e-mail.

    O QR precisa abrir SEM login: quem recebe está no celular, no meio de um
    pareamento que já falhou uma vez — exigir senha ali é perder a pessoa. Mas
    o QR **é** a credencial de acesso ao WhatsApp, então o link não pode ser
    adivinhável nem eterno.

    Assinado com a `SECRET_KEY` e amarrado ao pedido: só vale para aquele
    pedido, e o pedido morre quando a sessão conecta ou quando alguém aperta
    Parar. Sem banco novo, sem token guardado, sem nada para expirar sozinho.
    """
    import hashlib
    from flask import current_app
    chave = (current_app.config.get('SECRET_KEY') or '')
    if isinstance(chave, str):
        chave = chave.encode('utf-8')
    bruto = b'qr:%d:' % int(pedido_id) + chave
    return hashlib.sha256(bruto).hexdigest()[:24]


def cidade_curta(cidade):
    """Como a cidade aparece DENTRO do nome da sessão (`<papel>-<cidade>`).

    Palmeirópolis usa **`pls`** — abreviação herdada de quando o projeto tinha
    uma cidade só. Cidade nova entra com o **slug**, que é o padrão.

    POR QUE PRECISOU (P-64, 13/08/2026)
    O site montava sempre pelo slug (`suplente-palmeiropolis`) enquanto a
    realidade no Docker e no `.env` era `suplente-pls`. Quem clicava em
    "Conectar" criava uma instância com nome que ninguém mais reconhecia — e o
    site continuava sem achar a sessão que já existia.
    """
    guardado = (secretstore.get(secretstore.WAHA_CIDADE_CURTA, scope=cidade) or '').strip()
    if guardado:
        return re.sub(r'[^a-z0-9-]+', '', guardado.lower())

    # SEM REGISTRO, PERGUNTE AO WAHA — NÃO INVENTE (14/08/2026).
    #
    # O campo "forma curta" existe desde 13/08 e continuava VAZIO, então isto
    # caía no slug e produzia `principal-palmeiropolis` enquanto o Docker, o
    # `.env` e o compose diziam `principal-pls` (regra 16-0-A). O site era a
    # única metade do sistema fora do padrão, e o preço foi o "Conectar"
    # tentando criar instância nova em cima de uma que já existia.
    #
    # Exigir que alguém preencha um campo para o sistema parar de errar é
    # empurrar para o admin um trabalho que a máquina faz sozinha: a resposta
    # está no próprio WAHA da cidade.
    # PRIMEIRO O QUE A MAQUINA JA CONTOU — nao custa rede.
    #
    # Isto também é o que segura a tela do pareamento: ela pergunta o andamento
    # de 3 em 3 segundos, e cada pergunta passa por aqui uma vez POR PESSOA. Sem
    # esta parada, uma cidade sem a forma curta no cofre viraria uma rajada de
    # chamadas ao WAHA — e sonda demais foi o que derrubou o Chromium em 06/08.
    for item in instancias(cidade):
        achado = _curta_do_nome(item.get('sessao'))
        if achado:
            try:
                secretstore.set_value(secretstore.WAHA_CIDADE_CURTA, achado,
                                      scope=cidade, updated_by='relato-da-maquina')
            except Exception as exc:
                logger.debug('Não consegui guardar a forma curta: %s', exc)
            return achado

    achado = _curta_da_sessao_existente(cidade)
    if achado:
        try:
            secretstore.set_value(secretstore.WAHA_CIDADE_CURTA, achado,
                                  scope=cidade, updated_by='descoberto-no-waha')
        except Exception as exc:
            logger.debug('Não consegui guardar a forma curta: %s', exc)
        return achado
    return slug_cidade(cidade)


def _curta_da_sessao_existente(cidade):
    """`pls`, a partir do nome real da sessão (`Principal-Pls`). None se não der.

    Tira o prefixo do papel e devolve o resto em minúsculas — é exatamente a
    metade "cidade" do padrão `<papel>-<cidade>` da regra 16-0-A.
    """
    nome = _sessao_ja_existente(cidade)
    if not nome:
        return None
    curta = _curta_do_nome(nome)
    if curta:
        return curta
    # Nome fora do padrão (`reserva2-x`, ou sem papel): não dá para separar com
    # segurança, e chutar aqui renomearia a cidade inteira.
    resto = re.sub(r'^reserva\d*-', '', str(nome).strip().lower())
    if resto == str(nome).strip().lower():
        return None
    return re.sub(r'[^a-z0-9-]+', '', resto) or None


def mesmo_nome(a, b):
    """Dois nomes de sessao apontam para a MESMA sessao?

    Nome de sessao NAO distingue maiuscula de minuscula (regra 16-0):
    `Principal-PLS`, `principal-pls` e `PRINCIPAL-pls` sao a mesma coisa. Quem
    digita e gente, e detalhe de digitacao nao pode derrubar ranking.

    POR QUE ISTO PRECISOU EXISTIR NO SITE (11/08/2026)
    A automacao ja comparava assim (`wahasessao.mesmo_nome`); o site comparava
    cru. Em 11/08, ao reparear, o dono digitou `Principal-Pls` no lugar de
    `Principal-pls` — uma letra. A partir dali `GET /api/sessions/principal-pls`
    passou a devolver **404** (a API do WAHA distingue caixa no caminho), e as
    duas metades do sistema deixaram de concordar sobre qual sessao existe.

    ➜ Isto NAO substitui a regra de ouro do DICIONARIO: para ACHAR a sessao,
    pergunte `GET /api/sessions` e use a que voltar. Esta funcao serve para
    COMPARAR o que voltou com o que esta guardado — nunca para procurar.
    """
    return (a or '').strip().lower() == (b or '').strip().lower()


def nome_sessao(cidade):
    """Nome da sessao desta cidade no WAHA.

    Ordem: o que estiver no cofre (escolha explicita do operador), senao o
    slug da cidade. NUNCA aceita valor vindo do navegador.
    """
    guardado = (secretstore.get(secretstore.WAHA_SESSION, scope=cidade) or '').strip()
    # `default` NÃO É NOME, É RESQUÍCIO (14/08/2026). Era o valor da primeira
    # instalação, antes de existir `<papel>-<cidade>` (regra 16-0-A). Ficou
    # gravado no cofre e, por ter valor, vencia a descoberta automática: o site
    # procurava uma sessão chamada `default`, que não existe em lugar nenhum, e
    # concluía "não há sessão" — daí tentar criar instância nova em cima de uma
    # que já estava lá. Tratar como vazio é o que junta as duas metades.
    if guardado and guardado.lower() not in ('default', 'undefined', 'none'):
        return guardado
    # Sem registro utilizável, a verdade está no WAHA. Montar `<papel>-<slug>`
    # aqui foi o que criou o segundo vocabulário.
    achado = _sessao_ja_existente(cidade)
    if achado:
        try:
            secretstore.set_value(secretstore.WAHA_SESSION, achado, scope=cidade,
                                  updated_by='descoberto-no-waha')
        except Exception as exc:
            logger.debug('Não consegui guardar o nome descoberto: %s', exc)
        return achado
    return slug_cidade(cidade)


def preservar_sessao_existente():
    """Migração única: cidade que JÁ tinha WAHA continua na sessão 'default'.

    Sem isto, ligar o nome-por-slug trocaria a sessao debaixo de uma cidade que
    hoje funciona: a tela passaria a perguntar por 'palmeiropolis', que nao
    existe no WAHA, e o admin veria erro sem ter mexido em nada. Cidade NOVA
    ja nasce com o slug; a troca da cidade antiga e um botao na tela, quando o
    operador puder parear de novo.
    """
    from app.models import Config
    ajustadas = []
    try:
        cidades = [c.cidade for c in Config.query.all()]
    except Exception as exc:
        logger.debug('preservar_sessao_existente: %s', exc)
        return ajustadas
    for cidade in cidades:
        if (secretstore.get(secretstore.WAHA_SESSION, scope=cidade) or '').strip():
            continue
        if not (secretstore.get(secretstore.WAHA_URL, scope=cidade) or '').strip():
            continue  # cidade sem WAHA configurado: nasce com o slug
        secretstore.set_value(secretstore.WAHA_SESSION, 'default',
                              scope=cidade, updated_by='migracao')
        ajustadas.append(cidade)
    if ajustadas:
        logger.warning('Sessao do WhatsApp mantida como "default" em: %s '
                       '(troque pelo slug na tela quando puder parear de novo)',
                       ', '.join(ajustadas))
    return ajustadas


def numero_responsavel(cidade=None):
    """Numero que recebe os codigos de pareamento e os alertas.

    Cai para o global quando a cidade nao tem um proprio: e o numero do dono
    da instalacao que socorre quem ainda nao vinculou nada.
    """
    proprio = (secretstore.get(secretstore.WAHA_NUMERO_MASTER, scope=cidade) or '').strip()
    if proprio:
        return proprio
    return (secretstore.get(secretstore.WAHA_NUMERO_MASTER) or '').strip() or None


def _sessao_ja_existente(cidade):
    """O nome da sessão que a instância desta cidade JÁ tem. None se não houver.

    Pergunta ao WAHA no endereço que a cidade tem cadastrado — o mesmo que o
    site usa para publicar. Se ele responde, existe instância: não se cria
    outra, adota-se a que está lá.

    Devolve o nome na CAIXA EXATA que o servidor usou: o caminho HTTP do WAHA
    distingue maiúscula de minúscula (`/api/sessions/principal-pls` devolve 404
    para `Principal-Pls`), então guardar a forma errada quebra tudo depois.
    """
    import requests
    base = (secretstore.get(secretstore.WAHA_URL, scope=cidade) or '').strip().rstrip('/')
    if not base:
        return None
    chave = secretstore.get(secretstore.WAHA_API_KEY, scope=cidade) or ''
    cab = {'X-Api-Key': chave} if chave else {}
    try:
        resp = requests.get('%s/api/sessions' % base, params={'all': 'true'},
                            headers=cab, timeout=8)
        if resp.status_code != 200:
            return None
        sessoes = resp.json() or []
    except Exception as exc:
        logger.debug('Não consegui perguntar as sessões de %s: %s', cidade, exc)
        return None
    # Preferir a que está de pé; na falta, qualquer uma serve — o que importa é
    # não inventar um nome novo. `FAILED` é justamente o caso em que se pareia.
    nomes = [s.get('name') for s in sessoes
             if isinstance(s, dict) and s.get('name')]
    if not nomes:
        return None
    for s in sessoes:
        if isinstance(s, dict) and s.get('status') == 'WORKING' and s.get('name'):
            return s['name']
    return nomes[0]


# ===================== FILA DE PAREAMENTO =====================

def pedir_vinculacao(cidade, telefone, pedido_por=None, recriar=False,
                     usuario=None, email=None):
    """Coloca um pedido de vinculacao na fila. Devolve (ok, mensagem).

    UM PEDIDO POR PESSOA, NAO POR CIDADE
    Cada admin tem o proprio WhatsApp (ver publicador._sessao_do_usuario), e a
    cidade pode ter principal E suplente ao mesmo tempo. Se a fila fosse por
    cidade, pedir o suplente cancelaria o pedido do principal.

    O papel (principal/suplente/reserva) sai da `ordem_envio` do usuario — a
    mesma que a tela "Quem publica no grupo" ja controla. Ninguem digita isso
    duas vezes.
    """
    ensure_schema()
    from app.blueprints.auth import normalizar_telefone_usuario

    numero = normalizar_telefone_usuario(telefone)
    if not numero:
        return False, ('Telefone inválido. Use DDD + número, '
                       'como (DD) 9XXXX-XXXX.')

    # PAREAMENTO ABERTO SEGURA OS CONTAINERS (18/09/2026, plano A4 / E6).
    # Medido em 14/09 (22:34, 23:38): o Guardião só olha `containers_ate` e
    # derrubou o WhatsApp no meio do pareamento. Mesma proteção do "Publicar
    # agora" (`rodada.pedir`): nunca encurta prazo nem mexe em "até mandarem
    # parar". 45 min cobrem a espera da Fila (até 15), a subida e o código.
    try:
        from app.blueprints.admin import _garantir_containers_de_pe
        _garantir_containers_de_pe(cidade, minutos_minimo=45,
                                   por=pedido_por or email or 'conectar')
    except Exception as exc:
        logger.warning('Pareamento: nao consegui proteger os containers por '
                       '45 min (%s) — o Guardiao pode derrubar o pareamento.', exc)

    if usuario is not None:
        from app import publicador
        sessao = publicador._sessao_do_usuario(usuario)
        usuario_id = usuario.id
        ordem = usuario.ordem_envio or 0
        papel = 'principal' if ordem == 1 else ('suplente' if ordem == 2 else 'reserva')
        email = (email or usuario.email or '').strip()
    else:
        # Caminho antigo: instalacao que ainda usa uma sessao por cidade.
        sessao = nome_sessao(cidade)
        usuario_id, papel = None, 'principal'
        email = (email or pedido_por or '').strip()

    # ANTES DE CRIAR QUALQUER COISA, VEJA O QUE A MÁQUINA JÁ TEM (P-64).
    #
    # O nome era CALCULADO (`<papel>-<slug da cidade>`) e dava
    # `principal-palmeiropolis`, enquanto a realidade no Docker e no `.env`
    # sempre foi `principal-pls`. O site não achava a sessão que existia,
    # concluía "não há nenhuma" e mandava CRIAR uma instância nova — que nasceu
    # sem rede, não achou o `auto-postgres` e ficou reiniciando: 72 vezes em 25
    # minutos, enquanto a tela dizia "ligando o computador da automação".
    #
    # Agora a fonte é o relato da própria máquina (`POST /api/instancias`), e
    # ele é POR PAPEL. A versão anterior perguntava `_sessao_ja_existente`, que
    # devolve UMA sessão da cidade preferindo a que está de pé: pedir o suplente
    # adotava a sessão do PRINCIPAL e ainda gravava isso no cofre.
    reaproveitada = instancia_do_papel(cidade, papel)
    porta = instancia = None
    if reaproveitada and reaproveitada.get('sessao'):
        if not mesmo_nome(reaproveitada['sessao'], sessao):
            logger.warning('Pareamento: a instância %s desta cidade usa a sessão '
                           '"%s"; o site ia pedir "%s". Fica a que existe.',
                           papel, reaproveitada['sessao'], sessao)
        sessao = reaproveitada['sessao']
        porta = reaproveitada['porta']
        instancia = reaproveitada['container']
    else:
        # A CIDADE JA TEM CASA? ENTAO A SESSAO NOVA MORA NELA (09/09/2026).
        #
        # Nao existe container "de suplente" no desenho atual: e' um WAHA por
        # cidade, com as sessoes dentro dele. Sem esta busca, pedir o suplente
        # nao achava instancia do papel, caia em `proxima_porta_livre()` e
        # mandava criar um SEGUNDO WAHA da mesma cidade — foi o container da
        # 3002 de 08/09, que precisou ser removido na mao.
        #
        # Aproveita so' o ENDERECO (container e porta). O nome da sessao
        # continua sendo o do papel de quem pediu: a linha achada e' quase
        # sempre a do principal, e herdar o nome dela faria o suplente gravar
        # a sessao do principal no cofre — defeito ja conhecido, corrigido em
        # 14/08, que nao pode voltar por esta porta.
        casa = container_da_cidade(cidade)
        if casa:
            porta = casa['porta']
            instancia = casa['container']
            logger.info('Pareamento: %s entra no container que a cidade já '
                        'tem (%s, porta %s) — sem criar outro WAHA.',
                        papel, instancia, porta)
        else:
            # O ENDERECO CADASTRADO VALE COMO RESPOSTA (09/09/2026).
            #
            # OVO E GALINHA, medido nesta madrugada: `registrar_instancias.py`
            # NAO relata container sem sessao ("container vazio so gasta
            # memoria"), e a identidade de uma linha de `instancias_waha` e' a
            # SESSAO. Ou seja, no exato momento em que se precisa parear — que
            # e' quando nao ha sessao — o site fica sem saber a porta e diz
            # "ainda nao sei em que porta esta cidade roda", como em 08/09.
            # So' nao travou de vez porque sobrou o registro de uma passada
            # anterior; apagar aquela linha teria fechado o ciclo.
            #
            # O cofre ja guarda `waha_base_url` da cidade (o mesmo endereco que
            # o site usa para publicar). Ele responde a pergunta sem depender
            # de haver sessao — e sem inventar numero, que e' o que a guarda
            # do zumbi da 3001 proibe.
            try:
                base = (secretstore.get(secretstore.WAHA_URL, scope=cidade) or '').strip()
                cadastrada = urlparse(base).port if base else None
            except Exception as exc:
                logger.debug('Pareamento: não consegui ler o endereço da '
                             'cidade no cofre (%s)', exc)
                cadastrada = None
            if cadastrada:
                porta = int(cadastrada)
                logger.info('Pareamento: sem instância relatada; usando a '
                            'porta %s do endereço cadastrado da cidade.', porta)

    agora = datetime.now()
    # Pedido em aberto DESTA PESSOA (ou desta cidade, no caminho antigo).
    if usuario_id:
        existente = db.session.execute(sa_text("""
            SELECT id, porta FROM sessoes_whatsapp
            WHERE usuario_id = :u AND situacao IN (:p, :e)
            ORDER BY pedido_em DESC LIMIT 1
        """), {'u': usuario_id, 'p': PENDENTE, 'e': EM_ANDAMENTO}).fetchone()
    else:
        existente = db.session.execute(sa_text("""
            SELECT id, porta FROM sessoes_whatsapp
            WHERE cidade = :c AND usuario_id IS NULL AND situacao IN (:p, :e)
            ORDER BY pedido_em DESC LIMIT 1
        """), {'c': cidade, 'p': PENDENTE, 'e': EM_ANDAMENTO}).fetchone()

    # A PORTA DA INSTANCIA QUE EXISTE VENCE TUDO. Quando a maquina ja relatou o
    # container deste papel, nao ha porta a escolher: e a dele, e o agente vai
    # encontrar o container de pe e seguir em frente sem criar nada.
    if not porta:
        # A porta ja concedida a esta sessao continua sendo dela: trocar de
        # porta a cada tentativa deixaria containers orfaos ocupando a faixa.
        if existente and existente[1]:
            porta = int(existente[1])
        else:
            # SO HERDA A PORTA DE UMA LINHA DA MESMA SESSAO (14/08/2026).
            # A comparacao era crua (`sessao = :s`) e, com o nome divergindo por
            # uma letra ou pelo slug, uma linha velha de OUTRA instancia doava a
            # porta: foi assim que o pedido do principal saiu com a 3001, que e
            # a do suplente.
            atual = None
            for linha in db.session.execute(sa_text(
                    "SELECT sessao, porta FROM sessoes_whatsapp "
                    "WHERE porta IS NOT NULL AND porta > 0 "
                    "ORDER BY pedido_em DESC")).fetchall():
                if mesmo_nome(linha[0], sessao):
                    atual = linha[1]
                    break
            porta = int(atual) if atual else proxima_porta_livre(cidade)
    if not porta:
        return False, ('Ainda não sei em que porta esta cidade roda o WhatsApp, '
                       'e não vou inventar um número — foi assim que nasceu um '
                       'container que reiniciou 854 vezes. Ligue o computador da '
                       'automação: assim que ele subir, ele conta ao site quais '
                       'instâncias existem e o botão passa a funcionar.')

    # O CONTAINER LEVA O NOME DA SESSAO, NAO O DA PORTA (11/08/2026).
    # `auto-waha-<porta>` criava um segundo vocabulario para a mesma coisa: a
    # realidade no Docker sempre foi `auto-waha-principal-pls` /
    # `auto-waha-suplente-pls`, e a tela dizia `auto-waha-3001`. Duas listas
    # para o mesmo objeto divergem sozinhas — foi a causa raiz de 02-03/08 em
    # outra roupa. Quem le o nome agora sabe o papel e a cidade.
    if not instancia:
        instancia = 'auto-waha-%s' % sessao.lower()

    if existente:
        pedido_id = existente[0]
        db.session.execute(sa_text("""
            UPDATE sessoes_whatsapp
            SET telefone = :t, sessao = :s, situacao = :sit, pedido_em = :em,
                pedido_por = :por, recriar = :rec, codigo = NULL, codigo_em = NULL,
                mensagem = NULL, usuario_id = :u, papel = :pa, porta = :pt,
                instancia = :ins, email = :mail, etapa = :et, etapa_em = :em
            WHERE id = :i
        """), {'t': numero, 's': sessao, 'sit': PENDENTE, 'em': agora,
               'por': pedido_por, 'rec': 1 if recriar else 0, 'i': existente[0],
               'u': usuario_id, 'pa': papel, 'pt': porta, 'ins': instancia,
               'mail': email, 'et': 'ligando'})
    else:
        resultado = db.session.execute(sa_text("""
            INSERT INTO sessoes_whatsapp
                (cidade, sessao, telefone, situacao, pedido_por, pedido_em,
                 recriar, usuario_id, papel, porta, instancia, email,
                 etapa, etapa_em)
            VALUES (:c, :s, :t, :sit, :por, :em, :rec, :u, :pa, :pt, :ins,
                    :mail, :et, :em)
        """), {'c': cidade, 's': sessao, 't': numero, 'sit': PENDENTE,
               'por': pedido_por, 'em': agora, 'rec': 1 if recriar else 0,
               'u': usuario_id, 'pa': papel, 'pt': porta, 'ins': instancia,
               'mail': email, 'et': 'ligando'})
        pedido_id = resultado.lastrowid
    _registrar_passo(pedido_id, 'ligando',
                     'pedido registrado por %s — sessão %s, porta %s%s'
                     % (pedido_por or 'alguém', sessao, porta,
                        ' (instância que já existe)' if reaproveitada else ''))
    db.session.commit()
    logger.info('Pedido de vinculacao na fila: %s / %s / sessao %s / porta %s / %s',
                cidade, papel, sessao, porta, numero)

    # A VERDADE HONESTA SOBRE O "QUANDO" (reescrita em 16/08/2026).
    #
    # O texto antigo dizia que o Wake-on-LAN so servia para maquina DORMINDO e
    # que, desligada de vez, o pedido esperaria a proxima rodada. Isso era
    # verdade enquanto o recurso estava desarmado na placa; hoje o WoL liga a
    # maquina desligada (regra 21-H) e o site fica insistindo ate ela responder.
    # Prometer menos do que se faz tambem e errar: quem lia aquilo ia ligar o
    # computador na mao sem precisar.
    if '@' in (email or ''):
        destino = ('O código chega no e-mail %s — o do cadastro de quem é dono '
                   'do número — e também pelo WhatsApp do outro aparelho da '
                   'cidade, quando ele estiver de pé.' % email)
    else:
        destino = ('ATENÇÃO: esta pessoa está sem e-mail no cadastro, então o '
                   'código só pode chegar pelo WhatsApp do outro aparelho da '
                   'cidade. Cadastre o e-mail dela em Usuários para não '
                   'depender disso.')
    return True, ('Pedido registrado e guardado — ele não se perde se a máquina '
                  'estiver desligada. Se o computador da automação estiver '
                  'desligado ou dormindo, o site manda o sinal que o liga e fica '
                  'insistindo por até 20 minutos, preparando a instância %s '
                  'assim que ele responder. %s Acompanhe o andamento nesta tela.'
                  % (instancia, destino))


# Quanto tempo uma sessao recem-pareada continua suspeita PARA COLETA.
#
# Aparelho novo nao recebe o historico completo. O whatsapp-web.js entrega as
# mensagens antigas aos poucos, e no caso que gerou a P-38 vieram 21 blocos de
# 39 mensagens com o link de uma atleta faltando. Somar km em cima disso perde link
# sem deixar rastro — o pior desfecho do sistema (regra 4-I).
#
# 24 h porque a entrega do historico e progressiva e NAO AVISA quando terminou:
# nao existe "ja sincronizou tudo" para perguntar ao WAHA (regra 16-B). Sem
# sinal de fim, o unico jeito honesto e uma janela generosa: a janela larga
# custa um aviso a mais, a curta custa km.
#
# Vale so para COLETA. Publicar funciona desde o primeiro minuto — e a regra
# "publicar != ler" de novo.
HORAS_PAREAMENTO_RECENTE = 24


def pareamento_recente(cidade, horas=HORAS_PAREAMENTO_RECENTE):
    """A cidade pareou o WhatsApp ha pouco? Sempre um dict, nunca excecao.

    `recente` ja vem decidido daqui: quem pergunta (a pipeline, pelo
    /api/health) nao precisa conhecer a regra nem carregar uma segunda copia
    dela. Mesma ideia do `alertas.acao_da_etapa` — quem sabe e o site.

    `recente=False` com `pareada_em=None` quer dizer "nunca pareou por aqui",
    que e o caso normal de quem ja estava de pe antes desta fila existir. Isso
    NAO e motivo de alarme: e ausencia de registro, nao prova de problema.
    """
    from datetime import timedelta
    vazio = {'sessao': None, 'pareada_em': None, 'horas_desde': None,
             'recente': False, 'janela_horas': horas}
    try:
        ensure_schema()
        linha = db.session.execute(sa_text("""
            -- SO CONTA QUEM REALMENTE PAREOU (19/09/2026, itens 48/61).
            -- Pedido que terminou em "ja estava conectada" nao gera codigo, e
            -- fazia a pipeline avisar "sessao pareada ha N minutos" sem que
            -- ninguem tivesse pareado nada (visto nos pedidos 62 e 63).
            SELECT sessao, atendido_em FROM sessoes_whatsapp
            WHERE cidade = :c AND situacao = :at AND atendido_em IS NOT NULL
              AND codigo IS NOT NULL AND TRIM(codigo) <> ''
            ORDER BY atendido_em DESC LIMIT 1
        """), {'c': cidade, 'at': ATENDIDO}).first()
    except Exception as exc:
        logger.warning('Nao consegui apurar o pareamento de %s: %s', cidade, exc)
        return vazio
    if not linha or not linha[1]:
        return vazio

    quando = linha[1]
    if isinstance(quando, str):
        try:
            quando = datetime.fromisoformat(quando)
        except ValueError:
            return vazio

    desde = datetime.now() - quando
    horas_desde = desde.total_seconds() / 3600.0
    return {
        'sessao': linha[0],
        'pareada_em': quando.isoformat(timespec='seconds'),
        # 3 casas (18/09/2026, E-E): com 1 casa a conta andava em degraus de
        # 6 min — a pipeline disse "6 min" para uma entrega de ~3,6 min.
        'horas_desde': round(horas_desde, 3),
        'recente': desde < timedelta(hours=horas),
        'janela_horas': horas,
    }


def pedido_atual(cidade):
    """Ultimo pedido desta cidade (qualquer situacao), ou None."""
    ensure_schema()
    linha = db.session.execute(sa_text("""
        SELECT id, sessao, telefone, situacao, pedido_por, pedido_em,
               atendido_em, codigo, codigo_em, mensagem
        FROM sessoes_whatsapp WHERE cidade = :c
        ORDER BY pedido_em DESC LIMIT 1
    """), {'c': cidade}).fetchone()
    if not linha:
        return None
    return {
        'id': linha[0], 'sessao': linha[1], 'telefone': linha[2],
        'situacao': linha[3], 'pedido_por': linha[4], 'pedido_em': linha[5],
        'atendido_em': linha[6], 'codigo': linha[7], 'codigo_em': linha[8],
        'mensagem': linha[9],
    }


def pedido_aberto_do_usuario(cidade, usuario_id):
    """id do pedido em aberto DAQUELA pessoa, ou None.

    Existe para não usar `pedido_atual`, que devolve o último da CIDADE: com
    dois admins pedindo quase junto, o atendimento imediato pegaria o pedido do
    outro — e mandaria o código para o telefone errado.
    """
    ensure_schema()
    linha = db.session.execute(sa_text("""
        SELECT id FROM sessoes_whatsapp
        WHERE cidade = :c AND usuario_id = :u AND situacao IN (:p, :e)
        ORDER BY pedido_em DESC LIMIT 1
    """), {'c': cidade, 'u': usuario_id, 'p': PENDENTE, 'e': EM_ANDAMENTO}).fetchone()
    return linha[0] if linha else None


def cancelar_pedido_de(usuario_id, cidade, por=None):
    """Para o pedido DAQUELA pessoa. Devolve (ok, mensagem).

    POR QUE PRECISOU EXISTIR (12/08/2026)
    `cancelar_pedido` cancela o pedido DA CIDADE — o do caminho antigo, sem
    usuario. A tela "WhatsApp de quem publica" tem um pedido POR PESSOA, e
    nenhum deles tinha como ser parado: o dono clicou Conectar no suplente, o
    pedido travou em "Ligando o computador da automação…" e nao havia botao
    nenhum para interromper. Ficar preso a um pedido que nao anda e pior que
    nao ter o botao Conectar.
    """
    ensure_schema()
    linha = db.session.execute(sa_text("""
        SELECT id, sessao FROM sessoes_whatsapp
        WHERE usuario_id = :u AND cidade = :c AND situacao IN (:p, :e)
        ORDER BY pedido_em DESC LIMIT 1
    """), {'u': usuario_id, 'c': cidade,
           'p': PENDENTE, 'e': EM_ANDAMENTO}).fetchone()
    if not linha:
        # REGISTRO ANTIGO TAMBEM SE LIMPA (14/08/2026). Pedido que terminou em
        # `falhou`/`interrompido` nao anda mais, mas continuava na tela com o
        # nome de instancia de quando foi criado — e sem botao nenhum, porque
        # "parar" so olhava para o que estava em andamento. Sumir com a linha
        # nao desconecta nada: ela e so o registro de uma tentativa passada.
        apagados = db.session.execute(sa_text(
            "DELETE FROM sessoes_whatsapp WHERE usuario_id = :u AND cidade = :c "
            "AND situacao NOT IN (:p, :e)"),
            {'u': usuario_id, 'c': cidade,
             'p': PENDENTE, 'e': EM_ANDAMENTO}).rowcount
        db.session.commit()
        if apagados:
            logger.info('Limpei %s registro(s) antigo(s) de pareamento do '
                        'usuario %s (%s)', apagados, usuario_id, por)
            return True, ('Registro antigo removido. Pode clicar em Conectar '
                          'quando quiser.')
        return False, 'Não há pedido em andamento para esta pessoa.'
    db.session.execute(sa_text(
        "UPDATE sessoes_whatsapp SET situacao = :s, mensagem = :m "
        "WHERE id = :i"),
        {'s': CANCELADO, 'i': linha[0],
         'm': 'Interrompido na tela por %s' % (por or 'alguém')})
    _registrar_passo(linha[0], 'desfecho:%s' % CANCELADO,
                     'interrompido na tela por %s' % (por or 'alguém'))
    db.session.commit()
    logger.warning('Pedido %s (sessao %s) interrompido por %s',
                   linha[0], linha[1], por)
    return True, ('Pedido interrompido. A porta que ele reservava está livre '
                  'de novo — pode tentar Conectar quando quiser.')


def cancelar_pedido(cidade, por=None):
    ensure_schema()
    db.session.execute(sa_text("""
        UPDATE sessoes_whatsapp SET situacao = :cancel, mensagem = :m
        WHERE cidade = :c AND situacao IN (:p, :e)
    """), {'cancel': CANCELADO, 'm': f'cancelado por {por or "admin"}',
           'c': cidade, 'p': PENDENTE, 'e': EM_ANDAMENTO})
    db.session.commit()


# Quanto tempo o relato da máquina continua valendo como "notícia fresca". A
# máquina conta o que existe nela a cada rodada e a cada passada da fila (15 em
# 15 min); 40 minutos cobrem uma passada perdida sem transformar máquina
# desligada — que é o normal — em alarme.
JANELA_RELATO_MIN = 40


# O código de pareamento do WhatsApp vale cerca de um minuto. Dez minutos é
# folga larga para quem está com o celular na mão e ainda assim curto o
# bastante para o código não virar mobília na tela.
VALIDADE_CODIGO_MIN = 10


def _limpar_codigos_vencidos(cidade):
    """Apaga códigos de pareamento que já não servem para nada (20/08/2026).

    O código de vincular aparelho vale cerca de um minuto, mas ficava guardado
    para sempre: a tela continuava mostrando o mesmo código dias depois, e ele
    sobrevivia a fechar o navegador e a reiniciar o computador.

    Isso é risco à toa — código de parear aparelho exposto sem prazo — e
    engana: em 20/08 o dono viu na tela o código da véspera e entendeu que o
    sistema estava pedindo reconexão, com a sessão conectada e publicando.

    `anotar_etapa` já apaga o código no instante em que a etapa vira
    `conectado`; esta função cobre o resto — o pareamento que ninguém terminou,
    e os códigos que ficaram para trás antes desta mudança existir.
    """
    from datetime import timedelta
    limite = datetime.now() - timedelta(minutes=VALIDADE_CODIGO_MIN)
    resultado = db.session.execute(sa_text("""
        UPDATE sessoes_whatsapp
           SET codigo = NULL, codigo_em = NULL
         WHERE cidade = :c
           AND codigo IS NOT NULL
           AND (codigo_em IS NULL OR codigo_em < :lim)
    """), {'c': cidade, 'lim': limite})
    if resultado.rowcount:
        db.session.commit()
        logger.info('Codigos de pareamento vencidos apagados em %s: %s',
                    cidade, resultado.rowcount)


def estado_da_sessao(sessao_prevista, instancias, cidade=None, agora=None):
    """O estado da sessao MAIS o sinal da camada de baixo (16/09/2026).

    Duas camadas, duas verdades: o VINCULO da sessao (o que `_calcular_estado_da_sessao`
    calcula) e se a automacao esta de pe naquela porta. Elas viajam juntas e
    separadas — `automacao_parada=True` diz "o WhatsApp nao esta rodando ali
    agora", sem apagar o vinculo que a sessao tem. Nada nesta tela pode dizer
    "de pe" o que nao esta de pe.

    O sinal e' injetado aqui, num ponto so, para nao ter de repetir a chave em
    cada um dos oito retornos do calculo — e para o dia em que aparecer o
    nono.
    """
    sinal = {}
    saida = _calcular_estado_da_sessao(sessao_prevista, instancias, cidade=cidade,
                              agora=agora, _sinal=sinal)
    if isinstance(saida, dict):
        saida['automacao_parada'] = bool(sinal.get('automacao_parada'))
        # O vinculo continua "Conectado", mas a frase dele dizia "de pe e pode
        # publicar" — e com a automacao parada isso e' mentira sobre a camada
        # de baixo. Troca so a frase; o rotulo e o `pronta` (o vinculo) ficam.
        if saida['automacao_parada'] and saida.get('chave') == 'conectado':
            saida['detalhe'] = ('O WhatsApp está vinculado e volta a publicar '
                                'quando a automação subir.')
    return saida


# ENDEREÇO MUDO NÃO SE PERGUNTA DE NOVO NA TELA (18/09/2026, plano C7).
# Medido no log de acesso: com os containers parados, `/andamento` levava
# 10,3 s em TODA volta (0,7 s com eles de pé). Réplica com uma porta que aceita
# e não responde: cada pergunta espera o prazo inteiro (3 a 6 s) e a tela
# repete as mesmas perguntas por sessão, a cada atualização. Só para EXIBIÇÃO:
# quem decide criar container (`porta_tem_waha` no provisionamento) continua
# perguntando sempre — resposta velha ali poderia gerar container duplicado.
_BASE_MUDA = {}
_BASE_MUDA_SEG = 60


def _base_muda(base):
    """Este endereço ficou mudo há menos de 60 s? (só exibição)"""
    quando = _BASE_MUDA.get(base or '')
    return bool(quando and (time.time() - quando) < _BASE_MUDA_SEG)


def _calcular_estado_da_sessao(sessao_prevista, instancias, cidade=None,
                               agora=None, _sinal=None):
    """O que ESTÁ ACONTECENDO com a sessão desta pessoa, agora (19/08/2026;
    checagem ao vivo acrescentada em 22/08/2026).

    POR QUE ISTO PRECISOU EXISTIR
    A tela mostrava o andamento do último PEDIDO de pareamento e chamava aquilo
    de estado. Um pedido bem-sucedido em 17/08 deixava o card verde escrito
    "Conectado. Não precisa fazer nada" — para sempre. Em 19/08 as duas sessões
    foram apagadas e recriadas vazias, o topo da página já dizia
    `SCAN_QR_CODE` em vermelho, e os dois cards continuavam verdes dizendo que
    não havia nada a fazer. A mesma variável causava o oposto do outro lado:
    sem pedido nenhum, o card dizia "nunca conectado" sobre uma sessão que
    publica há meses.

    Pedido é HISTÓRIA ("o que eu pedi e como foi"); sessão é ESTADO ("como está
    agora"). São perguntas diferentes e não podem sair da mesma fonte.

    A verdade vem de `instancias_waha`, que é o que a MÁQUINA relatou (regra
    16-0-D) — nunca de nome de sessão nem de pedido antigo.

    CHECAGEM AO VIVO (22/08/2026). `instancias_waha` só é atualizada por
    `registrar_instancias.py`, chamado a cada RODADA — 2x ao dia. Isso fazia
    `visto_em` passar de `JANELA_RELATO_MIN` (40 min) quase o tempo todo,
    mesmo com a sessão saudável, e o card ficava preso em "sem notícia
    recente" o dia inteiro (achado do dono, 22/08 — `AGORA.md`). Com
    `cidade` disponível, pergunta a sessão AGORA (`GET /api/sessions/<nome>`,
    a mesma leitura que `_falar_com_o_waha` já faz no pareamento e que
    `app.maquina.estado()` já faz para o banner da máquina) — só leitura,
    nunca derruba a sessão (regra 16-A: perguntar não é a mesma coisa que
    reiniciar). A resposta ao vivo manda quando existe; o relato antigo (com
    prazo de validade) só decide quando a máquina não respondeu agora — é
    a rede de segurança para quando ela está mesmo desligada.
    """
    from datetime import datetime, timedelta
    agora = agora or datetime.now()
    alvo = (sessao_prevista or '').strip().lower()

    achada = None
    for inst in instancias or ():
        if (inst.get('sessao') or '').strip().lower() == alvo:
            achada = inst
            break

    if not achada and cidade:
        # SEM LINHA NO REGISTRO, PERGUNTA AO CONTAINER DA CIDADE (15/09/2026).
        # Aqui se respondia "Sem notícia da máquina" sem perguntar nada a
        # ninguém. Medido em 15/09: o suplente conectou às 14:27:02 e o quadro
        # dele mentiu até 14:35:25 (primeiro relato com a sessão), sem o botão
        # de limpar — enquanto o principal, que já tinha linha desde 14:01:40,
        # aparecia "Conectado". Mesmo código, dados diferentes. Um container por
        # cidade: se o site sabe onde a cidade mora, pergunta lá pela sessão.
        # Prazo curto (exibição, não ação); sem resposta, segue "sem relato".
        casa = container_da_cidade(cidade)
        if casa and casa.get('host') and casa.get('porta') and not _base_muda(
                'http://%s:%d' % (casa['host'], int(casa['porta']))):
            base_casa = 'http://%s:%d' % (casa['host'], int(casa['porta']))
            cab_casa = {'X-Api-Key': casa['chave']} if casa.get('chave') else {}
            st_casa, mo_casa = _estado_da_sessao(base_casa, cab_casa,
                                                 sessao_prevista, timeout=4)
            if st_casa is None:
                _BASE_MUDA[base_casa] = time.time()
            if st_casa is not None:
                achada = {'sessao': sessao_prevista, 'base': base_casa,
                          'papel': None, 'visto_em': None,
                          'status': st_casa, 'motor': mo_casa,
                          'pronta': (st_casa == 'WORKING'
                                     and (mo_casa or '').upper() == 'CONNECTED')}

    if not achada:
        return {'chave': 'sem_relato', 'rotulo': 'Sem notícia da máquina',
                'detalhe': 'A máquina ainda não contou que esta sessão existe. '
                           'Se ela estiver desligada, isso é o normal.',
                'cor': 'secondary', 'pronta': False}

    visto = achada.get('visto_em')
    if isinstance(visto, str):
        try:
            visto = datetime.fromisoformat(visto)
        except ValueError:
            visto = None

    ao_vivo_status = ao_vivo_motor = None
    if cidade and achada.get('base') and achada.get('papel'):
        try:
            inst_com_chave = instancia_do_papel(cidade, achada['papel'])
        except Exception:
            inst_com_chave = None
        if (inst_com_chave and inst_com_chave.get('base') == achada.get('base')
                and not _base_muda(inst_com_chave['base'])):
            cab = {'X-Api-Key': inst_com_chave['chave']} if inst_com_chave.get('chave') else {}
            # Prazo curto de proposito (mesmo raciocinio de
            # app.maquina.TIMEOUT_HTTP): isto e exibicao, nao acao, e maquina
            # desligada nao responde nunca -- esperar o padrao de 20s por
            # sessao travaria a tela do admin a toa.
            ao_vivo_status, ao_vivo_motor = _estado_da_sessao(
                inst_com_chave['base'], cab, sessao_prevista, timeout=6)
            if ao_vivo_status is None:
                _BASE_MUDA[inst_com_chave['base']] = time.time()

    if ao_vivo_status is not None:
        # A pergunta ao vivo respondeu: ela manda, e nao ha "relato velho"
        # possivel sobre uma resposta que acabou de chegar.
        status = ao_vivo_status
        motor = (ao_vivo_motor or '').upper()
        pronta = (status == 'WORKING' and motor == 'CONNECTED')
        velho = False
    else:
        # SEM RESPOSTA AO VIVO (maquina desligada, endereco mudou, rede fora,
        # ou `cidade` nao foi passada por um chamador antigo): cai no relato
        # antigo -- e so ESSE precisa de prazo de validade, senao uma sessao
        # que estava conectada quando a maquina desligou continuaria
        # "conectada" na tela a noite inteira.
        status = (achada.get('status') or '').upper()
        motor = (achada.get('motor') or '').upper()
        pronta = bool(achada.get('pronta'))
        velho = bool(visto and (agora - visto) > timedelta(minutes=JANELA_RELATO_MIN))

        # SAO DUAS CAMADAS, E CADA UMA DIZ A SUA VERDADE (16/09/2026, o dono
        # explicou o desenho).
        #
        # Em 15/09 este trecho TROCAVA o estado da sessao por "WhatsApp
        # parado" quando o container nao respondia. Errado: o estado da sessao
        # e' o VINCULO validado antes da parada, e ele volta conectado quando
        # os containers sobem. Container de pe ou parado e' outra camada.
        #
        # O que se mede aqui continua valendo — a ponte do Windows aceita a
        # ligacao e corta (erro 104): ha resposta, e ela diz que nao ha WhatsApp
        # rodando ali (`porta_tem_waha`: False = sem WAHA; None = nao sei). So
        # que agora isso viaja como um SINAL AO LADO (`automacao_parada`), sem
        # apagar o vinculo: a tela mostra a sessao vinculada e, ao lado, que a
        # automacao esta parada — nunca "de pe" o que nao esta.
        try:
            from urllib.parse import urlparse
            from app import maquina
            alvo_url = urlparse(achada.get('base') or '')
            if alvo_url.hostname and alvo_url.port:
                _base_alvo = achada.get('base') or ''
                _parada = _base_muda(_base_alvo)
                if not _parada and maquina.porta_tem_waha(
                        alvo_url.hostname, alvo_url.port) is False:
                    _parada = True
                    _BASE_MUDA[_base_alvo] = time.time()
                if _parada and _sinal is not None:
                    _sinal['automacao_parada'] = True
        except Exception:
            pass

    if velho:
        return {'chave': 'sem_noticia', 'rotulo': 'Sem notícia recente',
                'detalhe': 'A última vez que a máquina falou desta sessão foi há '
                           'mais de %s minutos. Costuma ser a máquina desligada.'
                           % JANELA_RELATO_MIN,
                'cor': 'secondary', 'pronta': False, 'visto_em': visto}

    if pronta:
        return {'chave': 'conectado', 'rotulo': 'Conectado',
                'detalhe': 'A sessão está de pé e pode publicar.',
                'cor': 'success', 'pronta': True, 'numero': achada.get('numero'),
                'visto_em': visto}

    if status == 'NAO_EXISTE':
        return {'chave': 'nao_existe', 'rotulo': 'Sessão não existe na máquina',
                'detalhe': 'Perguntei agora e a máquina não tem nenhuma sessão '
                           'com este nome. Clique em Conectar para criar.',
                'cor': 'warning', 'pronta': False, 'visto_em': visto}

    if status in ('SCAN_QR_CODE',) or motor in ('UNPAIRED', 'UNPAIRED_IDLE'):
        return {'chave': 'precisa_parear', 'rotulo': 'Precisa conectar',
                'detalhe': 'O WhatsApp desta pessoa não está vinculado. '
                           'Clique em Conectar para receber o código no celular.',
                'cor': 'warning', 'pronta': False, 'visto_em': visto}

    if status == 'FAILED':
        return {'chave': 'falhou', 'rotulo': 'Desconectado pelo WhatsApp',
                'detalhe': 'O WhatsApp desvinculou este aparelho. Só conectar de '
                           'novo resolve — religar não traz a sessão de volta.',
                'cor': 'danger', 'pronta': False, 'visto_em': visto}

    if status in ('STARTING', 'STOPPED') or not status:
        return {'chave': 'subindo', 'rotulo': 'Preparando',
                'detalhe': 'A sessão está subindo (%s). Costuma levar menos de '
                           'um minuto.' % (status or 'sem status'),
                'cor': 'info', 'pronta': False, 'visto_em': visto}

    # NÃO SEI nunca vira "está tudo bem" nem "está quebrado" (regra 16-A).
    return {'chave': 'nao_sei', 'rotulo': 'Estado incerto',
            'detalhe': 'A máquina respondeu "%s / %s", que não é um estado '
                       'conhecido.' % (status or '?', motor or '?'),
            'cor': 'secondary', 'pronta': False, 'visto_em': visto}


def painel_da_cidade(cidade):
    """Uma linha por admin que publica: quem é, em que passo está, o que falta.

    A tela precisa disto para responder a unica pergunta que o admin faz —
    "e agora, o que eu tenho que fazer?" — sem obrigar ninguem a interpretar
    porta, container ou nome de sessao.

    Cada linha leva DUAS coisas que não se confundem: `pedido` é a história do
    último Conectar, e `estado` é como a sessão está agora (ver
    `estado_da_sessao`). Misturar as duas foi o defeito de 19/08.
    """
    ensure_schema()
    from app import publicador
    from app.models import Usuario

    # Antes de montar a tela, tira da frente o que já não vale (código de
    # pareamento vencido). Barato: só escreve quando há o que apagar.
    try:
        _limpar_codigos_vencidos(cidade)
    except Exception as exc:
        db.session.rollback()
        logger.warning('painel_da_cidade (%s): nao consegui limpar codigos '
                       'vencidos: %s', cidade, exc)

    pedidos = {}
    for linha in db.session.execute(sa_text("""
        SELECT usuario_id, id, sessao, telefone, situacao, codigo, codigo_em,
               etapa, etapa_em, papel, porta, instancia, mensagem, email
        FROM sessoes_whatsapp
        WHERE cidade = :c AND usuario_id IS NOT NULL
        ORDER BY pedido_em ASC
    """), {'c': cidade}).fetchall():
        pedidos[linha[0]] = {
            'id': linha[1], 'sessao': linha[2], 'telefone': linha[3],
            'situacao': linha[4], 'codigo': linha[5], 'codigo_em': linha[6],
            'etapa': linha[7], 'etapa_em': linha[8], 'papel': linha[9],
            'porta': linha[10], 'instancia': linha[11], 'mensagem': linha[12],
            'email': linha[13],
            'etapa_titulo': ETAPA_TITULO.get(linha[7] or '', ''),
        }

    # O QUE A MÁQUINA RELATOU, lido UMA vez para todas as linhas.
    try:
        relatadas = instancias(cidade)
    except Exception as exc:
        logger.warning('painel_da_cidade (%s): nao consegui ler as instancias: %s',
                       cidade, exc)
        relatadas = []

    saida = []
    for usuario in Usuario.query.filter(
            Usuario.cidade == cidade, Usuario.ativo.is_(True)
    ).order_by(Usuario.ordem_envio.asc(), Usuario.nome.asc()).all():
        ordem = usuario.ordem_envio or 0
        if not ordem and not pedidos.get(usuario.id):
            continue   # nao publica e nunca pediu: nao polui a tela
        sessao_prevista = publicador._sessao_do_usuario(usuario)
        saida.append({
            'usuario_id': usuario.id,
            'nome': usuario.nome,
            'email': usuario.email,
            'telefone': usuario.telefone,
            'ordem': ordem,
            'papel': ('principal' if ordem == 1 else
                      'suplente' if ordem == 2 else
                      'reserva' if ordem else 'não publica'),
            'sessao_prevista': sessao_prevista,
            'pedido': pedidos.get(usuario.id),
            # COMO A SESSÃO ESTÁ AGORA — não como foi o último pedido.
            'estado': estado_da_sessao(sessao_prevista, relatadas, cidade=cidade),
        })
    return saida


def tempo_humano(segundos):
    """"8 minutos", "2 horas e 10 minutos" — nunca "480 seg".

    Convenção da casa: quem converte é o código, não a pessoa. Nós contamos em
    base 10 e a hora é base 60; obrigar o admin a dividir por 60 no meio de um
    problema é empurrar trabalho para quem menos pode fazê-lo.
    """
    try:
        segundos = int(segundos or 0)
    except (TypeError, ValueError):
        return 'agora'
    if segundos < 60:
        return 'menos de um minuto'
    minutos, horas = (segundos // 60) % 60, segundos // 3600
    dias, horas = horas // 24, horas % 24

    def plural(n, um, muitos):
        return '%d %s' % (n, um if n == 1 else muitos)

    if dias:
        partes = [plural(dias, 'dia', 'dias')]
        if horas:
            partes.append(plural(horas, 'hora', 'horas'))
    elif horas:
        partes = [plural(horas, 'hora', 'horas')]
        if minutos:
            partes.append(plural(minutos, 'minuto', 'minutos'))
    else:
        partes = [plural(minutos, 'minuto', 'minutos')]
    return ' e '.join(partes)


# O QUE FAZER QUANDO CADA PASSO FALHA (regra 16-I).
#
# A automação manda a frase técnica ("nao consegui subir a instancia
# auto-waha-..."), que é a verdade e não ajuda ninguém. Aqui ela ganha uma
# tradução e o próximo passo. A frase técnica continua aparecendo embaixo —
# esconder o detalhe seria trocar um problema por outro.
_MOTIVOS = {
    'ligando': {
        'o_que_houve': 'O computador da automação não chegou a atender.',
        'o_que_fazer': 'Ele passa o dia dormindo de propósito. Tente de novo em '
                       'alguns minutos: o pedido acorda a máquina pela rede. Se '
                       'ela estiver desligada na tomada, ligue-a na mão.',
    },
    'subindo': {
        'o_que_houve': 'O WhatsApp desta pessoa não pôde ser preparado na máquina.',
        'o_que_fazer': 'Quase sempre é uma instância que já existe com outro '
                       'nome ou uma porta ocupada. Clique em Tentar de novo: '
                       'agora o site pergunta à máquina o que existe antes de '
                       'criar qualquer coisa.',
    },
    'criando': {
        'o_que_houve': 'A instância subiu, mas a sessão do WhatsApp não foi criada.',
        'o_que_fazer': 'Tente de novo. Se repetir, marque "refazer do zero" — '
                       'aí a sessão é apagada e recriada, e você pareia de novo.',
    },
    'codigo': {
        'o_que_houve': 'O WhatsApp não devolveu o código de pareamento.',
        'o_que_fazer': 'Confira se o número está certo no cadastro e tente de '
                       'novo. Não há limite de tentativas.',
    },
    'aguardando': {
        'o_que_houve': 'O código foi gerado e ninguém o digitou no celular a tempo.',
        'o_que_fazer': 'O código vale cerca de 1 minuto. Deixe o celular na mão, '
                       'clique em Tentar de novo e digite assim que chegar.',
    },
}
_MOTIVO_PADRAO = {
    'o_que_houve': 'O preparo do WhatsApp parou no meio.',
    'o_que_fazer': 'Clique em Tentar de novo. Se repetir três vezes, é caso de '
                   'olhar a máquina da automação.',
}


def motivo_da_falha(etapa, mensagem=None):
    """{o_que_houve, o_que_fazer, detalhe} — em português, com o próximo passo."""
    base = dict(_MOTIVOS.get((etapa or '').strip(), _MOTIVO_PADRAO))
    base['detalhe'] = (mensagem or '').strip() or None
    return base


def andamento_da_cidade(cidade):
    """O que a tela do pareamento mostra AO VIVO, uma linha por pessoa (P-75).

    Junta o painel da cidade com o que só o tempo responde: em que passo está,
    há quanto tempo, se já passou do previsto, o que já aconteceu e — quando
    falha — o motivo com o que fazer.

    Antes disto a tela era estática: quem clicava em Conectar olhava para uma
    tabela parada e só descobria o desfecho recarregando a página na mão.
    """
    agora = datetime.now()
    saida = painel_da_cidade(cidade)
    for linha in saida:
        pedido = linha.get('pedido')
        linha['ativo'] = False
        if not pedido:
            continue

        situacao = pedido.get('situacao')
        etapa = pedido.get('etapa') or 'ligando'
        # EM CURSO INCLUI "esperando você digitar o código".
        #
        # A automação marca o pedido como ATENDIDO no instante em que o código
        # sai (é o desfecho da parte dela), mas para quem está na tela o
        # trabalho continua: falta digitar no celular e falta a sessão conectar.
        # Tratar isso como terminado apagava o botão de parar, trocava o de
        # tentar de novo por "limpar" e ainda fazia a tela parar de perguntar
        # justamente no minuto que mais importa.
        ativo = (situacao in (PENDENTE, EM_ANDAMENTO)
                 or (situacao == ATENDIDO and etapa != 'conectado'))
        linha['ativo'] = ativo
        pedido['em_curso'] = ativo

        desde = pedido.get('etapa_em')
        if isinstance(desde, str):
            try:
                desde = datetime.fromisoformat(desde)
            except ValueError:
                desde = None
        segundos = int((agora - desde).total_seconds()) if desde else 0
        limite = ETAPA_LIMITE.get(etapa, 0)

        pedido['ha_quanto_tempo'] = tempo_humano(segundos)
        pedido['segundos_na_etapa'] = segundos
        # AVISO, NUNCA SUBSTITUICAO (convenção da casa): passar do tempo é uma
        # segunda informação; o passo em que está continua sendo a primeira.
        pedido['passou_do_tempo'] = bool(ativo and limite and segundos > limite)
        pedido['limite_humano'] = tempo_humano(limite) if limite else None
        pedido['etapa_titulo'] = ETAPA_TITULO.get(etapa, etapa)
        # PEDIDO ENCERRADO NAO TEM PASSO "AGORA" (14/09/2026).
        # A tela pinta bolinha girando em todo passo "agora" e desenha os passos
        # sempre. O pedido 51 falhou em 13/09 19:01 com a etapa guardada em
        # "ligando" — e a tela passou mais de 27 horas mostrando "Ligando o
        # computador" com a bolinha rodando, sem processo nenhum por tras.
        # Terminado, o passo em que ele parou vira "parou", com a hora.
        pedido['passos'] = [{
            'chave': chave,
            'titulo': titulo,
            'estado': ('feito' if _ordem_etapa(chave) < _ordem_etapa(etapa)
                       else ('agora' if ativo else 'parou') if chave == etapa
                       else 'falta'),
        } for chave, titulo, _ in ETAPAS]
        if not ativo and isinstance(desde, datetime):
            pedido['parou_em'] = desde.strftime('%d/%m %H:%M')
        if situacao == ATENDIDO and etapa == 'conectado':
            for passo in pedido['passos']:
                passo['estado'] = 'feito'
        pedido['historico'] = historico_do_pedido(pedido['id'])
        pedido['motivo'] = (motivo_da_falha(etapa, pedido.get('mensagem'))
                            if situacao == FALHOU else None)
    return saida


def _ordem_etapa(chave):
    for indice, (nome, _, _) in enumerate(ETAPAS):
        if nome == chave:
            return indice
    return -1


def fila_pendente(limite=20):
    """O que o agente do Dell consome quando a maquina sobe.

    Devolve tudo que o provisionamento precisa saber sem ter que consultar o
    site de novo: porta, nome do container e papel. O agente nao decide nada
    disso — quem manda e a aplicacao, que e quem conhece as regras da cidade.
    """
    ensure_schema()
    linhas = db.session.execute(sa_text("""
        SELECT id, cidade, sessao, telefone, recriar, pedido_em, pedido_por,
               usuario_id, papel, porta, instancia, email
        FROM sessoes_whatsapp WHERE situacao = :p
        ORDER BY pedido_em ASC LIMIT :l
    """), {'p': PENDENTE, 'l': limite}).fetchall()
    return [{
        'id': r[0], 'cidade': r[1], 'sessao': r[2], 'telefone': r[3],
        'recriar': bool(r[4]), 'pedido_em': str(r[5]), 'pedido_por': r[6],
        'usuario_id': r[7], 'papel': r[8] or 'principal',
        'porta': int(r[9]) if r[9] else None,
        # Reserva pelo nome da SESSAO, nunca pela porta — mesmo motivo do
        # comentario em `pedir_vinculacao`.
        'instancia': r[10] or ('auto-waha-%s' % r[2].lower() if r[2] else None),
        'email': r[11],
        'numero_responsavel': numero_responsavel(r[1]),
        # Limite de tempo de cada passo, para o agente nao ficar preso para
        # sempre esperando algo que nao vem.
        'timeouts': ETAPA_LIMITE,
    } for r in linhas]


def _registrar_passo(pedido_id, etapa, mensagem=None):
    """Guarda UM passo do pedido, para a tela contar a história depois (P-75).

    Não faz commit: quem chama já commita a mudança principal, e um passo sem
    a mudança que ele descreve seria pior que passo nenhum.
    """
    try:
        db.session.execute(sa_text("""
            INSERT INTO sessoes_whatsapp_passos (pedido_id, etapa, mensagem, em)
            VALUES (:i, :et, :msg, :em)
        """), {'i': pedido_id, 'et': etapa,
               'msg': str(mensagem)[:300] if mensagem else None,
               'em': datetime.now()})
    except Exception as exc:
        # O rastro é para CONTAR a história; falhar aqui não pode impedir o
        # pareamento de acontecer.
        logger.debug('Não consegui gravar o passo %s do pedido %s: %s',
                     etapa, pedido_id, exc)


# Como cada desfecho aparece na história, em português de gente.
_TITULO_DESFECHO = {
    EM_ANDAMENTO: 'O computador da automação pegou o pedido',
    ATENDIDO:     'Código de pareamento gerado',
    FALHOU:       'Não deu certo',
    CANCELADO:    'Interrompido na tela',
}


def _titulo_do_passo(etapa):
    chave = (etapa or '').strip()
    if chave.startswith('desfecho:'):
        return _TITULO_DESFECHO.get(chave.split(':', 1)[1], chave)
    return ETAPA_TITULO.get(chave, chave)


def historico_do_pedido(pedido_id, limite=40):
    """Os passos daquele pedido, do mais antigo para o mais novo."""
    ensure_schema()
    linhas = db.session.execute(sa_text("""
        SELECT etapa, mensagem, em FROM sessoes_whatsapp_passos
        WHERE pedido_id = :i ORDER BY id DESC LIMIT :l
    """), {'i': pedido_id, 'l': limite}).fetchall()
    saida = []
    for etapa, mensagem, em in reversed(linhas):
        quando = em
        if isinstance(quando, str):
            try:
                quando = datetime.fromisoformat(quando)
            except ValueError:
                quando = None
        saida.append({
            'etapa': etapa,
            'titulo': _titulo_do_passo(etapa),
            'mensagem': mensagem,
            'hora': quando.strftime('%H:%M') if quando else '',
            'quando': quando.isoformat(timespec='seconds') if quando else None,
        })
    return saida


def anotar_etapa(pedido_id, etapa, mensagem=None):
    """O agente conta em que passo esta; a tela mostra ao vivo.

    Sem isto o admin da cidade aperta o botao e olha para uma tela parada por
    tres minutos, sem saber se esta funcionando ou travou — e apertar de novo
    e o que mais estraga um provisionamento pela metade.
    """
    ensure_schema()
    if etapa not in ETAPA_LIMITE:
        return False

    # CONECTOU: O CÓDIGO SAI DA TELA NA HORA (20/08/2026, pedido do dono).
    #
    # O código de pareamento ficava guardado para sempre depois de usado, e a
    # tela continuava mostrando ele — sobrevivia a fechar o navegador e até a
    # reiniciar o computador. Duas coisas ruins de uma vez:
    #
    #   1. Segurança: código de vincular aparelho à mostra, sem prazo, para
    #      quem abrir a página. Depois de conectado ele não serve para mais
    #      nada, então guardá-lo é só risco.
    #   2. Confunde: em 20/08 o dono viu o código de 21:23 da véspera ainda na
    #      tela e entendeu que o sistema estava pedindo reconexão — enquanto a
    #      sessão estava conectada e publicando. Foi procurar defeito onde não
    #      havia.
    #
    # Some junto o `codigo_em`, senão a tela ainda calcula "há quanto tempo".
    limpar_codigo = (etapa == 'conectado')
    db.session.execute(sa_text("""
        UPDATE sessoes_whatsapp
        SET etapa = :et, etapa_em = :em, mensagem = COALESCE(:msg, mensagem)
        %s
        WHERE id = :i
    """ % (', codigo = NULL, codigo_em = NULL' if limpar_codigo else '')),
        {'et': etapa, 'em': datetime.now(), 'msg': mensagem, 'i': pedido_id})
    _registrar_passo(pedido_id, etapa, mensagem)
    db.session.commit()
    logger.info('Pedido %s -> etapa %s%s', pedido_id, etapa,
                (' (%s)' % mensagem) if mensagem else '')
    return True


def marcar(pedido_id, situacao, codigo=None, mensagem=None):
    """A automacao confirma o que aconteceu com um pedido."""
    ensure_schema()
    if situacao not in (EM_ANDAMENTO, ATENDIDO, FALHOU, CANCELADO):
        return False
    agora = datetime.now()
    db.session.execute(sa_text("""
        UPDATE sessoes_whatsapp
        SET situacao = :sit,
            codigo = COALESCE(:cod, codigo),
            codigo_em = CASE WHEN :cod IS NOT NULL THEN :em ELSE codigo_em END,
            atendido_em = CASE WHEN :sit IN (:at, :fa) THEN :em ELSE atendido_em END,
            mensagem = COALESCE(:msg, mensagem)
        WHERE id = :i
    """), {'sit': situacao, 'cod': codigo, 'em': agora, 'at': ATENDIDO,
           'fa': FALHOU, 'msg': mensagem, 'i': pedido_id})
    # O DESFECHO TAMBEM E UM PASSO (P-75). Sem esta linha, um pedido que falha
    # um segundo depois de subir some da historia: a tela mostra "falhou" e
    # ninguem descobre ate onde ele tinha chegado.
    _registrar_passo(pedido_id, 'desfecho:%s' % situacao,
                     mensagem or ('código %s gerado' % codigo if codigo else None))
    db.session.commit()
    logger.info('Pedido de vinculacao %s marcado como %s', pedido_id, situacao)

    # Chegou codigo? Manda por e-mail para quem pediu.
    #
    # POR QUE AQUI E NAO NA AUTOMACAO
    # Quem gera o codigo e o WAHA, no Dell. Mas o Dell desliga — inclusive na
    # parada das 22h, que pode cair entre gerar o codigo e entregar. Este ponto
    # roda no Synology, que fica ligado sempre: assim que o codigo chega, o
    # e-mail sai, e continua sendo possivel reenviar depois sem religar nada.
    if codigo:
        _entregar_codigo(pedido_id, codigo)
    return True


# ===================== O SITE INSISTE, NAO TENTA UMA VEZ =====================
#
# POR QUE MUDOU (16/08/2026 — o "Conectar nunca funcionou de verdade")
#
# O clique fazia DUAS coisas de uma vez: mandava o Wake-on-LAN e, no mesmo
# segundo, uma tentativa de falar com o WAHA. Com a maquina desligada — que e o
# NORMAL (regra 21-G) — essa unica tentativa morria em conexao recusada, o
# pedido ficava anotado como "a maquina atende na rodada dela" e o site nunca
# mais tentava. Quem atenderia depois era o `provisionar.py`, dentro do
# `executar.sh`, que so roda nos horarios do ranking ou quando ha mensagem na
# fila: o pedido ficava horas parado com a maquina ja LIGADA na frente do dono.
#
# Agora o site espera a maquina subir e faz o trabalho ele mesmo. Ele so nao
# consegue criar instancia NOVA (isso e Docker, e Docker mora no Dell); quando a
# instancia daquele papel ja existe — o caso de toda cidade que ja funcionou uma
# vez —, criar a sessao e pedir o codigo e chamada HTTP, e o site sabe fazer.
PRAZO_ATENDIMENTO_SEG = 1200     # 20 min: ligar do zero (S5 -> Windows -> WSL ->
                                 # Docker -> WAHA) leva de 3 a 6 min medidos; o
                                 # resto e folga para a maquina que demora.
INTERVALO_TENTATIVA_SEG = 15     # de quanto em quanto tempo se pergunta de novo
INTERVALO_WOL_SEG = 120          # o pacote magico se repete: ele nao tem resposta
MAX_WOL = 6                      # 6 pacotes em 12 min. Nao ligou, nao vai ligar.

# Quem ja esta sendo atendido NESTE worker. O uWSGI roda mais de um worker e
# dois cliques seguidos abririam duas insistencias para o mesmo pedido — o dobro
# de chamadas ao WAHA para o mesmo resultado.
_EM_CURSO = set()


def _pedido_para_atender(pedido_id):
    """O pedido, se ele ainda estiver aberto. None quando acabou.

    Lido a CADA volta de proposito: e assim que o botao Parar da tela e o
    atendimento pela maquina interrompem a insistencia — sem isso o site
    continuaria pedindo codigo de um pedido que o dono ja cancelou.
    """
    try:
        db.session.remove()      # sem isto a volta seguinte le a foto velha
    except Exception:
        pass
    linha = db.session.execute(sa_text(
        'SELECT sessao, telefone, papel, recriar, situacao, etapa '
        'FROM sessoes_whatsapp WHERE id = :i'), {'i': pedido_id}).fetchone()
    if not linha or linha[4] not in (PENDENTE, EM_ANDAMENTO):
        return None
    return {'sessao': linha[0], 'telefone': linha[1], 'papel': linha[2] or '',
            'recriar': linha[3], 'situacao': linha[4], 'etapa': linha[5]}


def _maquina_responde(inst):
    """A instancia relatada aceita conexao? E o teste mais barato de 'ligada'.

    Endereco e porta vem do relato da propria maquina (regra 16-0-D), nao de
    campo digitado. Timeout curto de proposito: maquina desligada nao responde
    nunca, e esperar 10s por um "nao" atrasa a proxima tentativa a toa.
    """
    from app import maquina
    host = inst.get('host')
    porta = inst.get('porta')
    if not host or not porta:
        return False
    # ACEITAR LIGACAO NAO E SER O WHATSAPP (15/09/2026).
    # Isto usava `porta_responde` (so o aperto de mao TCP). A ponte do Windows
    # (portproxy) aceita a ligacao na 3000 mesmo com o container PARADO, e so
    # corta depois (erro 104). Medido nos pedidos 52 e 53 (14/09 23:03 e 23:19):
    # com os containers parados desde 22:34, o site escreveu "a instancia
    # auto-waha-pls respondeu" e ficou falando com um WhatsApp que nao existia.
    # `porta_tem_waha` faz a pergunta certa — e a propria docstring dela conta
    # este mesmo defeito, de 09/09. "Nao sei" (None) nao vira "respondeu".
    try:
        return maquina.porta_tem_waha(host, int(porta)) is True
    except Exception:
        return False


def atender_no_site(app, cidade, pedido_id):
    """Resolve o pareamento sem esperar a rodada da maquina — insistindo.

    Roda em thread: ligar a maquina e esperar a sessao subir leva minutos, e
    prender a tela nisso e o que a P-75 veio resolver. Quem conta o andamento e
    o acompanhamento ao vivo, passo a passo.

    NUNCA MARCA `falhou` POR CONTA PROPRIA. Se o prazo acabar, o pedido continua
    PENDENTE e a maquina o atende na rodada dela: e a mesma filosofia da entrega
    imediata da fila (regra 16-P) — atalho, nunca dono.
    """
    with app.app_context():
        if pedido_id in _EM_CURSO:
            logger.info('Pedido %s ja esta sendo atendido neste worker.', pedido_id)
            return
        _EM_CURSO.add(pedido_id)
        try:
            _insistir(cidade, pedido_id)
        except Exception as exc:
            logger.warning('Atendimento do pedido %s parou por erro: %s',
                           pedido_id, exc)
            try:
                anotar_etapa(pedido_id, 'ligando',
                             'nao deu para resolver agora (%s) — o pedido '
                             'continua guardado e a maquina atende quando subir'
                             % str(exc)[:80])
            except Exception:
                pass
        finally:
            _EM_CURSO.discard(pedido_id)


def _insistir(cidade, pedido_id):
    """O laco: acorda a maquina, espera ela responder e ai faz o trabalho."""
    from app import ligarmaquina

    fim = time.time() + PRAZO_ATENDIMENTO_SEG
    ultimo_wol = 0.0
    wol_enviados = 0
    # O QUE JA FOI FEITO NESTE PEDIDO. Mora aqui, e nao dentro do passo, porque
    # e o que impede o conserto em laco (regra 16-E: um conserto por problema).
    memoria = {'restart_feito': False, 'ultima_nota': '', 'pegou': False}

    while time.time() < fim:
        dados = _pedido_para_atender(pedido_id)
        if not dados:
            logger.info('Pedido %s saiu do ar (cancelado ou ja atendido).', pedido_id)
            return

        inst = instancia_do_papel(cidade, dados['papel'])
        if not inst or not inst.get('base'):
            # A CASA DA CIDADE SERVE (15/09/2026, um container por cidade).
            # Sem linha deste papel — sessao recem-apagada, ou o suplente que
            # ainda nao existe —, a sessao nova nasce DENTRO do container que a
            # cidade ja tem. Desistir aqui pintava "Ligando" com ele de pe.
            casa = container_da_cidade(cidade)
            if casa and casa.get('host') and casa.get('porta'):
                inst = {'papel': dados['papel'], 'container': casa['container'],
                        'sessao': dados['sessao'], 'host': casa['host'],
                        'porta': casa['porta'], 'chave': casa['chave'],
                        'base': 'http://%s:%d' % (casa['host'], casa['porta']),
                        'status': casa.get('status'), 'motor': None,
                        'pronta': False}
        if not inst or not inst.get('base'):
            # A MAQUINA NUNCA CONTOU QUE TEM ESTA INSTANCIA. Nao ha endereco
            # para onde ligar nem porta para testar: quem resolve e o
            # `provisionar.py`, que cria o container quando a maquina subir.
            _nota(pedido_id, 'ligando', memoria,
                  'ainda nao sei em que instancia deste papel a cidade roda o '
                  'WhatsApp — quem conta isso e a propria maquina, assim que '
                  'ela ligar')
            return

        if not _maquina_responde(inst):
            # PORTA MUDA COM O COMPUTADOR LIGADO NAO E COMPUTADOR DESLIGADO
            # (14/09/2026). Este laco perguntava SO pela porta do WAHA — que so
            # responde com o container de pe e a ponte feita. Fora da janela o
            # Guardiao para os containers, e aqui se concluia "desligado": a
            # tela mostrava "Ligando o computador" e saia Wake-on-LAN para uma
            # maquina ligada. Pior, a maquina atendia o MESMO pedido ao mesmo
            # tempo: em 13/09 19:00:17 ela escreveu "pedindo o codigo" e 60 ms
            # depois este laco voltou a tela para "ligando".
            # O desempate e o carimbo que o proprio Windows deixa ao falar com o
            # site (`maquina._windows_de_pe`, o mesmo que `maquina.estado` usa
            # desde 14/08): nao depende de WSL, container nem ponte.
            from app import maquina
            if maquina._windows_de_pe(cidade):
                if not _mais_adiante(dados.get('etapa'), 'subindo'):
                    _nota(pedido_id, 'subindo', memoria,
                          'o computador da automacao esta ligado — esperando a '
                          'instancia do WhatsApp subir')
                time.sleep(INTERVALO_TENTATIVA_SEG)
                continue
            # A TELA NAO ANDA PARA TRAS: se a maquina ja contou uma etapa mais
            # adiante, quem esta errado e este teste de porta, nao ela.
            if _mais_adiante(dados.get('etapa'), 'ligando'):
                time.sleep(INTERVALO_TENTATIVA_SEG)
                continue
            agora = time.time()
            if wol_enviados < MAX_WOL and (agora - ultimo_wol) >= INTERVALO_WOL_SEG:
                ultimo_wol = agora
                wol_enviados += 1
                try:
                    ligarmaquina.ligar(cidade)
                except Exception as exc:
                    logger.warning('Wake-on-LAN do pedido %s falhou: %s',
                                   pedido_id, exc)
                _nota(pedido_id, 'ligando', memoria,
                      'mandei o sinal que liga o computador (%da vez) — ele '
                      'costuma levar de 1 a 3 minutos para responder'
                      % wol_enviados)
            else:
                _nota(pedido_id, 'ligando', memoria,
                      'esperando o computador da automacao responder')
            time.sleep(INTERVALO_TENTATIVA_SEG)
            continue

        # A maquina respondeu. Daqui para a frente e tudo HTTP, e o site faz.
        if not memoria['pegou']:
            memoria['pegou'] = True
            marcar(pedido_id, EM_ANDAMENTO)
            _nota(pedido_id, 'subindo', memoria,
                  'a instancia %s respondeu — o site vai direto ao que falta'
                  % (inst.get('container') or inst.get('sessao') or ''))

        desfecho = _falar_com_o_waha(cidade, pedido_id, inst, dados, memoria,
                                     fim)
        if desfecho in ('pronto', 'parar'):
            return
        time.sleep(INTERVALO_TENTATIVA_SEG)

    minutos = PRAZO_ATENDIMENTO_SEG // 60
    _nota(pedido_id, 'ligando', memoria,
          'insisti por %d minutos e nao consegui terminar. O pedido continua '
          'guardado: ele e atendido na proxima vez que a maquina subir, e o '
          'codigo e reenviado.' % minutos)
    logger.warning('Pedido %s: prazo do site esgotado, segue pendente para a maquina.',
                   pedido_id)


def _mais_adiante(atual, nova):
    """A etapa `atual` ja passou de `nova`? (ordem de ETAPAS). Desconhecida = nao."""
    ordem = [chave for chave, _titulo, _limite in ETAPAS]
    try:
        return ordem.index(atual or '') > ordem.index(nova)
    except ValueError:
        return False


def _nota(pedido_id, etapa, memoria, mensagem):
    """Escreve no historico SO QUANDO MUDA (16/08/2026).

    A insistencia pergunta de 15 em 15 segundos. Anotar a cada volta enchia a
    tela com a mesma frase repetida vinte vezes — o dono viu isso no primeiro
    teste — e enterrava a unica linha que importava. Repetir nao e informar.
    """
    chave = '%s|%s' % (etapa, mensagem)
    if memoria.get('ultima_nota') == chave:
        return
    memoria['ultima_nota'] = chave
    anotar_etapa(pedido_id, etapa, mensagem)


def _le_o_grupo(base, cab, nome, cidade):
    """A sessao le o grupo da cidade? True/False, ou None sem grupo cadastrado.

    Mesma leitura da prontidao da maquina (`limit=1`, sem midia): so LE, nunca
    envia nada ao grupo (19/09/2026, "Refazer a conexao").
    """
    import requests
    grupo = (secretstore.get(secretstore.WHATSAPP_GRUPO, scope=cidade) or '').strip()
    if not grupo:
        return None
    try:
        r = requests.get('%s/api/%s/chats/%s/messages' % (base, nome, grupo),
                         params={'limit': 1, 'downloadMedia': 'false'},
                         headers=cab, timeout=30)
    except Exception:
        return False
    return r.status_code == 200


_COPIAS_A_LIMPAR = 'whatsapp_copias_a_limpar'


def pedir_limpeza_de_copias(cidade, sessao):
    """Anota que as copias EM ARQUIVO desta sessao, anteriores a agora, sao
    cadaver (a sessao foi refeita). Quem apaga e a maquina (`faxina_cidade.py`),
    so as desta sessao e so as mais antigas que este pedido (19/09/2026)."""
    atual = []
    try:
        atual = json.loads(secretstore.get(_COPIAS_A_LIMPAR, scope=cidade) or '[]')
    except ValueError:
        atual = []
    atual = [p for p in atual if p.get('sessao') != sessao]
    atual.append({'sessao': sessao,
                  'antes_de': datetime.now().isoformat(timespec='seconds')})
    secretstore.set_value(_COPIAS_A_LIMPAR, json.dumps(atual), scope=cidade,
                          updated_by='refazer-conexao')


def copias_a_limpar(cidade):
    """Lista de {sessao, antes_de} pendentes, ou []."""
    try:
        return json.loads(secretstore.get(_COPIAS_A_LIMPAR, scope=cidade) or '[]')
    except ValueError:
        return []


def _estado_da_sessao(base, cab, nome, timeout=20):
    """(status, motor) da sessao, ou (None, None) quando nao deu para perguntar.

    `None` NAO e `FAILED`: nao saber e nao saber (regra 16-A, tres respostas).

    `timeout` default (20s) serve os fluxos de acao (pareamento), onde quem
    clicou ja espera alguma demora. Quem chama isto para simples EXIBICAO de
    status (`estado_da_sessao`) passa um prazo bem mais curto -- maquina
    desligada nao responde nunca, e esperar 20s por sessao so para pintar um
    card trava a tela do admin a toa (mesmo raciocinio de
    app.maquina.TIMEOUT_HTTP).
    """
    import requests
    try:
        resp = requests.get('%s/api/sessions/%s' % (base, nome),
                            headers=cab, timeout=timeout)
    except Exception:
        return None, None
    if resp.status_code == 404:
        return 'NAO_EXISTE', None
    if resp.status_code != 200:
        return None, None
    try:
        corpo = resp.json() or {}
    except ValueError:
        return None, None
    motor = corpo.get('engine') or {}
    return corpo.get('status'), (motor.get('state') if isinstance(motor, dict) else None)


def _falar_com_o_waha(cidade, pedido_id, inst, dados, memoria, fim):
    """Prepara a sessao e pede o codigo. Devolve 'pronto', 'parar' ou 'ainda'.

    A RECEITA E A MESMA DO `conectar_whatsapp.sh` (16/08/2026)

    O site mandava `start` para toda sessao que nao estivesse WORKING. Medido no
    log do WAHA no primeiro teste do botao:

        session:principal-pls - Starting session...
        session:principal-pls - Session is already running.

    Ou seja: para o WAHA, `FAILED` e uma sessao QUE JA ESTA RODANDO — so o motor
    dela morreu. O `start` devolve 201 e nao faz nada, a sessao nunca chega em
    `SCAN_QR_CODE` e o botao Conectar fica girando para sempre. Quem tira dali e
    o `restart`.

    QUEM ESCOLHE O CONSERTO E A COPIA, NAO O ESTADO (regra 16-E-bis) — e por
    isso aqui NUNCA se apaga sozinho: `restart` traz a sessao de volta sem QR
    quando a copia presta, e nao apaga nada quando ela nao presta. Refazer do
    zero (que apaga a copia) continua sendo decisao de gente, na caixinha
    "refazer a sessao" da tela.

    E O RESTART ACONTECE UMA VEZ SO (regra 16-E). Cada reinicio e uma chance de
    perder o vinculo (15-C); insistir em laco seria a aposta mais cara da casa.
    """
    import requests

    base = inst['base']
    cab = {'X-Api-Key': inst['chave']} if inst.get('chave') else {}
    cabj = dict(cab, **{'Content-Type': 'application/json'})
    nome = inst.get('sessao') or dados['sessao']

    status, motor = _estado_da_sessao(base, cab, nome)
    if status is None:
        # 19/09/2026 (regra do dono): no "Refazer a conexao", motor mudo e caso
        # de reconstruir o container — o botao certo e a faxina da cidade.
        _nota(pedido_id, 'subindo', memoria,
              ('o WhatsApp (motor) nao responde — se continuar assim, use "Fazer '
               'a faxina desta cidade", que reconstroi o container')
              if dados['recriar'] else
              'a maquina respondeu, mas o WhatsApp ainda nao — continuo tentando')
        return 'ainda'

    # REFAZER A CONEXAO SO APAGA O QUE NAO VOLTA (19/09/2026, regra do dono).
    # Ate hoje a conferencia "ja esta conectada" vinha ANTES do recriar, e o
    # botao — que so aparece com a sessao conectada — nao fazia nada (provado
    # numa replica da funcao real). Agora: conectada e LENDO o grupo -> fica;
    # conectada sem ler -> reinicia ate 3 vezes, conferindo a leitura depois de
    # cada uma; so se as 3 falharem (ou em FAILED) apaga e refaz para parear.
    if (dados['recriar'] and not memoria.get('recriado')
            and status == 'WORKING' and motor == 'CONNECTED'):
        le = _le_o_grupo(base, cab, nome, cidade)
        if le is not False:
            marcar(pedido_id, ATENDIDO, mensagem='estava conectada e lendo — nada a refazer'
                   if le else 'ja estava conectada (sem grupo para conferir a leitura)')
            anotar_etapa(pedido_id, 'conectado',
                         'a sessao %s esta conectada e lendo o grupo — nada a '
                         'refazer' % nome if le else
                         'a sessao %s ja esta conectada — nao precisou parear' % nome)
            return 'pronto'
        for tentativa in (1, 2, 3):
            _nota(pedido_id, 'criando', memoria,
                  'a sessao %s esta conectada mas nao le o grupo — reiniciando '
                  '(tentativa %d de 3)' % (nome, tentativa))
            try:
                requests.post('%s/api/sessions/%s/restart' % (base, nome),
                              headers=cabj, timeout=40)
            except Exception:
                pass
            for _ in range(30):                     # ate ~150 s para voltar
                time.sleep(5)
                st2, mo2 = _estado_da_sessao(base, cab, nome)
                if st2 == 'WORKING' and mo2 == 'CONNECTED':
                    break
            time.sleep(45)          # a pagina recarrega logo depois de subir (18/09)
            if _le_o_grupo(base, cab, nome, cidade):
                marcar(pedido_id, ATENDIDO,
                       mensagem='voltou a ler depois do reinicio %d' % tentativa)
                anotar_etapa(pedido_id, 'conectado',
                             'a sessao %s voltou a ler o grupo depois do reinicio '
                             '%d — nao precisou apagar nem parear' % (nome, tentativa))
                return 'pronto'
        status, motor = 'NAO_LE', None       # 3 reinicios sem leitura: refaz

    # JA ESTA CONECTADA: nao ha o que parear. Acontece quando alguem clica em
    # Conectar por desencargo, ou quando a sessao voltou sozinha da copia.
    if status == 'WORKING' and motor == 'CONNECTED':
        marcar(pedido_id, ATENDIDO, mensagem='ja estava conectada')
        anotar_etapa(pedido_id, 'conectado',
                     'a sessao %s ja esta conectada — nao precisou parear' % nome)
        return 'pronto'

    if dados['recriar'] and not memoria.get('recriado'):
        # DECISAO EXPLICITA DE GENTE: a caixinha "refazer a sessao do zero".
        # E o unico caminho que apaga a copia (regra 15-A). Uma vez so por
        # pedido (19/09/2026): o laco chama de novo quando o codigo demora, e
        # sem a marca apagaria a sessao recem-criada a cada volta.
        memoria['recriado'] = True
        _nota(pedido_id, 'criando', memoria,
              'refazendo a sessao %s do zero, como voce pediu' % nome)
        requests.delete('%s/api/sessions/%s' % (base, nome), headers=cab, timeout=30)
        # As copias em ARQUIVO desta sessao (no Dell) viram cadaver: a maquina
        # apaga so as dela, na proxima passada (19/09/2026, item 62).
        try:
            pedir_limpeza_de_copias(cidade, nome)
        except Exception as exc:
            logger.warning('Refazer %s: nao consegui pedir a limpeza das copias: %s', nome, exc)
        time.sleep(5)
        requests.post('%s/api/sessions' % base, headers=cabj,
                      json={'name': nome, 'start': True}, timeout=40)
    elif dados['recriar'] and memoria.get('recriado'):
        pass    # ja refeita nesta tentativa: so falta o codigo, logo abaixo
    elif status == 'NAO_EXISTE':
        _nota(pedido_id, 'criando', memoria, 'criando a sessao %s' % nome)
        requests.post('%s/api/sessions' % base, headers=cabj,
                      json={'name': nome, 'start': True}, timeout=40)
    elif status == 'STOPPED':
        _nota(pedido_id, 'criando', memoria, 'ligando a sessao %s' % nome)
        requests.post('%s/api/sessions/%s/start' % (base, nome),
                      headers=cabj, timeout=40)
    elif status == 'FAILED':
        # `FAILED` NAO VOLTA — E REINICIAR SO PIORA (16/08/2026, medido)
        #
        # Este ponto reiniciava a sessao, confiando na copia do Postgres. O log
        # do WAHA, no teste das 22:55, mostra o que de fato acontece:
        #
        #     Session has been logged out!  (LOGOUT)
        #     RemoteAuth.logout -> disconnect -> deleteRemoteSession -> delete
        #     KnexTimeoutError: Timeout acquiring a connection
        #
        # Com a sessao em `FAILED`, o WhatsApp JA DESVINCULOU o aparelho do lado
        # dele. O reinicio nao reconecta: ele faz o `whatsapp-web.js` chamar
        # `logout()`, que PEDE PARA APAGAR a copia (aqui so nao apagou porque o
        # banco deu timeout — a mesma sorte de 09/08). A copia existe e nao
        # serve: e cadaver, nao salvacao.
        #
        # Regra do dono, 16/08: "failed nunca volta; deleta inclusive a sessao
        # salva no Postgres e cria outra do zero". E o caminho seguro AQUI
        # porque este trecho so roda quando ALGUEM clicou em Conectar, com o
        # celular na mao — que e exatamente a condicao que a regra 15-A protege
        # (nao jogar fora copia quando nao ha ninguem para parear).
        if memoria['restart_feito']:
            marcar(pedido_id, FALHOU,
                   mensagem='refiz a sessao do zero e ela caiu de novo')
            anotar_etapa(pedido_id, 'criando',
                         'refiz a sessao do zero e ela caiu de novo. Isso nao e '
                         'mais coisa de tentar de novo: confira se o computador '
                         'da automacao esta saudavel e, se estiver, chame quem '
                         'cuida do sistema.')
            return 'parar'
        memoria['restart_feito'] = True
        _nota(pedido_id, 'criando', memoria,
              'a sessao %s caiu de vez (o WhatsApp desvinculou o aparelho) — '
              'apagando e refazendo do zero para voce parear agora' % nome)
        requests.delete('%s/api/sessions/%s' % (base, nome),
                        headers=cab, timeout=30)
        time.sleep(5)
        requests.post('%s/api/sessions' % base, headers=cabj,
                      json={'name': nome, 'start': True}, timeout=40)
    elif status in ('STARTING', 'SCAN_QR_CODE', 'WORKING'):
        _nota(pedido_id, 'criando', memoria, 'preparando a sessao %s' % nome)

    # ESPERAR A SESSAO FICAR PRONTA PARA PAREAR — com prazo (regra 16-D).
    # Pedir o codigo antes da hora devolve erro e queima a tentativa.
    pronta = False
    for _ in range(20):          # ~60s
        time.sleep(3)
        status, motor = _estado_da_sessao(base, cab, nome)
        if status in ('SCAN_QR_CODE', 'WORKING'):
            pronta = True
            break
    if not pronta:
        _nota(pedido_id, 'criando', memoria,
              'a sessao ainda nao ficou pronta para parear (situacao: %s) — '
              'continuo tentando' % (status or 'sem resposta'))
        return 'ainda'

    anotar_etapa(pedido_id, 'codigo', 'pedindo o codigo ao WhatsApp')
    numero = re.sub(r'\D', '', str(dados['telefone'] or ''))
    try:
        pedido_codigo = requests.post(
            '%s/api/%s/auth/request-code' % (base, nome),
            headers=cabj, json={'phoneNumber': numero}, timeout=60)
    except Exception as exc:
        _nota(pedido_id, 'codigo', memoria,
              'nao consegui pedir o codigo agora (%s) — tento de novo'
              % str(exc)[:80])
        return 'ainda'

    codigo = ''
    if pedido_codigo.status_code in (200, 201):
        try:
            codigo = (pedido_codigo.json() or {}).get('code') or ''
        except ValueError:
            codigo = ''
    if not codigo:
        detalhe = ''
        try:
            detalhe = str((pedido_codigo.json() or {}).get('message') or '')[:120]
        except ValueError:
            detalhe = (pedido_codigo.text or '')[:120]
        _nota(pedido_id, 'codigo', memoria,
              'o WhatsApp nao devolveu o codigo agora (HTTP %s%s) — tento de novo'
              % (pedido_codigo.status_code, (': %s' % detalhe) if detalhe else ''))
        return 'ainda'

    marcar(pedido_id, ATENDIDO, codigo=codigo,
           mensagem='codigo gerado pelo site, sem esperar a rodada')
    anotar_etapa(pedido_id, 'aguardando',
                 'digite o codigo no celular %s' % numero)
    logger.info('Pareamento %s atendido pelo proprio site (sessao %s)',
                pedido_id, nome)

    # E AGORA ESPERA CONECTAR — a tela precisa fechar o ciclo (16/08/2026).
    #
    # Sem isto o acompanhamento ficava em "Esperando voce digitar o codigo" para
    # sempre, mesmo com o WhatsApp ja conectado: quem escrevia a etapa
    # `conectado` era so o `provisionar.py`, na rodada seguinte da maquina.
    # "Pronto" tem de aparecer na hora em que ficou pronto.
    _esperar_conectar(base, cab, nome, pedido_id, fim)
    return 'pronto'


def _etapa_atual(pedido_id):
    """A etapa gravada agora, lida do banco. '' quando nao der para saber."""
    try:
        db.session.remove()
        linha = db.session.execute(sa_text(
            'SELECT etapa FROM sessoes_whatsapp WHERE id = :i'),
            {'i': pedido_id}).fetchone()
        return (linha[0] or '') if linha else ''
    except Exception:
        return ''


def _gravar_conexao_no_registro(pedido_id, nome):
    """Grava no registro de instancias a sessao que ACABOU de conectar.

    A TELA NAO PODE ESPERAR A MAQUINA CONTAR O QUE O SITE JA SABE (15/09/2026).
    O registro (`instancias_waha`) so era escrito pelo relato da maquina, que
    chega na passada seguinte da Fila. Medido em 15/09: o suplente conectou pelo
    site as 14:27:02 e a tela ficou ate 14:35:25 dizendo "Sem noticia da maquina
    — a maquina ainda nao contou que esta sessao existe", sem o botao de limpar
    (que so aparece quando ha relato). O principal parecia diferente so porque
    tinha passado por um relato antes. Mesmo codigo, momentos diferentes.

    Busca pela SESSAO, nunca pelo container (mesma regra de
    `registrar_instancias`): as duas sessoes moram no mesmo container. Onde a
    cidade mora sai de `container_da_cidade`. O relato seguinte da maquina
    confirma ou corrige; aqui so se afirma o que acabou de ser medido
    (WORKING + CONNECTED). Nunca levanta excecao.
    """
    try:
        db.session.remove()
        pedido = db.session.execute(sa_text(
            'SELECT cidade, papel FROM sessoes_whatsapp WHERE id = :i'),
            {'i': pedido_id}).fetchone()
        if not pedido or not pedido[0]:
            return
        cidade, papel = pedido[0], ((pedido[1] or '').strip().lower() or None)
        casa = container_da_cidade(cidade)
        if not casa:
            return
        dados = {'c': cidade, 'pa': papel, 'cont': casa['container'], 's': nome,
                 'h': casa['host'], 'pt': casa['porta'], 'k': casa.get('chave'),
                 'em': datetime.now()}
        existente = db.session.execute(sa_text(
            'SELECT id FROM instancias_waha WHERE cidade = :c AND lower(sessao) = :s'),
            {'c': cidade, 's': nome.lower()}).fetchone()
        if existente:
            db.session.execute(sa_text("""
                UPDATE instancias_waha
                SET papel = COALESCE(:pa, papel), container = :cont, host = :h,
                    porta = :pt, chave = COALESCE(:k, chave),
                    status = 'WORKING', motor = 'CONNECTED', visto_em = :em
                WHERE id = :i
            """), dict(dados, i=existente[0]))
        else:
            db.session.execute(sa_text("""
                INSERT INTO instancias_waha
                    (cidade, papel, container, sessao, host, porta, chave,
                     status, motor, alcancavel, visto_em)
                VALUES (:c, :pa, :cont, :s, :h, :pt, :k,
                        'WORKING', 'CONNECTED', 1, :em)
            """), dados)
        db.session.commit()
        logger.info('Registro: sessao %s de %s gravada como conectada (pedido %s).',
                    nome, cidade, pedido_id)
    except Exception as exc:
        try:
            db.session.rollback()
        except Exception:
            pass
        logger.warning('Nao consegui gravar a conexao de %s no registro: %s', nome, exc)


def _esperar_conectar(base, cab, nome, pedido_id, fim):
    """Acompanha ate WORKING+CONNECTED. Nunca levanta excecao."""
    try:
        while time.time() < fim:
            time.sleep(10)
            status, motor = _estado_da_sessao(base, cab, nome)
            if status == 'WORKING' and motor == 'CONNECTED':
                # UM "CONECTOU" SO. Dois vigias podem estar olhando a mesma
                # sessao (a insistencia do clique e a confirmacao do vigia, que
                # passa de 15 em 15 min) — no primeiro pareamento que deu certo
                # a linha "o WhatsApp conectou" saiu duas vezes no historico.
                # Escrever duas vezes a mesma verdade e ruido, e ruido esconde
                # o que importa.
                if _etapa_atual(pedido_id) != 'conectado':
                    anotar_etapa(pedido_id, 'conectado',
                                 'o WhatsApp %s conectou' % nome)
                    logger.info('Pedido %s: sessao %s conectada.', pedido_id, nome)
                _gravar_conexao_no_registro(pedido_id, nome)
                return
            if status == 'FAILED':
                anotar_etapa(pedido_id, 'aguardando',
                             'a sessao caiu enquanto esperava o codigo ser '
                             'digitado — peca o codigo de novo nesta tela')
                return
    except Exception as exc:
        logger.debug('Acompanhamento da conexao do pedido %s parou: %s',
                     pedido_id, exc)


# ===================== QUEM ESTA ESPERANDO PAREAMENTO =====================

# Por quanto tempo um pedido de pareamento continua "de agora" para quem toma
# decisao por causa dele: a maquina (subir a stack) e a parada limpa (adiar o
# desligamento). Depois disso ele continua na tela e na fila da maquina, mas
# para de mandar em ninguem.
JANELA_PAREAMENTO_ATIVO_MIN = 60


def pareamento_aberto(cidade=None):
    """Ha pedido de pareamento esperando? (bool, quantos, ha quanto tempo).

    POR QUE PRECISOU EXISTIR (16/08/2026)
    O `MoveMentes_Fila.ps1` pergunta ao site de 15 em 15 minutos — e tambem no
    instante em que a maquina liga — se ha mensagem na fila ou pedido de
    ranking; se nao ha, ele sai SEM tocar no WSL. So que quem atende pareamento
    e o `provisionar.py`, que roda dentro do `executar.sh`, que so e chamado
    ali. Com a fila vazia, o pedido de Conectar ficava esperando os horarios do
    ranking: era por isso que o botao "nunca funcionava" com a maquina
    desligada. Agora este contador viaja junto com a fila.
    """
    ensure_schema()
    # SO CONTA O QUE AINDA E DE AGORA (regra 16-D: esperar sem prazo, nao).
    #
    # Pedido pendente nao caduca sozinho no banco — e nem deve, porque a tela
    # continua mostrando ate alguem parar ou refazer. Mas quem PERGUNTA aqui e
    # a maquina, para decidir se sobe a stack e se pode dormir: um pedido de
    # tres dias atras faria a maquina subir de 15 em 15 minutos, para sempre, e
    # nunca mais dormir. Uma hora e generoso — o passo "esperando voce digitar o
    # codigo" tem 10 minutos de prazo.
    # O CODIGO ENTREGUE NAO ENCERRA O PAREAMENTO (16/08/2026). Entre "o codigo
    # saiu" e "o WhatsApp conectou" ainda falta a pessoa digitar — e e
    # exatamente ali que a maquina nao pode dormir nem parar de rodar. Por isso
    # entra tambem o pedido `atendido` que ainda nao chegou em `conectado`.
    janela = datetime.now() - timedelta(minutes=JANELA_PAREAMENTO_ATIVO_MIN)
    sql = ("SELECT COUNT(*), MIN(pedido_em) FROM sessoes_whatsapp "
           "WHERE pedido_em >= :desde AND ("
           "  situacao IN (:p, :e)"
           "  OR (situacao = :a AND (etapa IS NULL OR etapa <> 'conectado'))"
           ")")
    parametros = {'p': PENDENTE, 'e': EM_ANDAMENTO, 'a': ATENDIDO,
                  'desde': janela}
    if cidade:
        sql += ' AND cidade = :c'
        parametros['c'] = cidade
    linha = db.session.execute(sa_text(sql), parametros).fetchone()
    quantos = int(linha[0] or 0) if linha else 0
    desde = linha[1] if linha else None
    if isinstance(desde, str):
        try:
            desde = datetime.fromisoformat(desde)
        except ValueError:
            desde = None
    segundos = int((datetime.now() - desde).total_seconds()) if desde else 0
    return {'tem': quantos > 0, 'quantos': quantos,
            'ha_quanto_tempo': tempo_humano(segundos) if quantos else None}


def pedidos_abertos():
    """(id, cidade) de cada pedido esperando — para o vigia retomar a insistencia.

    A insistencia mora numa thread do worker que atendeu o clique. Worker
    reciclado no meio (e ele recicla: `py-autoreload` reage a cada arquivo salvo
    em producao) levava a insistencia junto, em silencio. O vigia, que passa de
    15 em 15 minutos, relanca.
    """
    ensure_schema()
    # Mesma janela do `pareamento_aberto`: retomar a insistencia de um pedido de
    # ontem seria mandar Wake-on-LAN para sempre, de 15 em 15 minutos.
    janela = datetime.now() - timedelta(minutes=JANELA_PAREAMENTO_ATIVO_MIN)
    linhas = db.session.execute(sa_text(
        "SELECT id, cidade FROM sessoes_whatsapp WHERE situacao IN (:p, :e) "
        "AND pedido_em >= :desde ORDER BY pedido_em ASC LIMIT 10"),
        {'p': PENDENTE, 'e': EM_ANDAMENTO, 'desde': janela}).fetchall()
    return [(int(r[0]), r[1]) for r in linhas]


def pedidos_para_confirmar():
    """Codigo entregue, e ninguem confirmou que conectou.

    O BURACO QUE ISTO FECHA (16/08/2026)
    Quando quem pareia e o SITE, o pedido vira `atendido` no instante em que o
    codigo sai — e a etapa `conectado` era escrita so pelo `provisionar.py`, na
    rodada seguinte da maquina. Com a fila vazia essa rodada podia nem
    acontecer: a tela ficava em "Esperando voce digitar o codigo" com o WhatsApp
    ja conectado. "Pronto" tem de aparecer quando ficou pronto — senao a pessoa
    clica de novo, e clicar de novo no meio de um pareamento e o que mais
    estraga pareamento.
    """
    ensure_schema()
    janela = datetime.now() - timedelta(minutes=JANELA_PAREAMENTO_ATIVO_MIN)
    linhas = db.session.execute(sa_text(
        "SELECT id, cidade FROM sessoes_whatsapp "
        "WHERE situacao = :a AND (etapa IS NULL OR etapa <> 'conectado') "
        "AND atendido_em IS NOT NULL AND atendido_em >= :desde "
        "ORDER BY atendido_em ASC LIMIT 10"),
        {'a': ATENDIDO, 'desde': janela}).fetchall()
    return [(int(r[0]), r[1]) for r in linhas]


# Quanto tempo o site fica olhando se a pessoa digitou o codigo, por rodada de
# confirmacao. O vigia passa de 15 em 15 minutos e recomeca enquanto valer a
# janela — entao "acabou o tempo" aqui nao e desistir, e so soltar o worker.
PRAZO_CONFIRMACAO_SEG = 600


def confirmar_conexao(app, cidade, pedido_id):
    """Olha a sessao ate ela conectar e escreve `conectado` na tela.

    Nao pede codigo, nao cria nada, nao reinicia nada: so olha e conta. E o
    fecho do ciclo do pareamento feito pelo site.
    """
    import time as _time

    with app.app_context():
        marca = ('confirmar', pedido_id)
        if marca in _EM_CURSO:
            return
        _EM_CURSO.add(marca)
        try:
            linha = db.session.execute(sa_text(
                'SELECT papel, sessao, situacao, etapa FROM sessoes_whatsapp '
                'WHERE id = :i'), {'i': pedido_id}).fetchone()
            if not linha or linha[2] != ATENDIDO or linha[3] == 'conectado':
                return
            inst = instancia_do_papel(cidade, linha[0] or '')
            if not inst or not inst.get('base') or not _maquina_responde(inst):
                return          # maquina fora do ar: nao ha o que confirmar
            cab = {'X-Api-Key': inst['chave']} if inst.get('chave') else {}
            _esperar_conectar(inst['base'], cab, inst.get('sessao') or linha[1],
                              pedido_id, _time.time() + PRAZO_CONFIRMACAO_SEG)
        except Exception as exc:
            logger.warning('Confirmacao do pareamento %s falhou: %s',
                           pedido_id, exc)
        finally:
            _EM_CURSO.discard(marca)


def _entregar_codigo(pedido_id, codigo):
    """Faz o código chegar a quem vai digitar. Ordem da regra 17-A.

    1. **WhatsApp de outra sessão viva da cidade.** Quem recebe está com o
       CELULAR na mão, no meio de um pareamento — chegar ali é um toque, e o
       e-mail exige abrir outro aplicativo. Para parear o suplente, sai pela
       sessão do principal; para parear o principal, pela do suplente.
    2. **E-mail**, quando nenhuma outra sessão está de pé. É o único canal que
       não depende da peça quebrada (regra 21), e por isso nunca pode faltar.
    3. **QR** vai JUNTO no e-mail, como reforço — nunca como caminho principal:
       celular nenhum escaneia a si mesmo, então o QR só serve se a pessoa
       puder abri-lo em OUTRA tela. O e-mail diz isso com todas as letras.
    """
    enviado_por_whatsapp = _codigo_pelo_whatsapp(pedido_id, codigo)
    # O e-mail sai de qualquer jeito: ele leva o QR, que é o plano B de quem
    # digitou o código e não funcionou. Mandar os dois custa um e-mail.
    email_ok = _enviar_codigo_por_email(pedido_id, codigo)
    return enviado_por_whatsapp or email_ok


def _codigo_pelo_whatsapp(pedido_id, codigo):
    """Manda o código pela sessão de OUTRO papel da mesma cidade.

    Nunca pela sessão que está sendo pareada — ela não existe ainda — e nunca
    para o número da própria sessão que envia (regra 20, que dá HTTP 500).
    """
    import requests
    from app.filamensagens import _chat_id

    try:
        linha = db.session.execute(sa_text(
            'SELECT cidade, telefone, papel FROM sessoes_whatsapp WHERE id = :i'),
            {'i': pedido_id}).fetchone()
    except Exception as exc:
        logger.debug('Sem pedido %s para avisar por WhatsApp: %s', pedido_id, exc)
        return False
    if not linha:
        return False
    cidade, telefone, papel = linha[0], linha[1], (linha[2] or '')
    if not telefone:
        return False

    mensageira = None
    for item in instancias(cidade):
        if not item['pronta'] or not item['alcancavel']:
            continue
        if mesmo_nome(item['papel'] or '', papel):
            continue       # é justamente a que está sendo pareada
        # NUNCA MANDAR PARA O PROPRIO NUMERO DA SESSAO (regra 20/22-D):
        # comparar por país+DDD e os 8 últimos, porque o WAHA devolve o número
        # sem o nono dígito e o cadastro guarda com.
        if item['numero'] and item['numero'][-8:] == re.sub(r'\D', '', telefone)[-8:]:
            continue
        mensageira = instancia_do_papel(cidade, item['papel'])
        if mensageira:
            break
    if not mensageira:
        return False

    cab = {'X-Api-Key': mensageira['chave']} if mensageira.get('chave') else {}
    chat = _chat_id(mensageira['base'], cab, mensageira['sessao'], telefone)
    if not chat:
        logger.info('Código do pedido %s: %s não existe no WhatsApp da sessão %s',
                    pedido_id, telefone, mensageira['sessao'])
        return False

    texto = ('*Código para conectar o WhatsApp ao MoveMentes*\n\n'
             '*%s*\n\n'
             'No celular: WhatsApp → Aparelhos conectados → Conectar um '
             'aparelho → _Conectar com número de telefone_ → digite o código.\n'
             'Ele vale cerca de 1 minuto; se perder a janela, peça outro na '
             'tela — não tem limite.' % codigo)
    try:
        resp = requests.post(
            '%s/api/sendText' % mensageira['base'],
            headers=dict(cab, **{'Content-Type': 'application/json'}),
            json={'session': mensageira['sessao'], 'chatId': chat, 'text': texto},
            timeout=30)
    except Exception as exc:
        logger.warning('Código do pedido %s não saiu pelo WhatsApp: %s',
                       pedido_id, str(exc)[:120])
        return False
    if resp.status_code in (200, 201):
        logger.info('Código do pedido %s entregue pelo WhatsApp da sessão %s',
                    pedido_id, mensageira['sessao'])
        _registrar_passo(pedido_id, 'codigo',
                         'código enviado pelo WhatsApp de %s' % mensageira['sessao'])
        return True
    logger.warning('Código do pedido %s: o WhatsApp respondeu HTTP %s',
                   pedido_id, resp.status_code)
    return False


def _enviar_codigo_por_email(pedido_id, codigo):
    """Entrega o codigo de pareamento AO DONO DO NUMERO. Nunca derruba o fluxo.

    O DESTINATARIO E O E-MAIL DO CADASTRO, NAO O DE QUEM CLICOU (16/08/2026)

    Ate hoje o codigo ia para o `pedido_por` — o e-mail de quem estava logado no
    momento do clique. Duas coisas erradas nisso:

    1. Quem tem o celular na mao e o DONO do numero. O master que pareia o
       suplente recebia na propria caixa um codigo que so serve no aparelho da
       outra pessoa, e a mensagem da tela ja prometia o contrario.
    2. Regra do dono, 16/08: **o e-mail que recebe o codigo tem de ser o do
       cadastro daquele usuario**. Se o destino pudesse ser escolhido no
       caminho, quem entrasse na conta de um admin conectaria o WAHA a um numero
       que nao e o dele. O cadastro e a unica fonte que exige o master para
       mudar — o pedido nao carrega e-mail digitado em lugar nenhum.

    Sem e-mail no cadastro o codigo NAO sai por aqui, e o motivo fica no
    historico do pedido: mandar "para alguem" seria justamente o furo acima.
    (O caminho antigo, de uma sessao por cidade e sem usuario dono, continua
    caindo no `pedido_por` — la nao existe cadastro de quem pareia.)
    """
    from app import correio

    try:
        linha = db.session.execute(sa_text("""
            SELECT cidade, pedido_por, telefone, usuario_id
            FROM sessoes_whatsapp WHERE id = :i
        """), {'i': pedido_id}).fetchone()
    except Exception as exc:
        logger.warning('Nao consegui ler o pedido %s para avisar por e-mail: %s',
                       pedido_id, exc)
        return False

    if not linha:
        return False
    cidade, quem_pediu, telefone = linha[0], (linha[1] or '').strip(), linha[2]
    usuario_id = linha[3]

    para = ''
    if usuario_id:
        try:
            from app.models import Usuario
            dono = Usuario.query.get(int(usuario_id))
            para = (getattr(dono, 'email', '') or '').strip()
        except Exception as exc:
            logger.warning('Pedido %s: nao consegui ler o cadastro do dono: %s',
                           pedido_id, exc)
            para = ''
    else:
        para = quem_pediu

    if '@' not in para:
        logger.warning('Pedido %s: o dono do numero nao tem e-mail no cadastro — '
                       'codigo NAO enviado por e-mail.', pedido_id)
        try:
            _registrar_passo(pedido_id, 'codigo',
                             'o codigo NAO foi por e-mail: o usuario dono deste '
                             'numero esta sem e-mail no cadastro. Cadastre o '
                             'e-mail dele em Usuarios e peca o codigo de novo.')
            db.session.commit()
        except Exception:
            pass
        return False

    # O QR VAI JUNTO, COMO REFORÇO (regra 17-A). Link absoluto, porque quem
    # abre está no cliente de e-mail, fora do site. Falhar aqui não pode
    # impedir o código de sair — ele é o caminho principal.
    qr_url = None
    try:
        from flask import url_for
        qr_url = url_for('admin.qr_pareamento', pedido_id=pedido_id,
                         token=token_qr(pedido_id), _external=True)
    except Exception as exc:
        logger.debug('Sem link de QR para o pedido %s: %s', pedido_id, exc)

    try:
        from app.telefone import bonito
        ok, detalhe = correio.enviar_codigo_pareamento(
            para=para, cidade=cidade, codigo=codigo, telefone=bonito(telefone),
            qr_url=qr_url)
    except Exception as exc:
        logger.warning('Pedido %s: falha ao enviar o e-mail do codigo: %s',
                       pedido_id, exc)
        return False

    if ok:
        logger.info('Pedido %s: codigo enviado por e-mail para %s', pedido_id, para)
    else:
        logger.warning('Pedido %s: e-mail do codigo NAO saiu (%s)', pedido_id, detalhe)
    return ok


# ===================== O QUE A MAQUINA TEM DE FATO =====================

def registrar_instancias(cidade, itens, host=None, por='automacao'):
    """A maquina conta quais instancias WAHA existem. Devolve (gravadas, avisos).

    QUEM SABE E A MAQUINA, NAO O CAMPO DE TEXTO (14/08/2026)
    Endereco, porta, chave e nome de sessao eram digitados na tela. Digitar
    quatro coisas certas e mante-las em dia enquanto containers sobem e descem
    e trabalho que ninguem faz — e quando uma delas ficou errada (a chave), o
    site passou a ver "automacao quebrada" com o WhatsApp funcionando.

    A CHAVE PASSA A VIR JUNTO, e isso muda uma decisao antiga do agente
    ("a chave fica no Dell e nao volta para o site"). O motivo daquela decisao
    era que so o agente falava com o WAHA; hoje o SITE tambem fala — entrega
    imediata da fila, vigia, tela de sessao. Chave que o site precisa e nao tem
    vira campo digitado a mao, que foi o defeito. Ela viaja pelo mesmo HTTPS
    autenticado que ja carrega o token da automacao e mora no cofre, que nunca
    devolve valor para a tela.
    """
    ensure_schema()
    agora = datetime.now()
    gravadas, avisos = 0, []
    # O que cada container relatou AGORA — a base para saber o que sumiu.
    vistas_por_container = {}
    for item in (itens or []):
        container = (item.get('container') or '').strip()
        if not container:
            avisos.append('instância sem nome de container — ignorada')
            continue
        # A SESSAO E' A IDENTIDADE (08/09/2026, fusao por cidade). Sem ela nao
        # da para saber de QUEM e' a linha: depois da fusao, duas sessoes
        # dividem o mesmo container, entao o container deixou de identificar.
        # Relato sem sessao seria uma linha que colide com a irma.
        sessao_item = (item.get('sessao') or '').strip()
        if not sessao_item:
            avisos.append('instância %s relatada sem sessão — ignorada' % container)
            continue
        porta = item.get('porta')
        try:
            porta = int(porta) if porta else None
        except (TypeError, ValueError):
            porta = None
        dados = {
            'c': cidade, 'cont': container,
            'pa': (item.get('papel') or '').strip().lower() or None,
            's': (item.get('sessao') or '').strip() or None,
            'h': (item.get('host') or host or '').strip() or None,
            'pt': porta,
            'k': (item.get('chave') or '').strip() or None,
            'st': (item.get('status') or '').strip() or None,
            'mo': (item.get('motor') or '').strip() or None,
            'nu': re.sub(r'\D', '', str(item.get('numero') or '')) or None,
            'al': 0 if item.get('alcancavel') is False else 1,
            'em': agora,
        }
        # Procura pela SESSAO, nao pelo container (08/09/2026). Buscar por
        # container faria as duas sessoes irmas encontrarem a mesma linha e
        # sobrescreverem uma a outra a cada relato.
        existente = db.session.execute(sa_text(
            'SELECT id FROM instancias_waha WHERE cidade = :c AND sessao = :s'),
            {'c': cidade, 's': sessao_item}).fetchone()
        if existente:
            # Chave vazia no relato NAO apaga a que ja esta guardada: relato
            # parcial e comum (o container pode estar parado no momento) e
            # perder a credencial por causa disso deixaria o site cego de novo.
            # `container` PASSOU A SER ATUALIZADO (08/09/2026). Antes ele era a
            # chave da busca, entao nunca mudava -- e nao precisava mesmo. Com
            # a fusao a sessao MUDA DE CASA (`suplente-pls` saiu do
            # `auto-waha-suplente-pls` para o `auto-waha-principal-pls`), e uma
            # linha que guarda o container velho manda o site bater num
            # container que nao existe mais.
            db.session.execute(sa_text("""
                UPDATE instancias_waha
                SET papel = :pa, sessao = COALESCE(:s, sessao),
                    container = COALESCE(:cont, container),
                    host = COALESCE(:h, host), porta = COALESCE(:pt, porta),
                    chave = COALESCE(:k, chave), status = :st, motor = :mo,
                    numero = COALESCE(:nu, numero), alcancavel = :al,
                    visto_em = :em
                WHERE id = :i
            """), dict(dados, i=existente[0]))
        else:
            db.session.execute(sa_text("""
                INSERT INTO instancias_waha
                    (cidade, papel, container, sessao, host, porta, chave,
                     status, motor, numero, alcancavel, visto_em)
                VALUES (:c, :pa, :cont, :s, :h, :pt, :k, :st, :mo, :nu, :al, :em)
            """), dados)
        gravadas += 1
        vistas_por_container.setdefault(container, set()).add(sessao_item.lower())

    # SESSÃO QUE SUMIU TEM DE SUMIR DO REGISTRO TAMBÉM (08/09/2026).
    #
    # Este relato sempre foi um `upsert`: gravava o que chegou e nunca tirava o
    # que deixou de chegar. Resultado medido no mesmo dia: a sessão
    # `suplente-pls` foi apagada do WAHA e o site continuou mostrando ela como
    # **WORKING** — um registro que afirma, com todas as letras, algo que não
    # existe mais. Foi esse tipo de mentira que fez o dono clicar em "Limpar"
    # esperando desconectar, e que me levou a diagnosticar o site como fora do
    # ar quando ele estava de pé.
    #
    # A GUARDA CONTRA O ESTRAGO OPOSTO: só se apaga linha de um container que
    # RELATOU ALGUMA SESSÃO agora. Relato vazio significa "não consegui
    # perguntar" (o WAHA pode estar subindo), e "não sei" nunca pode virar
    # "então não existe" — foi assim que uma faxina quase apagou sessão viva em
    # 05/08. Container que não apareceu no relato não é tocado.
    for cont, sessoes_vistas in vistas_por_container.items():
        if not sessoes_vistas:
            continue
        linhas = db.session.execute(sa_text(
            'SELECT id, sessao FROM instancias_waha '
            'WHERE cidade = :c AND container = :cont'),
            {'c': cidade, 'cont': cont}).fetchall()
        for linha_id, sessao_guardada in linhas:
            if (sessao_guardada or '').lower() in sessoes_vistas:
                continue
            db.session.execute(sa_text(
                'DELETE FROM instancias_waha WHERE id = :i'), {'i': linha_id})
            avisos.append('a sessão "%s" não existe mais em %s — tirei do registro'
                          % (sessao_guardada, cont))
            logger.info('Sessão %s sumiu de %s (%s) — registro removido',
                        sessao_guardada, cont, cidade)

    db.session.commit()

    if gravadas:
        avisos.extend(_alinhar_cofre(cidade, por=por))
    logger.info('Instâncias de %s registradas pela máquina: %s (%s)',
                cidade, gravadas, por)
    return gravadas, avisos


def apagar_sessao(cidade, nome, por='', confirmado_working=False):
    """Apaga UMA sessão no WAHA e esquece o registro dela. (ok, recado).

    POR QUE ISTO EXISTE (08/09/2026)
    A tela tinha `salvar`, `provisionar` e `definir_papel` — e nenhuma forma de
    APAGAR. O dono tirou uma sessão pelo site e ela continuou WORKING dentro do
    WAHA, consumindo memória, enquanto a tela seguia dizendo "conectada" (e
    dizia a verdade: ela estava mesmo). Registro que some sem a sessão sumir é
    pior que não ter botão — cria uma sessão que o site não enxerga e ninguém
    limpa.

    O QUE ESTE BOTÃO É, E O QUE ELE NÃO É
    A tela já tinha dois vizinhos, e nenhum servia:
      * **Limpar** (`parar_pedido`) some com o REGISTRO do pedido e não
        desconecta nada — foi clicando nele que a sessão sumiu da tela e
        continuou viva no WAHA, gastando memória;
      * **Refazer a conexão** apaga a sessão **e recria**, então sempre cobra
        um pareamento.
    Faltava apagar **sem recriar**: é o único caminho que devolve a memória de
    um número que não se quer mais por ora.

    AS GUARDAS, E O QUE CADA UMA EVITA
      * O estado vem do **WAHA agora**, nunca do que o site tem guardado: o
        registro do site é uma foto, e foto envelhece — foi assim que a tela
        passou a mostrar "conectada" para uma sessão que o site já não tinha.
      * Apagar uma sessão `WORKING` **é permitido**, porque é exatamente o caso
        de "quero enxugar a RAM até a próxima migração". Mas custa um
        pareamento e a cidade fica sem aquele número até lá — por isso quem
        chama tem de mandar `confirmado_working=True`, e a tela exige o nome
        digitado. Proibir seria transformar o dono em refém do próprio sistema.
      * Sessão que o WAHA não conhece mais só limpa o registro — não é erro,
        é o próprio caso que este botão veio resolver.

    O BANCO DA SESSÃO NÃO É DERRUBADO DAQUI, de propósito: ele vive no Postgres
    do Dell, e um site que apaga banco em outra máquina é um acoplamento que
    cobra caro no dia em que a rede oscilar. Ele fica para a faxina, que roda
    onde o banco mora.
    """
    import requests
    nome = (nome or '').strip()
    if not nome:
        return False, 'Sem nome de sessão para apagar.'

    base = (secretstore.get(secretstore.WAHA_URL, scope=cidade) or '').strip().rstrip('/')
    if not base:
        return False, 'Esta cidade não tem endereço de WhatsApp cadastrado.'
    chave = secretstore.get(secretstore.WAHA_API_KEY, scope=cidade) or ''
    cab = {'X-Api-Key': chave} if chave else {}

    # 1) O QUE O WAHA DIZ AGORA. Sem isto, apagar seria um tiro no escuro.
    estado, real = None, nome
    try:
        resp = requests.get('%s/api/sessions' % base, params={'all': 'true'},
                            headers=cab, timeout=10)
        if resp.status_code == 200:
            for s in (resp.json() or []):
                if isinstance(s, dict) and (s.get('name') or '').lower() == nome.lower():
                    estado = s.get('status')
                    # O NOME VAI COMO O SERVIDOR ESCREVEU: a rota distingue
                    # maiúscula de minúscula e devolve 404 para a forma errada.
                    real = s.get('name') or nome
                    break
    except Exception as exc:
        # CONTAINER PARADO: O PEDIDO FICA GUARDADO (18/09/2026, plano A2/E2).
        # Regra 19: pedido que depende da máquina não se perde. Fica no cofre
        # da cidade e se cumpre quando a máquina contar que subiu
        # (`/api/instancias` → `cumprir_apagar_pendente`).
        guardado = json.dumps({'sessao': nome, 'por': por or '',
                               'working': bool(confirmado_working),
                               'em': datetime.now().strftime('%d/%m %H:%M')})
        if secretstore.set_value(_APAGAR_PENDENTE, guardado, scope=cidade,
                                 updated_by=por or 'site'):
            logger.info('Apagar sessão %s de %s GUARDADO (WhatsApp parado: %s)',
                        nome, cidade, exc)
            return True, ('O WhatsApp da cidade está parado agora. O pedido de '
                          'apagar a sessão "%s" ficou guardado e será cumprido '
                          'assim que a máquina subir o WhatsApp.' % nome)
        return False, 'Não consegui falar com o WhatsApp da cidade: %s' % exc

    if estado == 'WORKING' and not confirmado_working:
        # Não é proibição: é a única parada obrigatória do caminho. Quem
        # confirma sabe que vai ter de parear de novo, com o celular na mão.
        return False, ('A sessão "%s" está FUNCIONANDO agora. Apagar libera a '
                       'memória dela, mas este número sai do ar até você parear '
                       'de novo. Marque a confirmação para prosseguir.' % real)

    # 2) APAGAR NO WAHA. Sessão que ele não conhece cai direto no passo 3.
    if estado is not None:
        try:
            r = requests.delete('%s/api/sessions/%s' % (base, real),
                                headers=cab, timeout=30)
            if r.status_code >= 400 and r.status_code != 404:
                return False, ('O WhatsApp recusou apagar a sessão (HTTP %s).'
                               % r.status_code)
        except Exception as exc:
            return False, 'Não consegui apagar a sessão no WhatsApp: %s' % exc

    # 3) ESQUECER O REGISTRO. Só depois de a sessão ter saído de verdade — a
    # ordem importa: apagar o registro primeiro deixaria a sessão órfã se o
    # passo 2 falhasse, que é exatamente o defeito que se está consertando.
    try:
        # APAGAR A SESSAO NAO APAGA A INSTANCIA (15/09/2026).
        # A linha de `instancias_waha` guarda a sessao E o endereco da
        # instancia (host, porta, chave). O DELETE levava os dois: em 14/09
        # 23:26:34 o dono limpou a unica sessao da cidade e, 23 s depois, o
        # Conectar respondeu "ainda nao sei em que instancia a cidade roda o
        # WhatsApp" — com o container de pe na 3000. A tela voltou a "Ligando"
        # e o botao de limpar sumiu. Um container por cidade (09/09): a
        # instancia continua conhecida; so a sessao sai do registro.
        db.session.execute(sa_text(
            "UPDATE instancias_waha SET status = 'SEM_SESSAO', motor = NULL "
            'WHERE cidade = :c AND lower(sessao) = :s'),
            {'c': cidade, 's': nome.lower()})
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning('Sessão %s saiu do WAHA mas o registro ficou: %s', real, exc)
        return True, ('A sessão "%s" foi apagada no WhatsApp, mas o registro no '
                      'site não saiu. A próxima passada da automação corrige.' % real)

    logger.info('Sessão %s de %s apagada por %s (estava em %s)',
                real, cidade, por or '?', estado or 'não existia no WAHA')
    if estado is None:
        return True, ('A sessão "%s" já não existia no WhatsApp — o registro do '
                      'site foi limpo.' % real)
    return True, ('Sessão "%s" apagada (estava em %s). Para usar este número de '
                  'novo, pareie pela tela.' % (real, estado))


# Pedido de apagar sessão feito com o WhatsApp parado (18/09/2026, plano A2/E2).
_APAGAR_PENDENTE = 'whatsapp_apagar_pendente'


def apagar_pendente(cidade):
    """O pedido guardado de apagar sessão desta cidade (dict) ou None."""
    bruto = secretstore.get(_APAGAR_PENDENTE, scope=cidade)
    if not bruto:
        return None
    try:
        return json.loads(bruto)
    except ValueError:
        return None


def cumprir_apagar_pendente(cidade):
    """Cumpre o pedido guardado, se houver. Devolve o recado ou None.

    Chamado quando a máquina conta que subiu (`/api/instancias`). Só esquece
    o pedido se o WhatsApp respondeu; parado de novo, ele volta a ficar
    guardado pelo próprio `apagar_sessao`.
    """
    pedido = apagar_pendente(cidade)
    if not pedido:
        return None
    secretstore.delete(_APAGAR_PENDENTE, scope=cidade)
    ok, recado = apagar_sessao(cidade, pedido.get('sessao'),
                               por=pedido.get('por') or 'pedido guardado',
                               confirmado_working=bool(pedido.get('working')))
    logger.info('Pedido guardado de apagar sessão (%s, %s): %s',
                cidade, pedido.get('em'), recado)
    return recado


# ==================== FAXINA DA CIDADE (18/09/2026, plano A10 + item 5) ====================
# O botão pede; quem executa é a máquina (`scripts/faxina_cidade.py`), na
# passada da fila. O ALCANCE sai daqui, da cidade de quem clicou (regra 3):
# o container da cidade e o sufixo dele — a máquina não escolhe cidade.
_FAXINA_PEDIDA = 'whatsapp_faxina_pedida'
_FAXINA_RESULTADO = 'whatsapp_faxina_resultado'


def _pareamento_em_curso(cidade):
    """Há pedido de pareamento desta cidade nos últimos 20 min?

    Só os recentes: o pedido 1 de 01/08 continua `em_andamento` no banco há
    semanas (plano, item 9) e travaria a faxina para sempre.
    """
    ensure_schema()
    limite = datetime.now() - timedelta(minutes=20)   # mesmo tipo do INSERT
    r = db.session.execute(sa_text(
        'SELECT count(*) FROM sessoes_whatsapp WHERE cidade = :c '
        'AND situacao IN (:p, :e, :a) AND pedido_em >= :l'),
        {'c': cidade, 'p': PENDENTE, 'e': EM_ANDAMENTO, 'a': ATENDIDO,
         'l': limite}).scalar()
    return bool(r)


def pedir_faxina(cidade, por=''):
    """Registra o pedido de faxina da cidade. (ok, recado)."""
    casa = container_da_cidade(cidade)
    if not casa or not casa.get('container'):
        return False, ('O site ainda não sabe qual é o container desta cidade — '
                       'nada a limpar.')
    try:
        em_curso = _pareamento_em_curso(cidade)
    except Exception as exc:
        logger.warning('faxina: nao consegui ver se ha pareamento em curso: %s', exc)
        return False, ('Não consegui conferir se há pareamento em andamento — '
                       'faxina recusada por segurança. Tente de novo.')
    if em_curso:
        return False, ('Há um pareamento em andamento nesta cidade. Espere ele '
                       'terminar: a faxina poderia apagar a sessão que está '
                       'sendo conectada.')
    container = casa['container'].strip()
    sufixo = container.split('auto-waha-', 1)[-1] if container.startswith('auto-waha-') else ''
    if not sufixo:
        return False, ('O nome do container (%s) não diz de que cidade ele é — '
                       'faxina recusada para não encostar em outra cidade.' % container)
    pedido = json.dumps({'container': container, 'sufixo': sufixo,
                         'por': por or '', 'em': datetime.now().isoformat(timespec='seconds')})
    if not secretstore.set_value(_FAXINA_PEDIDA, pedido, scope=cidade,
                                 updated_by=por or 'site'):
        return False, 'Não consegui guardar o pedido de faxina.'
    logger.info('Faxina pedida em %s por %s (container %s)', cidade, por, container)
    return True, ('Faxina pedida para o container %s. A máquina executa na '
                  'próxima passada (em poucos minutos) e o resultado aparece '
                  'nesta tela.' % container)


def faxina_pedida(cidade):
    """O pedido de faxina em aberto (dict) ou None."""
    bruto = secretstore.get(_FAXINA_PEDIDA, scope=cidade)
    try:
        return json.loads(bruto) if bruto else None
    except ValueError:
        return None


def registrar_faxina(cidade, linhas):
    """A máquina conta o que fez: guarda o resultado e fecha o pedido."""
    texto = json.dumps({'em': datetime.now().strftime('%d/%m %H:%M'),
                        'linhas': [str(x)[:300] for x in (linhas or [])][:60]})
    secretstore.set_value(_FAXINA_RESULTADO, texto, scope=cidade,
                          updated_by='maquina-da-automacao')
    secretstore.delete(_FAXINA_PEDIDA, scope=cidade)
    logger.info('Faxina de %s concluída: %s', cidade, ' | '.join(map(str, linhas or []))[:500])


# ==================== VERSOES (19/09/2026, plano item 67) ====================
# So o master supremo ve e decide. A maquina relata (1x por dia) e executa a
# troca do WAHA que ele escolheu (`scripts/versoes.py`).
_VERSOES = 'versoes_relato'
_VERSAO_PEDIDA = 'versao_waha_pedida'
_VERSAO_RESULTADO = 'versao_waha_resultado'


def versoes(cidade):
    """O ultimo relato da maquina (dict) ou None."""
    try:
        bruto = secretstore.get(_VERSOES, scope=cidade)
        return json.loads(bruto) if bruto else None
    except ValueError:
        return None


def pedir_troca_de_versao(cidade, tag, por=''):
    """Guarda a troca pedida. (ok, recado). A maquina confere tag e janela."""
    tag = (tag or '').strip()
    rel = versoes(cidade) or {}
    validas = {t for t, _d in ((rel.get('waha') or {}).get('publicadas') or [])} | {'latest'}
    if tag not in validas:
        return False, 'Versão desconhecida: %s.' % tag
    if tag == (rel.get('waha') or {}).get('instalada'):
        return False, 'Essa já é a versão instalada.'
    secretstore.set_value(_VERSAO_PEDIDA, json.dumps({
        'tag': tag, 'por': por, 'em': datetime.now().isoformat(timespec='seconds')}),
        scope=cidade, updated_by=por or 'site')
    logger.warning('TROCA DE VERSAO DO WAHA pedida em %s por %s: %s', cidade, por, tag)
    return True, ('Troca para %s pedida. A máquina executa na próxima passada '
                  '(recusa perto do horário do ranking) e o resultado aparece '
                  'aqui.' % tag)


def troca_de_versao_pedida(cidade):
    try:
        bruto = secretstore.get(_VERSAO_PEDIDA, scope=cidade)
        return json.loads(bruto) if bruto else None
    except ValueError:
        return None


def ultima_troca_de_versao(cidade):
    try:
        bruto = secretstore.get(_VERSAO_RESULTADO, scope=cidade)
        return json.loads(bruto) if bruto else None
    except ValueError:
        return None


def ultima_faxina(cidade):
    """O último resultado de faxina ({em, linhas}) ou None."""
    bruto = secretstore.get(_FAXINA_RESULTADO, scope=cidade)
    try:
        return json.loads(bruto) if bruto else None
    except ValueError:
        return None


def _alinhar_cofre(cidade, por='automacao'):
    """Põe no cofre o endereço/chave/sessão da instância que de fato publica.

    O cofre continua sendo a fonte de todo mundo que já existia (`maquina`,
    `filamensagens`, a tela). Em vez de mudar cada leitor, o relato da máquina
    passa a MANTER o cofre certo — e aí a correção alcança tudo de uma vez.

    Escolhe a instância do PRINCIPAL que esteja alcançável; sem ela, qualquer
    uma alcançável com sessão de pé. Nunca escolhe uma que o site não alcança
    (hoje a 3001 não tem ponte de rede — regra P-73), porque trocar o endereço
    bom por um inalcançável apagaria a única coisa que funciona.
    """
    linhas = db.session.execute(sa_text("""
        SELECT papel, sessao, host, porta, chave, status FROM instancias_waha
        WHERE cidade = :c AND alcancavel = 1 AND host IS NOT NULL AND porta > 0
        ORDER BY CASE WHEN papel = 'principal' THEN 0 ELSE 1 END,
                 CASE WHEN status = 'WORKING' THEN 0 ELSE 1 END, porta
    """), {'c': cidade}).fetchall()
    if not linhas:
        return ['nenhuma instância alcançável pelo site — o cofre ficou como estava']

    papel, sessao, host, porta, chave, _status = linhas[0]
    avisos = []
    base = 'http://%s:%d' % (host, int(porta))
    if (secretstore.get(secretstore.WAHA_URL, scope=cidade) or '').strip().rstrip('/') != base:
        secretstore.set_value(secretstore.WAHA_URL, base, scope=cidade, updated_by=por)
        avisos.append('endereço passou a ser %s' % base)
    if chave and (secretstore.get(secretstore.WAHA_API_KEY, scope=cidade) or '') != chave:
        secretstore.set_value(secretstore.WAHA_API_KEY, chave, scope=cidade, updated_by=por)
        avisos.append('a chave de acesso foi corrigida pela própria máquina')
    if sessao:
        if not mesmo_nome(secretstore.get(secretstore.WAHA_SESSION, scope=cidade) or '', sessao):
            secretstore.set_value(secretstore.WAHA_SESSION, sessao, scope=cidade,
                                  updated_by=por)
            avisos.append('a sessão desta cidade é "%s"' % sessao)
        curta = _curta_do_nome(sessao)
        if curta and (secretstore.get(secretstore.WAHA_CIDADE_CURTA, scope=cidade) or '') != curta:
            secretstore.set_value(secretstore.WAHA_CIDADE_CURTA, curta, scope=cidade,
                                  updated_by=por)
            avisos.append('a forma curta da cidade é "%s"' % curta)
    if avisos:
        logger.warning('Cofre de %s alinhado pela máquina (%s): %s',
                       cidade, papel or 'sem papel', '; '.join(avisos))
    return avisos


def instancias(cidade):
    """As instâncias que a máquina relatou. Lista vazia se ela nunca falou."""
    ensure_schema()
    linhas = db.session.execute(sa_text("""
        SELECT papel, container, sessao, host, porta, status, motor, numero,
               alcancavel, visto_em
        FROM instancias_waha WHERE cidade = :c
        ORDER BY CASE WHEN papel = 'principal' THEN 0
                      WHEN papel = 'suplente' THEN 1 ELSE 2 END, porta
    """), {'c': cidade}).fetchall()
    return [{
        'papel': r[0], 'container': r[1], 'sessao': r[2], 'host': r[3],
        'porta': r[4], 'status': r[5], 'motor': r[6], 'numero': r[7],
        'alcancavel': bool(r[8]), 'visto_em': r[9],
        # A chave NUNCA sai daqui para a tela.
        'base': ('http://%s:%d' % (r[3], int(r[4]))) if r[3] and r[4] else None,
        'pronta': (r[5] == 'WORKING' and r[6] == 'CONNECTED'),
    } for r in linhas]


def instancia_do_papel(cidade, papel):
    """A instância daquele papel, com a chave — para o site falar com ela."""
    ensure_schema()
    r = db.session.execute(sa_text("""
        SELECT papel, container, sessao, host, porta, chave, status, motor
        FROM instancias_waha
        WHERE cidade = :c AND papel = :p AND host IS NOT NULL AND porta > 0
        ORDER BY visto_em DESC LIMIT 1
    """), {'c': cidade, 'p': (papel or '').lower()}).fetchone()
    if not r:
        return None
    return {'papel': r[0], 'container': r[1], 'sessao': r[2],
            'host': r[3], 'porta': int(r[4]),
            'base': 'http://%s:%d' % (r[3], int(r[4])), 'chave': r[5],
            'status': r[6], 'motor': r[7],
            'pronta': (r[6] == 'WORKING' and r[7] == 'CONNECTED')}


def container_da_cidade(cidade):
    """O container que ESTA cidade ja tem — sem olhar papel. None se nao ha.

    UM CONTAINER POR CIDADE (09/09/2026). Ate aqui o pareamento perguntava
    `instancia_do_papel(cidade, papel)`. Com o desenho novo — um WAHA por
    cidade, com as duas sessoes dentro dele — nao existe container "de
    suplente": o relato da maquina traz UMA linha, com papel `principal`.
    Pedir o suplente nao achava nada, caia em `proxima_porta_livre()` e
    mandava CRIAR outro WAHA da mesma cidade. Foi assim que nasceu o
    container da 3002 em 08/09.

    Devolve so' ONDE a cidade mora (container, host, porta, chave). O NOME DA
    SESSAO NAO VEM DAQUI de proposito: a linha encontrada e' quase sempre a do
    principal, e adotar o nome dela para o suplente era outro defeito ja
    conhecido — o pedido do suplente gravava a sessao do principal no cofre.
    Quem responde pelo nome continua sendo o papel de quem pediu.

    Prefere a instancia que esta de pe; na falta, a mais recente.
    """
    ensure_schema()
    r = db.session.execute(sa_text("""
        SELECT container, host, porta, chave, status
        FROM instancias_waha
        WHERE cidade = :c AND host IS NOT NULL AND porta > 0
          AND container IS NOT NULL AND TRIM(container) <> ''
        ORDER BY CASE WHEN status = 'WORKING' THEN 0 ELSE 1 END, visto_em DESC
        LIMIT 1
    """), {'c': cidade}).fetchone()
    if not r:
        return None
    return {'container': r[0], 'host': r[1], 'porta': int(r[2]),
            'chave': r[3], 'status': r[4]}


def _curta_do_nome(nome):
    """`pls` a partir de `principal-pls`. None quando o nome foge do padrão."""
    resto = str(nome or '').strip().lower()
    for prefixo in ('principal-', 'suplente-'):
        if resto.startswith(prefixo):
            return re.sub(r'[^a-z0-9-]+', '', resto[len(prefixo):]) or None
    return None


def situacao_por_cidade():
    """Resumo para o painel do dono da instalacao: uma linha por cidade."""
    ensure_schema()
    from app.models import Config
    saida = []
    for (cidade,) in db.session.query(Config.cidade).order_by(Config.cidade).all():
        url = (secretstore.get(secretstore.WAHA_URL, scope=cidade) or '').strip()
        saida.append({
            'cidade': cidade,
            'sessao': nome_sessao(cidade),
            'slug': slug_cidade(cidade),
            'waha_configurado': bool(url),
            'responsavel': numero_responsavel(cidade),
            'pedido': pedido_atual(cidade),
        })
    return saida
