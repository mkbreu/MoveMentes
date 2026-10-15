// Movimente-se - Main JavaScript

document.addEventListener('DOMContentLoaded', function () {
    var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    tooltipTriggerList.forEach(function (tooltipTriggerEl) {
        new bootstrap.Tooltip(tooltipTriggerEl);
    });

    document.querySelectorAll('.card').forEach(function (card, index) {
        setTimeout(function () {
            card.classList.add('fade-in');
        }, index * 100);
    });
});

function formatFileSize(bytes) {
    if (bytes === 0) return '0 Bytes';
    var k = 1024;
    var sizes = ['Bytes', 'KB', 'MB', 'GB'];
    var i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function showLoading(message) {
    message = message || 'Processando...';
    return [
        '<div class="text-center py-5">',
        '  <div class="spinner-border text-primary" role="status">',
        '    <span class="visually-hidden">Carregando...</span>',
        '  </div>',
        '  <p class="mt-3">' + message + '</p>',
        '</div>'
    ].join('');
}

function showAlert(message, type) {
    type = type || 'info';
    var mainContent = document.querySelector('main .container');
    if (!mainContent) return;

    var alert = document.createElement('div');
    alert.className = 'alert alert-' + type + ' alert-dismissible fade show';
    alert.setAttribute('role', 'alert');
    alert.innerHTML = String(message || '') +
        '<button type="button" class="btn-close" data-bs-dismiss="alert"></button>';
    mainContent.insertAdjacentElement('afterbegin', alert);

    setTimeout(function () {
        if (alert.parentNode) alert.remove();
    }, 5000);
}

function confirmAction(message, callback) {
    if (confirm(message)) callback();
}

function formatDate(dateString) {
    var date = new Date(dateString);
    return date.toLocaleDateString('pt-BR', {
        day: '2-digit',
        month: '2-digit',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
    });
}

function debounce(func, wait) {
    var timeout;
    return function executedFunction() {
        var args = arguments;
        clearTimeout(timeout);
        timeout = setTimeout(function () {
            func.apply(null, args);
        }, wait);
    };
}

function validateFileUpload(input, maxSizeMB) {
    maxSizeMB = maxSizeMB || 2048;
    var file = input.files && input.files[0];
    if (!file) return false;

    var maxSize = maxSizeMB * 1024 * 1024;
    if (file.size > maxSize) {
        showAlert('O arquivo e muito grande. Tamanho maximo: ' + maxSizeMB + 'MB', 'danger');
        input.value = '';
        return false;
    }

    var lowerName = file.name.toLowerCase();
    if (!lowerName.endsWith('.zip') && !lowerName.endsWith('.txt')) {
        showAlert('Selecione apenas arquivos ZIP ou TXT.', 'danger');
        input.value = '';
        return false;
    }

    return true;
}

function copyToClipboard(text, successMessage) {
    successMessage = successMessage || 'Copiado para a area de transferencia!';
    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(function () {
            showAlert(successMessage, 'success');
        }).catch(function () {
            fallbackCopyToClipboard(text, successMessage);
        });
    } else {
        fallbackCopyToClipboard(text, successMessage);
    }
}

function fallbackCopyToClipboard(text, successMessage) {
    var textArea = document.createElement('textarea');
    textArea.value = text;
    textArea.style.position = 'fixed';
    textArea.style.left = '-999999px';
    document.body.appendChild(textArea);
    textArea.focus();
    textArea.select();

    try {
        document.execCommand('copy');
        showAlert(successMessage, 'success');
    } catch (err) {
        showAlert('Erro ao copiar. Copie manualmente.', 'danger');
    }

    document.body.removeChild(textArea);
}

document.addEventListener('change', function (event) {
    if (event.target && event.target.type === 'file') {
        validateFileUpload(event.target);
    }
});

window.MovimenteSe = {
    showAlert: showAlert,
    showLoading: showLoading,
    confirmAction: confirmAction,
    formatDate: formatDate,
    formatFileSize: formatFileSize,
    copyToClipboard: copyToClipboard,
    validateFileUpload: validateFileUpload,
    debounce: debounce
};

/* ============================================================
   ALTURA DA NAVBAR, PUBLICADA PARA O CSS  (08/09/2026)

   POR QUE ISTO EXISTE
   Telas com cabeçalho congelado precisam saber ONDE a navbar termina, para
   colar logo abaixo dela. Um número fixo no CSS erra: a navbar é `sticky-top`
   e a altura dela MUDA com a largura da tela — medida em 08/09 ela tinha
   102px numa janela estreita (o menu quebra em duas linhas) contra ~56px numa
   larga. Cabeçalho colado no lugar errado ou tapa o conteúdo, ou deixa uma
   faixa vazia.

   Aqui a altura é MEDIDA e publicada em `--altura-navbar`. O CSS usa
   `top: var(--altura-navbar, 72px)`: se este script não rodar, o 72px cobre o
   caso comum e nada quebra.
   ============================================================ */
(function () {
    var navbar = document.querySelector('body > nav.navbar');
    if (!navbar) { return; }

    function publicarAltura() {
        var altura = Math.round(navbar.getBoundingClientRect().height);
        if (altura > 0) {
            document.documentElement.style.setProperty('--altura-navbar', altura + 'px');
        }
    }

    publicarAltura();
    // `debounce` já existe neste arquivo: redimensionar dispara dezenas de
    // eventos por segundo, e medir a cada um deixa a rolagem travada.
    window.addEventListener('resize', debounce(publicarAltura, 150));
    // O menu do celular abre e fecha mudando a altura da navbar.
    var menu = document.getElementById('navbarNav');
    if (menu) {
        menu.addEventListener('shown.bs.collapse', publicarAltura);
        menu.addEventListener('hidden.bs.collapse', publicarAltura);
    }
})();
