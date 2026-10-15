#/volume1/web/movimente-se/app/models.py

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
import json
from sqlalchemy import inspect, text
from app import db

# Hierarquia de acesso. O numero e o que manda em toda comparacao de
# privilegio: quem tem numero maior enxerga e resolve o que o menor faz.
# 'master_supremo' e o dono da instalacao -- unico que atravessa a fronteira
# de cidade e o unico que abre a pagina de comandos.
TIPO_VIEWER = 'viewer'
TIPO_ADMIN = 'admin'
TIPO_APROVADOR = 'aprovador'
TIPO_MASTER = 'master'
TIPO_SUPREMO = 'master_supremo'

# O PATROCINADOR/AUDITOR NAO CABE NA RETA (P-91, 28/08/2026).
#
# A hierarquia acima e' LINEAR: quem tem numero maior pode tudo que o menor
# pode. O patrocinador que audita o projeto -- ou o analista do Ministerio --
# nao cabe nela: e' SO-LEITURA (privilegio baixo) e ATRAVESSA CIDADE
# (privilegio que so o supremo tem). E' ortogonal, nao hierarquico.
#
# A saida: nivel 1, igual ao viewer, para que TODO decorator existente ja o
# barre corretamente sem precisar ser revisto um a um -- e a travessia de
# cidade entra por uma propriedade separada (`pode_trocar_cidade`).
#
# ⚠️ Nivel baixo NAO basta sozinho: telas sem nivel declarado no catalogo
# ficariam abertas. Para usuario EXTERNO vale allowlist, negando por padrao, e
# a verificacao roda no servidor (`app/__init__.py`), nunca so escondendo card.
TIPO_PATROCINADOR = 'patrocinador'

NIVEIS_USUARIO = {
    TIPO_VIEWER: 1,
    TIPO_PATROCINADOR: 1,
    TIPO_ADMIN: 2,
    TIPO_APROVADOR: 3,
    TIPO_MASTER: 4,
    TIPO_SUPREMO: 5,
}

ROTULOS_TIPO = {
    TIPO_VIEWER: 'Visualizador',
    TIPO_PATROCINADOR: 'Patrocinador / auditor',
    TIPO_ADMIN: 'Administrador',
    TIPO_APROVADOR: 'Aprovador',
    TIPO_MASTER: 'Master da cidade',
    TIPO_SUPREMO: 'Master supremo',
}


class Usuario(UserMixin, db.Model):
    """Modelo de usuário do sistema"""
    __tablename__ = 'usuarios'
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(100), unique=True, nullable=False)
    senha_hash = db.Column(db.String(255), nullable=False)
    tipo = db.Column(db.String(20), default='viewer')
    cidade = db.Column(db.String(100), nullable=False)
    ativo = db.Column(db.Boolean, default=False)
    # Telefone no formato que o WAHA entende (55DDD9XXXXXXXX). Todo usuario tem,
    # do viewer ao supremo: e por ele que o sistema avisa o responsavel quando
    # o ranking falha, e e por ele que da para entrar sem lembrar o e-mail.
    telefone = db.Column(db.String(20), nullable=True, index=True)
    # Senha entregue pelo admin: obriga a troca no primeiro acesso.
    senha_provisoria = db.Column(db.Boolean, default=False, nullable=True)
    senha_alterada_em = db.Column(db.DateTime, nullable=True)
    # ---- quem publica no grupo pelo WhatsApp ----
    # Cada admin tem o proprio WhatsApp ligado ao sistema. Estes campos dizem
    # QUEM de fato publica — e em que ordem, se o de cima falhar.
    #   ordem_envio 1 = principal, 2 = suplente, 3+ = reserva.
    # NUNCA dois publicam ao mesmo tempo: a aplicacao escolhe UM (o primeiro
    # da ordem com sessao viva) e o orquestrador obedece. Duas pessoas
    # publicando o mesmo ranking e mensagem repetida no grupo.
    envia_ranking = db.Column(db.Boolean, default=False, nullable=True)
    envia_avisos = db.Column(db.Boolean, default=False, nullable=True)
    ordem_envio = db.Column(db.Integer, default=0, nullable=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    gerado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    modificado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    ult_modificacao = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    reset_token = db.Column(db.String(64), nullable=True)
    reset_token_expiry = db.Column(db.DateTime, nullable=True)
    # TROCA DE E-MAIL COM CONFIRMACAO (20/09/2026, P-103). O endereco novo fica
    # AQUI, em espera, ate alguem com acesso ao e-mail ATUAL clicar no link —
    # e so entao vira `email`. Colunas criadas por `contas.ensure_schema()`;
    # declaradas aqui porque sem isso o SQLAlchemy ignora a atribuicao sem
    # erro nenhum (`ERROS.md` §4.35).
    email_novo = db.Column(db.String(120), nullable=True)
    email_token = db.Column(db.String(64), nullable=True)
    email_token_expiry = db.Column(db.DateTime, nullable=True)
    atividades = db.relationship('Activity', backref='admin_upload', lazy='dynamic')
    gerado_por_usuario = db.relationship('Usuario', remote_side=[id], backref='usuarios_gerados', primaryjoin="Usuario.gerado_por_id == Usuario.id")
    modificado_por_usuario = db.relationship('Usuario', remote_side=[id], backref='usuarios_modificados', primaryjoin="Usuario.modificado_por_id == Usuario.id")

    def set_password(self, password):
        """Define a senha do usuário"""
        self.senha_hash = generate_password_hash(password)

    def check_password(self, password):
        """Verifica a senha do usuário"""
        return check_password_hash(self.senha_hash, password)

    # ---------- nivel de acesso ----------

    @property
    def tipo_efetivo(self):
        """O tipo que vale AGORA, nesta sessão.

        ENTRAR COMO ATLETA (25/09/2026): quem entra pelo botão da página pública
        do atleta usa a mesma conta, mas a sessão vale só como visualizador --
        mesmo para admin, master ou supremo. Só o USUÁRIO LOGADO é rebaixado: a
        marca da sessão está amarrada ao id dele, então os outros usuários que o
        código consulta (`pode_gerir`, listas) continuam com o tipo verdadeiro.
        O patrocinador já é só-leitura e não muda.
        """
        if self.tipo != TIPO_PATROCINADOR and self.id is not None:
            try:
                from flask import has_request_context, session
                if (has_request_context() and session.get('modo_atleta')
                        and str(session.get('_user_id')) == str(self.id)):
                    return TIPO_VIEWER
            except Exception:
                pass
        return self.tipo

    @property
    def nivel(self):
        return NIVEIS_USUARIO.get(self.tipo_efetivo, 0)

    @property
    def rotulo_tipo(self):
        return ROTULOS_TIPO.get(self.tipo_efetivo, self.tipo_efetivo or '')

    @property
    def is_supremo(self):
        return self.tipo_efetivo == TIPO_SUPREMO

    @property
    def is_auditor(self):
        """Patrocinador ou analista externo: le, atravessa cidade, nao edita."""
        return self.tipo == TIPO_PATROCINADOR

    @property
    def pode_trocar_cidade(self):
        """Quem enxerga mais de uma cidade.

        O supremo, porque e' o dono; o auditor, porque auditar UMA cidade nao
        prova nada sobre um projeto que se apresenta como circuito.
        """
        return self.is_supremo or self.is_auditor

    @property
    def is_master(self):
        """Master da cidade ou supremo. Substitui o antigo tipo == 'master'."""
        return self.nivel >= NIVEIS_USUARIO[TIPO_MASTER]

    @property
    def pode_aprovar(self):
        return self.nivel >= NIVEIS_USUARIO[TIPO_APROVADOR]

    @property
    def pode_administrar(self):
        return self.nivel >= NIVEIS_USUARIO[TIPO_ADMIN]

    @property
    def pode_ver(self):
        """Qualquer pessoa logada — inclusive visualizador e patrocinador.

        Existe para o catalogo poder dizer, numa linha, que aquela tela e' de
        ACOMPANHAR. Sem ela, 'sem nivel' significaria 'todo mundo', e o
        visualizador acabaria vendo card de tela que nao abre (20/09/2026).
        """
        return self.nivel >= NIVEIS_USUARIO[TIPO_VIEWER]

    # ---------- cidade em que esta trabalhando ----------

    @property
    def cidade_ativa(self):
        """Cidade em que este usuario esta trabalhando AGORA.

        Para todo mundo e a cidade do cadastro. So o master supremo troca, e a
        escolha vive na SESSAO -- nunca num parametro de URL, senao trocar o
        nome no endereco daria acesso aos dados da cidade vizinha.
        """
        if not self.pode_trocar_cidade:
            return self.cidade
        try:
            from flask import g, has_request_context, session
        except Exception:
            return self.cidade
        try:
            if not has_request_context():
                return self.cidade
            escolhida = session.get('cidade_ativa')
        except Exception:
            return self.cidade
        if not escolhida or escolhida == self.cidade:
            return self.cidade
        # Uma consulta por request, guardada no g: a propriedade e lida dezenas
        # de vezes ao montar uma pagina.
        validas = getattr(g, '_cidades_validas', None)
        if validas is None:
            try:
                validas = {row[0] for row in db.session.query(Config.cidade).all()}
            except Exception:
                validas = set()
            try:
                g._cidades_validas = validas
            except Exception:
                pass
        return escolhida if escolhida in validas else self.cidade

    # ---------- quem mexe em quem ----------

    def pode_gerir(self, outro):
        """Pode editar/criar/apagar o cadastro de `outro`?

        O supremo mexe em todo mundo, em qualquer cidade. Os demais so mexem
        em gente da propria cidade e de nivel estritamente menor -- e ninguem
        se rebaixa por engano.
        """
        if outro is None:
            return False
        if self.is_supremo:
            return True
        if outro.id == self.id:
            return False
        if (outro.cidade or '') != (self.cidade or ''):
            return False
        return self.nivel > outro.nivel

    def pode_desativar(self, outro):
        """Desativar e mais brando que apagar: privilegio IGUAL ja basta.

        Decisao do operador (24/07): 'qualquer admin de privilegio igual ou
        superior pode desativar'. Continua valendo a fronteira de cidade e a
        proibicao de desativar a si mesmo (senao alguem se tranca do lado de
        fora).
        """
        if outro is None or outro.id == self.id:
            return False
        if self.is_supremo:
            return True
        if not self.pode_aprovar:
            return False
        if (outro.cidade or '') != (self.cidade or ''):
            return False
        return self.nivel >= outro.nivel

    def tipos_que_pode_conceder(self):
        """Niveis que este usuario pode atribuir a outra pessoa.

        Ninguem cria alguem mais forte que si mesmo; so o supremo cria outro
        supremo (e por isso a instalacao nunca fica sem dono).
        """
        teto = self.nivel if self.is_supremo else self.nivel - 1
        tipos = [t for t, n in sorted(NIVEIS_USUARIO.items(), key=lambda kv: kv[1])
                 if n <= teto]
        # O patrocinador tem nivel 1 mas ATRAVESSA CIDADE: quem o cria tem de
        # ser master ou supremo, nunca um admin comum. Sem esta linha, o nivel
        # sozinho o entregaria a qualquer um que pudesse criar um viewer.
        if not self.is_master and TIPO_PATROCINADOR in tipos:
            tipos.remove(TIPO_PATROCINADOR)
        return tipos

    def __repr__(self):
        return f'<Usuario {self.email}>'


class Activity(db.Model):
    """Modelo de atividades do Strava"""
    __tablename__ = 'activities'
    id = db.Column(db.Integer, primary_key=True)
    id_atividade = db.Column(db.String(50), nullable=False)
    id_usuario_strava = db.Column(db.String(50), nullable=False)
    nome_usuario = db.Column(db.String(100), nullable=False)
    modalidade = db.Column(db.String(50), nullable=False)
    distancia = db.Column(db.Float, nullable=False)
    data_hora = db.Column(db.DateTime, nullable=False)
    cidade = db.Column(db.String(100), nullable=False)
    id_admin_upload = db.Column(db.Integer, db.ForeignKey('usuarios.id'))
    arquivo_origem = db.Column(db.String(255))
    link_original = db.Column(db.String(500))
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    tempo_minutos = db.Column(db.Integer, nullable=True)
    elevacao_total = db.Column(db.Float, nullable=True)
    velocidade_media = db.Column(db.Float, nullable=True)
    strava_flags = db.Column(db.Text, nullable=True)

    telefone_whatsapp = db.Column(db.String(20), nullable=True)
    pace_min_km = db.Column(db.Float, nullable=True)
    distancia_raw = db.Column(db.Float, nullable=True)
    gps_fingerprint = db.Column(db.String(40), nullable=True, index=True)
    status      = db.Column(db.String(20), default='ativa', nullable=True)  # 'ativa', 'removida'
    splits_json = db.Column(db.Text, nullable=True)  # JSON com parciais por km
    processed_file_id = db.Column(db.Integer, db.ForeignKey("processed_files.id"), nullable=True)
    # Quando True, a atividade continua no ranking mensal mas NAO conta no
    # ranking anual (punicao para reincidente decidida na aprovacao da revisao).
    excluir_do_anual = db.Column(db.Boolean, default=False, nullable=True)
    # Atividade trazida por importacao de perfil Strava, sem link postado no
    # grupo (historico do atleta, ou atleta que trocou de conta). CONTA
    # normalmente no ranking/medalhas -- a tag e so para permitir, depois, um
    # total separado de "quanto veio de fato de link postado no grupo".
    fora_do_grupo = db.Column(db.Boolean, default=False, nullable=True)
    # O ESPORTE COMO O STRAVA DIZ, palavra por palavra ('Run', 'Walk', 'Hike',
    # 'Ride'...). Ate 28/08/2026 este dado CHEGAVA e era jogado fora: o
    # `SPORT_MAP` reduzia tudo a `corrida_caminhada`/`ciclismo` e o original se
    # perdia. Sem ele, separar corrida de caminhada so dava por inferencia de
    # ritmo -- criterio, nao dado. Fica nulo nas 10 mil linhas antigas (nao ha
    # como saber sem raspar de novo), e ai o ritmo continua sendo o desempate.
    esporte_strava = db.Column(db.String(30), nullable=True)
    # Marca SO as atividades que o botao "arquivar perfil e os quilometros"
    # (Fase 3 do plano das melhorias) tirou do ar. `status='removida'` sozinho
    # nao bastava para reverter: e' o MESMO valor que o anticheat usa para
    # atividade rejeitada com causa, e desarquivar em lote ressuscitaria essas
    # tambem. Com a marca, desarquivar so devolve `status='ativa'` onde ESTA
    # marca esta True -- o que o anticheat rejeitou continua rejeitado.
    oculta_por_arquivamento = db.Column(db.Boolean, default=False, nullable=True)
    # Esta velocidade entra na media do atleta mais veloz? (Fase 4 do plano
    # das melhorias, 28/08/2026). Default True; o anticheat poe False
    # SOZINHO quando gera VELOCIDADE_ANORMAL ou TEMPO_IMPOSSIVEL, e um admin
    # pode reverter na tela de revisao. NAO tira km do ranking (regra 14) --
    # mexe so' na media de velocidade, pra ninguem de moto liderar a tabela.
    velocidade_confiavel = db.Column(db.Boolean, default=True, nullable=True)
    __table_args__ = (
        db.Index('idx_cidade_data', 'cidade', 'data_hora'),
        db.Index('idx_strava_id', 'id_atividade'),
        db.UniqueConstraint('id_atividade', 'cidade', name='_atividade_cidade_uc')
    )
    
    def __repr__(self):
        return f'<Activity {self.id_atividade} - {self.nome_usuario}>'


class Config(db.Model):
    """Modelo de configurações por cidade"""
    __tablename__ = 'config'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), unique=True, nullable=False)
    meta_corrida_caminhada = db.Column(db.Integer, default=30)
    meta_ciclismo = db.Column(db.Integer, default=100)
    patrocinador_mes = db.Column(db.String(200), default='')
    chave_pix = db.Column(db.String(200), default='')
    qr_pix = db.Column(db.String(255), default='')
    # Limites configuraveis do anti-cheat. Os valores padrao preservam os
    # limites que eram fixos no codigo antes desta configuracao existir.
    pace_max_corrida_curta = db.Column(db.Float, default=4.167)
    pace_max_corrida_longa = db.Column(db.Float, default=5.0)
    velocidade_min_ciclismo = db.Column(db.Float, nullable=True)
    velocidade_max_ciclismo = db.Column(db.Float, default=45.0)
    # Tolerancia do anti-cheat: quantas atividades suspeitas do MESMO tipo de
    # flag sao toleradas no mes. A partir da seguinte (por padrao a 4a) a
    # atividade daquele tipo sai do ranking. Configuravel por cidade.
    tolerancia_flags = db.Column(db.Integer, default=3)
    # Segundos de espera entre buscas ao Strava (rate limiter). Quanto maior,
    # mais devagar e mais seguro contra bloqueio. So torna a coleta mais lenta
    # que o perfil da opcao escolhida, nunca mais rapida.
    strava_delay_segundos = db.Column(db.Float, default=4.0)
    # Desde quando o grupo de WhatsApp desta cidade existe (mes/ano). Piso
    # para importacao de historico de atleta por perfil Strava -- nada antes
    # disso e importado, porque o grupo nao existia. Vazio ate o master/admin
    # preencher (Painel > Cidades); sem valor, vale so o piso por atleta
    # (mes do primeiro link dele no grupo).
    piso_importacao_ano = db.Column(db.Integer, nullable=True)
    piso_importacao_mes = db.Column(db.Integer, nullable=True)
    # Link do grupo de WhatsApp da cidade, mostrado a quem conclui a
    # inscricao publica (Fase 2 do plano das melhorias). So master/aprovador
    # edita -- se qualquer um pudesse trocar, alguem poria o link de outro
    # grupo na porta de entrada do projeto.
    link_grupo_whatsapp = db.Column(db.String(300), nullable=True)
    # Corte de pace (min/km) que separa corrida de caminhada na tabela
    # "somente corrida" (Fase 4). Decisao do dono: 7.0, mais rigoroso que a
    # literatura (7.5-8.5), de proposito -- errar para o lado de nao chamar
    # caminhada rapida de corrida.
    pace_corte_corrida = db.Column(db.Float, default=7.0)
    # Cidade ATIVA de verdade (P-81, 20/08/2026). Default FALSO de proposito:
    # cidade com atividade real ja passa pelo OUTRO criterio (tem ranking
    # rodando -- `consolidado.cidades_ativas()`), entao so precisa desta flag
    # quem esta vazia e ainda assim deve ser tratada como ativa (cidade nova,
    # em fase de implantacao, antes do primeiro link chegar). Sem isso,
    # Minaçu (vazia) mandava Wake-on-LAN as 10:27 e e-mail de "Automacao sem
    # sinal" todo dia, porque `vigia._cidades()` devolvia TODA linha de
    # `Config`, sem distinguir.
    ativa = db.Column(db.Boolean, default=False)
    ult_atualizacao_config = db.Column(db.DateTime, default=datetime.utcnow)
    modificado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    
    def __repr__(self):
        return f'<Config {self.cidade}>'


class AnticheatOverride(db.Model):
    """Limites individuais do anti-cheat por atleta (chave: strava_id).

    Cada campo e opcional: quando nulo, o anti-cheat usa o limite da cidade
    (Config) e, na falta dele, o valor historico do codigo. Serve para casos
    legitimos fora da curva (ex.: atleta que corre pace < 4 min/km).
    """
    __tablename__ = 'anticheat_override'
    id = db.Column(db.Integer, primary_key=True)
    strava_id = db.Column(db.String(50), unique=True, nullable=False, index=True)
    nome_atleta = db.Column(db.String(100), nullable=True)
    cidade = db.Column(db.String(100), nullable=True, index=True)
    pace_max_corrida_curta = db.Column(db.Float, nullable=True)
    pace_max_corrida_longa = db.Column(db.Float, nullable=True)
    velocidade_min_ciclismo = db.Column(db.Float, nullable=True)
    velocidade_max_ciclismo = db.Column(db.Float, nullable=True)
    observacao = db.Column(db.String(255), nullable=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    def __repr__(self):
        return f'<AnticheatOverride {self.strava_id} - {self.nome_atleta}>'


class ProcessedFile(db.Model):
    """Modelo para controle de arquivos processados"""
    __tablename__ = 'processed_files'
    id = db.Column(db.Integer, primary_key=True)
    arquivo_nome = db.Column(db.String(255), nullable=False)
    cidade = db.Column(db.String(100), nullable=False)
    hash_arquivo = db.Column(db.String(64), nullable=False)
    total_atividades = db.Column(db.Integer, default=0)
    atividades_validas = db.Column(db.Integer, default=0)
    processado_em = db.Column(db.DateTime, default=datetime.utcnow)
    processado_por = db.Column(db.Integer, db.ForeignKey('usuarios.id'))
    tempo_processamento = db.Column(db.Float)
    status = db.Column(db.String(20), default='sucesso')
    mensagem_erro = db.Column(db.Text)
    links_encontrados_mes = db.Column(db.Integer, default=0)
    atividades_pendentes  = db.Column(db.Integer, default=0)
    links_scrapeados = db.Column(db.Integer, default=0)
    pendencias_json = db.Column(db.Text, nullable=True)
    # Tipo de processamento escolhido no upload ('mes_atual', 'mes_atual_anterior',
    # 'periodo_especifico', 'todos_periodos') + o(s) periodo(s) (ano-mes) resolvido(s)
    # para aquele tipo. Junto com hash_arquivo/cidade formam a chave de deduplicacao:
    # o mesmo arquivo pode ser reprocessado se o tipo ou o periodo forem diferentes.
    tipo_periodo = db.Column(db.String(30), default='')
    valor_periodo = db.Column(db.String(120), default='')
    atividades_pf = db.relationship('Activity', backref='processed_file', lazy='dynamic', foreign_keys='Activity.processed_file_id')
    __table_args__ = (
        db.UniqueConstraint('hash_arquivo', 'cidade', 'tipo_periodo', 'valor_periodo', name='_hash_cidade_periodo_uc'),
    )
    
    def __repr__(self):
        return f'<ProcessedFile {self.arquivo_nome}>'


class MonthlyBackup(db.Model):
    """Modelo para backup mensal dos rankings"""
    __tablename__ = 'monthly_backup'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    ano = db.Column(db.Integer, nullable=False)
    json_ranking = db.Column(db.Text, nullable=False)
    texto_whatsapp = db.Column(db.Text, nullable=True)
    patrocinador_nome_mes = db.Column(db.String(200), nullable=True)
    patrocinador_logo_blob = db.Column(db.LargeBinary, nullable=True)
    patrocinador_logo_mime = db.Column(db.String(100), nullable=True)
    patrocinador_logo_filename = db.Column(db.String(255), nullable=True)
    total_atletas = db.Column(db.Integer, default=0)
    total_medalhas = db.Column(db.Integer, default=0)
    timestamp_geracao = db.Column(db.DateTime, default=datetime.utcnow)
    gerado_por = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    __table_args__ = (
        db.UniqueConstraint('cidade', 'mes', 'ano', name='_cidade_mes_ano_uc'),
    )
    
    def get_ranking_data(self):
        """Retorna os dados do ranking como dicionário"""
        return json.loads(self.json_ranking)
    
    def __repr__(self):
        return f'<MonthlyBackup {self.cidade} - {self.mes}/{self.ano}>'


class PostagemAgenda(db.Model):
    """Os horarios em que a cidade publica, e o que sai em cada um.

    POR QUE EXISTE: os horarios estavam presos no Agendador do Windows e no
    systemd — mudar 10:30 para 11:00 exigia mexer em duas maquinas. Agora a
    cidade define aqui e o orquestrador pergunta ao sistema o que fazer.

    Cada linha e um horario. As caixas dizem o que sai naquele horario, para
    a cidade poder mandar o ranking duas vezes por dia mas as promocoes so
    uma vez, por exemplo.
    """
    __tablename__ = 'postagem_agenda'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    horario = db.Column(db.String(5), nullable=False)   # 'HH:MM'
    ativo = db.Column(db.Boolean, default=True, nullable=True)
    envia_ranking = db.Column(db.Boolean, default=True, nullable=True)
    envia_patrocinadores = db.Column(db.Boolean, default=True, nullable=True)
    envia_parabens = db.Column(db.Boolean, default=True, nullable=True)
    envia_avisos = db.Column(db.Boolean, default=True, nullable=True)
    # Promocao de patrocinador sai UMA vez por dia, no fim de tudo — depois
    # dos parabens. Por isso a caixa e separada e so um horario a marca.
    envia_promocoes = db.Column(db.Boolean, default=False, nullable=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('cidade', 'horario', name='_agenda_cidade_horario_uc'),
    )

    def __repr__(self):
        return f'<PostagemAgenda {self.cidade} {self.horario}>'


class PatrocinadorCatalogo(db.Model):
    """Ficha do patrocinador na cidade — o que se repete mes a mes.

    POR QUE EXISTE: os patrocinadores voltam. Sem catalogo, o admin redigita
    nome, bordao, legenda e reenvia a logo toda vez. Com catalogo ele escolhe
    da lista e o sistema COPIA tudo para o mes novo.

    A copia e proposital: o mes vinculado nunca aponta para c'a. Se a ficha
    mudar depois (logo nova, bordao novo), os meses ja fechados continuam
    exatamente como sairam no ranking daquela epoca.
    """
    __tablename__ = 'patrocinador_catalogo'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    # Nome interno: como a EQUIPE identifica este patrocinador. Pode ser a
    # razao social, ou um apelido para diferenciar dois de nome parecido.
    nome_interno = db.Column(db.String(200), nullable=False)
    # Nome que o PUBLICO ve no ranking e nas telas. Pode mudar de mes para mes.
    nome_exibicao = db.Column(db.String(200), nullable=True)
    patrocinador_logo_blob = db.Column(db.LargeBinary, nullable=True)
    patrocinador_logo_mime = db.Column(db.String(100), nullable=True)
    patrocinador_logo_filename = db.Column(db.String(255), nullable=True)
    telefone = db.Column(db.String(30), nullable=True)
    bordao = db.Column(db.String(200), nullable=True)
    legenda = db.Column(db.Text, nullable=True)
    o_que_banca = db.Column(db.String(200), nullable=True)
    banca_medalhas = db.Column(db.Boolean, default=False, nullable=True)
    patrocinio_continuo = db.Column(db.Boolean, default=False, nullable=True)
    # APARECE NA LISTA DA PAGINA INICIAL? (28/08/2026, pedido do dono)
    # A porta de entrada mostra todos os patrocinadores que ja apoiaram, com
    # logo e mes. Nem todo apoiador quer exposicao publica -- alguns preferem
    # aparecer so na mensagem do grupo. Padrao e' APARECER.
    mostrar_na_home = db.Column(db.Boolean, default=True, nullable=True)
    # ONDE A MEDALHA E RETIRADA. O balcao e do patrocinador e nao muda de mes
    # para mes, entao mora aqui e nao no registro mensal.
    #
    # ESTAS DUAS LINHAS FALTAVAM ATE 14/08/2026 — e a falta delas nao dava erro
    # nenhum. A tabela TINHA as colunas (o `avisos.ensure_schema` as criava por
    # ALTER TABLE), mas o MODELO nao. Sem a coluna declarada, o SQLAlchemy
    # ignora `ficha.endereco = ...`: vira um atributo solto no objeto Python,
    # some no commit, e a leitura seguinte devolve vazio.
    #
    # O dono passou uma manha inteira digitando endereco e link do Maps,
    # salvando, voltando e achando os campos em branco. Nao havia mensagem de
    # erro para investigar porque, do ponto de vista do banco, nada foi pedido.
    #
    # REGRA QUE FICA: coluna criada por ALTER TABLE em outro modulo TEM de ser
    # declarada no modelo tambem. Tabela e modelo sao duas metades; uma sem a
    # outra falha em silencio, que e o pior desfecho.
    endereco = db.Column(db.String(300), nullable=True)
    maps_url = db.Column(db.String(500), nullable=True)
    # DE QUANTO EM QUANTO ELE APARECE (06/09/2026, pedido do dono).
    # 5 = aparece sempre que puder; 1 = aparece raramente. Nao e' ordem nem
    # importancia do apoiador como pessoa: e' o quanto o que ele oferece
    # justifica ocupar a vaga da mensagem. O caso que originou isto: uma
    # bioimpedancia simbolica, oferecida para atrair cliente -- merece
    # aparecer, nao merece aparecer toda rodada.
    #
    # Vira PESO no rodizio: com pesos 5 e 1, o primeiro aparece cinco vezes
    # para cada uma do segundo. Zero tira do rodizio sem apagar a ficha.
    peso_frequencia = db.Column(db.Integer, default=3, nullable=True)
    ativo = db.Column(db.Boolean, default=True, nullable=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    __table_args__ = (
        db.UniqueConstraint('cidade', 'nome_interno', name='_catalogo_cidade_nome_uc'),
    )

    @property
    def exibicao(self):
        return (self.nome_exibicao or '').strip() or (self.nome_interno or '').strip()

    def __repr__(self):
        return f'<PatrocinadorCatalogo {self.cidade} - {self.nome_interno}>'


class PatrocinadorMensal(db.Model):
    """Patrocinador de UM periodo (cidade, ano, mes). Copia independente.

    Nada aqui e compartilhado com outro mes: nome, nome de exibicao, logo,
    bordao, legenda e telefone sao gravados por periodo. E por isso que o
    mesmo patrocinador pode aparecer em Janeiro e em Julho como dois
    registros, com textos diferentes, sem um contaminar o outro.

    `categoria='principal'` (ordem 0) e o Patrocinador do Mes que abre o
    ranking; 'adicional' sao os apoios. So existe UM principal por periodo.
    """
    __tablename__ = 'patrocinadores_mensais'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    mes = db.Column(db.Integer, nullable=False)
    ano = db.Column(db.Integer, nullable=False)
    # Nome interno (identificacao da equipe). Continua com o nome antigo da
    # coluna para nao quebrar nada que ja le por aqui.
    patrocinador_nome = db.Column(db.String(200), nullable=False)
    # O que aparece no ranking e nas telas. Vazio = usa o nome interno.
    nome_exibicao = db.Column(db.String(200), nullable=True)
    # De qual ficha do catalogo este mes foi copiado (so rastreabilidade:
    # mudar a ficha NUNCA altera o que ja foi gravado aqui).
    catalogo_id = db.Column(db.Integer, db.ForeignKey('patrocinador_catalogo.id'), nullable=True)
    patrocinador_logo_blob = db.Column(db.LargeBinary, nullable=True)
    patrocinador_logo_mime = db.Column(db.String(100), nullable=True)
    patrocinador_logo_filename = db.Column(db.String(255), nullable=True)
    ordem = db.Column(db.Integer, default=1, nullable=False)
    categoria = db.Column(db.String(20), default='adicional')
    telefone = db.Column(db.String(30), nullable=True)
    bordao = db.Column(db.String(200), nullable=True)
    legenda = db.Column(db.Text, nullable=True)
    # O que este patrocinador banca naquele mes, na voz que vai para o grupo:
    # "Te premiara com as medalhas", "Vai patrocinar a bioimpedancia".
    # Entra depois do travessao na mensagem de abertura do mes.
    o_que_banca = db.Column(db.String(200), nullable=True)
    # Quem paga as medalhas e a informacao que o grupo mais pergunta.
    banca_medalhas = db.Column(db.Boolean, default=False, nullable=True)
    # Quando o grupo ja foi avisado deste patrocinador (para nao repetir).
    anunciado_em = db.Column(db.DateTime, nullable=True)
    # Patrocinio que se repete todo mes (a nutricionista sorteia sempre).
    # Muda o FECHO do agradecimento: para quem volta todo mes, "contamos com
    # voce no ano que vem" soa como despedida de quem nunca mais volta.
    patrocinio_continuo = db.Column(db.Boolean, default=False, nullable=True)
    # Excecao de UM mes: por padrao vale o que diz o catalogo do
    # patrocinador (`PatrocinadorCatalogo.mostrar_na_home`).
    mostrar_na_home = db.Column(db.Boolean, default=True, nullable=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    criado_por = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    # O peso deste apoio NESTE mes (06/09/2026). Copiado da ficha quando o
    # patrocinador entra no mes; da para ajustar so' aqui sem mexer no
    # catalogo. Ver o comentario em PatrocinadorCatalogo.peso_frequencia.
    peso_frequencia = db.Column(db.Integer, default=3, nullable=True)
    __table_args__ = (
        db.UniqueConstraint('cidade', 'mes', 'ano', 'ordem', name='_cidade_mes_ano_ordem_uc'),
    )

    @property
    def exibicao(self):
        """Nome que vai para o ranking e para a interface.

        Regra pedida pelo operador: o publico ve o nome DEFINIDO, mesmo que
        internamente o cadastro tenha outro nome.
        """
        return (self.nome_exibicao or '').strip() or (self.patrocinador_nome or '').strip()

    @property
    def is_principal(self):
        return (self.categoria or 'adicional') == 'principal'

    # ENDERECO E MAPA MORAM NA FICHA DO CATALOGO, nao no mes: o balcao do
    # patrocinador nao muda de um mes para o outro, e repetir o cadastro todo
    # mes so cria chance de digitar diferente.
    #
    # Exposto aqui como propriedade para a TELA poder mostrar `sp.endereco`
    # dentro do macro que serve TODOS os patrocinadores. Antes eu tinha
    # passado uma variavel global do principal, e o campo do apoio aparecia
    # com o endereco do principal — ou vazio.
    @property
    def _ficha(self):
        """A ficha do catálogo deste patrocinador. Por id, ou pelo NOME.

        `catalogo_id` está **vazio em todos os registros existentes** (medido em
        14/08): o vínculo nunca foi gravado, e a tela sempre casou catálogo com
        mês pelo `nome_interno`. Depender só do id fazia endereço e mapa não
        salvarem e não aparecerem — sem erro nenhum na tela, que é o pior tipo
        de defeito.
        """
        from app.models import PatrocinadorCatalogo
        if self.catalogo_id:
            ficha = PatrocinadorCatalogo.query.get(self.catalogo_id)
            if ficha:
                return ficha
        nome = (self.patrocinador_nome or '').strip()
        if not nome:
            return None
        return PatrocinadorCatalogo.query.filter_by(
            cidade=self.cidade, nome_interno=nome).first()

    @property
    def endereco(self):
        f = self._ficha
        return (getattr(f, 'endereco', '') or '') if f else ''

    @property
    def maps_url(self):
        f = self._ficha
        return (getattr(f, 'maps_url', '') or '') if f else ''

    def __repr__(self):
        return f'<PatrocinadorMensal {self.cidade} - {self.mes}/{self.ano} - {self.patrocinador_nome}>'


class ShortLink(db.Model):
    """Encurtador interno para links públicos do ranking."""
    __tablename__ = 'short_links'
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(32), unique=True, nullable=False, index=True)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    ano = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    active = db.Column(db.Boolean, default=True, nullable=False)

    def __repr__(self):
        return f'<ShortLink {self.slug} {self.cidade} {self.mes}/{self.ano}>'


class YearlyStats(db.Model):
    """Modelo para estatísticas anuais"""
    __tablename__ = 'yearly_stats'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False)
    modalidade = db.Column(db.String(50), nullable=False)
    id_usuario_strava = db.Column(db.String(50), nullable=False)
    total_km_acumulado_no_ano = db.Column(db.Float, default=0.0)
    nome_atual_usuario = db.Column(db.String(100))
    ranking_anual_json = db.Column(db.Text)
    ano = db.Column(db.Integer, nullable=False)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow)
    modificado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    __table_args__ = (
        db.UniqueConstraint('cidade', 'modalidade', 'id_usuario_strava', 'ano', name='_cidade_modalidade_usuario_ano_uc'),
    )
    
    def __repr__(self):
        return f'<YearlyStats {self.cidade} - {self.nome_atual_usuario} - {self.ano}>'


class Atleta(db.Model):
    """Modelo para armazenar dados de atletas únicos por Strava ID"""
    __tablename__ = 'atletas'
    id = db.Column(db.Integer, primary_key=True)
    strava_id = db.Column(db.String(50), unique=True, nullable=False)
    nome_atual = db.Column(db.String(100))
    # O NOME QUE A PESSOA QUER VER NO RANKING (29/08/2026, regra do dono).
    #
    # `nome_atual` e um SNAPSHOT do Strava: qualquer atividade nova o
    # reescreve (`anticheat._get_ou_criar_atleta`). Entao nao dava para
    # corrigir nada ali -- a correcao durava ate o proximo link postado.
    #
    # Este campo e o oposto: so muda quando GENTE muda. A Ana Exemplo e
    # conhecida como Ana Modelo, a Bia Exemplo como Bia Modelo, e o
    # carlos escreveu o proprio nome em minuscula no Strava. Nenhum deles
    # vai pedir; quem sabe e o admin, e agora ele pode ajustar.
    #
    # Vazio = usa o nome do Strava, como sempre foi.
    nome_exibicao = db.Column(db.String(60), nullable=True)
    # Quem escolheu e quando -- inclusive o proprio atleta, pelo link que
    # ele recebe. Sem isto nao da para saber se o nome foi escolha da
    # pessoa ou palpite do admin.
    nome_exibicao_por = db.Column(db.String(120), nullable=True)
    nome_exibicao_em = db.Column(db.DateTime, nullable=True)
    telefone_normalizado = db.Column(db.String(20), unique=True, nullable=True)
    # E-mail do atleta (nao do usuario que loga — sao coisas diferentes). Fica
    # aqui para a agenda ter uma coluna so por pessoa; hoje quase todo atleta
    # esta vazio e so os administradores tem e-mail de verdade.
    email = db.Column(db.String(150), nullable=True)
    # Quem digitou o telefone na mao, e quando. E o que faz a deducao
    # automatica nao desfazer conferencia feita por gente (regra 22-E).
    telefone_confirmado_em = db.Column(db.DateTime, nullable=True)
    telefone_confirmado_por = db.Column(db.String(120), nullable=True)
    # URL da foto de perfil no Strava (CDN publica). Atualizada quando a sessao
    # esta ativa; exibida nas paginas de atleta e conferencia (nunca no ranking).
    foto_url = db.Column(db.String(500), nullable=True)
    foto_atualizada_em = db.Column(db.DateTime, nullable=True)
    # Perfil arquivado/sem uso (ex.: duplicata que a pessoa criou). Some das
    # listas por padrao e mostra selo na pagina publica. Desarquiva SOZINHO
    # quando uma atividade nova e processada para este strava_id.
    arquivado = db.Column(db.Boolean, default=False, nullable=True)
    # A PESSOA PODE PEDIR PARA NAO APARECER (28/08/2026, regra do dono).
    # Marcado: o nome sai de TODA pagina publica -- inicial, consolidada,
    # conferencia -- e a pagina propria dela (`/ath/<id>`) deixa de existir.
    # Os quilometros CONTINUAM no banco e continuam somando no total do
    # projeto: o que ela pediu foi anonimato, nao apagar o que fez.
    oculto_no_publico = db.Column(db.Boolean, default=False, nullable=True)
    arquivado_em = db.Column(db.DateTime, nullable=True)
    arquivado_motivo = db.Column(db.String(200), nullable=True)
    # Diferente de `arquivado` (duplicata/perfil sem uso -- volta SOZINHO
    # quando chega atividade nova, `anticheat._get_ou_criar_atleta`). Este
    # campo marca que a PESSOA pediu para sair -- e por isso NAO deve voltar
    # sozinha: o admin arquiva com "e os quilometros" quando o pedido veio do
    # proprio atleta. Fase 3 do plano das melhorias, 28/08/2026.
    saiu_a_pedido = db.Column(db.Boolean, default=False, nullable=True)
    # Tipo de bicicleta ('speed', 'mtb', 'gravel'), marcado pelo admin da
    # cidade. Comparar Speed com MTB e' injusto -- a bicicleta explica a
    # diferenca de velocidade, nao o atleta (Fase 4 do plano das melhorias,
    # 28/08/2026). Aparece como selo ao lado do nome, sem tabela separada.
    tipo_bicicleta = db.Column(db.String(12), default='mtb', nullable=True)
    # 'M', 'F' ou None. Preenchido a mao pelo dono -- o Strava nao publica.
    # Coluna criada por ALTER TABLE TEM de ser declarada aqui, senao o
    # SQLAlchemy ignora a atribuicao sem erro nenhum (convencao do projeto).
    sexo = db.Column(db.String(1), nullable=True)
    # MESCLAGEM DE ATLETAS DUPLICADOS (07/09/2026, pedido do dono).
    #
    # NULL = atleta nao mesclado, ou e' o CABECA do grupo (quem recebe o km
    # consolidado). Preenchido = "meus dados pertencem ao atleta com este id"
    # -- sem cadeias: mesclar um atleta que ja e' cabeca de outro grupo
    # repoe todos os membros direto no novo cabeca (nunca A->B->C), para
    # resolver o grupo sempre custar 1 consulta indexada.
    atleta_canonico_id = db.Column(db.Integer, db.ForeignKey('atletas.id'), nullable=True)
    mesclado_em = db.Column(db.DateTime, nullable=True)
    mesclado_por = db.Column(db.String(120), nullable=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    flags = db.relationship('FlagAnticheat', backref=db.backref('atleta', lazy=True))
    consolidacoes = db.relationship('Consolidacao', backref=db.backref('atleta', lazy=True))
    controle_disciplinar = db.relationship('ControleDisciplinar', backref=db.backref('atleta', lazy=True))

    def __repr__(self):
        return f'<Atleta {self.strava_id} - {self.nome_atual}>'

    def strava_ids_do_grupo(self):
        """Todos os strava_id do grupo mesclado (ou so' o proprio, se nao mesclado)."""
        cid = self.atleta_canonico_id or self.id
        membros = Atleta.query.filter(
            db.or_(Atleta.id == cid, Atleta.atleta_canonico_id == cid)).all()
        return [m.strava_id for m in membros] or [self.strava_id]

    def atleta_ids_do_grupo(self):
        """Todos os Atleta.id do grupo mesclado (ou so' o proprio)."""
        cid = self.atleta_canonico_id or self.id
        ids = [r[0] for r in db.session.query(Atleta.id).filter(
            db.or_(Atleta.id == cid, Atleta.atleta_canonico_id == cid)).all()]
        return ids or [self.id]

    @staticmethod
    def resolver_canonico(atleta):
        """Dado um Atleta qualquer, devolve o cabeca do grupo (ou o proprio, se nao mesclado)."""
        if atleta is not None and atleta.atleta_canonico_id:
            return Atleta.query.get(atleta.atleta_canonico_id) or atleta
        return atleta

    @staticmethod
    def mapa_strava_canonico():
        """{strava_id do membro mesclado: strava_id do cabeca do grupo}, so' os mesclados.

        Vazio na imensa maioria das consultas (mesclagem e' rara) -- usado para
        montar um CASE WHEN em consultas SQL agregadas (`app/consolidado.py`)
        que nao podem resolver o grupo em Python, uma atividade de cada vez.
        """
        pares = (db.session.query(Atleta.strava_id, Atleta.atleta_canonico_id)
                 .filter(Atleta.atleta_canonico_id.isnot(None)).all())
        if not pares:
            return {}
        ids_cabeca = {cid for _, cid in pares}
        cabecas = {a.id: a.strava_id for a in
                   Atleta.query.filter(Atleta.id.in_(ids_cabeca)).all()}
        return {sid: cabecas[cid] for sid, cid in pares if cid in cabecas}


class FlagAnticheat(db.Model):
    """Modelo para registrar flags do motor anti-cheat"""
    __tablename__ = 'flag_anticheat'
    id = db.Column(db.Integer, primary_key=True)
    atleta_id = db.Column(db.Integer, db.ForeignKey('atletas.id'), nullable=False)
    atividade_id = db.Column(db.Integer, db.ForeignKey('activities.id'), nullable=False)
    tipo_flag = db.Column(db.String(50), nullable=False)
    nivel = db.Column(db.String(20), default='baixo')
    status = db.Column(db.String(20), default='aberta')
    descricao = db.Column(db.Text)
    criada_em = db.Column(db.DateTime, default=datetime.utcnow)
    resolvida_em = db.Column(db.DateTime, nullable=True)
    resolvida_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    observacao_admin = db.Column(db.Text)
    revisao_solicitada   = db.Column(db.Boolean, default=False)
    justificativa_atleta = db.Column(db.Text, nullable=True)
    recursos_utilizados  = db.Column(db.Integer, default=0)
    max_recursos         = db.Column(db.Integer, default=2)
    solicitado_revisao_em = db.Column(db.DateTime, nullable=True)
    atividade = db.relationship('Activity', backref=db.backref('flags_anticheat', lazy=True))
    resolvida_por = db.relationship('Usuario', backref=db.backref('flags_resolvidas', lazy=True))
    
    def __repr__(self):
        return f'<FlagAnticheat {self.tipo_flag} - {self.status}>'


class ControleDisciplinar(db.Model):
    """Modelo para histórico disciplinar mensal de atletas"""
    __tablename__ = 'controle_disciplinar'
    id = db.Column(db.Integer, primary_key=True)
    atleta_id = db.Column(db.Integer, db.ForeignKey('atletas.id'), nullable=False)
    mes_ano = db.Column(db.String(7), nullable=False)
    avisos = db.Column(db.Integer, default=0)
    remocoes = db.Column(db.Integer, default=0)
    total_flags = db.Column(db.Integer, default=0)
    atividades_suspeitas = db.Column(db.Integer, default=0)
    eliminado_do_ranking = db.Column(db.Boolean, default=False)
    pedidos_revisao     = db.Column(db.Integer, default=0)
    pedidos_revisao_mes = db.Column(db.Integer, default=0)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('atleta_id', 'mes_ano', name='_atleta_mes_uc'),
    )
    
    def __repr__(self):
        return f'<ControleDisciplinar {self.atleta_id} - {self.mes_ano}>'


class Consolidacao(db.Model):
    """Modelo expandido para consolidação de atividades por período"""
    __tablename__ = 'consolidacao'
    id = db.Column(db.Integer, primary_key=True)
    atleta_id = db.Column(db.Integer, db.ForeignKey('atletas.id'), nullable=False)
    periodo = db.Column(db.String(10), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)
    km_run = db.Column(db.Float, default=0)
    km_bike = db.Column(db.Float, default=0)
    km_total = db.Column(db.Float, default=0)
    elevacao_total = db.Column(db.Float, default=0)
    tempo_total_min = db.Column(db.Integer, default=0)
    atividades_count = db.Column(db.Integer, default=0)
    atividades_validas = db.Column(db.Integer, default=0)
    atividades_suspeitas = db.Column(db.Integer, default=0)
    atividades_removidas = db.Column(db.Integer, default=0)
    ranking_anual_pos = db.Column(db.Integer)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('atleta_id', 'periodo', 'tipo', name='_atleta_periodo_tipo_uc'),
    )
    
    def __repr__(self):
        return f'<Consolidacao {self.atleta_id} - {self.periodo}>'


class EvidenciaRevisao(db.Model):
    """Arquivos de evidência anexados a flags anticheat (admin ou atleta)."""
    __tablename__ = 'evidencia_revisao'
    id           = db.Column(db.Integer, primary_key=True)
    flag_id      = db.Column(db.Integer, db.ForeignKey('flag_anticheat.id'), nullable=False)
    arquivo_nome = db.Column(db.String(255), nullable=False)
    arquivo_path = db.Column(db.String(500), nullable=False)
    tipo_mime    = db.Column(db.String(100))
    tamanho      = db.Column(db.Integer)
    # 'atleta' ou email/nome do usuário admin
    enviado_por  = db.Column(db.String(50), nullable=False)
    enviado_em   = db.Column(db.DateTime, default=datetime.utcnow)
    flag         = db.relationship('FlagAnticheat', backref=db.backref('evidencias', lazy=True))

    def __repr__(self):
        return f'<EvidenciaRevisao flag={self.flag_id} {self.arquivo_nome}>'


class Feedback(db.Model):
    """Feedback publico enviado pela pagina do ranking.

    tipo:
      - 'revisao_ranking' (nome do atleta obrigatorio)
      - 'sugestao'
      - 'reclamacao'
    status: 'nova' | 'lida' | 'resolvida'
    """
    __tablename__ = 'feedback'
    id          = db.Column(db.Integer, primary_key=True)
    cidade      = db.Column(db.String(100), nullable=False, index=True)
    tipo        = db.Column(db.String(30), nullable=False)
    nome_atleta = db.Column(db.String(100), nullable=True)
    mensagem    = db.Column(db.Text, nullable=False)
    contato     = db.Column(db.String(120), nullable=True)
    status      = db.Column(db.String(20), default='nova', nullable=False)
    criado_em   = db.Column(db.DateTime, default=datetime.utcnow)
    resolvido_em = db.Column(db.DateTime, nullable=True)
    resolvido_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    def __repr__(self):
        return f'<Feedback {self.tipo} - {self.cidade} - {self.status}>'


class PrestacaoContas(db.Model):
    """Prestacao de contas mensal por cidade (transparencia publica).

    Um registro por (cidade, mes, ano). Valores em reais. O saldo e derivado
    (valor_patrocinado - soma dos custos) e nao e persistido.
    """
    __tablename__ = 'prestacao_contas'
    id                = db.Column(db.Integer, primary_key=True)
    cidade            = db.Column(db.String(100), nullable=False, index=True)
    mes               = db.Column(db.Integer, nullable=False)
    ano               = db.Column(db.Integer, nullable=False)
    valor_patrocinado = db.Column(db.Float, default=0.0)
    custo_medalhas    = db.Column(db.Float, default=0.0)
    custo_arte        = db.Column(db.Float, default=0.0)
    custo_energia     = db.Column(db.Float, default=0.0)
    outros_custos     = db.Column(db.Float, default=0.0)
    observacao        = db.Column(db.Text, nullable=True)
    criado_em         = db.Column(db.DateTime, default=datetime.utcnow)
    atualizado_em     = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    modificado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    comprovantes = db.relationship(
        'ComprovantePrestacao', backref='prestacao', lazy=True,
        cascade='all, delete-orphan',
    )
    __table_args__ = (
        db.UniqueConstraint('cidade', 'mes', 'ano', name='_prestacao_cidade_mes_ano_uc'),
    )

    @property
    def total_custos(self):
        return (
            (self.custo_medalhas or 0) + (self.custo_arte or 0) +
            (self.custo_energia or 0) + (self.outros_custos or 0)
        )

    @property
    def saldo(self):
        return (self.valor_patrocinado or 0) - self.total_custos

    def __repr__(self):
        return f'<PrestacaoContas {self.cidade} - {self.mes}/{self.ano}>'


class ComprovantePrestacao(db.Model):
    """Comprovante (imagem/PDF) anexado a uma prestacao de contas."""
    __tablename__ = 'comprovante_prestacao'
    id           = db.Column(db.Integer, primary_key=True)
    prestacao_id = db.Column(db.Integer, db.ForeignKey('prestacao_contas.id'), nullable=False)
    arquivo_nome = db.Column(db.String(255), nullable=False)
    arquivo_path = db.Column(db.String(500), nullable=False)
    tipo_mime    = db.Column(db.String(100))
    tamanho      = db.Column(db.Integer)
    enviado_por  = db.Column(db.String(120))
    enviado_em   = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f'<ComprovantePrestacao prestacao={self.prestacao_id} {self.arquivo_nome}>'


class Adesao(db.Model):
    """Quem entrou no projeto pela porta da frente, e o que aceitou.

    POR QUE ESTA TABELA EXISTE (P-100, DOC E)
    Ate 28/08/2026 so se entrava no projeto sendo adicionado ao grupo de
    WhatsApp por alguem. Isso e' o que o Decreto 12.861/2026 (art. 25, I) chama
    de "circuito privado", e reprova o projeto no Ministerio do Esporte. Aqui a
    pessoa entra sozinha, sem login e sem depender de ninguem.

    ⚠️ CADA CONSENTIMENTO E' SEPARADO, E SO O PRIMEIRO E' OBRIGATORIO.
    A LGPD (art. 11) trata comorbidade como DADO SENSIVEL: exige consentimento
    especifico, finalidade declarada e revogacao possivel. Amarrar isso ao
    aceite geral -- "aceito tudo" -- e' exatamente o que a lei nao admite, e
    seria a primeira coisa que um analista olharia.

    ⚠️ `observacao_saude` NUNCA aparece em pagina publica, relatorio exportado
    ou lista que saia da associacao sem anonimizar.
    """
    __tablename__ = 'adesoes'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    nome = db.Column(db.String(120), nullable=False)
    telefone = db.Column(db.String(20), nullable=True, index=True)
    email = db.Column(db.String(150), nullable=True)
    strava_id = db.Column(db.String(50), nullable=True, index=True)

    # --- o aceite obrigatorio ---
    versao_termos = db.Column(db.String(20), nullable=False)
    aceite_em = db.Column(db.DateTime, default=datetime.utcnow)
    aceite_ip = db.Column(db.String(45), nullable=True)
    aceite_navegador = db.Column(db.String(250), nullable=True)

    # --- os dois consentimentos opcionais, cada um com a sua data ---
    consentimento_saude = db.Column(db.Boolean, default=False)
    consentimento_saude_em = db.Column(db.DateTime, nullable=True)
    observacao_saude = db.Column(db.Text, nullable=True)  # DADO SENSIVEL

    consentimento_imagem = db.Column(db.Boolean, default=False)
    consentimento_imagem_em = db.Column(db.DateTime, nullable=True)

    # A pessoa escolhe se o nome dela aparece nas paginas publicas.
    aparecer_publico = db.Column(db.Boolean, default=True)

    # Chave que a propria pessoa usa para voltar e mudar de ideia, sem login.
    chave = db.Column(db.String(64), unique=True, nullable=False, index=True)

    revogado_em = db.Column(db.DateTime, nullable=True)
    revogado_motivo = db.Column(db.String(300), nullable=True)

    # Conta criada a partir desta adesao (o admin ativa depois).
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def ativa(self):
        return self.revogado_em is None

    def __repr__(self):
        return f'<Adesao {self.nome} - {self.cidade}>'


class LancamentoCaixa(db.Model):
    """Livro-caixa do projeto: uma linha por entrada ou saída (P-97).

    POR QUE ISTO EXISTE, ao lado da `PrestacaoContas`
    A `PrestacaoContas` é a FOTO do mês -- quatro totais e um saldo, feita para
    a página pública. Ela não diz o que foi comprado, de quem, com qual
    documento. Sem isso não há escrituração, e sem escrituração não há isenção
    fiscal nem prestação de contas ao Ministério que se sustente.

    Aqui é o detalhe: cada gasto e cada apoio, com data, documento, categoria e
    comprovante. Os totais da `PrestacaoContas` passam a poder ser CONFERIDOS
    contra a soma daqui, em vez de digitados de memória.

    ⚠️ O PÚBLICO VÊ TOTAL POR CATEGORIA, NUNCA A LINHA. Nome de fornecedor,
    número de documento e valor unitário são internos: transparência é mostrar
    para onde foi o dinheiro, não expor com quem se comprou.
    """
    __tablename__ = 'lancamentos_caixa'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    data = db.Column(db.Date, nullable=False, index=True)
    tipo = db.Column(db.String(10), nullable=False)          # 'entrada' | 'saida'
    descricao = db.Column(db.String(300), nullable=False)
    categoria = db.Column(db.String(60), nullable=False, index=True)
    valor = db.Column(db.Float, nullable=False)

    # documento fiscal: o que transforma gasto em gasto comprovável
    documento_tipo = db.Column(db.String(20), nullable=True)  # 'nf' | 'recibo' | 'sem'
    documento_numero = db.Column(db.String(60), nullable=True)
    contraparte = db.Column(db.String(160), nullable=True)    # de quem / para quem

    # vínculo opcional com a rubrica do orçamento aprovado (Lei de Incentivo)
    rubrica = db.Column(db.String(120), nullable=True)

    observacao = db.Column(db.Text, nullable=True)
    anexo_nome = db.Column(db.String(255), nullable=True)
    anexo_path = db.Column(db.String(500), nullable=True)
    anexo_mime = db.Column(db.String(100), nullable=True)

    # Endereço ALEATÓRIO do comprovante, para a planilha e o PDF que o dono envia:
    # quem recebe abre o arquivo sem login, mas só quem tem o link. Nasce com a
    # linha; o master pode trocá-lo (o antigo deixa de funcionar).
    link_token = db.Column(db.String(60), nullable=True, unique=True, index=True)

    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    @property
    def sinal(self):
        """+1 para entrada, -1 para saída. O valor guardado é sempre positivo."""
        return 1 if (self.tipo or '') == 'entrada' else -1

    @property
    def efeito(self):
        return round((self.valor or 0.0) * self.sinal, 2)

    def __repr__(self):
        return f'<LancamentoCaixa {self.data} {self.tipo} {self.valor}>'


class ConciliacaoCaixa(db.Model):
    """O que o extrato bancário diz, mês a mês, e a diferença explicada.

    Livro-caixa sem conciliação é planilha: ninguém sabe se está completo. Aqui
    se registra o saldo do extrato e, quando ele não bate com o do livro, a
    justificativa -- que é exatamente o que um contador pede no fechamento.
    """
    __tablename__ = 'conciliacoes_caixa'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    ano = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    saldo_extrato = db.Column(db.Float, nullable=True)
    justificativa = db.Column(db.Text, nullable=True)
    conferido_em = db.Column(db.DateTime, default=datetime.utcnow)
    conferido_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    __table_args__ = (
        db.UniqueConstraint('cidade', 'ano', 'mes', name='_conciliacao_cidade_ano_mes_uc'),
    )


class TentativaPublica(db.Model):
    """Uma tentativa em formulário PÚBLICO (cadastro de atleta, pedido de relatório).

    Serve só para limitar: quem erra o telefone de um atleta dez vezes por hora,
    ou pede relatório em rajada, para de ser atendido. Guarda o IP e a chave
    (atleta ou e-mail) -- nada além disso.
    """
    __tablename__ = 'tentativas_publicas'
    id = db.Column(db.Integer, primary_key=True)
    tipo = db.Column(db.String(20), nullable=False)
    chave = db.Column(db.String(120), nullable=True)
    ip = db.Column(db.String(45), nullable=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class PedidoRelatorio(db.Model):
    """Alguém pediu o relatório completo na página pública de transparência.

    O `token` é o endereço da página do relatório e dos comprovantes dentro
    dela; tudo vale até `expira_em` (30 dias). Quem recebe o e-mail do pedido é o
    master (supremo + o da cidade), que só encaminha.
    """
    __tablename__ = 'pedidos_relatorio'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    nome = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), nullable=False)
    telefone = db.Column(db.String(30), nullable=True)
    token = db.Column(db.String(64), nullable=False, unique=True, index=True)
    periodo_de = db.Column(db.Date, nullable=False)
    periodo_ate = db.Column(db.Date, nullable=False)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    expira_em = db.Column(db.DateTime, nullable=False)
    ip = db.Column(db.String(45), nullable=True)
    email_enviado = db.Column(db.Boolean, default=False)
    email_erro = db.Column(db.Text, nullable=True)
    visualizacoes = db.Column(db.Integer, default=0)

    @property
    def vigente(self):
        return self.expira_em is not None and self.expira_em > datetime.utcnow()


class CadastroAtleta(db.Model):
    """Acesso de visualizador pedido pelo próprio atleta, pela página dele.

    Ordem: telefone conferido -> e-mail confirmado (`confirmado_em`) -> aviso ao
    master/admin da cidade -> aprovação (`aprovado_em`). O usuário nasce inativo e
    só passa a valer na aprovação. Não há aprovação obrigatória de nada além
    disto: é o único caso em que uma pessoa de fora ganha login.
    """
    __tablename__ = 'cadastros_atleta'
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=False, index=True)
    strava_id = db.Column(db.String(50), nullable=False)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    token_confirma = db.Column(db.String(64), nullable=False, unique=True, index=True)
    confirma_expira = db.Column(db.DateTime, nullable=False)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    confirmado_em = db.Column(db.DateTime, nullable=True)
    aviso_enviado_em = db.Column(db.DateTime, nullable=True)
    aprovado_em = db.Column(db.DateTime, nullable=True)
    aprovado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    rejeitado_em = db.Column(db.DateTime, nullable=True)
    usuario = db.relationship('Usuario', foreign_keys=[usuario_id])


class TipoAcao(db.Model):
    """Um tipo de ação/evento do projeto e QUAIS campos ele pede (24/09/2026).

    O master cria os tipos ("Corrida", "Pedalada", "Palestra"...) e marca, na
    criação, quais campos aparecem para preencher. `campos` é uma lista separada
    por vírgula com as chaves de `execucao.CAMPOS_ACAO`. Data e nome são sempre
    pedidos.
    """
    __tablename__ = 'tipos_acao'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    nome = db.Column(db.String(30), nullable=False)
    campos = db.Column(db.String(200), nullable=True)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)

    @property
    def lista_campos(self):
        return [c for c in (self.campos or '').split(',') if c]


class HistoricoCaixa(db.Model):
    """Quem lançou, editou ou apagou cada linha do livro-caixa, e quando.

    POR QUE ISTO EXISTE (pedido do dono, 24/09/2026)
    Dinheiro de terceiros passa por este livro. Se ninguém sabe quem mexeu, a
    pergunta "quem lançou essa despesa?" não tem resposta -- e sem resposta a
    confiança acaba. Aqui cada lançamento, edição e exclusão deixa uma linha,
    inclusive as do master supremo.

    ⚠️ `lancamento_id` NÃO é chave estrangeira de propósito: a linha do livro
    pode ser apagada e este registro tem de continuar de pé. `detalhe` guarda o
    retrato da linha (antes e depois), em JSON, para dar para refazer à mão.
    """
    __tablename__ = 'historico_caixa'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    lancamento_id = db.Column(db.Integer, nullable=True, index=True)
    acao = db.Column(db.String(10), nullable=False)          # lancou | editou | apagou
    resumo = db.Column(db.String(300), nullable=True)
    detalhe = db.Column(db.Text, nullable=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    quando = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class AcaoEducativa(db.Model):
    """O que o projeto ENSINOU, com data e autor (P-95).

    POR QUE ISTO SUSTENTA A ISENCAO
    A isencao fiscal e o enquadramento de inclusao social (que dobra a deducao
    do patrocinador, de 2% para 4%) exigem prova de **continuidade** da acao
    educativa. Uma acao semestral isolada nao cumpre; o conteudo que ja e'
    enviado ao grupo toda semana cumpre -- desde que fique **arquivado com data
    e autor**, que e' exatamente o que esta tabela faz. Custo: R$ 0.
    """
    __tablename__ = 'acoes_educativas'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    data = db.Column(db.Date, nullable=False, index=True)
    tema = db.Column(db.String(200), nullable=False)
    ministrante = db.Column(db.String(160), nullable=True)
    canal = db.Column(db.String(30), nullable=True)
    participantes = db.Column(db.Integer, nullable=True)
    descricao = db.Column(db.Text, nullable=True)
    material_nome = db.Column(db.String(255), nullable=True)
    material_path = db.Column(db.String(500), nullable=True)
    material_mime = db.Column(db.String(100), nullable=True)
    # 24/09/2026: a tela virou "ações e eventos do projeto". O tipo diz o que é:
    # 'acao_educativa' | 'evento' | 'pedalada' | 'outro'. O que já existia é ação educativa.
    tipo = db.Column(db.String(30), default='acao_educativa')
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    def __repr__(self):
        return '<AcaoEducativa %s %s>' % (self.data, self.tema)


class AcompanhamentoNutricional(db.Model):
    """O pilar aprofundado: 3 meses de acompanhamento, com criterio registrado.

    ⚠️ O CRITERIO DE ESCOLHA FICA ESCRITO, e por isso e' campo e nao memoria.
    Perante um analista, "por que essa pessoa e nao outra?" e' a pergunta que
    derruba um projeto social. O criterio do DOC D, nesta ordem: manifestou
    interesse -> tem condicao de saude declarada (COM consentimento, LGPD art.
    11) -> cumpriu as metas com regularidade no semestre.

    ⚠️ A CONDICAO DE SAUDE NAO E' COPIADA PARA CA. Ela mora na `Adesao`, onde a
    pessoa a informou e de onde ela pode apaga-la. Aqui fica so a marca de que
    o criterio foi atendido -- espalhar dado sensivel por tabelas e' como se
    perde o controle de quem pode ve-lo.
    """
    __tablename__ = 'acompanhamentos_nutricionais'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    beneficiario_nome = db.Column(db.String(120), nullable=False)
    beneficiario_strava_id = db.Column(db.String(50), nullable=True, index=True)
    adesao_id = db.Column(db.Integer, db.ForeignKey('adesoes.id'), nullable=True)

    criterio_interesse = db.Column(db.Boolean, default=False)
    # ⚠️ SUBSTITUIDO por `criterio_sorteio` (Fase 5 do plano das melhorias,
    # 28/08/2026): a condicao de saude saiu do cadastro publico (Fase 2) --
    # o projeto parou de coletar esse dado, entao ele nao pode mais ser
    # criterio de escolha. Fica so' para registro historico do que ja foi
    # decidido antes disso; nenhuma tela nova grava aqui.
    criterio_saude = db.Column(db.Boolean, default=False)
    criterio_sorteio = db.Column(db.Boolean, default=False)
    criterio_regularidade = db.Column(db.Boolean, default=False)
    criterio_observacao = db.Column(db.Text, nullable=True)

    inicio = db.Column(db.Date, nullable=True)
    fim = db.Column(db.Date, nullable=True)
    profissional = db.Column(db.String(160), nullable=True)

    entregou_plano = db.Column(db.Boolean, default=False)
    entregou_cartilha = db.Column(db.Boolean, default=False)
    entregou_relatorio = db.Column(db.Boolean, default=False)

    custo = db.Column(db.Float, default=0.0)
    documento_numero = db.Column(db.String(60), nullable=True)
    situacao = db.Column(db.String(20), default='em_andamento')
    observacao = db.Column(db.Text, nullable=True)
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)
    criado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    @property
    def entregaveis_ok(self):
        return sum([bool(self.entregou_plano), bool(self.entregou_cartilha),
                    bool(self.entregou_relatorio)])

    def __repr__(self):
        return '<Acompanhamento %s>' % self.beneficiario_nome


class MidiaProjeto(db.Model):
    """Foto, video e documento do projeto -- carregados, nunca editando dado (P-99).

    ⚠️ NADA NASCE PUBLICO. O arquivo entra marcado como interno, e so vira
    publico quando alguem confirma que ha **consentimento de imagem** de quem
    aparece (P-100). Sem consentimento registrado, a midia nao pode ser publica
    nem entrar em relatorio que saia do projeto.

    ⚠️ VIDEO NAO RODA NO DS215j. 512 MB de RAM nao servem video. O fluxo e':
    sobe aqui (interno) -> alguem publica no YouTube a mao -> o link e'
    registrado -> ai sim vira publico. O relatorio embute as fotos e LISTA os
    links dos videos, nunca tenta servi-los.
    """
    __tablename__ = 'midias_projeto'
    id = db.Column(db.Integer, primary_key=True)
    cidade = db.Column(db.String(100), nullable=False, index=True)
    tipo = db.Column(db.String(20), nullable=False)
    titulo = db.Column(db.String(200), nullable=False)
    descricao = db.Column(db.Text, nullable=True)
    data = db.Column(db.Date, nullable=True, index=True)

    arquivo_nome = db.Column(db.String(255), nullable=True)
    arquivo_path = db.Column(db.String(500), nullable=True)
    arquivo_mime = db.Column(db.String(100), nullable=True)
    arquivo_bytes = db.Column(db.Integer, nullable=True)

    link_youtube = db.Column(db.String(300), nullable=True)

    publico = db.Column(db.Boolean, default=False)
    consentimento_confirmado = db.Column(db.Boolean, default=False)
    consentimento_observacao = db.Column(db.String(300), nullable=True)

    enviado_em = db.Column(db.DateTime, default=datetime.utcnow)
    enviado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)
    liberado_em = db.Column(db.DateTime, nullable=True)
    liberado_por_id = db.Column(db.Integer, db.ForeignKey('usuarios.id'), nullable=True)

    # 24/09/2026: a que ação/evento esta mídia pertence. Sem vínculo, a foto fica
    # solta e não prova nada -- é o que o dono pediu para não acontecer.
    acao_id = db.Column(db.Integer, db.ForeignKey('acoes_educativas.id'),
                        nullable=True, index=True)
    acao = db.relationship('AcaoEducativa', backref=db.backref('midias', lazy='select'))

    @property
    def pode_ser_publica(self):
        """Video so e' publico com link do YouTube; qualquer midia, so com consentimento."""
        if not self.consentimento_confirmado:
            return False
        if self.tipo == 'video':
            return bool(self.link_youtube)
        return True

    def __repr__(self):
        return '<MidiaProjeto %s %s>' % (self.tipo, self.titulo)


def ensure_runtime_schema():
    """Aplica apenas adicoes idempotentes em instalacoes ja existentes."""
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    def _add_columns(conn, table, statements):
        if table not in tables:
            return
        for stmt in statements:
            try:
                conn.execute(text(stmt))
            except Exception as exc:
                msg = str(exc).lower()
                if 'duplicate column name' not in msg and 'already exists' not in msg:
                    raise

    def _migrate_processed_files_periodo_uc(conn, tables):
        """Recria processed_files trocando o UNIQUE(hash_arquivo, cidade) antigo
        por UNIQUE(hash_arquivo, cidade, tipo_periodo, valor_periodo).
        SQLite nao suporta ALTER TABLE para trocar constraint, entao a tabela e
        reconstruida uma unica vez (idempotente: checa o SQL salvo em
        sqlite_master e sai se a constraint nova ja estiver aplicada)."""
        if 'processed_files' not in tables:
            return
        row = conn.execute(text(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='processed_files'"
        )).fetchone()
        # 'valor_periodo' aparece no SQL assim que a coluna e adicionada via
        # ALTER TABLE, mesmo sem a constraint nova -- por isso o guard checa o
        # NOME da constraint (so existe apos a reconstrucao), nao a coluna.
        if not row or not row[0] or '_hash_cidade_periodo_uc' in row[0]:
            return
        conn.execute(text("ALTER TABLE processed_files RENAME TO processed_files_old_uc"))
        conn.execute(text("""
            CREATE TABLE processed_files (
                id INTEGER PRIMARY KEY,
                arquivo_nome VARCHAR(255) NOT NULL,
                cidade VARCHAR(100) NOT NULL,
                hash_arquivo VARCHAR(64) NOT NULL,
                total_atividades INTEGER DEFAULT 0,
                atividades_validas INTEGER DEFAULT 0,
                processado_em DATETIME,
                processado_por INTEGER REFERENCES usuarios (id),
                tempo_processamento FLOAT,
                status VARCHAR(20) DEFAULT 'sucesso',
                mensagem_erro TEXT,
                links_encontrados_mes INTEGER DEFAULT 0,
                atividades_pendentes INTEGER DEFAULT 0,
                links_scrapeados INTEGER DEFAULT 0,
                pendencias_json TEXT,
                tipo_periodo VARCHAR(30) DEFAULT '',
                valor_periodo VARCHAR(120) DEFAULT '',
                CONSTRAINT _hash_cidade_periodo_uc UNIQUE (hash_arquivo, cidade, tipo_periodo, valor_periodo)
            )
        """))
        conn.execute(text("""
            INSERT INTO processed_files (
                id, arquivo_nome, cidade, hash_arquivo, total_atividades,
                atividades_validas, processado_em, processado_por, tempo_processamento,
                status, mensagem_erro, links_encontrados_mes, atividades_pendentes,
                links_scrapeados, pendencias_json, tipo_periodo, valor_periodo
            )
            SELECT
                id, arquivo_nome, cidade, hash_arquivo, total_atividades,
                atividades_validas, processado_em, processado_por, tempo_processamento,
                status, mensagem_erro, links_encontrados_mes, atividades_pendentes,
                links_scrapeados, pendencias_json, tipo_periodo, valor_periodo
            FROM processed_files_old_uc
        """))
        conn.execute(text("DROP TABLE processed_files_old_uc"))

    def _bootstrap_master_supremo(conn):
        """A instalacao nunca pode ficar sem dono.

        Se ainda nao existe nenhum master supremo, promove o master ativo mais
        antigo. Roda uma unica vez na pratica (assim que existe um supremo, a
        condicao nunca mais e verdadeira) e evita o cenario em que o novo nivel
        entra no ar e ninguem consegue abrir as telas que so ele abre.
        """
        if 'usuarios' not in tables:
            return
        try:
            ja_tem = conn.execute(text(
                "SELECT COUNT(*) FROM usuarios WHERE tipo = :t"
            ), {'t': TIPO_SUPREMO}).scalar()
            if ja_tem:
                return
            alvo = conn.execute(text(
                "SELECT id, email FROM usuarios WHERE tipo = :m AND ativo = 1 "
                "ORDER BY id LIMIT 1"
            ), {'m': TIPO_MASTER}).fetchone()
            if not alvo:
                alvo = conn.execute(text(
                    "SELECT id, email FROM usuarios ORDER BY id LIMIT 1"
                )).fetchone()
            if not alvo:
                return
            conn.execute(text("UPDATE usuarios SET tipo = :t WHERE id = :i"),
                         {'t': TIPO_SUPREMO, 'i': alvo[0]})
            import logging as _logging
            _logging.getLogger('app').warning(
                'Master supremo definido automaticamente: %s (id=%s). '
                'Ele e o unico que atravessa a fronteira de cidade.',
                alvo[1], alvo[0],
            )
        except Exception as exc:
            import logging as _logging
            _logging.getLogger('app').warning(
                'Nao consegui definir o master supremo automaticamente: %s', exc)

    with db.engine.begin() as conn:
        _add_columns(conn, 'usuarios', (
            "ALTER TABLE usuarios ADD COLUMN telefone VARCHAR(20)",
            "ALTER TABLE usuarios ADD COLUMN senha_provisoria BOOLEAN DEFAULT 0",
            "ALTER TABLE usuarios ADD COLUMN senha_alterada_em DATETIME",
            "ALTER TABLE usuarios ADD COLUMN envia_ranking BOOLEAN DEFAULT 0",
            "ALTER TABLE usuarios ADD COLUMN envia_avisos BOOLEAN DEFAULT 0",
            "ALTER TABLE usuarios ADD COLUMN ordem_envio INTEGER DEFAULT 0",
        ))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS postagem_agenda (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                horario VARCHAR(5) NOT NULL,
                ativo BOOLEAN DEFAULT 1,
                envia_ranking BOOLEAN DEFAULT 1,
                envia_patrocinadores BOOLEAN DEFAULT 1,
                envia_parabens BOOLEAN DEFAULT 1,
                envia_avisos BOOLEAN DEFAULT 1,
                envia_promocoes BOOLEAN DEFAULT 0,
                criado_em DATETIME,
                CONSTRAINT _agenda_cidade_horario_uc UNIQUE (cidade, horario)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_agenda_cidade ON postagem_agenda (cidade)"))
        if 'usuarios' in tables:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_usuarios_telefone "
                "ON usuarios (telefone)"
            ))
        _add_columns(conn, 'config', (
            "ALTER TABLE config ADD COLUMN pace_max_corrida_curta FLOAT DEFAULT 4.167",
            "ALTER TABLE config ADD COLUMN pace_max_corrida_longa FLOAT DEFAULT 5.0",
            "ALTER TABLE config ADD COLUMN velocidade_min_ciclismo FLOAT",
            "ALTER TABLE config ADD COLUMN velocidade_max_ciclismo FLOAT DEFAULT 45.0",
            "ALTER TABLE config ADD COLUMN qr_pix VARCHAR(255) DEFAULT ''",
            "ALTER TABLE config ADD COLUMN strava_delay_segundos FLOAT DEFAULT 4.0",
            "ALTER TABLE config ADD COLUMN tolerancia_flags INTEGER DEFAULT 3",
            "ALTER TABLE config ADD COLUMN piso_importacao_ano INTEGER",
            "ALTER TABLE config ADD COLUMN piso_importacao_mes INTEGER",
            "ALTER TABLE config ADD COLUMN link_grupo_whatsapp VARCHAR(300)",
            "ALTER TABLE config ADD COLUMN pace_corte_corrida FLOAT DEFAULT 7.0",
            "ALTER TABLE config ADD COLUMN ativa BOOLEAN DEFAULT 0",
        ))
        _add_columns(conn, 'activities', (
            "ALTER TABLE activities ADD COLUMN telefone_whatsapp VARCHAR(20)",
            "ALTER TABLE activities ADD COLUMN pace_min_km FLOAT",
            "ALTER TABLE activities ADD COLUMN distancia_raw FLOAT",
            "ALTER TABLE activities ADD COLUMN gps_fingerprint VARCHAR(40)",
            "ALTER TABLE activities ADD COLUMN status VARCHAR(20) DEFAULT 'ativa'",
            "ALTER TABLE activities ADD COLUMN splits_json TEXT",
            "ALTER TABLE activities ADD COLUMN processed_file_id INTEGER REFERENCES processed_files (id)",
            "ALTER TABLE activities ADD COLUMN excluir_do_anual BOOLEAN DEFAULT 0",
            "ALTER TABLE activities ADD COLUMN fora_do_grupo BOOLEAN DEFAULT 0",
            # O esporte como o Strava diz ('Run', 'Walk', 'Hike', 'Ride'...).
            # Ate 28/08/2026 o dado chegava e era descartado pelo SPORT_MAP.
            "ALTER TABLE activities ADD COLUMN esporte_strava VARCHAR(30)",
            "ALTER TABLE activities ADD COLUMN oculta_por_arquivamento BOOLEAN DEFAULT 0",
            "ALTER TABLE activities ADD COLUMN velocidade_confiavel BOOLEAN DEFAULT 1",
        ))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS acoes_educativas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                data DATE NOT NULL,
                tema VARCHAR(200) NOT NULL,
                ministrante VARCHAR(160),
                canal VARCHAR(30),
                participantes INTEGER,
                descricao TEXT,
                material_nome VARCHAR(255),
                material_path VARCHAR(500),
                material_mime VARCHAR(100),
                criado_em DATETIME,
                criado_por_id INTEGER REFERENCES usuarios (id)
            )
        """))
        _add_columns(conn, 'acoes_educativas', (
            "ALTER TABLE acoes_educativas ADD COLUMN tipo VARCHAR(30) DEFAULT 'acao_educativa'",
        ))
        _add_columns(conn, 'midias_projeto', (
            "ALTER TABLE midias_projeto ADD COLUMN acao_id INTEGER REFERENCES acoes_educativas (id)",
        ))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS acompanhamentos_nutricionais (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                beneficiario_nome VARCHAR(120) NOT NULL,
                beneficiario_strava_id VARCHAR(50),
                adesao_id INTEGER REFERENCES adesoes (id),
                criterio_interesse BOOLEAN DEFAULT 0,
                criterio_saude BOOLEAN DEFAULT 0,
                criterio_regularidade BOOLEAN DEFAULT 0,
                criterio_observacao TEXT,
                inicio DATE,
                fim DATE,
                profissional VARCHAR(160),
                entregou_plano BOOLEAN DEFAULT 0,
                entregou_cartilha BOOLEAN DEFAULT 0,
                entregou_relatorio BOOLEAN DEFAULT 0,
                custo FLOAT DEFAULT 0,
                documento_numero VARCHAR(60),
                situacao VARCHAR(20),
                observacao TEXT,
                criado_em DATETIME,
                criado_por_id INTEGER REFERENCES usuarios (id)
            )
        """))
        _add_columns(conn, 'acompanhamentos_nutricionais', (
            "ALTER TABLE acompanhamentos_nutricionais ADD COLUMN criterio_sorteio BOOLEAN DEFAULT 0",
        ))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS midias_projeto (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                tipo VARCHAR(20) NOT NULL,
                titulo VARCHAR(200) NOT NULL,
                descricao TEXT,
                data DATE,
                arquivo_nome VARCHAR(255),
                arquivo_path VARCHAR(500),
                arquivo_mime VARCHAR(100),
                arquivo_bytes INTEGER,
                link_youtube VARCHAR(300),
                publico BOOLEAN DEFAULT 0,
                consentimento_confirmado BOOLEAN DEFAULT 0,
                consentimento_observacao VARCHAR(300),
                enviado_em DATETIME,
                enviado_por_id INTEGER REFERENCES usuarios (id),
                liberado_em DATETIME,
                liberado_por_id INTEGER REFERENCES usuarios (id)
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS lancamentos_caixa (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                data DATE NOT NULL,
                tipo VARCHAR(10) NOT NULL,
                descricao VARCHAR(300) NOT NULL,
                categoria VARCHAR(60) NOT NULL,
                valor FLOAT NOT NULL,
                documento_tipo VARCHAR(20),
                documento_numero VARCHAR(60),
                contraparte VARCHAR(160),
                rubrica VARCHAR(120),
                observacao TEXT,
                anexo_nome VARCHAR(255),
                anexo_path VARCHAR(500),
                anexo_mime VARCHAR(100),
                criado_em DATETIME,
                criado_por_id INTEGER REFERENCES usuarios (id)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_caixa_cidade_data "
            "ON lancamentos_caixa (cidade, data)"))
        _add_columns(conn, 'lancamentos_caixa', (
            "ALTER TABLE lancamentos_caixa ADD COLUMN link_token VARCHAR(60)",
        ))
        if 'lancamentos_caixa' in tables:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_lancamentos_caixa_link_token "
                "ON lancamentos_caixa (link_token)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS conciliacoes_caixa (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                ano INTEGER NOT NULL,
                mes INTEGER NOT NULL,
                saldo_extrato FLOAT,
                justificativa TEXT,
                conferido_em DATETIME,
                conferido_por_id INTEGER REFERENCES usuarios (id),
                CONSTRAINT _conciliacao_cidade_ano_mes_uc UNIQUE (cidade, ano, mes)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tipos_acao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                nome VARCHAR(30) NOT NULL,
                campos VARCHAR(200),
                criado_por_id INTEGER REFERENCES usuarios (id),
                criado_em DATETIME
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS tentativas_publicas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo VARCHAR(20) NOT NULL,
                chave VARCHAR(120),
                ip VARCHAR(45),
                criado_em DATETIME
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_tentativas_publicas_criado_em "
            "ON tentativas_publicas (criado_em)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS pedidos_relatorio (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                nome VARCHAR(100) NOT NULL,
                email VARCHAR(150) NOT NULL,
                telefone VARCHAR(30),
                token VARCHAR(64) NOT NULL,
                periodo_de DATE NOT NULL,
                periodo_ate DATE NOT NULL,
                criado_em DATETIME,
                expira_em DATETIME NOT NULL,
                ip VARCHAR(45),
                email_enviado BOOLEAN DEFAULT 0,
                email_erro TEXT,
                visualizacoes INTEGER DEFAULT 0
            )
        """))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_pedidos_relatorio_token "
            "ON pedidos_relatorio (token)"))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS cadastros_atleta (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL REFERENCES usuarios (id),
                strava_id VARCHAR(50) NOT NULL,
                cidade VARCHAR(100) NOT NULL,
                token_confirma VARCHAR(64) NOT NULL,
                confirma_expira DATETIME NOT NULL,
                criado_em DATETIME,
                confirmado_em DATETIME,
                aviso_enviado_em DATETIME,
                aprovado_em DATETIME,
                aprovado_por_id INTEGER REFERENCES usuarios (id),
                rejeitado_em DATETIME
            )
        """))
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_cadastros_atleta_token "
            "ON cadastros_atleta (token_confirma)"))
        # 24/09/2026: quem lançou, editou e apagou no livro-caixa.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS historico_caixa (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                lancamento_id INTEGER,
                acao VARCHAR(10) NOT NULL,
                resumo VARCHAR(300),
                detalhe TEXT,
                usuario_id INTEGER REFERENCES usuarios (id),
                quando DATETIME
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_historico_caixa_cidade "
            "ON historico_caixa (cidade, quando)"))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS adesoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                nome VARCHAR(120) NOT NULL,
                telefone VARCHAR(20),
                email VARCHAR(150),
                strava_id VARCHAR(50),
                versao_termos VARCHAR(20) NOT NULL,
                aceite_em DATETIME,
                aceite_ip VARCHAR(45),
                aceite_navegador VARCHAR(250),
                consentimento_saude BOOLEAN DEFAULT 0,
                consentimento_saude_em DATETIME,
                observacao_saude TEXT,
                consentimento_imagem BOOLEAN DEFAULT 0,
                consentimento_imagem_em DATETIME,
                aparecer_publico BOOLEAN DEFAULT 1,
                chave VARCHAR(64) NOT NULL UNIQUE,
                revogado_em DATETIME,
                revogado_motivo VARCHAR(300),
                usuario_id INTEGER REFERENCES usuarios (id),
                criado_em DATETIME
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_adesoes_cidade ON adesoes (cidade)"))

        _add_columns(conn, 'patrocinador_catalogo', (
            # Aparece na lista publica da pagina inicial? (28/08/2026)
            # Fica no CATALOGO porque quem nao quer aparecer nao quer em mes
            # nenhum -- o registro mensal tem o mesmo campo so como excecao.
            "ALTER TABLE patrocinador_catalogo ADD COLUMN mostrar_na_home BOOLEAN DEFAULT 1",
        ))

        _add_columns(conn, 'patrocinadores_mensais', (
            "ALTER TABLE patrocinadores_mensais ADD COLUMN mostrar_na_home BOOLEAN DEFAULT 1",
            # Frequencia com que este apoio aparece (06/09/2026). Ver o modelo.
            "ALTER TABLE patrocinadores_mensais ADD COLUMN peso_frequencia INTEGER DEFAULT 3",
        ))
        _add_columns(conn, 'patrocinador_catalogo', (
            "ALTER TABLE patrocinador_catalogo ADD COLUMN peso_frequencia INTEGER DEFAULT 3",
        ))

        _add_columns(conn, 'atletas', (
            # Quem pediu para nao aparecer em pagina publica (28/08/2026).
            "ALTER TABLE atletas ADD COLUMN oculto_no_publico BOOLEAN DEFAULT 0",
            # SEXO, PREENCHIDO A MAO (09/09/2026).
            #
            # O Strava NAO publica esse dado -- a documentacao oficial diz, com
            # todas as letras, que "your gender selection will not appear on
            # your profile page". Medido tambem por raspagem, com e sem cookie:
            # com cookie vem o genero da CONTA LOGADA (o que ja escreveu o nome
            # errado em 62 atletas, ERROS.md 4.39); sem cookie nao vem nada,
            # embora nome e cidade venham. Nao ha o que raspar.
            #
            # Entao e' o dono quem marca, na tela do atleta. 'M', 'F' ou vazio.
            # NUNCA aparece em pagina publica por pessoa: serve so' para a
            # contagem agregada da home (quantas atividades de homens, quantas
            # de mulheres). Sexo de alguem nao e' dado de vitrine.
            "ALTER TABLE atletas ADD COLUMN sexo VARCHAR(1)",
        ))
        _add_columns(conn, 'flag_anticheat', (
            "ALTER TABLE flag_anticheat ADD COLUMN revisao_solicitada BOOLEAN DEFAULT 0",
            "ALTER TABLE flag_anticheat ADD COLUMN justificativa_atleta TEXT",
            "ALTER TABLE flag_anticheat ADD COLUMN recursos_utilizados INTEGER DEFAULT 0",
            "ALTER TABLE flag_anticheat ADD COLUMN max_recursos INTEGER DEFAULT 2",
            "ALTER TABLE flag_anticheat ADD COLUMN solicitado_revisao_em DATETIME",
        ))
        _add_columns(conn, 'atletas', (
            # O nome escolhido para o ranking (29/08/2026). Ver o modelo.
            "ALTER TABLE atletas ADD COLUMN nome_exibicao VARCHAR(60)",
            "ALTER TABLE atletas ADD COLUMN nome_exibicao_por VARCHAR(120)",
            "ALTER TABLE atletas ADD COLUMN nome_exibicao_em DATETIME",
            "ALTER TABLE atletas ADD COLUMN foto_url VARCHAR(500)",
            "ALTER TABLE atletas ADD COLUMN foto_atualizada_em DATETIME",
            "ALTER TABLE atletas ADD COLUMN arquivado BOOLEAN DEFAULT 0",
            "ALTER TABLE atletas ADD COLUMN arquivado_em DATETIME",
            "ALTER TABLE atletas ADD COLUMN arquivado_motivo VARCHAR(200)",
            "ALTER TABLE atletas ADD COLUMN saiu_a_pedido BOOLEAN DEFAULT 0",
            "ALTER TABLE atletas ADD COLUMN tipo_bicicleta VARCHAR(12) DEFAULT 'mtb'",
        ))
        _add_columns(conn, 'atletas', (
            # Mesclagem de atletas duplicados (07/09/2026). Ver o modelo.
            "ALTER TABLE atletas ADD COLUMN atleta_canonico_id INTEGER REFERENCES atletas (id)",
            "ALTER TABLE atletas ADD COLUMN mesclado_em DATETIME",
            "ALTER TABLE atletas ADD COLUMN mesclado_por VARCHAR(120)",
        ))
        if 'atletas' in tables:
            try:
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS idx_atleta_canonico ON atletas (atleta_canonico_id)"))
            except Exception:
                pass
        _add_columns(conn, 'controle_disciplinar', (
            "ALTER TABLE controle_disciplinar ADD COLUMN atividades_suspeitas INTEGER DEFAULT 0",
            "ALTER TABLE controle_disciplinar ADD COLUMN pedidos_revisao INTEGER DEFAULT 0",
            "ALTER TABLE controle_disciplinar ADD COLUMN pedidos_revisao_mes INTEGER DEFAULT 0",
        ))
        _add_columns(conn, 'processed_files', (
            "ALTER TABLE processed_files ADD COLUMN links_encontrados_mes INTEGER DEFAULT 0",
            "ALTER TABLE processed_files ADD COLUMN atividades_pendentes INTEGER DEFAULT 0",
            "ALTER TABLE processed_files ADD COLUMN links_scrapeados INTEGER DEFAULT 0",
            "ALTER TABLE processed_files ADD COLUMN pendencias_json TEXT",
            "ALTER TABLE processed_files ADD COLUMN tipo_periodo VARCHAR(30) DEFAULT ''",
            "ALTER TABLE processed_files ADD COLUMN valor_periodo VARCHAR(120) DEFAULT ''",
        ))
        _migrate_processed_files_periodo_uc(conn, tables)
        _add_columns(conn, 'patrocinadores_mensais', (
            "ALTER TABLE patrocinadores_mensais ADD COLUMN categoria VARCHAR(20) DEFAULT 'adicional'",
            "ALTER TABLE patrocinadores_mensais ADD COLUMN telefone VARCHAR(30)",
            "ALTER TABLE patrocinadores_mensais ADD COLUMN bordao VARCHAR(200)",
            "ALTER TABLE patrocinadores_mensais ADD COLUMN legenda TEXT",
            "ALTER TABLE patrocinadores_mensais ADD COLUMN nome_exibicao VARCHAR(200)",
            "ALTER TABLE patrocinadores_mensais ADD COLUMN catalogo_id INTEGER",
            "ALTER TABLE patrocinadores_mensais ADD COLUMN o_que_banca VARCHAR(200)",
            "ALTER TABLE patrocinadores_mensais ADD COLUMN banca_medalhas BOOLEAN DEFAULT 0",
            "ALTER TABLE patrocinadores_mensais ADD COLUMN anunciado_em DATETIME",
            "ALTER TABLE patrocinadores_mensais ADD COLUMN patrocinio_continuo BOOLEAN DEFAULT 0",
        ))
        _add_columns(conn, 'patrocinador_catalogo', (
            "ALTER TABLE patrocinador_catalogo ADD COLUMN o_que_banca VARCHAR(200)",
            "ALTER TABLE patrocinador_catalogo ADD COLUMN banca_medalhas BOOLEAN DEFAULT 0",
            "ALTER TABLE patrocinador_catalogo ADD COLUMN patrocinio_continuo BOOLEAN DEFAULT 0",
            # Estas duas tambem sao criadas pelo `avisos.ensure_schema`. Ficam
            # aqui de proposito: o modulo que DECLARA a coluna no modelo e o que
            # tem de garantir a coluna na tabela. Depender de outro modulo rodar
            # primeiro e como o defeito de 14/08, so que ao contrario.
            "ALTER TABLE patrocinador_catalogo ADD COLUMN endereco VARCHAR(300)",
            "ALTER TABLE patrocinador_catalogo ADD COLUMN maps_url VARCHAR(500)",
        ))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS migracoes_unicas (
                nome VARCHAR(120) PRIMARY KEY,
                executada_em DATETIME
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS texto_mensagem (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                chave VARCHAR(40) NOT NULL,
                corpo TEXT NOT NULL,
                atualizado_em DATETIME,
                atualizado_por VARCHAR(120),
                CONSTRAINT _texto_cidade_chave_uc UNIQUE (cidade, chave)
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS patrocinador_catalogo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                nome_interno VARCHAR(200) NOT NULL,
                nome_exibicao VARCHAR(200),
                patrocinador_logo_blob BLOB,
                patrocinador_logo_mime VARCHAR(100),
                patrocinador_logo_filename VARCHAR(255),
                telefone VARCHAR(30),
                bordao VARCHAR(200),
                legenda TEXT,
                ativo BOOLEAN DEFAULT 1,
                criado_em DATETIME,
                atualizado_em DATETIME,
                criado_por INTEGER,
                CONSTRAINT _catalogo_cidade_nome_uc UNIQUE (cidade, nome_interno)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_catalogo_cidade "
            "ON patrocinador_catalogo (cidade)"
        ))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS evidencia_revisao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                flag_id INTEGER NOT NULL REFERENCES flag_anticheat (id),
                arquivo_nome VARCHAR(255) NOT NULL,
                arquivo_path VARCHAR(500) NOT NULL,
                tipo_mime VARCHAR(100),
                tamanho INTEGER,
                enviado_por VARCHAR(50) NOT NULL,
                enviado_em DATETIME
            )
        """))
        if 'activities' in tables:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS idx_activities_usuario "
                "ON activities (id_usuario_strava)"
            ))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS anticheat_override (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strava_id VARCHAR(50) NOT NULL UNIQUE,
                nome_atleta VARCHAR(100),
                cidade VARCHAR(100),
                pace_max_corrida_curta FLOAT,
                pace_max_corrida_longa FLOAT,
                velocidade_min_ciclismo FLOAT,
                velocidade_max_ciclismo FLOAT,
                observacao VARCHAR(255),
                criado_em DATETIME,
                criado_por INTEGER
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_anticheat_override_cidade "
            "ON anticheat_override (cidade)"
        ))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                tipo VARCHAR(30) NOT NULL,
                nome_atleta VARCHAR(100),
                mensagem TEXT NOT NULL,
                contato VARCHAR(120),
                status VARCHAR(20) NOT NULL DEFAULT 'nova',
                criado_em DATETIME,
                resolvido_em DATETIME,
                resolvido_por_id INTEGER
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_feedback_cidade_status "
            "ON feedback (cidade, status)"
        ))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS prestacao_contas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                mes INTEGER NOT NULL,
                ano INTEGER NOT NULL,
                valor_patrocinado FLOAT DEFAULT 0.0,
                custo_medalhas FLOAT DEFAULT 0.0,
                custo_arte FLOAT DEFAULT 0.0,
                custo_energia FLOAT DEFAULT 0.0,
                outros_custos FLOAT DEFAULT 0.0,
                observacao TEXT,
                criado_em DATETIME,
                atualizado_em DATETIME,
                modificado_por_id INTEGER,
                CONSTRAINT _prestacao_cidade_mes_ano_uc UNIQUE (cidade, mes, ano)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_prestacao_cidade "
            "ON prestacao_contas (cidade, ano, mes)"
        ))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS comprovante_prestacao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prestacao_id INTEGER NOT NULL REFERENCES prestacao_contas (id),
                arquivo_nome VARCHAR(255) NOT NULL,
                arquivo_path VARCHAR(500) NOT NULL,
                tipo_mime VARCHAR(100),
                tamanho INTEGER,
                enviado_por VARCHAR(120),
                enviado_em DATETIME
            )
        """))

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS patrocinadores_mensais (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                mes INTEGER NOT NULL,
                ano INTEGER NOT NULL,
                patrocinador_nome VARCHAR(200) NOT NULL,
                patrocinador_logo_blob BLOB,
                patrocinador_logo_mime VARCHAR(100),
                patrocinador_logo_filename VARCHAR(255),
                ordem INTEGER NOT NULL DEFAULT 1,
                categoria VARCHAR(20) DEFAULT 'adicional',
                telefone VARCHAR(30),
                bordao VARCHAR(200),
                legenda TEXT,
                criado_em DATETIME,
                criado_por INTEGER,
                CONSTRAINT _cidade_mes_ano_ordem_uc UNIQUE (cidade, mes, ano, ordem)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_patrocinadores_mensais_periodo "
            "ON patrocinadores_mensais (cidade, ano, mes)"
        ))

        _bootstrap_master_supremo(conn)
