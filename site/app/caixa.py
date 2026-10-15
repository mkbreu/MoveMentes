# -*- coding: utf-8 -*-
"""As contas do livro-caixa (P-97) -- a conta mora aqui, a tela só mostra.

Separado do blueprint de propósito: os mesmos números alimentam a tela interna,
a visão pública da transparência e o relatório que vai ao Ministério. Se a soma
morasse na rota, o relatório teria de recalcular por conta própria -- e três
lugares somando a mesma coisa é como se produz três números diferentes.
"""
import secrets
from datetime import date, timedelta

from sqlalchemy import func

from app import db
from app.models import LancamentoCaixa, ConciliacaoCaixa, PrestacaoContas

# As categorias vêm do DOC E (P-97). O primeiro item de cada par é o que se
# guarda no banco; o segundo, o que a pessoa lê na tela.
CATEGORIAS = [
    ('medalhas', 'Medalhas'),
    ('premiacao', 'Premiação e brindes'),
    ('nutricao', 'Educação nutricional'),
    ('arte', 'Arte e material gráfico'),
    ('energia', 'Energia e internet'),
    ('equipamento', 'Equipamento e manutenção'),
    ('tarifa', 'Tarifa bancária'),
    ('cartorio', 'Cartório e taxas'),
    ('patrocinio', 'Patrocínio recebido'),
    ('doacao', 'Doação recebida'),
    ('outros', 'Outros'),
]

ROTULO_CATEGORIA = dict(CATEGORIAS)

# Categorias que são ENTRADA por natureza. A tela usa isto para sugerir o tipo
# certo e evitar o erro clássico de lançar patrocínio como despesa.
CATEGORIAS_DE_ENTRADA = {'patrocinio', 'doacao'}

TIPOS = (('entrada', 'Entrada'), ('saida', 'Saída'))

DOCUMENTOS = (
    ('nf', 'Nota fiscal'),
    ('recibo', 'Recibo'),
    ('sem', 'Sem documento'),
)


def _mes_inteiro(ano, mes):
    inicio = date(ano, mes, 1)
    fim = date(ano + (mes == 12), (mes % 12) + 1, 1)
    return inicio, fim


def fim_do_mes(ano, mes):
    """O último dia do mês (a data entra no período)."""
    _, fim = _mes_inteiro(ano, mes)
    return fim - timedelta(days=1)


def lancamentos(cidade, ano=None, mes=None):
    """As linhas do período, da mais recente para a mais antiga."""
    consulta = LancamentoCaixa.query.filter(LancamentoCaixa.cidade == cidade)
    if ano and mes:
        inicio, fim = _mes_inteiro(ano, mes)
        consulta = consulta.filter(LancamentoCaixa.data >= inicio,
                                   LancamentoCaixa.data < fim)
    elif ano:
        consulta = consulta.filter(LancamentoCaixa.data >= date(ano, 1, 1),
                                   LancamentoCaixa.data < date(ano + 1, 1, 1))
    return consulta.order_by(LancamentoCaixa.data.desc(),
                             LancamentoCaixa.id.desc()).all()


def resumo(cidade, ano=None, mes=None):
    """Entradas, saídas, saldo e o total por categoria -- em SQL."""
    filtros = [LancamentoCaixa.cidade == cidade]
    if ano and mes:
        inicio, fim = _mes_inteiro(ano, mes)
        filtros += [LancamentoCaixa.data >= inicio, LancamentoCaixa.data < fim]
    elif ano:
        filtros += [LancamentoCaixa.data >= date(ano, 1, 1),
                    LancamentoCaixa.data < date(ano + 1, 1, 1)]

    por_tipo = dict(db.session.query(
        LancamentoCaixa.tipo, func.coalesce(func.sum(LancamentoCaixa.valor), 0.0)
    ).filter(*filtros).group_by(LancamentoCaixa.tipo).all())

    entradas = round(por_tipo.get('entrada', 0.0), 2)
    saidas = round(por_tipo.get('saida', 0.0), 2)

    categorias = []
    for chave, valor, qtd in db.session.query(
            LancamentoCaixa.categoria,
            func.coalesce(func.sum(LancamentoCaixa.valor), 0.0),
            func.count(),
    ).filter(*filtros).filter(
            LancamentoCaixa.tipo == 'saida').group_by(
            LancamentoCaixa.categoria).all():
        categorias.append({
            'chave': chave,
            'rotulo': ROTULO_CATEGORIA.get(chave, chave),
            'valor': round(valor or 0.0, 2),
            'lancamentos': qtd,
        })
    categorias.sort(key=lambda x: x['valor'], reverse=True)

    # Quanto do que se gastou tem documento fiscal. É o número que um analista
    # olha primeiro, e por isso ele aparece na tela em vez de ficar implícito.
    total_saidas, com_doc = db.session.query(
        func.count(),
        func.sum(db.case(
            # Comprovante anexado também é prova (24/09/2026): antes só valia
            # quem tinha marcado "nota fiscal"/"recibo" no tipo.
            (db.or_(LancamentoCaixa.documento_tipo.in_(('nf', 'recibo')),
                    LancamentoCaixa.anexo_path.isnot(None)), 1), else_=0)),
    ).filter(*filtros).filter(LancamentoCaixa.tipo == 'saida').one()

    return {
        'entradas': entradas,
        'saidas': saidas,
        'saldo': round(entradas - saidas, 2),
        'por_categoria': categorias,
        'saidas_lancadas': total_saidas or 0,
        'saidas_com_documento': int(com_doc or 0),
    }


def conciliacao(cidade, ano, mes):
    return ConciliacaoCaixa.query.filter_by(
        cidade=cidade, ano=ano, mes=mes).first()


def sincronizar_prestacao(cidade, ano, mes, usuario_id=None):
    """Leva os totais do livro-caixa para a foto pública do mês (P-96).

    POR QUE ISTO EXISTE
    A `PrestacaoContas` estava VAZIA -- zero registros -- e a página pública de
    transparência renderizava sem nenhum mês. Para quem audita, transparência
    vazia é pior que não ter. Preenchê-la à mão, de memória, seria repetir o
    problema com outra roupa: dois lugares com o mesmo número, divergindo.

    Aqui a foto do mês passa a ser DERIVADA do livro-caixa. Quem lança uma
    despesa uma vez a vê aparecer na transparência sozinha.

    As quatro colunas da `PrestacaoContas` são fixas e antigas; as categorias do
    livro são mais finas. O mapeamento é explícito abaixo, e o que não couber
    nas três primeiras cai em `outros_custos` -- nunca se perde dinheiro no
    caminho.
    """
    dados = resumo(cidade, ano, mes)
    por_cat = {c['chave']: c['valor'] for c in dados['por_categoria']}

    medalhas = round(por_cat.pop('medalhas', 0.0) + por_cat.pop('premiacao', 0.0), 2)
    arte = round(por_cat.pop('arte', 0.0), 2)
    energia = round(por_cat.pop('energia', 0.0), 2)
    outros = round(sum(por_cat.values()), 2)

    registro = PrestacaoContas.query.filter_by(
        cidade=cidade, ano=ano, mes=mes).first()
    if registro is None:
        registro = PrestacaoContas(cidade=cidade, ano=ano, mes=mes)
        db.session.add(registro)

    registro.valor_patrocinado = dados['entradas']
    registro.custo_medalhas = medalhas
    registro.custo_arte = arte
    registro.custo_energia = energia
    registro.outros_custos = outros
    if usuario_id:
        registro.modificado_por_id = usuario_id
    db.session.commit()
    return registro


def meses_com_movimento(cidade):
    """(ano, mes) que têm lançamento -- para o seletor da tela."""
    linhas = db.session.query(
        func.strftime('%Y', LancamentoCaixa.data),
        func.strftime('%m', LancamentoCaixa.data),
    ).filter(LancamentoCaixa.cidade == cidade).distinct().all()
    saida = []
    for ano, mes in linhas:
        try:
            saida.append((int(ano), int(mes)))
        except (TypeError, ValueError):
            continue
    return sorted(set(saida), reverse=True)


def para_csv(cidade, ano=None):
    """O pacote do contador: uma linha por lançamento, tudo que ele pede.

    CSV e não PDF de propósito -- o contador abre no Excel e trabalha; um PDF
    ele teria de redigitar. E CSV não precisa de biblioteca nenhuma no
    servidor, que num NAS de 490 MB é o argumento que decide.
    """
    import csv
    import io as _io

    saida = _io.StringIO()
    escritor = csv.writer(saida, delimiter=';')
    escritor.writerow([
        'Data', 'Tipo', 'Categoria', 'Descricao', 'Contraparte',
        'Documento', 'Numero do documento', 'Rubrica', 'Valor', 'Observacao',
    ])
    for l in lancamentos(cidade, ano):
        escritor.writerow([
            l.data.strftime('%d/%m/%Y') if l.data else '',
            'Entrada' if l.tipo == 'entrada' else 'Saida',
            ROTULO_CATEGORIA.get(l.categoria, l.categoria),
            l.descricao or '',
            l.contraparte or '',
            dict(DOCUMENTOS).get(l.documento_tipo or '', ''),
            l.documento_numero or '',
            l.rubrica or '',
            # Saída NEGATIVA: quem abre o arquivo sem ler a coluna "Tipo" tem de
            # ver o sinal (24/09/2026, pedido do dono).
            ('%.2f' % valor_assinado(l)).replace('.', ','),
            (l.observacao or '').replace('\n', ' '),
        ])
    return saida.getvalue()


# ---------------------------------------------------------------------------
# Saldo, período e fluxo (24/09/2026)
# ---------------------------------------------------------------------------

def valor_assinado(l):
    """Entrada positiva, saída negativa -- o sinal que a pessoa lê de relance."""
    v = l.valor or 0.0
    return round(v if l.tipo == 'entrada' else -v, 2)


def saldo_ate(cidade, antes_de):
    """Saldo de tudo o que foi lançado ANTES de `antes_de` (a data não entra).

    É o "saldo que veio do período anterior": entra no começo de qualquer
    relatório para não haver furo entre um período e o seguinte. Não precisa
    de coluna nem de tabela -- é só a conta do que já está no livro.
    """
    por_tipo = dict(db.session.query(
        LancamentoCaixa.tipo, func.coalesce(func.sum(LancamentoCaixa.valor), 0.0)
    ).filter(LancamentoCaixa.cidade == cidade,
             LancamentoCaixa.data < antes_de).group_by(LancamentoCaixa.tipo).all())
    return round(por_tipo.get('entrada', 0.0) - por_tipo.get('saida', 0.0), 2)


def periodo(cidade, de, ate):
    """Os números de um período, JÁ com o saldo que veio de antes.

    `de` e `ate` são datas e entram os dois. `diferenca` é apoio recebido menos
    o investido NO PERÍODO; `saldo_final` soma o saldo anterior a ela.
    """
    por_tipo = dict(db.session.query(
        LancamentoCaixa.tipo, func.coalesce(func.sum(LancamentoCaixa.valor), 0.0)
    ).filter(LancamentoCaixa.cidade == cidade,
             LancamentoCaixa.data >= de, LancamentoCaixa.data <= ate
             ).group_by(LancamentoCaixa.tipo).all())
    entradas = round(por_tipo.get('entrada', 0.0), 2)
    saidas = round(por_tipo.get('saida', 0.0), 2)
    anterior = saldo_ate(cidade, de)
    diferenca = round(entradas - saidas, 2)
    return {
        'de': de, 'ate': ate,
        'saldo_anterior': anterior,
        'entradas': entradas,
        'saidas': saidas,
        'diferenca': diferenca,
        'saldo_final': round(anterior + diferenca, 2),
    }


def rotulo_documento(l):
    """O que a coluna "Documento" mostra. NUNCA "sem documento" quando há prova.

    Quem lê a página de despesas lê "sem documento" como "gasto sem prova".
    Se há comprovante anexado, o gasto TEM prova -- só não tem número de nota.
    """
    if l.documento_numero:
        tipo = {'nf': 'NF', 'recibo': 'Recibo'}.get(l.documento_tipo or '', 'Doc.')
        return '%s %s' % (tipo, l.documento_numero)
    if l.anexo_path:
        return 'Comprovante anexado'
    return 'Comprovante pendente'


def rotulo_comprovante(l):
    """O texto do link que abre o arquivo anexado."""
    return 'Recebimento' if l.tipo == 'entrada' else 'Pagamento'


def fluxo(cidade, de, ate):
    """Todas as linhas do período, da mais antiga para a mais nova, com o saldo
    correndo linha a linha -- o "extrato" do projeto.

    Devolve (saldo_anterior, linhas); cada linha é um dict com a `linha` do
    livro, o `valor` já com sinal e o `saldo` depois dela.
    """
    anterior = saldo_ate(cidade, de)
    corrente = anterior
    saida = []
    consulta = LancamentoCaixa.query.filter(
        LancamentoCaixa.cidade == cidade,
        LancamentoCaixa.data >= de, LancamentoCaixa.data <= ate,
    ).order_by(LancamentoCaixa.data, LancamentoCaixa.id).all()
    for l in consulta:
        v = valor_assinado(l)
        corrente = round(corrente + v, 2)
        saida.append({'linha': l, 'valor': v, 'saldo': corrente})
    return anterior, saida


# ---------------------------------------------------------------------------
# Quem mexeu (24/09/2026) e a planilha do contador
# ---------------------------------------------------------------------------

_CAMPOS_DO_RETRATO = ('data', 'tipo', 'valor', 'categoria', 'descricao',
                      'contraparte', 'documento_tipo', 'documento_numero',
                      'rubrica', 'observacao', 'anexo_nome')


def retrato(l):
    """A linha do livro como texto simples -- o que o histórico guarda."""
    saida = {}
    for campo in _CAMPOS_DO_RETRATO:
        v = getattr(l, campo, None)
        saida[campo] = v.isoformat() if hasattr(v, 'isoformat') else v
    return saida


def registrar(cidade, lancamento_id, acao, usuario_id, resumo, antes=None, depois=None):
    """Grava, SEM dar commit, uma linha do histórico. Quem chama confirma junto
    com a mudança no livro -- as duas coisas entram ou nenhuma entra."""
    import json
    from app.models import HistoricoCaixa
    db.session.add(HistoricoCaixa(
        cidade=cidade, lancamento_id=lancamento_id, acao=acao,
        usuario_id=usuario_id, resumo=(resumo or '')[:300],
        detalhe=json.dumps({'antes': antes, 'depois': depois},
                           ensure_ascii=False, default=str)))


def historico(cidade, limite=25):
    """As últimas mexidas do livro, da mais nova para a mais antiga, com o nome."""
    from app.models import HistoricoCaixa, Usuario
    linhas = db.session.query(HistoricoCaixa, Usuario.nome).outerjoin(
        Usuario, Usuario.id == HistoricoCaixa.usuario_id
    ).filter(HistoricoCaixa.cidade == cidade).order_by(
        HistoricoCaixa.quando.desc(), HistoricoCaixa.id.desc()).limit(limite).all()
    return [{'h': h, 'quem': nome or '(usuário removido)'} for h, nome in linhas]


def planilha_fluxo(cidade, de, ate, base_url):
    """O fluxo de caixa do período em .xlsx: tabela formatada, saída negativa,
    saldo correndo e, na última coluna, o link do comprovante de cada linha.

    `base_url` é o endereço do site (https://dominio) -- o link só abre para
    quem está logado, porque o comprovante pode ter CPF e CNPJ.
    """
    from app.planilha import gerar_xlsx, TEXTO, DATA, MOEDA, LINK
    anterior, itens = fluxo(cidade, de, ate)
    garantir_tokens([it['linha'] for it in itens])
    colunas = [
        {'titulo': 'Data', 'largura': 12, 'tipo': DATA},
        {'titulo': 'Tipo', 'largura': 9, 'tipo': TEXTO},
        {'titulo': 'Categoria', 'largura': 24, 'tipo': TEXTO},
        {'titulo': 'Descrição', 'largura': 40, 'tipo': TEXTO},
        {'titulo': 'De quem / para quem', 'largura': 28, 'tipo': TEXTO},
        {'titulo': 'Documento', 'largura': 20, 'tipo': TEXTO},
        {'titulo': 'Rubrica', 'largura': 26, 'tipo': TEXTO},
        {'titulo': 'Valor', 'largura': 15, 'tipo': MOEDA},
        {'titulo': 'Saldo', 'largura': 15, 'tipo': MOEDA},
        {'titulo': 'Comprovante', 'largura': 15, 'tipo': LINK},
    ]
    linhas = [[None, '', '', 'Saldo anterior (até %s)' % de.strftime('%d/%m/%Y'),
               '', '', '', None, anterior, None]]
    for it in itens:
        l = it['linha']
        link = None
        if l.anexo_path:
            link = (rotulo_comprovante(l), url_publica_comprovante(l, base_url))
        linhas.append([
            l.data, 'Entrada' if l.tipo == 'entrada' else 'Saída',
            ROTULO_CATEGORIA.get(l.categoria, l.categoria),
            l.descricao or '', l.contraparte or '', rotulo_documento(l),
            l.rubrica or '', it['valor'], it['saldo'], link,
        ])
    if not any(l[6] for l in linhas):
        # ninguém usa rubrica: a coluna vazia só polui a planilha
        colunas = [c for c in colunas if c['titulo'] != 'Rubrica']
        linhas = [l[:6] + l[7:] for l in linhas]
    p = periodo(cidade, de, ate)
    totais = [
        ('Saldo anterior', p['saldo_anterior']),
        ('Entradas do período', p['entradas']),
        ('Saídas do período', -p['saidas']),
        ('SALDO FINAL', p['saldo_final']),
    ]
    return gerar_xlsx(
        'Fluxo de caixa',
        'MoveMentes — Fluxo de caixa — %s' % cidade,
        'Período de %s a %s · saída em negativo'
        % (de.strftime('%d/%m/%Y'), ate.strftime('%d/%m/%Y')),
        colunas, linhas, totais)


def acumulado_prestacao(cidade):
    """O saldo corrido, mês a mês, da `PrestacaoContas` (a foto pública).

    Devolve {(ano, mes): {'anterior': x, 'mes': y, 'ate': z}}: o que veio dos
    meses anteriores, o resultado do mês e o consolidado até o fim dele. Sai da
    própria foto pública -- e não do livro -- para que o número da página
    pública some exatamente o que a página pública mostra, linha por linha.
    """
    corrente = 0.0
    saida = {}
    for r in PrestacaoContas.query.filter_by(cidade=cidade).order_by(
            PrestacaoContas.ano, PrestacaoContas.mes).all():
        mes = round(r.saldo, 2)
        saida[(r.ano, r.mes)] = {
            'anterior': round(corrente, 2), 'mes': mes,
            'ate': round(corrente + mes, 2)}
        corrente = round(corrente + mes, 2)
    return saida


# ---------------------------------------------------------------------------
# Comprovante por link aleatório (25/09/2026)
# ---------------------------------------------------------------------------

def garantir_tokens(linhas):
    """Dá um endereço aleatório a toda linha com comprovante que ainda não tem.

    As linhas antigas nasceram sem; elas ganham o token na primeira vez que a
    planilha ou o relatório é gerado. Um commit só, e só se algo mudou.
    """
    mudou = False
    for l in linhas:
        if l.anexo_path and not l.link_token:
            l.link_token = secrets.token_urlsafe(24)
            mudou = True
    if mudou:
        db.session.commit()


def url_publica_comprovante(l, base_url):
    """O endereço que vai dentro da planilha e do PDF (abre sem login)."""
    return '%s/ranking/comprovante/%s' % (base_url.rstrip('/'), l.link_token)


def por_token(token):
    """A linha dona do token, ou None. Token curto demais nem é consultado."""
    if not token or len(token) < 20:
        return None
    return LancamentoCaixa.query.filter_by(link_token=token).first()


def enviar_comprovante(linha):
    """A resposta com o arquivo. Imagem e PDF (já validados por conteúdo na
    entrada) abrem no navegador; o resto baixa. `sandbox` + `nosniff` impedem
    que o arquivo rode código no site. Devolve None se o arquivo sumiu."""
    import os
    from flask import send_file
    if not linha.anexo_path or not os.path.exists(linha.anexo_path):
        return None
    mime = (linha.anexo_mime or '').lower()
    inline = mime == 'application/pdf' or (
        mime.startswith('image/') and 'svg' not in mime)
    resposta = send_file(linha.anexo_path, mimetype=mime or None,
                         as_attachment=not inline,
                         download_name=linha.anexo_nome or 'comprovante')
    resposta.headers['X-Content-Type-Options'] = 'nosniff'
    resposta.headers['Content-Security-Policy'] = 'sandbox'
    resposta.headers['X-Robots-Tag'] = 'noindex, nofollow'
    resposta.headers['Referrer-Policy'] = 'no-referrer'
    return resposta
