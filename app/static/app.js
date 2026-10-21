// Linhas dinâmicas do formulário de movimentação
(function () {
  const tabela = document.getElementById('itens');
  if (!tabela) return;
  const corpo = tabela.tBodies[0];

  document.getElementById('add-linha').addEventListener('click', function () {
    const nova = corpo.rows[corpo.rows.length - 1].cloneNode(true);
    nova.querySelectorAll('input').forEach(function (i) { i.value = ''; });
    nova.querySelectorAll('select').forEach(function (s) { s.selectedIndex = 0; });
    corpo.appendChild(nova);
    nova.querySelector('select').focus();
  });

  corpo.addEventListener('click', function (ev) {
    if (!ev.target.classList.contains('remover')) return;
    if (corpo.rows.length > 1) ev.target.closest('tr').remove();
  });
})();

// Fecha o menu "+ Movimentar" ao clicar fora e controla o menu mobile (hambúrguer)
document.addEventListener('click', function (ev) {
  document.querySelectorAll('details.menu[open]').forEach(function (d) {
    if (!d.contains(ev.target)) d.removeAttribute('open');
  });
  const btnMob = document.getElementById('btn-menu-mobile');
  const topo = document.getElementById('topo-sistema');
  if (btnMob && topo) {
    if (btnMob.contains(ev.target)) {
      const aberto = topo.classList.toggle('menu-aberto');
      btnMob.setAttribute('aria-expanded', aberto ? 'true' : 'false');
      btnMob.textContent = aberto ? '✕ Fechar' : '☰ Menu';
    } else if (!topo.contains(ev.target) && topo.classList.contains('menu-aberto')) {
      topo.classList.remove('menu-aberto');
      btnMob.setAttribute('aria-expanded', 'false');
      btnMob.textContent = '☰ Menu';
    }
  }
});

// ---------------------------------------------------------------- Pedidos

// "Mais tarde" volta para a página em que o usuário está
document.addEventListener('submit', function (ev) {
  const campo = ev.target.querySelector('input[name=voltar][data-auto]');
  if (campo) campo.value = location.pathname + location.search;
});

// Checklist de separação: cada checkbox grava na hora; "Finalizar" só habilita com tudo marcado
(function () {
  const botao = document.getElementById('btn-finalizar');
  const progresso = document.getElementById('progresso');
  const caixas = function () { return Array.prototype.slice.call(document.querySelectorAll('.chk-sep')); };

  function atualizar() {
    const todas = caixas();
    const marcadas = todas.filter(function (c) { return c.checked; }).length;
    if (progresso) progresso.textContent = marcadas + '/' + todas.length + ' separados';
    if (botao) botao.disabled = !(todas.length > 0 && marcadas === todas.length && !botao.dataset.bloqueio);
  }

  document.addEventListener('change', function (ev) {
    const caixa = ev.target.closest ? ev.target.closest('.chk-sep') : null;
    if (!caixa) return;
    const dados = new FormData();
    dados.append('separado', caixa.checked ? '1' : '0');
    dados.append('csrf_token', document.body.dataset.csrf);
    caixa.disabled = true;
    fetch(caixa.dataset.url, { method: 'POST', body: dados, credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) return r.json().then(function (j) { throw new Error(j.erro || 'Erro'); });
      })
      .catch(function (e) {
        caixa.checked = !caixa.checked;
        alert('Não foi possível salvar: ' + e.message);
      })
      .then(function () { caixa.disabled = false; atualizar(); });
  });
  atualizar();
})();

// Alerta de novos pedidos (estilo "comanda"): consulta o servidor a cada 15 s
(function () {
  const url = document.body.dataset.novosUrl;
  if (!url) return;
  const dock = document.getElementById('dock-pedidos');
  const corpo = document.getElementById('dock-corpo');
  const qtd = document.getElementById('dock-qtd');
  const badge = document.getElementById('badge-pedidos');
  const tituloOriginal = document.title;
  let anterior = null;

  function bip() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      [880, 1175].forEach(function (freq, i) {
        const osc = ctx.createOscillator(), gain = ctx.createGain();
        osc.frequency.value = freq; osc.connect(gain); gain.connect(ctx.destination);
        gain.gain.value = 0.15;
        osc.start(ctx.currentTime + i * 0.18); osc.stop(ctx.currentTime + i * 0.18 + 0.15);
      });
    } catch (e) { /* o navegador pode bloquear áudio antes de qualquer clique */ }
  }

  function atualizar() {
    fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
      .then(function (r) { return r.json(); })   // sessão expirada devolve HTML: cai no catch
      .then(function (d) {
        if (badge) { badge.textContent = d.a_atender; badge.hidden = d.a_atender === 0; }
        const badgeMob = document.getElementById('badge-pedidos-mob');
        if (badgeMob) { badgeMob.textContent = d.a_atender; badgeMob.hidden = d.a_atender === 0; }
        if (dock) {
          dock.hidden = d.novos === 0;
          if (d.novos > 0) { corpo.innerHTML = d.html; qtd.textContent = d.novos; }
          if (anterior !== null && d.novos > anterior) { bip(); dock.classList.remove('minimizado'); }
        }
        document.title = d.novos > 0 ? '(' + d.novos + ') ' + tituloOriginal : tituloOriginal;
        anterior = d.novos;
      })
      .catch(function () { /* tenta de novo no próximo ciclo */ });
  }

  const minimizar = document.getElementById('dock-min');
  if (minimizar) minimizar.addEventListener('click', function () { dock.classList.toggle('minimizado'); });
  atualizar();
  setInterval(atualizar, 15000);
})();

// Checkboxes de "Equipamentos utilizados por padrão" no perfil do colaborador
(function () {
  const form = document.getElementById('form-padrao-colab');
  if (!form) return;
  const url = form.dataset.ajaxUrl;
  const contador = document.getElementById('contador-padrao');
  const badge = document.getElementById('badge-padrao');
  const status = document.getElementById('status-padrao');

  form.addEventListener('change', function (ev) {
    const chk = ev.target.closest ? ev.target.closest('.chk-padrao') : null;
    if (!chk || !url) return;
    const dados = new FormData();
    dados.append('ajax', '1');
    dados.append('epi_id', chk.value);
    dados.append('marcado', chk.checked ? '1' : '0');
    const campoCargo = form.querySelector('input[name="cargo"]');
    if (campoCargo && campoCargo.value) dados.append('cargo', campoCargo.value);
    dados.append('csrf_token', document.body.dataset.csrf);
    fetch(url, { method: 'POST', body: dados, credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && typeof d.total === 'number') {
          if (contador) contador.textContent = d.total;
          if (badge) badge.textContent = d.total;
          if (status) status.textContent = 'Salvo automaticamente ✓';
        }
      })
      .catch(function () {
        if (status) status.textContent = 'Clique em "Salvar equipamentos padrão" para gravar.';
      });
  });
})();

// Seletores de Tamanho / Numeração na aba "Equipamentos utilizados por padrão" do perfil do colaborador
(function () {
  const form = document.getElementById('form-tamanhos-colab');
  if (!form) return;
  const url = form.dataset.ajaxUrl;
  const status = document.getElementById('status-tamanhos-colab');

  form.addEventListener('change', function (ev) {
    const sel = ev.target.closest ? ev.target.closest('.sel-tamanho-colab') : null;
    if (!sel || !url) return;
    const dados = new FormData();
    dados.append('ajax', '1');
    dados.append('epi_id', sel.dataset.epiId || '');
    dados.append('tamanho', sel.value || '');
    dados.append('csrf_token', document.body.dataset.csrf);
    fetch(url, { method: 'POST', body: dados, credentials: 'same-origin' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok && status) {
          status.textContent = 'Tamanho salvo automaticamente ✓';
        }
      })
      .catch(function () {});
  });
})();
