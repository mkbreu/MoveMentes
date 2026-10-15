# -*- coding: utf-8 -*-
"""Arquivo enviado por gente -- a validação, num lugar só (regra 28).

POR QUE ISTO É UM MÓDULO, E NÃO CÓDIGO REPETIDO
Comprovante do livro-caixa, material de ação educativa, foto e vídeo de
terceiro: são três telas recebendo arquivo de fora. Três implementações da
mesma validação viram, com o tempo, três validações diferentes -- e a mais
fraca é a que vale, porque é por ela que se entra.

AS QUATRO REGRAS, todas obrigatórias:
  1. **Whitelist**, nunca blacklist -- o que não está na lista não entra.
  2. **Validar por CONTEÚDO**, não pela extensão. Extensão é o que a pessoa
     digitou; os primeiros bytes são o que o arquivo é.
  3. **Nome novo, gerado aqui.** Nome vindo de fora é travessia de caminho
     (`../../instance/secrets.json`). O nome original fica só como rótulo.
  4. **Servir sempre como download** (`as_attachment=True`), nunca inline. Um
     `.html` ou `.svg` aberto inline roda no NOSSO domínio e herda a sessão de
     quem abriu.

O QUE FICA DE FORA, sempre: executável e script (`.exe .msi .py .bat .cmd .ps1
.js .jar .sh .vbs .scr .com .dll`), **qualquer arquivo com macro**
(`.docm .xlsm .pptm .xlsb`), e `.html`/`.svg`, que carregam script.
"""
import os
import secrets

# --- os três conjuntos que as telas usam ---
IMAGENS = {'.jpg', '.jpeg', '.png', '.webp', '.heic'}
VIDEOS = {'.mp4', '.mov'}
DOCUMENTOS = {'.pdf', '.docx', '.xlsx', '.pptx', '.odt', '.ods', '.txt', '.csv'}

COMPROVANTE = IMAGENS | {'.pdf'}
MATERIAL = IMAGENS | DOCUMENTOS
MIDIA = IMAGENS | VIDEOS | DOCUMENTOS

# Escrito para ficar à vista de quem for mexer: NÃO acrescente nada daqui às
# listas acima. Está aqui como documentação executável, não como filtro (a
# whitelist já basta) -- e como aviso.
PROIBIDO_SEMPRE = {
    '.exe', '.msi', '.py', '.bat', '.cmd', '.ps1', '.js', '.jar', '.sh',
    '.vbs', '.scr', '.com', '.dll',
    '.docm', '.xlsm', '.pptm', '.xlsb',   # macro VBA
    '.html', '.htm', '.svg',              # carregam script
}

TAMANHO_MAX = {
    'imagem': 8 * 1024 * 1024,
    'documento': 15 * 1024 * 1024,
    'video': 120 * 1024 * 1024,
}

# Assinatura dos primeiros bytes -> mime. Só o que a whitelist aceita.
_ASSINATURAS = (
    (b'\xff\xd8\xff', 'image/jpeg'),
    (b'\x89PNG\r\n\x1a\n', 'image/png'),
    (b'%PDF-', 'application/pdf'),
    (b'PK\x03\x04', 'application/zip'),   # docx/xlsx/pptx/odt/ods são zip
)


class AnexoRecusado(ValueError):
    """Recusa com motivo em português, para ir direto à tela."""


def _mime_pelo_conteudo(stream, ext):
    inicio = stream.read(32)
    stream.seek(0)

    for assinatura, mime in _ASSINATURAS:
        if inicio.startswith(assinatura):
            return mime
    if inicio[:4] == b'RIFF' and inicio[8:12] == b'WEBP':
        return 'image/webp'
    # HEIC e MP4/MOV: caixa 'ftyp' no offset 4
    if inicio[4:8] == b'ftyp':
        marca = inicio[8:12]
        if marca in (b'heic', b'heix', b'hevc', b'mif1', b'msf1'):
            return 'image/heic'
        return 'video/quicktime' if ext == '.mov' else 'video/mp4'
    # texto puro: aceita só se a extensão prometia texto e não há byte nulo
    if ext in ('.txt', '.csv') and b'\x00' not in inicio:
        return 'text/plain' if ext == '.txt' else 'text/csv'
    return None


def _familia(ext):
    if ext in VIDEOS:
        return 'video'
    if ext in IMAGENS:
        return 'imagem'
    return 'documento'


def guardar(arquivo, pasta, permitidos):
    """Valida e grava. Devolve (nome_original, caminho, mime, tamanho).

    Devolve `(None, None, None, 0)` quando não veio arquivo -- anexo costuma
    ser opcional, e obrigar a tela a distinguir "não mandou" de "mandou errado"
    é o tipo de detalhe que se esquece.
    """
    if not arquivo or not getattr(arquivo, 'filename', ''):
        return None, None, None, 0

    nome_original = arquivo.filename
    ext = os.path.splitext(nome_original)[1].lower()

    if ext in PROIBIDO_SEMPRE:
        raise AnexoRecusado(
            'Arquivo do tipo %s não é aceito em nenhuma tela do sistema — '
            'ele pode conter programa ou macro.' % ext)
    if ext not in permitidos:
        raise AnexoRecusado(
            'Tipo de arquivo não aceito aqui (%s). Aceitos: %s.'
            % (ext, ', '.join(sorted(permitidos))))

    arquivo.stream.seek(0, os.SEEK_END)
    tamanho = arquivo.stream.tell()
    arquivo.stream.seek(0)
    familia = _familia(ext)
    teto = TAMANHO_MAX[familia]
    if tamanho > teto:
        raise AnexoRecusado(
            'Arquivo grande demais: %.1f MB, e o limite para %s é %d MB.'
            % (tamanho / 1048576.0, familia, teto // 1048576))
    if tamanho == 0:
        raise AnexoRecusado('O arquivo chegou vazio.')

    mime = _mime_pelo_conteudo(arquivo.stream, ext)
    if not mime:
        raise AnexoRecusado(
            'O conteúdo do arquivo não confere com a extensão %s. '
            'Envie o original, sem renomear.' % ext)

    os.makedirs(pasta, exist_ok=True)
    destino = os.path.join(pasta, '%s%s' % (secrets.token_hex(16), ext))
    arquivo.save(destino)
    return nome_original[:255], destino, mime, tamanho


def apagar(caminho):
    """Remove o arquivo, sem quebrar se ele já não estiver lá."""
    if not caminho:
        return
    try:
        if os.path.exists(caminho):
            os.remove(caminho)
    except OSError:
        pass
