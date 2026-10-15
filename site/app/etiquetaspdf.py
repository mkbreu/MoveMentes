# -*- coding: utf-8 -*-
"""Folha de etiquetas em PDF, com as medidas travadas (P-140).

O PROBLEMA (relato do dono, setembro/2026)
As etiquetas dos medalhistas saíram FORA dos adesivos do papel Pimaco Inkjet
6087. A grade da tela está com os parâmetros oficiais da Pimaco e a conta fecha
simétrica (14,5 + 3×47,5 + 44,45 = 201,45, sobrando 14,45 à direita) — o que
sobra é o caminho entre a tela e o papel.

E ELE TEM TRÊS ARMADILHAS, TODAS FORA DO NOSSO CONTROLE quando se imprime HTML:
  1. **escala** — "Ajustar à página" encolhe tudo alguns por cento, e o erro
     cresce a cada coluna e a cada linha;
  2. **papel** — imprimir em A4 (210 mm) uma folha desenhada para Carta
     (215,9 mm) reescala 2,7%, que é exatamente o sintoma de "foi saindo fora";
  3. **margens do navegador** — com margem padrão, o conteúdo desce e entra.

PDF NÃO É ENFEITE, É O CONSERTO: aqui a página é Carta de verdade (612 × 792
pt) e cada etiqueta tem coordenada absoluta em pontos. Sobra uma única coisa
para o humano acertar no diálogo: **Tamanho real / 100%** (nunca "ajustar").

E PARA NÃO FICAR NO "ACHO QUE ESTÁ CERTO": a folha pode sair com uma **régua de
calibração** de 100 mm no rodapé. Quem imprime mede com uma régua de verdade:
100 mm medidos = impressora fiel; 97 mm = está encolhendo 3%, e aí o defeito é
do diálogo de impressão, não da grade.

SEM BIBLIOTECA NENHUMA, de propósito: o NAS tem 490 MB e nem `reportlab` nem
`weasyprint` estão instalados (conferido em 20/09/2026). PDF é texto; 1.4 com
Helvetica resolve isto em algumas centenas de linhas e nunca mais precisa de
instalação.
"""
import logging

logger = logging.getLogger(__name__)

MM = 72.0 / 25.4          # 1 mm em pontos PostScript (2,8346)
FONTE_NOME = 'F1'         # Helvetica-Bold
FONTE_DETALHE = 'F2'      # Helvetica


def _texto_pdf(valor):
    """Texto no jeito do PDF: WinAnsi, com parêntese e barra escapados."""
    bruto = (valor or '')
    try:
        bruto = bruto.encode('cp1252', 'replace').decode('cp1252')
    except Exception:
        bruto = bruto.encode('ascii', 'replace').decode('ascii')
    for velho, novo in (('\\', r'\\'), ('(', r'\('), (')', r'\)')):
        bruto = bruto.replace(velho, novo)
    return bruto


def _largura_helvetica(texto, tamanho, negrito=False):
    """Largura aproximada em pontos — serve para ENCURTAR nome que não cabe.

    Aproximação de propósito: a tabela de larguras da Helvetica inteira não
    paga o próprio peso aqui. 0,52 do tamanho por caractere erra para o lado
    seguro (Helvetica média fica perto de 0,5), então o corte sobra em vez de
    faltar — e etiqueta com nome cortado é melhor que nome invadindo a vizinha.
    """
    fator = 0.55 if negrito else 0.52
    return len(texto) * tamanho * fator


SUFIXO_CORTE = '...'


def _encurtar(texto, tamanho, largura_max, negrito=False):
    """Corta o nome que nao cabe, medindo COM o sufixo que vai sair.

    Defeito que isto corrige (achado pela prova em 20/09/2026): eu media com
    '…' (um caractere) e escrevia '...' (tres), entao o nome cortado ainda
    passava da largura da etiqueta e invadia a vizinha — exatamente o que se
    quer evitar aqui.
    """
    if _largura_helvetica(texto, tamanho, negrito) <= largura_max:
        return texto
    corte = texto
    while corte and _largura_helvetica(corte.rstrip() + SUFIXO_CORTE,
                                      tamanho, negrito) > largura_max:
        corte = corte[:-1]
    return (corte.rstrip() + SUFIXO_CORTE) if corte else texto[:1]


def _conteudo_da_folha(itens, modelo, com_regua, titulo=''):
    """O fluxo de desenho da página, em pontos, origem no canto INFERIOR."""
    largura_pt = modelo['papel_largura'] * MM
    altura_pt = modelo['papel_altura'] * MM
    etiq_l = modelo['largura'] * MM
    etiq_a = modelo['altura'] * MM
    passo_x = modelo['passo_horizontal'] * MM
    margem_x = modelo['margem_esquerda'] * MM
    margem_topo = modelo['margem_topo'] * MM
    fonte_nome = float(modelo.get('fonte_nome') or 8.2)
    fonte_det = float(modelo.get('fonte_detalhe') or 6.2)
    padding_x = 1.6 * MM

    partes = []
    for pos, item in enumerate(itens):
        coluna = pos % modelo['colunas']
        linha = pos // modelo['colunas']
        if linha >= modelo['linhas']:
            break
        x = margem_x + coluna * passo_x
        # PDF conta de baixo para cima; a grade, de cima para baixo.
        topo = altura_pt - margem_topo - linha * etiq_a
        nome = _encurtar(_texto_pdf(item.get('nome') or ''), fonte_nome,
                         etiq_l - 2 * padding_x, negrito=True)
        detalhe = _encurtar(_texto_pdf(item.get('detalhe') or ''), fonte_det,
                            etiq_l - 2 * padding_x)
        # Duas linhas de texto dentro da etiqueta, centradas na altura dela.
        y_nome = topo - (etiq_a / 2) - 0.3
        y_det = y_nome - (fonte_det + 1.2)
        partes.append('BT /%s %.2f Tf 1 0 0 1 %.2f %.2f Tm (%s) Tj ET'
                      % (FONTE_NOME, fonte_nome, x + padding_x, y_nome, nome))
        if detalhe:
            partes.append('BT /%s %.2f Tf 1 0 0 1 %.2f %.2f Tm (%s) Tj ET'
                          % (FONTE_DETALHE, fonte_det, x + padding_x, y_det, detalhe))

    if com_regua:
        # RÉGUA DE CALIBRAÇÃO: 100 mm exatos, marcados de 10 em 10, no rodapé.
        # Quem imprime confere com uma régua de verdade. Sai fora da área das
        # etiquetas (a grade ocupa até 12,7 mm do pé).
        y = 6.0 * MM
        x0 = margem_x
        partes.append('0.5 w 0 0 0 RG')
        partes.append('%.2f %.2f m %.2f %.2f l S' % (x0, y, x0 + 100 * MM, y))
        for i in range(0, 11):
            xi = x0 + i * 10 * MM
            alto = 3.5 * MM if i % 5 == 0 else 2.0 * MM
            partes.append('%.2f %.2f m %.2f %.2f l S' % (xi, y, xi, y + alto))
        partes.append('BT /%s 6.00 Tf 1 0 0 1 %.2f %.2f Tm (%s) Tj ET'
                      % (FONTE_DETALHE, x0, y + 4.5 * MM,
                         _texto_pdf('Régua de conferência: esta linha tem 100 mm '
                                    'exatos. Se a sua régua medir outro número, a '
                                    'impressão está com escala — imprima em '
                                    '"Tamanho real / 100%", papel Carta.')))
    if titulo:
        partes.append('BT /%s 6.00 Tf 1 0 0 1 %.2f %.2f Tm (%s) Tj ET'
                      % (FONTE_DETALHE, margem_x, 2.5 * MM, _texto_pdf(titulo)))
    return '\n'.join(partes), largura_pt, altura_pt


def gerar(itens, modelo, com_regua=True, titulo=''):
    """Devolve os bytes do PDF de UMA folha.

    `itens`: lista de {'nome', 'detalhe'} já na ordem da folha (inclusive as
    posições vazias, se houver pulo) — quem decide a ordem é a tela.
    """
    conteudo, largura_pt, altura_pt = _conteudo_da_folha(
        itens, modelo, com_regua, titulo)
    fluxo = conteudo.encode('cp1252', 'replace')

    objetos = [
        '<< /Type /Catalog /Pages 2 0 R >>',
        '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        ('<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.2f %.2f] '
         '/Resources << /Font << /%s 5 0 R /%s 6 0 R >> >> /Contents 4 0 R >>'
         % (largura_pt, altura_pt, FONTE_NOME, FONTE_DETALHE)),
        None,     # 4: o fluxo de desenho
        '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>',
        '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>',
    ]

    saida = bytearray(b'%PDF-1.4\n%\xe2\xe3\xcf\xd3\n')
    posicoes = []
    for numero, corpo in enumerate(objetos, start=1):
        posicoes.append(len(saida))
        if corpo is None:
            saida += ('%d 0 obj\n<< /Length %d >>\nstream\n'
                      % (numero, len(fluxo))).encode('ascii')
            saida += fluxo
            saida += b'\nendstream\nendobj\n'
        else:
            saida += ('%d 0 obj\n%s\nendobj\n' % (numero, corpo)).encode('ascii')

    inicio_xref = len(saida)
    saida += ('xref\n0 %d\n' % (len(objetos) + 1)).encode('ascii')
    saida += b'0000000000 65535 f \n'
    for pos in posicoes:
        saida += ('%010d 00000 n \n' % pos).encode('ascii')
    saida += ('trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n'
              % (len(objetos) + 1, inicio_xref)).encode('ascii')
    return bytes(saida)
