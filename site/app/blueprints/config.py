from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, current_app, abort
from flask_login import login_required, current_user
from app.navegacao import voltar
from app import db, dadoslocais
from app.models import Config, Usuario, PrestacaoContas, ComprovantePrestacao
from app.blueprints.auth import admin_required, master_or_aprovador_required, viewer_required
from werkzeug.utils import secure_filename
from datetime import datetime
import os
import time
import logging

config_bp = Blueprint('config', __name__, url_prefix='/config')
logger = logging.getLogger(__name__)

# ALERTA: As funções ALLOWED_EXTENSIONS e allowed_file foram removidas
# pois o upload de QR Code NÃO será mais gerenciado por este blueprint.
# O QR Code para doação será uma imagem estática gerenciada via init_db.py e auth/login.html.

def is_within_editable_period():
    """Verifica se ainda é possível editar as configurações (até o 6º dia do mês)"""
    today = datetime.now()
    # Uma lógica de "dias úteis" seria mais complexa e exigiria bibliotecas adicionais.
    # Por enquanto, mantemos a simplicidade do dia do mês.
    return today.day <= 6


def _patrocinador_do_mes_corrente(cidade):
    """Nome de exibicao do patrocinador do mes atual — ou 'A definir'.

    Patrocinador pertence a (cidade, ano, mes): perguntar 'qual e o da
    cidade' so faz sentido dizendo de qual mes se fala.
    """
    from app.blueprints.ranking import _sponsor_nome_periodo
    agora = datetime.now()
    return _sponsor_nome_periodo(cidade, agora.year, agora.month)

@config_bp.route('/')
@config_bp.route('/view')
@login_required
@viewer_required
def view_config():
    """Visualiza as configurações da cidade"""
    config = Config.query.filter_by(cidade=current_user.cidade_ativa).first()
    can_edit = is_within_editable_period()

    if not config:
        flash('Nenhuma configuração encontrada para sua cidade. Por favor, crie uma!', 'info')
        # Redireciona para a página de edição para que a primeira configuração seja criada
        return redirect(url_for('config.edit_config'))

    # Passa o tipo de usuário para o template para controle de UI
    return render_template('config/view.html',
                         config=config,
                         can_edit=can_edit,
                         current_day=datetime.now().day,
                         cidade=current_user.cidade_ativa,
                         user_type=current_user.tipo,
                         Usuario=Usuario) # <<< CORRIGIDO: Passar o modelo Usuario

@config_bp.route('/edit', methods=['GET', 'POST'])
@login_required
@master_or_aprovador_required
def edit_config():
    """Edita as configurações da cidade.

    P-92 (28/08/2026): esta rota exigia so @login_required. Qualquer usuario
    logado -- inclusive um viewer -- editava meta, chave PIX e configuracao da
    cidade. Regra que vale em todo o sistema: visualizador nao edita nada.
    """
    # Esta rota é para o user editar a config da SUA cidade.
    # Admins master/aprovador usarão admin.edit_city para editar configurações de OUTRAS cidades.
    config = Config.query.filter_by(cidade=current_user.cidade_ativa).first()
    can_edit = is_within_editable_period() # Verifica a regra do dia 6

    # Se a config não existir, cria uma nova para a cidade padrão do usuário logado
    if not config:
        config = Config(
            cidade=current_user.cidade_ativa,
            meta_corrida_caminhada=50,
            meta_ciclismo=200,
            patrocinador_mes='A definir',
            chave_pix=dadoslocais.obter('pix_chave_padrao'), # valor padrão para PIX (instance/dados_locais.json)
            qr_pix='qrcode_doacao.png' # <<< ATUALIZADO: Nome fixo do arquivo estático de QR
        )
        db.session.add(config)
        db.session.commit()
        flash(f'Configuração padrão criada para {current_user.cidade_ativa}.', 'info')

    if request.method == 'POST':
        # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
        # CORREÇÃO CRÍTICA DO SYNTAXERROR: TODO O PROCESSAMENTO DO POST DEVE ESTAR DENTRO DESTE TRY-EXCEPT PARA GARANTIR A SINTAXE CORRETA
        # <<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
        try:
            # Lógica para permitir edição de metas e patrocinador
            # Condição de edição: dentro do período OU se for Master/Aprovador
            # A checkbox 'flexible_edit' será implementada no próximo passo via models.py e formulário
            if not (can_edit or current_user.pode_aprovar):
                logger.warning(f"Tentativa de {current_user.email} editar metas/patrocinador fora do prazo para {config.cidade}.")
                flash('As metas e patrocinador só podem ser alterados até o dia 6 de cada mês, ou por um Master Admin/Aprovador.', 'warning')
                # Retorna o template para mostrar a mensagem, mas agora está dentro do try
                return render_template('config/edit.html',
                                       config=config,
                                       can_edit=can_edit,
                                       current_day=datetime.now().day,
                                       cidade=current_user.cidade_ativa,
                                       user_type=current_user.tipo,
                                       Usuario=Usuario) # <<< CORRIGIDO: Passar o modelo Usuario

            # Se a edição for permitida, atualiza os campos
            config.meta_corrida_caminhada = int(request.form.get('meta_corrida_caminhada', config.meta_corrida_caminhada))
            config.meta_ciclismo = int(request.form.get('meta_ciclismo', config.meta_ciclismo))
            # Patrocinador NAO se edita mais aqui: ele pertence a um mes, nao
            # a cidade. A tela agora manda para /ranking/patrocinadores, onde
            # cada periodo tem os proprios dados.

            def _numero_configurado(nome, atual, permite_vazio=False):
                valor = (request.form.get(nome, '') or '').strip().replace(',', '.')
                if not valor and permite_vazio:
                    return None
                if not valor:
                    return atual
                numero = float(valor)
                if numero <= 0:
                    raise ValueError(f'{nome} deve ser maior que zero.')
                return numero

            config.pace_max_corrida_curta = _numero_configurado(
                'pace_max_corrida_curta', config.pace_max_corrida_curta or 4.167
            )
            config.pace_max_corrida_longa = _numero_configurado(
                'pace_max_corrida_longa', config.pace_max_corrida_longa or 5.0
            )
            config.velocidade_min_ciclismo = _numero_configurado(
                'velocidade_min_ciclismo', config.velocidade_min_ciclismo, permite_vazio=True
            )
            config.velocidade_max_ciclismo = _numero_configurado(
                'velocidade_max_ciclismo', config.velocidade_max_ciclismo or 45.0
            )
            if (
                config.velocidade_min_ciclismo is not None and
                config.velocidade_min_ciclismo >= config.velocidade_max_ciclismo
            ):
                raise ValueError('A velocidade minima do ciclismo deve ser menor que a maxima.')

            # Rate limiter: espera entre buscas ao Strava (segundos). Limitado
            # a [3, 30] para nunca ficar rapido demais (bloqueio) nem lento demais.
            delay_strava = _numero_configurado(
                'strava_delay_segundos',
                config.strava_delay_segundos or 4.0,
            )
            if delay_strava is not None:
                config.strava_delay_segundos = min(30.0, max(3.0, delay_strava))

            # Corte de pace que separa corrida de caminhada na tabela "somente
            # corrida" (Fase 4 do plano das melhorias). Decisao do dono: 7.0
            # min/km, mais rigoroso que a literatura -- errar para o lado de
            # nao chamar caminhada rapida de corrida. A classificacao e'
            # derivada na consulta (consolidado.py), entao mudar aqui
            # reclassifica tudo na hora, sem migracao.
            config.pace_corte_corrida = _numero_configurado(
                'pace_corte_corrida', config.pace_corte_corrida or 7.0
            )

            # Link do grupo de WhatsApp da cidade -- mostrado a quem conclui a
            # inscricao publica (Fase 2). So chega aqui porque a rota inteira
            # ja exige master_or_aprovador: se qualquer um pudesse trocar,
            # alguem poria o link de outro grupo na porta de entrada.
            config.link_grupo_whatsapp = (
                request.form.get('link_grupo_whatsapp', '') or ''
            ).strip()[:300] or None

            # Campos PIX: Apenas Master pode editar a chave PIX (conforme plano).
            if current_user.is_master:
                config.chave_pix = request.form.get('chave_pix', config.chave_pix).strip()
            # O QR Code NÃO será mais gerenciado por upload aqui. Ele é um arquivo estático fixo.

            config.ult_atualizacao_config = datetime.utcnow() # <<< CORRIGIDO: Nome do campo e timezone para UTC
            config.modificado_por_id = current_user.id # <<< NOVIDADE: Auditoria

            db.session.commit()

            flash('Configurações atualizadas com sucesso!', 'success')
            logger.info(f"Configurações atualizadas para {config.cidade} por {current_user.email}.")

            # Redireciona para a página de visualização da config
            return redirect(url_for('config.view_config'))

        except Exception as e: # <<< ESTE except AGORA CORRESPONDE AO try ACIMA E ENGLOBARÁ TODO O POST
            db.session.rollback()
            logger.error(f"Erro ao atualizar configurações para {config.cidade}: {str(e)}", exc_info=True)
            flash(f'Erro ao atualizar configurações: {str(e)}', 'error')
            # Renderiza o template novamente em caso de erro, passando as variáveis necessárias
            return render_template('config/edit.html',
                                   config=config,
                                   can_edit=can_edit,
                                   current_day=datetime.now().day,
                                   cidade=current_user.cidade_ativa,
                                   user_type=current_user.tipo,
                                   Usuario=Usuario) # <<< CORRIGIDO: Passar o modelo Usuario

    # GET - mostrar formulário de edição
    return render_template('config/edit.html',
                           config=config,
                           can_edit=can_edit,
                           current_day=datetime.now().day,
                           cidade=current_user.cidade_ativa,
                           user_type=current_user.tipo,
                           Usuario=Usuario) # <<< CORRIGIDO: Passar o modelo Usuario

@config_bp.route('/pix-info') # <<< Rota simplificada para obter informações PIX da cidade do usuário logado
@login_required
def get_pix_info():
    """Retorna informações do PIX em JSON para exibição na página de login."""
    # Para o PIX na página de login, o plano é que a chave PIX padrão venha de
    # instance/dados_locais.json (pix_chave_padrao), e o QR Code seja um arquivo
    # estático fixo (qrcode_doacao.png).
    # Esta rota busca a configuração da cidade padrão 'Palmeirópolis' ou a primeira que encontrar.
    config = Config.query.filter_by(cidade='Palmeirópolis').first() 
    if not config: # Fallback se 'Palmeirópolis' não existir, ou se o DB estiver vazio.
        config = Config.query.first() # Pega qualquer configuração existente

    if not config:
        # Se não há config alguma no DB, retorna um PIX padrão fixo para exibir na tela de login
        return jsonify({
            'chave_pix': dadoslocais.obter('pix_chave_padrao'),
            'qr_pix': 'qrcode_doacao.png',
            'patrocinador': 'A definir'
        })
    
    # Retorna os dados da configuração encontrada, com fallbacks para os padrões se estiverem vazios
    return jsonify({
        'chave_pix': config.chave_pix or dadoslocais.obter('pix_chave_padrao'),
        'qr_pix': config.qr_pix or 'qrcode_doacao.png',
        'patrocinador': _patrocinador_do_mes_corrente(config.cidade)
    })


# ===== PRESTACAO DE CONTAS (TRANSPARENCIA) =====

MESES_PT_CONFIG = {
    1: 'Janeiro', 2: 'Fevereiro', 3: 'Marco', 4: 'Abril', 5: 'Maio', 6: 'Junho',
    7: 'Julho', 8: 'Agosto', 9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro',
}

_COMPROVANTE_TIPOS = {
    'image/jpeg', 'image/png', 'image/gif', 'image/webp',
    'application/pdf',
}
_COMPROVANTE_MAX_SIZE = 10 * 1024 * 1024  # 10 MB


def _config_project_root():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _parse_valor(raw):
    """Le valor em reais aceitando '1.234,56', '1234.56', '50' ou vazio."""
    s = (raw or '').strip().replace('R$', '').replace(' ', '')
    if not s:
        return 0.0
    if ',' in s and '.' in s:
        s = s.replace('.', '').replace(',', '.')
    elif ',' in s:
        s = s.replace(',', '.')
    try:
        return round(float(s), 2)
    except ValueError:
        return 0.0


def _meses_editaveis(cidade):
    """(ano, mes) dos ultimos 12 meses + registros existentes, sem futuros, desc."""
    now = datetime.now()
    pares = set()
    y, m = now.year, now.month
    for _ in range(12):
        pares.add((y, m))
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    for r in PrestacaoContas.query.filter_by(cidade=cidade).all():
        if (r.ano, r.mes) <= (now.year, now.month):
            pares.add((r.ano, r.mes))
    return sorted(pares, reverse=True)


@config_bp.route('/prestacao', methods=['GET', 'POST'])
@login_required
@viewer_required   # 20/09/2026 (item 73): LER e' de todo perfil — e' o que o
                   # patrocinador e o visualizador vieram ver. GRAVAR e' do
                   # admin para cima, conferido logo abaixo, no POST.
def prestacao():
    """Prestacao de contas do mes: todo mundo le, o admin edita."""
    cidade = current_user.cidade_ativa
    now = datetime.now()
    try:
        sel_ano = int(request.values.get('ano') or now.year)
        sel_mes = int(request.values.get('mes') or now.month)
    except (TypeError, ValueError):
        sel_ano, sel_mes = now.year, now.month
    if not (1 <= sel_mes <= 12):
        sel_mes = now.month
    # Nao permite editar meses futuros.
    if (sel_ano, sel_mes) > (now.year, now.month):
        sel_ano, sel_mes = now.year, now.month

    registro = PrestacaoContas.query.filter_by(
        cidade=cidade, ano=sel_ano, mes=sel_mes
    ).first()

    if request.method == 'POST':
        # O PORTAO DE ESCRITA (20/09/2026): quem so acompanha nem ve o
        # formulario, mas a recusa tem de estar no servidor — tela escondida
        # nao e' permissao (regra 26).
        if not current_user.pode_administrar:
            flash('Seu acesso é de acompanhamento: você lê a prestação de '
                  'contas, mas não altera.', 'error')
            return voltar('config.prestacao', ano=sel_ano, mes=sel_mes)
        if registro is None:
            registro = PrestacaoContas(cidade=cidade, ano=sel_ano, mes=sel_mes)
            db.session.add(registro)
        from app import caixa as _cx
        _campos = ('valor_patrocinado', 'custo_medalhas', 'custo_arte', 'custo_energia',
                   'outros_custos', 'observacao')
        _antes = {c: getattr(registro, c, None) for c in _campos}
        registro.valor_patrocinado = _parse_valor(request.form.get('valor_patrocinado'))
        registro.custo_medalhas = _parse_valor(request.form.get('custo_medalhas'))
        registro.custo_arte = _parse_valor(request.form.get('custo_arte'))
        registro.custo_energia = _parse_valor(request.form.get('custo_energia'))
        registro.outros_custos = _parse_valor(request.form.get('outros_custos'))
        registro.observacao = (request.form.get('observacao') or '').strip()[:2000] or None
        registro.modificado_por_id = current_user.id
        _cx.registrar(cidade, None, 'prestacao', current_user.id,
                      'Prestação de %02d/%d editada à mão' % (sel_mes, sel_ano),
                      antes=_antes, depois={c: getattr(registro, c, None) for c in _campos})
        db.session.commit()
        flash('Prestacao de contas salva.', 'success')
        return voltar('config.prestacao', ano=sel_ano, mes=sel_mes)

    historico = (
        PrestacaoContas.query.filter_by(cidade=cidade)
        .order_by(PrestacaoContas.ano.desc(), PrestacaoContas.mes.desc())
        .all()
    )
    from app.blueprints.ranking import _slugify_city
    from app import caixa as caixa_mod
    # O saldo corrido: o que veio dos meses anteriores + o do mes = consolidado.
    acumulado = caixa_mod.acumulado_prestacao(cidade)
    anteriores = [v['ate'] for (a, m), v in acumulado.items() if (a, m) < (sel_ano, sel_mes)]
    consolidado = {
        'anterior': (anteriores[-1] if anteriores else 0.0),
        'mes': (registro.saldo if registro else 0.0),
    }
    consolidado['ate'] = round(consolidado['anterior'] + consolidado['mes'], 2)
    return render_template(
        'config/prestacao.html',
        acumulado=acumulado,
        consolidado=consolidado,
        saldo_total=(list(acumulado.values())[-1]['ate'] if acumulado else 0.0),
        cidade=cidade,
        city_slug=_slugify_city(cidade),
        registro=registro,
        sel_ano=sel_ano, sel_mes=sel_mes,
        meses_opcoes=_meses_editaveis(cidade),
        meses_nome=MESES_PT_CONFIG,
        historico=historico,
    )


@config_bp.route('/prestacao/<int:pid>/comprovante', methods=['POST'])
@login_required
@admin_required  # 19/09/2026 (item 73): o admin ajuda o master na prestacao de contas
def prestacao_comprovante(pid):
    """Anexa um comprovante (imagem/PDF) a uma prestacao de contas."""
    registro = PrestacaoContas.query.get_or_404(pid)
    if registro.cidade != current_user.cidade_ativa and not current_user.is_supremo:
        abort(403)

    destino = url_for('config.prestacao', ano=registro.ano, mes=registro.mes)
    arquivo = request.files.get('comprovante')
    if not arquivo or not arquivo.filename:
        flash('Nenhum arquivo selecionado.', 'warning')
        return redirect(destino)

    mime = arquivo.content_type or ''
    if mime not in _COMPROVANTE_TIPOS:
        flash('Tipo de arquivo nao permitido. Use imagem ou PDF.', 'error')
        return redirect(destino)

    arquivo.seek(0, os.SEEK_END)
    tamanho = arquivo.tell()
    arquivo.seek(0)
    if tamanho > _COMPROVANTE_MAX_SIZE:
        flash('Arquivo muito grande. Maximo 10 MB.', 'error')
        return redirect(destino)

    pasta = os.path.join(_config_project_root(), 'uploads', 'comprovantes', str(pid))
    os.makedirs(pasta, exist_ok=True)
    nome_seguro = secure_filename(arquivo.filename) or 'comprovante'
    nome_final = f'{int(time.time())}_{nome_seguro}'
    caminho = os.path.join(pasta, nome_final)
    arquivo.save(caminho)

    comp = ComprovantePrestacao(
        prestacao_id=pid,
        arquivo_nome=nome_seguro,
        arquivo_path=caminho,
        tipo_mime=mime,
        tamanho=tamanho,
        enviado_por=current_user.email,
    )
    db.session.add(comp)
    from app import caixa as _cx
    _cx.registrar(registro.cidade, None, 'anexou', current_user.id,
                  'Comprovante público anexado à prestação de %02d/%d: %s' % (
                      registro.mes, registro.ano, nome_seguro))
    db.session.commit()
    flash('Comprovante anexado.', 'success')
    return redirect(destino)


@config_bp.route('/prestacao/comprovante/<int:cid>/remover', methods=['POST'])
@login_required
@admin_required  # 19/09/2026 (item 73): o admin ajuda o master na prestacao de contas
def prestacao_comprovante_remover(cid):
    """Remove um comprovante (arquivo + registro)."""
    comp = ComprovantePrestacao.query.get_or_404(cid)
    registro = comp.prestacao
    if registro and registro.cidade != current_user.cidade_ativa and not current_user.is_supremo:
        abort(403)
    destino = url_for(
        'config.prestacao',
        ano=registro.ano if registro else datetime.now().year,
        mes=registro.mes if registro else datetime.now().month,
    )
    try:
        if comp.arquivo_path and os.path.exists(comp.arquivo_path):
            os.remove(comp.arquivo_path)
    except OSError:
        logger.warning('Falha ao remover arquivo de comprovante %s', comp.arquivo_path)
    if registro:
        from app import caixa as _cx
        _cx.registrar(registro.cidade, None, 'removeu', current_user.id,
                      'Comprovante público removido da prestação de %02d/%d: %s' % (
                          registro.mes, registro.ano, comp.arquivo_nome))
    db.session.delete(comp)
    db.session.commit()
    flash('Comprovante removido.', 'success')
    return redirect(destino)
