# Estoque de EPIs

Sistema web para controlar **pedidos do Microsoft Forms (estilo comanda de restaurante), entradas, saídas (entregas por colaborador) e saldo de EPIs**, com alertas de reposição e relatórios para prestação de contas.

## Como rodar

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip ...
.venv/bin/python run.py                       # abre em http://localhost:5000
```

- Pela rede interna, os colegas acessam `http://IP-DO-COMPUTADOR:5000` (descubra o IP com `ipconfig`/`ip a`; libere a porta no firewall). Para só este PC: `python run.py --host 127.0.0.1`.
- No primeiro acesso o sistema pede para criar o **administrador**. Não existe senha padrão.
- Opcional (servidor mais robusto): `pip install waitress` — o `run.py` passa a usá-lo automaticamente.
- Testes: `.venv/bin/python -m unittest discover -s tests -t .`

## Fluxo de Pedidos (Microsoft Forms → Comanda → Separação → Retirada)

1. **Chegada do pedido**: quando o formulário é respondido (ou importado da planilha com as colunas **H a P**: *Nome, Local, Motivo, Vestimento, EPI, Sapatão, Camiseta, Calça, Observação*), o sistema cria um **card de pedido** e toca um alerta sonoro na tela.
2. **Iniciar separação ou Mais tarde**:
   - **Mais tarde**: o pedido sai do alerta de novos e fica guardado na categoria **Pendentes (deixados para mais tarde)** em *Pedidos*.
   - **Iniciar separação**: abre a tela do pedido com todos os itens solicitados e uma **checkbox** ao lado de cada item.
3. **Identificação inteligente**: o sistema casa automaticamente os textos do formulário com os EPIs cadastrados (ex.: coluna *Camiseta* com valor `M` casa com `Camiseta M`). Se algum texto for novo, você seleciona o EPI correspondente uma única vez e o sistema **aprende** para os próximos pedidos.
4. **Finalizar pedido**: quando todas as checkboxes estão marcadas, o botão **Finalizar pedido** é liberado. Ao clicar, o sistema **dá baixa automática no estoque** e muda o status para **Pronto para retirada**.
5. **Controle de saída / quem retirou**: na retirada (mesmo quando quem retira é um gestor, encarregado de obras ou portador), você informa **quem retirou** e uma **descrição** (cargo/obra), registrando data e hora e gerando o comprovante para assinatura.

### Configurando a integração com o Microsoft Forms

No menu **Integração** (visível para o Administrador) estão a URL (`POST /api/pedidos`), a chave `X-API-Key` e o passo a passo para:
- **Power Automate** (gatilho do Microsoft Forms ou Excel Online enviando JSON com `nome`, `local`, `motivo`, `vestimento`, `epi`, `sapatao`, `camiseta`, `calca`, `observacao`).
- **Importação direta de CSV** da planilha do Forms (lendo automaticamente as colunas H a P) ou **simulação manual** em *Pedidos*.

## Primeiros passos

1. **EPIs**: cadastre cada item (se há tamanhos, um cadastro por tamanho: "Luva G", "Bota 40", "Camiseta M"), com CA, validade do CA, **estoque mínimo** e **prazo de reposição**.
2. **Colaboradores**: cadastre nome, matrícula, setor e função.
3. **Saldo inicial**: *+ Movimentar → Ajuste / saldo inicial*, motivo "Saldo inicial".
4. A partir daí, atenda os **Pedidos** que chegam do Forms ou registre **Entradas**, **Entregas** e **Baixas** avulsas.

## O que responde às perguntas do RH/almoxarifado

| Pergunta | Onde |
|---|---|
| O que entra | Movimentações → tipo *Entrada*; Relatórios → Entradas × saídas por mês |
| O que sai, para quem e para qual local | Pedidos (controle de saída); Movimentações; ficha do colaborador; Relatórios por local/setor/colaborador |
| Item com mais saída | Painel ("Mais saem") e Relatórios → *Itens com mais saída* |
| Hora de fazer novo pedido | Painel → *Hora de pedir*, com quantidade sugerida |
| Prestar contas | Exportação CSV (abre no Excel) de pedidos/controle de saída, movimentações e estoque |

## Dados e backup

Tudo fica em `instance/epi.sqlite` (fora do git). **Faça cópia periódica desse arquivo** (com o sistema parado, ou usando `sqlite3 instance/epi.sqlite ".backup backup.sqlite"`). A chave de sessão fica em `instance/secret_key`.

> Na rede interna o sistema usa HTTP simples. Quando for publicado na internet para receber chamadas do Power Automate na nuvem, coloque atrás de um proxy reverso com HTTPS (ex.: Caddy ou Nginx) e defina `EPI_PROXY=1` e `EPI_COOKIE_SECURE=1`.
