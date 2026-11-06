// Gerenciador de Notificações Web Push Mobile (Android / iOS)
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

  window.ativarNotificacoesMobile = async function() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
      alert("Seu navegador não suporta notificações Push Web. No iPhone (iOS), adicione este site à Tela de Início pelo botão Compartilhar para habilitar.");
      return;
    }

    try {
      const perm = await Notification.requestPermission();
      if (perm !== 'granted') {
        alert("Permissão de notificações não concedida. Habilite as notificações nas configurações do navegador no seu celular.");
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
        alert("🔔 Notificações ativadas com sucesso! Enviamos um alerta de teste para o seu aparelho.");
      } else {
        alert("Erro ao registrar aparelho: " + (dadosIns.erro || "desconhecido"));
      }
    } catch (err) {
      console.error(err);
      alert("Erro ao ativar notificações: " + err.message);
    }
  };

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
        alert("📲 Notificação enviada para o seu celular! Verifique a barra de notificações.");
      } else {
        alert("Falha ao enviar teste: " + (dados.erro || "Verifique se ativou as notificações neste aparelho."));
      }
    } catch (e) {
      alert("Erro ao testar: " + e.message);
    }
  };

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

  function atualizarBotoesPush(ativo) {
    document.querySelectorAll('.btn-push-toggle').forEach(btn => {
      btn.classList.toggle('push-ativo', ativo);
      btn.textContent = ativo ? '🔔 Notificações no Celular: Ativas' : '🔔 Ativar Notificações no Celular';
      btn.title = ativo ? 'Notificações ativas neste aparelho (clique para testar)' : 'Receba avisos instantâneos no celular quando pedidos chegarem';
      btn.onclick = ativo ? window.testarNotificacaoMobile : window.ativarNotificacoesMobile;
    });
    document.querySelectorAll('.card-push-banner').forEach(b => {
      const tag = b.querySelector('.status-push-tag');
      if (tag) {
        tag.textContent = ativo ? '✅ Notificações Ativadas neste aparelho' : '⚪ Notificações não ativadas neste aparelho';
        tag.className = 'status-push-tag ' + (ativo ? 'badge verde' : 'badge cinza');
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
      }
    }).catch(err => {
      console.warn("ServiceWorker não pôde ser registrado:", err);
    });
  }
})();
