# -*- coding: utf-8 -*-
"""O livro-caixa do projeto (P-97) -- escrituração completa, integrada à
prestação de contas.

POR QUE ESTA TELA EXISTE
A `PrestacaoContas` é a foto pública do mês: quatro totais e um saldo. Ela não
sustenta isenção fiscal nem prestação de contas ao Ministério, porque não diz o
que foi comprado, de quem, com qual documento. Aqui é a escrituração: cada
entrada e cada saída, com data, categoria, documento e comprovante.

⚠️ E A FOTO PÚBLICA PASSA A SER DERIVADA DAQUI. Quem lançar a despesa uma vez a
vê aparecer na transparência sozinha (`caixa.sincronizar_prestacao`). Digitar o
mesmo número em dois lugares é como se produzem dois números diferentes.

FLUXO COPIADO DO ZOHO EXPENSE (referência do dono, DOC E):
anexar comprovante → categorizar → conferir → exportar. O que se copia é o
fluxo, não a stack.
"""
from datetime import datetime, date, timedelta
import logging
import os
import secrets

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    current_app, send_file, Response,
)
from flask_login import login_required, current_user
from app.navegacao import voltar

from app import db
from app import caixa as caixa_mod
from app.models import LancamentoCaixa, ConciliacaoCaixa
from app.blueprints.auth import admin_required, master_required, viewer_required

caixa_bp = Blueprint('caixa', __name__, url_prefix='/admin/caixa')
logger = logging.getLogger(__name__)

# A validação de arquivo mora em `app/anexos.py` -- uma só, para o livro-caixa,
# a ação educativa e a mídia de terceiro (regra 28). Três cópias da mesma
# validação viram três validações diferentes, e a mais fraca é a que vale.
from app import anexos as anexos_mod


def _pasta_anexos():
    raiz = str(current_app.config.get('UPLOAD_FOLDER') or 'uploads')
    return os.path.join(raiz, 'caixa')


def _guardar_anexo(arquivo):
    nome, caminho, mime, _tam = anexos_mod.guardar(
        arquivo, _pasta_anexos(), anexos_mod.COMPROVANTE)
    return nome, caminho, mime


def _valor(bruto):
    texto = (bruto or '').strip().replace('R$', '').replace(' ', '')
    if not texto:
        return 0.0
    # aceita 1.234,56 e 1234.56
    if ',' in texto:
        texto = texto.replace('.', '').replace(',', '.')
    try:
        return abs(round(float(texto), 2))
    except ValueError:
        return 0.0


def _data(bruto):
    try:
        return datetime.strptime((bruto or '').strip(), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


@caixa_bp.route('/', methods=['GET'])
@login_required
@viewer_required
def index():
    """O livro do mês, com o resumo e a conciliação.

    ⚠️ LER É `viewer_required`; MEXER continua `master_or_aprovador_required`.
    O patrocinador/auditor veio auditar a conta -- barrar a LEITURA aqui seria
    esvaziar o acesso dele. Quem não pode aprovar não vê os formulários (o
    template esconde) E não passa pelas rotas de POST (que seguem exigindo
    nível). Esconder o formulário sem fechar o POST foi exatamente o buraco da
    P-92; aqui as duas coisas andam juntas.
    """
    cidade = current_user.cidade_ativa
    hoje = date.today()
    try:
        ano = int(request.args.get('ano') or hoje.year)
        mes = int(request.args.get('mes') or hoje.month)
    except (TypeError, ValueError):
        ano, mes = hoje.year, hoje.month
    if not (1 <= mes <= 12):
        mes = hoje.month

    return render_template(
        'admin/livro_caixa.html',
        cidade=cidade,
        ano=ano, mes=mes,
        linhas=caixa_mod.lancamentos(cidade, ano, mes),
        resumo=caixa_mod.resumo(cidade, ano, mes),
        resumo_ano=caixa_mod.resumo(cidade, ano),
        conciliacao=caixa_mod.conciliacao(cidade, ano, mes),
        # O saldo que veio de antes + o do mês = o consolidado até o fim do mês.
        acumulado=caixa_mod.periodo(cidade, date(ano, mes, 1),
                                    caixa_mod.fim_do_mes(ano, mes)),
        historico=caixa_mod.historico(cidade),
        pode_lancar=current_user.pode_administrar,
        pode_editar=current_user.is_master,
        categorias=caixa_mod.CATEGORIAS,
        categorias_entrada=caixa_mod.CATEGORIAS_DE_ENTRADA,
        tipos=caixa_mod.TIPOS,
        documentos=caixa_mod.DOCUMENTOS,
        meses_com_movimento=caixa_mod.meses_com_movimento(cidade),
        hoje=hoje.isoformat(),
    )


def _da_cidade(linha):
    """A linha é da cidade em que o usuário trabalha -- ou ele é o supremo.

    ⚠️ `is_master` NÃO serve aqui: ele inclui o master de qualquer cidade, e o
    master da cidade A abriria o comprovante da cidade B só trocando o número
    na barra do navegador (regra "ação numa cidade nunca alcança outra").
    """
    return current_user.is_supremo or linha.cidade == current_user.cidade_ativa


def _ler_formulario():
    """Lê e confere o formulário de lançamento. Devolve (dados, erros)."""
    quando = _data(request.form.get('data'))
    valor = _valor(request.form.get('valor'))
    descricao = (request.form.get('descricao') or '').strip()
    categoria = (request.form.get('categoria') or '').strip()
    tipo = (request.form.get('tipo') or '').strip()

    erros = []
    if not quando:
        erros.append('Informe a data do lançamento.')
    elif quando > date.today():
        erros.append('Não dá para lançar uma data no futuro.')
    if valor <= 0:
        erros.append('Informe um valor maior que zero.')
    if len(descricao) < 3:
        erros.append('Escreva do que se trata.')
    if categoria not in dict(caixa_mod.CATEGORIAS):
        erros.append('Escolha uma categoria.')
    if tipo not in ('entrada', 'saida'):
        erros.append('Diga se é entrada ou saída.')

    dados = dict(
        data=quando, tipo=tipo, valor=valor,
        descricao=descricao[:300], categoria=categoria,
        documento_tipo=(request.form.get('documento_tipo') or 'sem').strip()[:20],
        documento_numero=(request.form.get('documento_numero') or '').strip()[:60] or None,
        contraparte=(request.form.get('contraparte') or '').strip()[:160] or None,
        rubrica=(request.form.get('rubrica') or '').strip()[:120] or None,
        observacao=(request.form.get('observacao') or '').strip()[:2000] or None,
    )
    return dados, erros


def _resumo_da_linha(l):
    return '%s de R$ %.2f — %s' % (
        'Entrada' if l.tipo == 'entrada' else 'Saída', l.valor or 0, l.descricao)


def _refazer_prestacao(cidade, *meses):
    """Refaz a foto pública de cada (ano, mês) citado -- sem repetir."""
    for ano, mes in sorted(set(meses)):
        try:
            caixa_mod.sincronizar_prestacao(cidade, ano, mes, current_user.id)
        except Exception as exc:
            logger.warning('nao consegui sincronizar a prestacao: %s', exc)


@caixa_bp.route('/lancar', methods=['POST'])
@login_required
@admin_required  # 19/09/2026 (item 73): o admin ajuda o master na prestacao de contas
def lancar():
    cidade = current_user.cidade_ativa
    dados, erros = _ler_formulario()
    quando = dados['data']

    anexo = (None, None, None)
    if not erros:
        try:
            anexo = _guardar_anexo(request.files.get('anexo'))
        except anexos_mod.AnexoRecusado as exc:
            erros.append(str(exc))

    if erros:
        for e in erros:
            flash(e, 'error')
        return voltar('caixa.index', ano=(quando or date.today()).year,
                                mes=(quando or date.today()).month)

    linha = LancamentoCaixa(
        cidade=cidade, **dados,
        anexo_nome=anexo[0], anexo_path=anexo[1], anexo_mime=anexo[2],
        criado_por_id=current_user.id,
    )
    if linha.anexo_path:
        linha.link_token = secrets.token_urlsafe(24)
    db.session.add(linha)
    db.session.flush()   # dá o id à linha, para o histórico apontar para ela
    caixa_mod.registrar(cidade, linha.id, 'lancou', current_user.id,
                        _resumo_da_linha(linha), depois=caixa_mod.retrato(linha))
    db.session.commit()

    # A foto pública se refaz sozinha: um lançamento, um lugar (P-96).
    _refazer_prestacao(cidade, (quando.year, quando.month))

    flash('Lançamento registrado.', 'success')
    return voltar('caixa.index', ano=quando.year, mes=quando.month)


@caixa_bp.route('/<int:lid>/editar', methods=['GET', 'POST'])
@login_required
@master_required   # 24/09/2026: o admin lança e apaga; CORRIGIR é do master
def editar(lid):
    """Corrige uma linha -- com o antes e o depois no histórico.

    O admin não edita de propósito: ele apaga e lança de novo (ficam os dois
    rastros). Só o master corrige no lugar, e o histórico diz quem, quando e o
    que estava escrito antes.
    """
    linha = LancamentoCaixa.query.get_or_404(lid)
    if not _da_cidade(linha):
        abort(403)

    if request.method == 'GET':
        return render_template(
            'admin/livro_caixa_editar.html', linha=linha, cidade=linha.cidade,
            ano=linha.data.year, mes=linha.data.month,
            categorias=caixa_mod.CATEGORIAS, tipos=caixa_mod.TIPOS,
            documentos=caixa_mod.DOCUMENTOS)

    dados, erros = _ler_formulario()
    anexo = (None, None, None)
    if not erros:
        try:
            anexo = _guardar_anexo(request.files.get('anexo'))
        except anexos_mod.AnexoRecusado as exc:
            erros.append(str(exc))
    if erros:
        for e in erros:
            flash(e, 'error')
        return redirect(url_for('caixa.editar', lid=lid))

    antes = caixa_mod.retrato(linha)
    mes_antigo = (linha.data.year, linha.data.month)
    for campo, valor in dados.items():
        setattr(linha, campo, valor)
    if anexo[1]:
        # Anexo novo. O arquivo antigo fica no disco (o histórico cita o nome).
        linha.anexo_nome, linha.anexo_path, linha.anexo_mime = anexo
    depois = caixa_mod.retrato(linha)
    if depois == antes:
        flash('Nada foi alterado.', 'info')
        return redirect(url_for('caixa.index', ano=linha.data.year, mes=linha.data.month))

    caixa_mod.registrar(linha.cidade, linha.id, 'editou', current_user.id,
                        _resumo_da_linha(linha), antes=antes, depois=depois)
    db.session.commit()
    _refazer_prestacao(linha.cidade, mes_antigo, (linha.data.year, linha.data.month))
    flash('Lançamento corrigido. O antes e o depois ficaram no histórico.', 'success')
    return redirect(url_for('caixa.index', ano=linha.data.year, mes=linha.data.month))


@caixa_bp.route('/<int:lid>/apagar', methods=['POST'])
@login_required
@admin_required  # 19/09/2026 (item 73): o admin ajuda o master na prestacao de contas
def apagar(lid):
    """Apaga um lançamento -- com backup do banco ANTES (regra 9)."""
    linha = LancamentoCaixa.query.get_or_404(lid)
    if not _da_cidade(linha):
        abort(403)

    try:
        from app.backupauto import backup_configuracao
        copia = backup_configuracao()
        logger.warning('backup antes de apagar lancamento %s: %s', lid, copia)
    except Exception as exc:
        # Sem backup, não apaga. A regra não tem exceção.
        logger.error('backup falhou; NAO vou apagar o lancamento %s: %s', lid, exc)
        flash('Não consegui fazer o backup antes de apagar. '
              'Nada foi apagado — tente de novo em instantes.', 'error')
        return voltar('caixa.index', ano=linha.data.year, mes=linha.data.month)

    ano, mes, cidade = linha.data.year, linha.data.month, linha.cidade
    # O comprovante FICA no disco (24/09/2026): apagar a linha não pode destruir
    # a prova, e o histórico guarda o retrato para refazer à mão se preciso.
    caixa_mod.registrar(cidade, linha.id, 'apagou', current_user.id,
                        _resumo_da_linha(linha), antes=caixa_mod.retrato(linha))
    db.session.delete(linha)
    db.session.commit()
    _refazer_prestacao(cidade, (ano, mes))
    flash('Lançamento apagado. Foi feita uma cópia do banco antes.', 'success')
    return voltar('caixa.index', ano=ano, mes=mes)


@caixa_bp.route('/<int:lid>/comprovante')
@login_required
@viewer_required   # 24/09/2026: o patrocinador/auditor precisa abrir a prova
def comprovante(lid):
    """Serve o comprovante SEMPRE como download, nunca aberto na página.

    Arquivo enviado por gente não se abre inline: um HTML ou um SVG rodando no
    nosso domínio herdaria a sessão de quem abriu (regra 28).
    """
    linha = LancamentoCaixa.query.get_or_404(lid)
    if not _da_cidade(linha):
        abort(403)
    resposta = caixa_mod.enviar_comprovante(linha)
    if resposta is None:
        abort(404)
    return resposta


@caixa_bp.route('/<int:lid>/revogar-link', methods=['POST'])
@login_required
@master_required   # o link que já saiu por planilha/PDF deixa de funcionar
def revogar_link(lid):
    linha = LancamentoCaixa.query.get_or_404(lid)
    if not _da_cidade(linha):
        abort(403)
    linha.link_token = None
    caixa_mod.garantir_tokens([linha])
    caixa_mod.registrar(linha.cidade, linha.id, 'revogou', current_user.id,
                        'Link público do comprovante trocado — ' + _resumo_da_linha(linha))
    db.session.commit()
    flash('Link antigo desativado. A próxima planilha já sai com o novo.', 'success')
    return voltar('caixa.index', ano=linha.data.year, mes=linha.data.month)


@caixa_bp.route('/conciliar', methods=['POST'])
@login_required
@admin_required  # 19/09/2026 (item 73): o admin ajuda o master na prestacao de contas
def conciliar():
    cidade = current_user.cidade_ativa
    try:
        ano = int(request.form.get('ano'))
        mes = int(request.form.get('mes'))
    except (TypeError, ValueError):
        abort(400)
    registro = ConciliacaoCaixa.query.filter_by(
        cidade=cidade, ano=ano, mes=mes).first()
    if registro is None:
        registro = ConciliacaoCaixa(cidade=cidade, ano=ano, mes=mes)
        db.session.add(registro)
    antes = {'saldo_extrato': registro.saldo_extrato, 'justificativa': registro.justificativa}
    registro.saldo_extrato = _valor(request.form.get('saldo_extrato'))
    registro.justificativa = (request.form.get('justificativa') or '').strip()[:2000] or None
    registro.conferido_em = datetime.utcnow()
    registro.conferido_por_id = current_user.id
    caixa_mod.registrar(
        cidade, None, 'conciliou', current_user.id,
        'Conciliação de %02d/%d — extrato R$ %.2f' % (mes, ano, registro.saldo_extrato or 0),
        antes=antes,
        depois={'saldo_extrato': registro.saldo_extrato, 'justificativa': registro.justificativa})
    db.session.commit()
    flash('Conciliação salva.', 'success')
    return voltar('caixa.index', ano=ano, mes=mes)


@caixa_bp.route('/fluxo')
@login_required
@viewer_required
def fluxo():
    """O fluxo de caixa completo: toda entrada e saída do período, com o saldo
    correndo linha a linha e o saldo que veio de antes no topo.

    É uma página feita para imprimir -- o navegador de quem lê salva em PDF.
    """
    from app.blueprints.relatorios import _periodo
    de, ate = _periodo()
    cidade = current_user.cidade_ativa
    anterior, itens = caixa_mod.fluxo(cidade, de, ate)
    caixa_mod.garantir_tokens([it['linha'] for it in itens])
    return render_template(
        'admin/fluxo_caixa.html',
        cidade=cidade, de=de, ate=ate, anterior=anterior, itens=itens,
        periodo=caixa_mod.periodo(cidade, de, ate),
        rotulo_categoria=caixa_mod.ROTULO_CATEGORIA,
        link_publico=lambda l: caixa_mod.url_publica_comprovante(l, 'https://%s' % request.host),
        gerado_em=datetime.now(),
    )


@caixa_bp.route('/planilha')
@login_required
@viewer_required
def planilha():
    """O fluxo do período em .xlsx: tabela formatada, saída negativa e o link
    do comprovante de cada linha."""
    from app.blueprints.relatorios import _periodo
    de, ate = _periodo()
    cidade = current_user.cidade_ativa
    conteudo = caixa_mod.planilha_fluxo(cidade, de, ate, 'https://%s' % request.host)
    nome = 'fluxo-de-caixa-%s-%s-a-%s.xlsx' % (
        cidade.lower().replace(' ', '-'), de.isoformat(), ate.isoformat())
    return Response(
        conteudo,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': 'attachment; filename="%s"' % nome},
    )


@caixa_bp.route('/exportar')
@login_required
@admin_required  # 19/09/2026 (item 73): o admin ajuda o master na prestacao de contas
def exportar():
    """O pacote do contador, em CSV."""
    cidade = current_user.cidade_ativa
    try:
        ano = int(request.args.get('ano') or date.today().year)
    except (TypeError, ValueError):
        ano = date.today().year
    conteudo = caixa_mod.para_csv(cidade, ano)
    nome = 'livro-caixa-%s-%s.csv' % (
        cidade.lower().replace(' ', '-'), ano)
    # BOM para o Excel abrir com acento certo -- sem ele o contador vê "Ã§".
    return Response(
        '﻿' + conteudo,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'attachment; filename="%s"' % nome},
    )
