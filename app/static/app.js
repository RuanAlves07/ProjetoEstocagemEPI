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

// Fecha menus suspensos (details.menu) ao clicar fora e controla o menu mobile (hambúrguer)
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

// ---------------------------------------------------------------- Quadro de assinatura
// <div class="quadro-assinatura" data-assinatura="id-do-campo-oculto"> com um <canvas> e botões
// [data-desfazer] / [data-limpar]. A imagem (PNG recortada no contorno dos traços) vai para o campo oculto
// a cada traço; o evento "assinatura" avisa a página (detail.vazio) para liberar o botão de envio.
(function () {
  function iniciar(caixa) {
    const canvas = caixa.querySelector("canvas");
    const campo = document.getElementById(caixa.dataset.assinatura);
    const ctx = canvas.getContext("2d");
    const tracos = [];
    let atual = null;

    function tracar(c) {
      c.lineWidth = 2.4; c.lineCap = "round"; c.lineJoin = "round"; c.strokeStyle = "#111";
      for (const t of tracos) {
        c.beginPath();
        t.forEach((p, i) => i ? c.lineTo(p.x, p.y) : c.moveTo(p.x, p.y));
        if (t.length === 1) c.lineTo(t[0].x + .1, t[0].y + .1);
        c.stroke();
      }
    }
    function recortada() {
      const pts = tracos.flat(), margem = 6, escala = 2;
      const x0 = Math.max(0, Math.min(...pts.map(p => p.x)) - margem), y0 = Math.max(0, Math.min(...pts.map(p => p.y)) - margem);
      const x1 = Math.max(...pts.map(p => p.x)) + margem, y1 = Math.max(...pts.map(p => p.y)) + margem;
      const saida = document.createElement("canvas");
      saida.width = Math.round((x1 - x0) * escala);
      saida.height = Math.round((y1 - y0) * escala);
      const c = saida.getContext("2d");
      c.setTransform(escala, 0, 0, escala, -x0 * escala, -y0 * escala);
      tracar(c);
      return saida.toDataURL("image/png");
    }
    function desenhar() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      tracar(ctx);
    }
    function mudou() {
      desenhar();
      if (campo) campo.value = tracos.length ? recortada() : "";
      caixa.dispatchEvent(new CustomEvent("assinatura", { bubbles: true, detail: { vazio: !tracos.length } }));
    }
    function ajustar() {
      const r = canvas.getBoundingClientRect();
      if (!r.width) return; // ainda escondido
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(r.width * dpr);
      canvas.height = Math.round(r.height * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      desenhar();
    }
    function ponto(e) {
      const r = canvas.getBoundingClientRect();
      return { x: e.clientX - r.left, y: e.clientY - r.top };
    }
    canvas.addEventListener("pointerdown", e => {
      if (!canvas.width) ajustar();
      canvas.setPointerCapture(e.pointerId); atual = [ponto(e)]; tracos.push(atual); desenhar();
    });
    canvas.addEventListener("pointermove", e => { if (atual) { atual.push(ponto(e)); desenhar(); } });
    ["pointerup", "pointercancel"].forEach(ev => canvas.addEventListener(ev, () => { if (atual) { atual = null; mudou(); } }));
    caixa.querySelector("[data-desfazer]")?.addEventListener("click", () => { tracos.pop(); mudou(); });
    caixa.querySelector("[data-limpar]")?.addEventListener("click", () => { tracos.length = 0; mudou(); });
    window.addEventListener("resize", ajustar);
    new ResizeObserver(ajustar).observe(canvas);
    ajustar();
  }
  document.querySelectorAll(".quadro-assinatura[data-assinatura]").forEach(iniciar);
})();

// ---------------------------------------------------------------- Fotos da entrega
// Reduz a foto do celular (até 1600 px, JPEG) antes de enviar e mostra a prévia ao lado do campo.
(function () {
  const LADO_MAX = 1600;

  async function reduzir(arquivo) {
    let bitmap;
    try {
      bitmap = await createImageBitmap(arquivo, { imageOrientation: "from-image" });
    } catch (e) {
      return arquivo; // formato que o navegador não abre (ex.: HEIC): vai como está
    }
    const escala = Math.min(1, LADO_MAX / Math.max(bitmap.width, bitmap.height));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bitmap.width * escala);
    canvas.height = Math.round(bitmap.height * escala);
    canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise(ok => canvas.toBlob(ok, "image/jpeg", 0.85));
    if (!blob) return arquivo;
    const nome = (arquivo.name || "foto").replace(/\.[^.]+$/, "") + ".jpg";
    return new File([blob], nome, { type: "image/jpeg" });
  }

  document.addEventListener("change", async e => {
    const campo = e.target;
    if (!campo.matches("input[type=file][data-foto]") || !campo.files.length) return;
    const original = campo.files[0];
    const previa = campo.closest(".foto-campo")?.querySelector("img.foto-previa");
    if (previa) { previa.src = URL.createObjectURL(original); previa.hidden = false; }
    const reduzida = await reduzir(original);
    if (reduzida !== original) {
      const dt = new DataTransfer();
      dt.items.add(reduzida);
      campo.files = dt.files;
    }
  });
})();

// ---------------------------------------------------------------- Máscaras de campo

// <input data-mascara="telefone|cpf">: formata enquanto digita e mantém o cursor no mesmo dígito
(function () {
  const mascaras = {
    telefone: function (d) {
      if (d.length > 11 && d.startsWith('55')) d = d.slice(2);   // colado com o DDI: +55 47 99999-8888
      d = d.slice(0, 11);
      if (!d) return '';
      if (d.length <= 2) return '(' + d;
      const meio = d.length === 11 ? 5 : 4;
      const resto = d.slice(2);
      return '(' + d.slice(0, 2) + ') ' + (resto.length > meio ? resto.slice(0, meio) + '-' + resto.slice(meio) : resto);
    },
    cpf: function (d) {
      d = d.slice(0, 11);
      return d.replace(/^(\d{3})(\d)/, '$1.$2').replace(/^(\d{3})\.(\d{3})(\d)/, '$1.$2.$3')
              .replace(/\.(\d{3})(\d{1,2})$/, '.$1-$2');
    },
  };

  function aplicar(campo, apagando) {
    const formatar = mascaras[campo.dataset.mascara];
    if (!formatar) return;
    let cursor = campo.selectionStart == null ? campo.value.length : campo.selectionStart;
    // Backspace logo depois de "(", ")", " " ou "-" não apaga dígito: apaga o dígito anterior
    if (apagando && campo.value.replace(/\D/g, '') === (campo.dataset.digitos || '')) {
      const antes = campo.value.slice(0, cursor).replace(/\d(?=\D*$)/, '');
      campo.value = antes + campo.value.slice(cursor);
      cursor = antes.length;
    }
    const digitosAntes = campo.value.slice(0, cursor).replace(/\D/g, '').length;
    const novo = formatar(campo.value.replace(/\D/g, ''));
    campo.dataset.digitos = novo.replace(/\D/g, '');
    if (novo === campo.value) return;
    campo.value = novo;
    if (document.activeElement !== campo) return;
    let pos = 0, vistos = 0;
    while (pos < novo.length && vistos < digitosAntes) { if (/\d/.test(novo[pos])) vistos++; pos++; }
    campo.setSelectionRange(pos, pos);
  }

  document.addEventListener('input', function (ev) {
    if (ev.target.matches && ev.target.matches('input[data-mascara]')) {
      aplicar(ev.target, ev.inputType === 'deleteContentBackward');
    }
  });
  document.querySelectorAll('input[data-mascara]').forEach(aplicar);
})();

// ---------------------------------------------------------------- Pedidos

// "Mais tarde" volta para a página em que o usuário está
document.addEventListener('submit', function (ev) {
  const campo = ev.target.querySelector('input[name=voltar][data-auto]');
  if (campo) campo.value = location.pathname + location.search;
});

// Checklist de separação: cada checkbox grava na hora; "Finalizar" só habilita com tudo marcado;
// "Finalizar com ressalva" fica disponível quando nem todos os itens puderam ser marcados.
(function () {
  const botao = document.getElementById('btn-finalizar');
  const btnRessalva = document.getElementById('btn-abrir-ressalva');
  const painelRessalva = document.getElementById('painel-ressalva');
  const resumoRessalva = document.getElementById('resumo-ressalva');
  const progresso = document.getElementById('progresso');
  const caixas = function () { return Array.prototype.slice.call(document.querySelectorAll('.chk-sep')); };

  function atualizar() {
    const todas = caixas();
    const marcadas = todas.filter(function (c) { return c.checked; }).length;
    const faltam = todas.length - marcadas;
    if (progresso) progresso.textContent = marcadas + '/' + todas.length + ' separados';
    if (botao) botao.disabled = !(todas.length > 0 && marcadas === todas.length && !botao.dataset.bloqueio);
    if (btnRessalva) {
      const bloqueadoColab = marcadas > 0 && btnRessalva.dataset.bloqueio;
      const podeRessalva = todas.length > 0 && faltam > 0 && !bloqueadoColab;
      btnRessalva.disabled = !podeRessalva;
      if (!podeRessalva && painelRessalva) painelRessalva.hidden = true;
    }
    if (resumoRessalva) {
      resumoRessalva.textContent = marcadas + ' separado(s) · ' + faltam + ' não atendido(s)';
    }
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

  function ehMobile() {
    return window.matchMedia('(max-width: 768px)').matches || /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent || '');
  }

  function bip() {
    if (ehMobile()) return;
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
          if (ehMobile()) {
            dock.hidden = true;
          } else {
            dock.hidden = d.novos === 0;
            if (d.novos > 0) { corpo.innerHTML = d.html; qtd.textContent = d.novos; }
            if (anterior !== null && d.novos > anterior) { bip(); dock.classList.remove('minimizado'); }
          }
        }
        document.title = d.novos > 0 ? '(' + d.novos + ') ' + tituloOriginal : tituloOriginal;
        anterior = d.novos;
      })
      .catch(function () { /* tenta de novo no próximo ciclo */ });
  }

  const minimizar = document.getElementById('dock-min');
  if (minimizar) minimizar.addEventListener('click', function () { dock.classList.toggle('minimizado'); });
  window.addEventListener('resize', function () {
    if (dock && ehMobile()) dock.hidden = true;
  });
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
