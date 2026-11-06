// Gerenciador de Notificações Web Push (PC e Mobile)
(function() {
  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/\-/g, '+').replace(/_/g, '/');
    const rawData = window.atob(base64);
    const outputArray = new Uint8Array(rawData.length);
    for (let i = 0; i < rawData.length; ++i) {
      outputArray[i] = rawData.charCodeAt(i);
    }
    return outputArray;
  }

  function ehMobile() {
    return window.matchMedia('(max-width: 768px)').matches || /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent || '');
  }

  function orientacaoDesbloqueio() {
    if (ehMobile()) {
      return "As notificações estão bloqueadas nas configurações do navegador no seu celular.\n\nPara desbloquear:\n1. Toque no ícone de cadeado 🔒 ou opções ⋮ ao lado da URL;\n2. Abra 'Configurações do site' > 'Notificações';\n3. Mude para 'Permitir' e recarregue a página.";
    }
    return "As notificações estão bloqueadas nas configurações deste navegador no computador.\n\nPara desbloquear:\n1. Clique no ícone de cadeado ou ajustes 🔒 à esquerda da barra de endereço (URL);\n2. Em 'Notificações', selecione 'Permitir' (ou 'Redefinir permissões');\n3. Recarregue a página (F5) e tente novamente.";
  }

  async function obterInscricaoExistente() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return null;
    try {
      const reg = await navigator.serviceWorker.ready;
      return await reg.pushManager.getSubscription();
    } catch (e) {
      console.warn("Erro ao buscar subscrição push:", e);
      return null;
    }
  }

  function solicitarPermissaoNavegador() {
    if (!('Notification' in window)) return Promise.resolve('unsupported');
    return new Promise(function(resolve) {
      try {
        const p = Notification.requestPermission(function(res) {
          resolve(res || Notification.permission);
        });
        if (p && typeof p.then === 'function') {
          p.then(resolve).catch(function() {
            resolve(Notification.permission);
          });
        }
      } catch (err) {
        try {
          Notification.requestPermission().then(resolve).catch(function() {
            resolve(Notification.permission);
          });
        } catch (e2) {
          resolve(Notification.permission);
        }
      }
    });
  }

  window.ativarNotificacoesMobile = async function() {
    if (window.isSecureContext === false && location.hostname !== 'localhost' && location.hostname !== '127.0.0.1') {
      alert("⚠️ As notificações Web Push exigem conexão segura HTTPS.\n\nSe estiver acessando por HTTP direto ou IP, acesse pelo domínio com HTTPS (ex: https://" + location.hostname + ").");
      return;
    }

    if (!('Notification' in window) || !('serviceWorker' in navigator) || !('PushManager' in window)) {
      alert("Seu navegador atual não suporta notificações Push Web. No iPhone (iOS), use o Safari e adicione o site à 'Tela de Início' pelo menu Compartilhar para habilitar.");
      return;
    }

    // Se o navegador já marcou como bloqueado (denied), o browser não reabre o prompt
    if (Notification.permission === 'denied') {
      alert(orientacaoDesbloqueio());
      const ajudaBox = document.getElementById('ajuda-push-bloqueado');
      if (ajudaBox) ajudaBox.style.display = 'block';
      return;
    }

    try {
      // Solicita permissão de forma compatível (Promise ou callback)
      const perm = await solicitarPermissaoNavegador();
      if (perm !== 'granted') {
        if (perm === 'denied') {
          alert(orientacaoDesbloqueio());
          const ajudaBox = document.getElementById('ajuda-push-bloqueado');
          if (ajudaBox) ajudaBox.style.display = 'block';
        } else {
          alert("A permissão para envio de notificações não foi confirmada. Clique em 'Permitir' na janela de autorização do navegador.");
        }
        atualizarBotoesPush(false);
        return;
      }

      const reg = await navigator.serviceWorker.ready;
      let sub = await reg.pushManager.getSubscription();

      if (!sub) {
        const respKey = await fetch('/api/push/chave-publica');
        const dadosKey = await respKey.json();
        if (!dadosKey.ok || !dadosKey.publicKey) {
          throw new Error("Não foi possível obter a chave pública de notificações.");
        }
        const appServerKey = urlBase64ToUint8Array(dadosKey.publicKey);
        sub = await reg.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: appServerKey
        });
      }

      const subJson = sub.toJSON();
      const respIns = await fetch('/api/push/inscrever', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          endpoint: sub.endpoint,
          keys: subJson.keys
        })
      });
      const dadosIns = await respIns.json();

      if (dadosIns.ok) {
        atualizarBotoesPush(true);
        // Dispara teste automático para confirmar no aparelho
        await fetch('/api/push/testar', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            endpoint: sub.endpoint,
            keys: subJson.keys
          })
        });
        alert("🔔 Notificações ativadas com sucesso! Enviamos um alerta de teste para este aparelho.");
      } else {
        alert("Erro ao registrar aparelho: " + (dadosIns.erro || "desconhecido"));
      }
    } catch (err) {
      console.error(err);
      alert("Erro ao ativar notificações: " + err.message);
    }
  };

  window.ativarNotificacoesPush = window.ativarNotificacoesMobile;

  window.testarNotificacaoMobile = async function() {
    try {
      const sub = await obterInscricaoExistente();
      const payload = sub ? { endpoint: sub.endpoint, keys: sub.toJSON().keys } : {};
      const resp = await fetch('/api/push/testar', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const dados = await resp.json();
      if (dados.ok) {
        alert("📲 Notificação de teste enviada! Verifique as notificações do seu aparelho.");
      } else {
        alert("Falha ao enviar teste: " + (dados.erro || "Verifique se as notificações estão ativadas neste aparelho."));
      }
    } catch (e) {
      alert("Erro ao testar: " + e.message);
    }
  };

  window.testarNotificacaoPush = window.testarNotificacaoMobile;

  window.desativarNotificacoesMobile = async function() {
    try {
      const sub = await obterInscricaoExistente();
      if (sub) {
        await fetch('/api/push/desinscrever', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ endpoint: sub.endpoint })
        });
        await sub.unsubscribe();
      }
      atualizarBotoesPush(false);
      alert("Notificações desativadas para este aparelho.");
    } catch (e) {
      alert("Erro ao desativar: " + e.message);
    }
  };

  window.desativarNotificacoesPush = window.desativarNotificacoesMobile;

  function atualizarBotoesPush(ativo) {
    const perm = ('Notification' in window) ? Notification.permission : 'unsupported';
    const ajudaBox = document.getElementById('ajuda-push-bloqueado');

    if (perm === 'denied' && ajudaBox) {
      ajudaBox.style.display = 'block';
    } else if (ajudaBox && ativo) {
      ajudaBox.style.display = 'none';
    }

    document.querySelectorAll('.btn-push-toggle').forEach(btn => {
      btn.classList.toggle('push-ativo', ativo);
      btn.textContent = ativo ? '🔔 Notificações Ativas neste Aparelho' : '🔔 Ativar Notificações neste Aparelho';
      btn.title = ativo ? 'Notificações ativas neste aparelho (clique para testar)' : 'Receba avisos instantâneos quando pedidos chegarem';
      btn.onclick = ativo ? window.testarNotificacaoMobile : window.ativarNotificacoesMobile;
    });

    document.querySelectorAll('.card-push-banner').forEach(b => {
      const tag = b.querySelector('.status-push-tag');
      if (tag) {
        if (ativo) {
          tag.textContent = '✅ Notificações Ativadas neste aparelho';
          tag.className = 'status-push-tag badge verde';
        } else if (perm === 'denied') {
          tag.textContent = '🚫 Bloqueado nas permissões do navegador';
          tag.className = 'status-push-tag badge vermelho';
        } else {
          tag.textContent = '⚪ Notificações não ativadas neste aparelho';
          tag.className = 'status-push-tag badge cinza';
        }
      }
      const btnTeste = b.querySelector('.btn-testar-push');
      if (btnTeste) btnTeste.hidden = !ativo;
      const btnAtivar = b.querySelector('.btn-ativar-push');
      if (btnAtivar) btnAtivar.hidden = ativo;
      const btnDesat = b.querySelector('.btn-desativar-push');
      if (btnDesat) btnDesat.hidden = !ativo;
    });
  }

  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').then(reg => {
      if ('pushManager' in reg) {
        reg.pushManager.getSubscription().then(sub => {
          atualizarBotoesPush(Boolean(sub));
        });
      } else {
        atualizarBotoesPush(false);
      }
    }).catch(err => {
      console.warn("ServiceWorker não pôde ser registrado:", err);
      atualizarBotoesPush(false);
    });
  } else {
    document.querySelectorAll('.card-push-banner').forEach(b => {
      const tag = b.querySelector('.status-push-tag');
      if (tag) {
        tag.textContent = '⚠️ Navegador não compatível com Web Push';
        tag.className = 'status-push-tag badge vermelho';
      }
    });
  }
})();
