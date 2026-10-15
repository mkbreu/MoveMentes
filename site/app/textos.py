# -*- coding: utf-8 -*-
"""Os textos que o sistema manda — editaveis, com padrao de fabrica.

COMO FUNCIONA
Cada texto tem um PADRAO escrito aqui no codigo. A cidade pode reescrever o
seu na tela; o que ela salvar fica na tabela `texto_mensagem`. "Restaurar o
padrao" apaga a linha da cidade e volta a valer o daqui.

Isso resolve dois problemas de uma vez: o operador muda o tom sem depender de
programador, e nenhuma cidade fica sem texto — se ninguem editou, o padrao
sai igual.

AS ETIQUETAS ENTRE CHAVES
Sao trocadas na hora do envio: {mes}, {patrocinador}, {atletas}, {km}...
Se uma etiqueta nao existir, ela some do texto em vez de aparecer crua — um
"{patrocinadorr}" digitado errado nao vai para o grupo.
"""
import logging
import re
from datetime import datetime

from sqlalchemy import text as sa_text

from app import db

logger = logging.getLogger(__name__)

# ---- chaves ----
AGRADECIMENTO = 'agradecimento'
ABERTURA_COM = 'abertura_com_patrocinador'
ABERTURA_SEM = 'abertura_sem_patrocinador'
PATROCINIO_ANTES = 'patrocinio_garantido'
PATROCINIO_DEPOIS = 'patrocinio_encontrado'
COBRANCA_MEDALHA = 'cobranca_medalha'
MEDALHAS_NO_GRUPO = 'medalhas_no_grupo'
RENOVACAO_PATROCINIO = 'renovacao_patrocinio'
PROSPECCAO_PATROCINIO = 'prospeccao_patrocinio'
FECHAMENTO_MES = 'fechamento_mes'

# Fecho do agradecimento. Muda conforme o patrocinio se repete ou nao — para
# quem banca todo mes, "contamos com voce no ano que vem" soa como despedida.
FECHO_UNICO = 'Contamos com o seu patrocínio no ano que vem.'
FECHO_CONTINUO = 'Até o mês que vem — e obrigado por estar sempre com a gente.'

PADROES = {
    AGRADECIMENTO: {
        'titulo': 'Agradecimento ao patrocinador',
        'onde': 'No privado do patrocinador',
        'quando': 'Dia 1º, sobre o mês que fechou',
        'etiquetas': '{cidade} {mes} {patrocinador} {atletas} {km} {corrida} {ciclismo} {fecho} {link}',
        'corpo': (
            # Tambem e privado: se apresenta antes de falar (P-25).
            'Olá! Aqui é o Assistente do Grupo MoveMentes {cidade}.\n'
            '\n'
            'O grupo agradece o seu patrocínio do mês de *{mes}*.\n'
            '\n'
            '*{patrocinador}* fez com que *{atletas} pessoas* desta cidade saíssem '
            'de casa e se movimentassem. Juntas, elas somaram:\n'
            '\n'
            '📍 *{km} km* percorridos\n'
            '🏃 *{corrida}* correndo e caminhando\n'
            '🚴 *{ciclismo}* pedalando\n'
            '\n'
            'Nosso muito obrigado e parabéns pela iniciativa. Patrocinar o '
            'MoveMentes não é só colocar uma logo num grupo: é o que tira gente '
            'do sedentarismo, faz a galera levantar cedo, sair do comodismo e faz '
            'da cidade um lugar mais saudável para todo mundo. Nada disso teria '
            'acontecido em {mes} sem você.\n'
            '\n'
            '{fecho}\n'
            '\n'
            '{link}'
        ),
    },
    ABERTURA_COM: {
        'titulo': 'Abertura do mês (com patrocinador)',
        'onde': 'No grupo',
        'quando': 'Dia 1º, quando já há patrocinador cadastrado',
        'etiquetas': '{mes} {meta_corrida} {meta_ciclismo} {patrocinador} {apoios}',
        'corpo': (
            '📅 *{mes} começou!*\n'
            '\n'
            'Contador zerado para todo mundo — o ranking recomeça hoje.\n'
            '\n'
            '*Metas do mês*\n'
            '🏃 Corrida/caminhada: *{meta_corrida} km*\n'
            '🚴 Ciclismo: *{meta_ciclismo} km*\n'
            'Bateu a meta, ganha medalha. 🏅\n'
            '\n'
            '🤝 Patrocinador do mês: {patrocinador}\n'
            '{apoios}'
        ),
    },
    ABERTURA_SEM: {
        'titulo': 'Abertura do mês (ainda sem patrocinador)',
        'onde': 'No grupo',
        'quando': 'Dia 1º, quando o mês começa sem patrocinador',
        'etiquetas': '{mes} {meta_corrida} {meta_ciclismo}',
        'corpo': (
            '📅 *{mes} começou!* — ainda buscando patrocinador...\n'
            '\n'
            'Ranking zerado! Caso alguém do grupo conheça um comerciante, queira '
            'patrocinar ou ser um patrocinador independente, fale com um dos '
            'admins.\n'
            '\n'
            '*Metas do mês*\n'
            '🏃 Corrida/caminhada: *{meta_corrida} km*\n'
            '🚴 Ciclismo: *{meta_ciclismo} km*\n'
            'Bateu a meta, ganha medalha. 🏅'
        ),
    },
    PATROCINIO_ANTES: {
        'titulo': 'Patrocínio garantido (antes do mês começar)',
        'onde': 'No grupo',
        'quando': 'Quando o patrocinador é cadastrado ANTES do dia 1º',
        'etiquetas': '{mes} {patrocinador} {banca} {apoios}',
        'corpo': (
            '🎉 *{mes} já tem patrocinador!*\n'
            '\n'
            '*{patrocinador}* vai bancar o mês de {mes} aqui no MoveMentes.\n'
            '{banca}'
            '{apoios}'
            '\n'
            'Ou seja: pode ir separando o tênis, porque medalha vai ter. '
            'Nosso muito obrigado a quem acredita no projeto antes mesmo '
            'de o mês começar. 👏'
        ),
    },
    PATROCINIO_DEPOIS: {
        'titulo': 'Patrocinador encontrado (mês já em andamento)',
        'onde': 'No grupo',
        'quando': 'Quando o patrocinador é cadastrado DEPOIS do dia 1º',
        'etiquetas': '{mes} {patrocinador} {banca} {apoios}',
        'corpo': (
            '🎉 *Achamos o patrocinador de {mes}!*\n'
            '\n'
            '*{patrocinador}* é o patrocinador do mês.\n'
            '{banca}'
            '{apoios}'
            '\n'
            'Quem já está somando quilômetro pode comemorar: a medalha de {mes} '
            'está garantida. Nosso muito obrigado! 👏'
        ),
    },
    FECHAMENTO_MES: {
        'titulo': 'Encerramento do mês',
        'onde': 'No grupo',
        'quando': 'Dia 1º, sobre o mês que acabou de fechar',
        'etiquetas': '{mes} {atletas} {medalhas} {km} {corrida} {ciclismo} {local} {link}',
        'corpo': (
            # NAO PROMETE LOCAL NEM PRAZO (ajuste do dono, 12/08): no dia 1o a
            # medalha ainda esta sendo produzida e o local pode nem estar
            # fechado. Prometer "retire em X" e depois mudar custa mais caro
            # que nao prometer.
            '🏁 *{mes} fechou. Parabéns a todos os atletas!*\n'
            '\n'
            'O contador parou. Foram *{atletas} pessoas* na rua e *{km} km* '
            'somados por esta cidade.\n'
            '\n'
            '🏅 *{medalhas} medalhas* conquistadas\n'
            '🏃 *{corrida}* correndo e caminhando\n'
            '🚴 *{ciclismo}* pedalando\n'
            '\n'
            'Medalhas em produção! Em breve divulgaremos o local de entrega.\n'
            '\n'
            'Ranking completo do mês: {link}'
        ),
    },
    RENOVACAO_PATROCINIO: {
        'titulo': 'Renovação de patrocínio',
        'onde': 'No privado do patrocinador',
        'quando': 'Quando você quiser convidar para o próximo ciclo',
        'etiquetas': '{cidade} {mes} {patrocinador} {atletas} {km} {medalhas} {link}',
        'corpo': (
            # AGRADECE PRIMEIRO, PEDE DEPOIS. Quem bancou merece ler o
            # resultado antes do convite — e o resultado E o argumento: numero
            # de gente que saiu de casa por causa dele.
            'Olá! Aqui é o Assistente do Grupo MoveMentes {cidade}.\n'
            '\n'
            'Passando para agradecer de novo pelo patrocínio de *{mes}*.\n'
            '\n'
            'O que a sua marca ajudou a fazer acontecer:\n'
            '📍 *{km} km* percorridos por *{atletas} pessoas* desta cidade\n'
            '🏅 *{medalhas} medalhas* entregues\n'
            '\n'
            'Cada uma dessas pessoas saiu de casa, se movimentou e levou para '
            'casa uma medalha com o seu nome nela.\n'
            '\n'
            'Gostaríamos muito de contar com você outra vez. Se fizer sentido, '
            'me avise que eu passo os detalhes do próximo ciclo.\n'
            '\n'
            'Ranking do mês: {link}\n'
            '\n'
            'Sigo à disposição para maiores esclarecimentos.'
        ),
    },
    PROSPECCAO_PATROCINIO: {
        'titulo': 'Convite a um novo patrocinador',
        'onde': 'No privado de quem você quer convidar',
        'quando': 'Quando você tiver um candidato',
        'etiquetas': '{nome} {cidade} {membros} {meta_corrida} {meta_ciclismo}',
        'corpo': (
            # A VENDA E A EXPOSICAO, nao a caridade. O numero de aparicoes por
            # dia e o argumento mais forte e vem cedo no texto; o lado social
            # vem depois, como reforco — nessa ordem porque quem paga decide
            # por retorno e se sente bem pelo resto.
            'Olá, {nome}! Sou o administrador do Grupo Esportivo MoveMentes '
            '{cidade}.\n'
            '\n'
            'Somos um grupo de *{membros} moradores* daqui que se movimentam '
            'todo dia — caminhada, corrida e ciclismo — e registram as '
            'atividades num ranking mensal.\n'
            '\n'
            '*Como funciona o patrocínio:*\n'
            'Durante o mês inteiro, a sua marca aparece no grupo *pelo menos '
            'duas vezes por dia*, junto com o ranking que todo mundo abre para '
            'ver a própria posição. É a sua logo, com o seu nome, na frente de '
            '{membros} pessoas da cidade, todos os dias.\n'
            '\n'
            'No fim do mês, quem passa de *{meta_corrida} km* correndo ou '
            'caminhando e quem passa de *{meta_ciclismo} km* pedalando recebe '
            'uma medalha — e ela é entregue no seu estabelecimento, o que leva '
            'essas pessoas até o seu balcão.\n'
            '\n'
            'Além da exposição, é um projeto que tira gente do sedentarismo e '
            'promove saúde na nossa cidade. Sua marca fica associada a isso.\n'
            '\n'
            'Sigo à disposição para maiores esclarecimentos.'
        ),
    },
    MEDALHAS_NO_GRUPO: {
        'titulo': 'Medalhas disponíveis (no grupo)',
        'onde': 'No grupo',
        'quando': 'Quando as medalhas chegam ao estabelecimento',
        'etiquetas': '{mes} {patrocinador} {local} {link} {lista}',
        'corpo': (
            # NO GRUPO NAO SE ASSINA (decisao do dono, 12/08): quem fala ali e
            # o grupo, e todo mundo ja sabe de onde vem. Assinatura e coisa de
            # mensagem privada, que chega de numero desconhecido.
            'Olá, pessoal! Espero encontrá-los bem. 🏅\n'
            '\n'
            'Passando para avisar que as *medalhas de {mes}* já estão '
            'disponíveis para retirada.\n'
            'Endereço: {local}\n'
            'Localização: {link}\n'
            'Patrocinador do mês: {patrocinador}\n'
            '\n'
            '*Medalhistas:*\n'
            '{lista}\n'
            '\n'
            'Bora que bora! 🚶🏃🚴'
        ),
    },
    COBRANCA_MEDALHA: {
        'titulo': 'Cobrança de medalha',
        'onde': 'No privado de quem bateu a meta',
        'quando': 'A partir do dia 8, quando você confirmar o envio',
        'etiquetas': '{nome} {cidade} {mes} {local} {link} {patrocinador}',
        'corpo': (
            # QUEM ASSINA FICA NO RODAPE, EM ITALICO (decisao do dono, 12/08).
            # A P-25 pedia identificacao na PRIMEIRA linha, contra golpe — quem
            # recebe nao tem este numero salvo. O que substitui a primeira linha
            # e o NOME DA PESSOA na saudacao: golpe nao sabe o primeiro nome de
            # quem bateu a meta, nem o estabelecimento da cidade. A assinatura
            # continua existindo, so mudou de lugar, como assinatura de e-mail.
            'Olá, {nome}! 🏅\n'
            'Parabéns por ter batido a meta de *{mes}*!\n'
            'Mas falta a parte boa: buscar o prêmio.\n'
            'Patrocinador: {patrocinador}\n'
            '\n'
            'Endereço: {local}\n'
            'Localização: {link}\n'
            '\n'
            'Estará disponível em horário comercial, qualquer dúvida pergunte '
            'no grupo.\n'
            'Grato\n'
            '_Assistente do Grupo MoveMentes {cidade}_'
        ),
    },
}


def _garantir_schema():
    with db.engine.begin() as conn:
        conn.execute(sa_text("""
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


def padrao(chave):
    return (PADROES.get(chave) or {}).get('corpo', '')


def corpo(cidade, chave):
    """O texto que vale para esta cidade: o dela, ou o padrao."""
    _garantir_schema()
    linha = db.session.execute(sa_text(
        'SELECT corpo FROM texto_mensagem WHERE cidade = :c AND chave = :k'
    ), {'c': cidade, 'k': chave}).fetchone()
    if linha and (linha[0] or '').strip():
        return linha[0]
    return padrao(chave)


def editado(cidade, chave):
    """Esta cidade reescreveu este texto?"""
    _garantir_schema()
    return bool(db.session.execute(sa_text(
        'SELECT 1 FROM texto_mensagem WHERE cidade = :c AND chave = :k'
    ), {'c': cidade, 'k': chave}).fetchone())


def salvar(cidade, chave, novo_corpo, por=None):
    _garantir_schema()
    if chave not in PADROES:
        return False
    novo_corpo = (novo_corpo or '').strip()
    if not novo_corpo:
        return restaurar(cidade, chave)
    db.session.execute(sa_text("""
        INSERT INTO texto_mensagem (cidade, chave, corpo, atualizado_em, atualizado_por)
        VALUES (:c, :k, :b, :em, :por)
        ON CONFLICT(cidade, chave) DO UPDATE SET
            corpo = :b, atualizado_em = :em, atualizado_por = :por
    """), {'c': cidade, 'k': chave, 'b': novo_corpo,
           'em': datetime.now(), 'por': por})
    db.session.commit()
    return True


def restaurar(cidade, chave):
    """Volta ao padrao de fabrica: apaga a versao da cidade."""
    _garantir_schema()
    db.session.execute(sa_text(
        'DELETE FROM texto_mensagem WHERE cidade = :c AND chave = :k'
    ), {'c': cidade, 'k': chave})
    db.session.commit()
    return True


_ETIQUETA_RE = re.compile(r'\{([a-z_]+)\}')


def montar(cidade, chave, dados):
    """Troca as etiquetas pelos valores. Etiqueta desconhecida SOME.

    Sumir e melhor que estourar: um "{patrocinadorr}" digitado errado nao
    pode ir para o grupo, nem derrubar o envio inteiro.
    """
    texto = corpo(cidade, chave)

    # {cidade} SEMPRE disponivel, sem o chamador precisar lembrar.
    #
    # E o que permite a mensagem privada se apresentar (P-25). Quem recebe um
    # aviso de medalha recebe de um numero que nao tem salvo: sem dizer de onde
    # vem, a pessoa fica sem saber se e o grupo da cidade dela ou golpe.
    # Injetar aqui, num lugar so, evita que cada ponto que monta mensagem tenha
    # de lembrar — que e como uma regra vira duas e elas divergem.
    dados = dict(dados or {})
    dados.setdefault('cidade', cidade)

    def _troca(m):
        return str(dados.get(m.group(1), '') or '')

    saida = _ETIQUETA_RE.sub(_troca, texto)
    # ROTULO SEM VALOR SOME A LINHA INTEIRA (12/08). "Localização: {link}" com
    # o mapa nao cadastrado virava "Localização:" pendurado no meio da
    # mensagem — pior que a informacao faltando, porque anuncia a falta.
    # Vale para qualquer "Rotulo:" que ficou sem nada depois.
    saida = re.sub(r'(?m)^[^\n:]{1,40}:[ \t]*$\n?', '', saida)
    # Linhas que ficaram vazias porque a etiqueta nao tinha valor (ex.: sem
    # apoios) nao devem virar buracos no meio da mensagem.
    saida = re.sub(r'\n{3,}', '\n\n', saida)
    return saida.strip()
