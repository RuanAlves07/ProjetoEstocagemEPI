import unittest
from unittest.mock import patch
from tests.test_app import Base
from app.db import get_db

class TestPushEndpoints(Base):
    def test_obter_chave_publica(self):
        resp = self.c.get('/api/push/chave-publica')
        self.assertEqual(resp.status_code, 200)
        dados = resp.get_json()
        self.assertTrue(dados.get('ok'))
        self.assertIn('publicKey', dados)
        self.assertTrue(len(dados['publicKey']) > 10)

    def test_inscrever_e_desinscrever(self):
        endpoint_teste = 'https://fcm.googleapis.com/fcm/send/fake-test-token-123'
        resp = self.c.post('/api/push/inscrever', json={
            'endpoint': endpoint_teste,
            'keys': {'p256dh': 'fake-p256dh', 'auth': 'fake-auth'}
        })
        self.assertEqual(resp.status_code, 200)
        dados = resp.get_json()
        self.assertTrue(dados.get('ok'))

        # Verifica no banco
        with self.app.app_context():
            db = get_db()
            inscricao = db.execute("SELECT * FROM push_inscricoes WHERE endpoint = ?", (endpoint_teste,)).fetchone()
            self.assertIsNotNone(inscricao)
            self.assertEqual(inscricao['auth'], 'fake-auth')

        # Desinscrever
        resp_des = self.c.post('/api/push/desinscrever', json={
            'endpoint': endpoint_teste
        })
        self.assertEqual(resp_des.status_code, 200)
        self.assertTrue(resp_des.get_json().get('ok'))

        with self.app.app_context():
            db = get_db()
            inscricao = db.execute("SELECT * FROM push_inscricoes WHERE endpoint = ?", (endpoint_teste,)).fetchone()
            self.assertIsNone(inscricao)

    @patch('app.push_service.enviar_push_dispositivo')
    def test_testar_push(self, mock_enviar):
        mock_enviar.return_value = (True, None)
        resp = self.c.post('/api/push/testar', json={
            'endpoint': 'https://fcm.fake/send/1',
            'keys': {'p256dh': 'k1', 'auth': 'k2'}
        })
        self.assertEqual(resp.status_code, 200)
        dados = resp.get_json()
        self.assertTrue(dados.get('ok'))
        self.assertTrue(mock_enviar.called)

    def test_chave_privada_pem_gera_assinatura_vapid(self):
        """Regressão: a chave PEM salva precisa ser aceita pelo pywebpush."""
        from app import push_service
        with self.app.app_context():
            chaves = push_service.obter_ou_criar_chaves_vapid(get_db())
        with patch('app.push_service.webpush') as mock_wp:
            ok, _ = push_service.enviar_push_dispositivo(
                {'endpoint': 'https://fcm.fake/x', 'keys': {'p256dh': 'a', 'auth': 'b'}},
                {'title': 't'}, chaves)
            self.assertTrue(ok)
            vapid = mock_wp.call_args.kwargs['vapid_private_key']
        cab = vapid.sign({'sub': 'mailto:a@b.c', 'aud': 'https://fcm.fake'})
        self.assertIn('Authorization', cab)

    def test_manifest_e_sw_routes(self):
        resp_manifest = self.c.get('/manifest.json')
        self.assertEqual(resp_manifest.status_code, 200)
        self.assertIn('json', resp_manifest.mimetype)

        resp_sw = self.c.get('/sw.js')
        self.assertEqual(resp_sw.status_code, 200)
        self.assertEqual(resp_sw.headers.get('Service-Worker-Allowed'), '/')

if __name__ == '__main__':
    unittest.main()
