# Indicadores: atuação de equipamentos da rede de distribuição

Painel web que mostra o histórico de aberturas de religadores, disjuntores e
chaves, lido da API REST do RTAC SEL. Para cada abertura mostra a hora e quanto
tempo levou até o fechamento seguinte.

O painel é **somente leitura**. Ele só faz `GET`. Nunca escreve, nunca comanda
equipamento, nunca altera configuração do RTAC.

Nada específico de uma rede está no código. Você cria os arquivos da sua
instalação seguindo os passos abaixo.

---

## Antes de começar

Confira estes três itens. Se algum faltar, resolva primeiro.

1. **Python 3.10 ou mais novo** instalado. Confira abrindo o PowerShell e
   digitando:

   ```powershell
   py --version
   ```

   Se aparecer "não é reconhecido", instale de python.org e marque a opção
   "Add Python to PATH" durante a instalação.

2. **A máquina alcança o RTAC.** Troque `10.0.0.196` pelo endereço do seu:

   ```powershell
   Test-NetConnection 10.0.0.196 -Port 443
   ```

   Precisa aparecer `TcpTestSucceeded : True`. Se aparecer `False`, o problema é
   de rede ou de rota, e nenhum passo seguinte vai funcionar.

3. **Uma conta de serviço no RTAC.** Se você ainda não tem, ela é criada no
   Passo 1.

---

## Passo 1: criar a conta de serviço no RTAC

Não use a sua conta de operador. Não use a conta de engenharia. Não use
nenhuma conta que consiga comandar equipamento.

O motivo: a senha fica em texto puro num arquivo dessa máquina. Se ela vazar, o
estrago possível é o que a conta permite fazer, e não o que este código faz.

O painel precisa de exatamente duas permissões: `API_Login` e `Report_Read`.

**No RTAC, vá em User, depois User Roles.** Você tem duas opções:

* **Recomendado.** Crie um papel novo e marque apenas `API_Login` e
  `Report_Read`. É o menor privilégio que faz o painel funcionar.
* **Aceitável.** Use o papel `Monitor`, que já vem de fábrica. Ele funciona,
  mas carrega a permissão `Reboot_Device` junto. Quem tiver essa senha
  consegue reiniciar o RTAC.

**Não use o papel `File Transfer`.** Ele tem `Report_Read` mas não tem
`API_Login`. Toda requisição volta erro `401`, e parece senha errada quando na
verdade é papel errado. `File Transfer` dá acesso por SFTP, não por API.

**Depois crie o usuário** e associe ao papel escolhido. Duas regras para a
senha:

1. Diferente do nome de usuário.
2. Não reaproveitada de nenhum outro sistema.

Anote usuário e senha. Você vai usar no Passo 3.

---

## Passo 2: instalar as dependências

Abra o PowerShell **na pasta `indicadores`** e rode:

```powershell
py -m pip install -r requirements.txt
```

Isso instala `requests`, `urllib3`, `python-dotenv` e `beautifulsoup4`.

---

## Passo 3: criar o arquivo `.env`

Este arquivo guarda o endereço e a credencial do RTAC. Ele nunca vai para o
repositório.

Ainda na pasta `indicadores`:

```powershell
Copy-Item .env.example .env
notepad .env
```

Preencha três campos, no mínimo:

| Campo | O que colocar |
|---|---|
| `SEL_RTAC_HOST` | endereço do RTAC na sua rede, sem `https://` |
| `SEL_RTAC_USER` | o usuário criado no Passo 1 |
| `SEL_RTAC_PASSWORD` | a senha dele |

Os outros campos já vêm com valor que funciona na maioria das instalações. O
próprio `.env.example` explica cada um, caso precise mexer.

Salve e feche o Notepad.

---

## Passo 4: testar a interface sem tocar no RTAC

Este passo é opcional, mas vale fazer antes do painel real. Ele mostra se a
interface funciona na sua máquina, usando dados falsos.

```powershell
py mock_aberturas.py
```

Abra `http://localhost:8423` no navegador. Você vai ver 7 anos de histórico
inventado. Nenhuma conexão é feita com o RTAC, e o `.env` nem é lido.

Para parar, volte ao PowerShell e aperte `Ctrl+C`.

---

## Passo 5: subir o painel de verdade

```powershell
py historico_aberturas.py
```

Abra `http://localhost:8422`.

A primeira carga demora, porque o painel busca o histórico completo de cada
equipamento. Acompanhe o PowerShell: quando aparecer a linha
`N equipamentos, M aberturas no total`, os dados estão prontos.

Enquanto a janela do PowerShell estiver aberta, o painel roda. Fechar a janela
mata o painel.

---

## Passo 6: colocar os nomes dos alimentadores

Opcional. Sem este passo, cada equipamento aparece só com o código, por exemplo
`RL-01`. Nada quebra.

O nome do alimentador não existe na API de alarmes nem no `dicionario_tags.csv`.
Ele só existe no projeto de HMI do seu RTAC.

1. Descubra o nome do seu projeto de HMI:

   ```
   GET /api/v1/hmi/projects
   ```

2. Baixe o projeto:

   ```
   GET /api/v1/hmi/projects/<NOME_QUE_APARECEU>
   ```

   A resposta é um JSON. O campo `Contents` traz o XML do HMI comprimido em
   LZMA *alone* e codificado em base64. Descompacte e procure os
   `DiagramTitle`. Eles vêm no formato `RL-01 (5009) - NOME DO ALIMENTADOR`.

3. Monte o arquivo:

   ```powershell
   Copy-Item nomes_equipamento.exemplo.json nomes_equipamento.json
   notepad nomes_equipamento.json
   ```

   A chave é o código como ele aparece **no começo** do campo `Category` do
   alarme. Se o `Category` é `RL-01 RELIGADOR`, a chave é `RL-01`. O valor é o
   nome que você quer ver no painel.

4. Reinicie o painel (`Ctrl+C` e rode de novo).

Equipamento que não estiver no arquivo continua aparecendo só com o código.
Chaves de interligação, bancos de capacitor e os disjuntores da subestação
normalmente não têm nome no HMI, então esse é o comportamento esperado para
eles.

---

## Passo 7: colocar a marca da sua distribuidora

Opcional. Duas coisas independentes:

**Logo.** Salve um arquivo `logo.png` dentro da pasta `indicadores`. Ele é
embutido na página, então a página continua funcionando sem buscar nada na
internet. Sem esse arquivo, o painel sobe sem logo e sem ícone de aba, e o
resto funciona igual.

**Título da aba.** Adicione uma linha no seu `.env`:

```
PAINEL_TITULO=Aberturas por equipamento
```

**Cores.** Ficam no bloco `:root` dentro de `historico_aberturas.py`. Troque os
tokens `--brand`, `--brand-mid`, `--brand-deep` e `--lime` pelas cores da sua
marca. Não mexa nas `--series-1`, `--series-2` e `--series-3` sem revalidar
contraste. O motivo está na seção "Por que o código é assim", no fim.

---

## Passo 8: abrir o painel para outras máquinas

Opcional, e leia o aviso antes.

> **Aviso.** O painel **não tem autenticação**. Quem alcançar a porta vê o
> inventário da subestação e o histórico completo de interrupções. Quem limita
> quem chega é a regra de firewall deste passo, e mais nada.

O código já vem com `BIND_HOST = "0.0.0.0"`, que atende em todas as placas.
Mesmo assim, é comum o painel continuar inacessível de fora. O sintoma é sempre
o mesmo: sobe normal, responde em `localhost`, e recusa conexão de outra
máquina.

Duas causas, nessa ordem de frequência:

1. Existe regra de **bloqueio** de entrada para `python.exe` no perfil ativo.
2. A placa está classificada como **Public**, que é o perfil onde essas regras
   de bloqueio costumam viver.

Descubra o seu caso antes de criar qualquer regra:

```powershell
Get-NetConnectionProfile
Get-NetIPAddress -AddressFamily IPv4
Get-NetFirewallRule -Action Block -Enabled True -Direction Inbound |
  Where-Object DisplayName -match 'python|Painel'
```

A primeira linha mostra qual placa está em qual perfil. A segunda mostra os
endereços e as sub-redes locais. A terceira lista as regras de bloqueio que
podem estar derrubando o acesso.

Com essas informações, crie a regra restrita à sub-rede da operação. Substitua
`10.0.0.0/24` pela sua sub-rede e `Private` pelo perfil que apareceu:

```powershell
New-NetFirewallRule -DisplayName "Painel Aberturas 8422" -Direction Inbound `
  -Action Allow -Protocol TCP -LocalPort 8422 `
  -RemoteAddress 10.0.0.0/24 -Profile Private
```

Não libere para qualquer origem. A regra restrita é a única coisa separando o
painel do resto da rede.

Se quiser o caminho inverso, ou seja, deixar o painel só nesta máquina, troque
no `historico_aberturas.py`:

```python
BIND_HOST = "127.0.0.1"
```

---

## Gerar o executável

O `.spec` fica na **raiz do projeto**, um nível acima. Rode de lá:

```powershell
py -m PyInstaller PainelAberturas.spec --noconfirm
```

O executável sai em `dist\PainelAberturas.exe`.

**Importante.** Os arquivos da sua instalação ficam **ao lado do executável**, e
não dentro dele. Copie os três para a mesma pasta do `.exe`:

```
PainelAberturas.exe
.env
nomes_equipamento.json     (se você fez o Passo 6)
logo.png                   (se você fez o Passo 7)
```

O motivo: quando congelado pelo PyInstaller, o programa é extraído numa pasta
temporária que some a cada execução. Ler os arquivos de lá deixaria você sem
como trocar a senha ou o cadastro sem recompilar tudo.

---

## Problemas comuns

| O que acontece | Causa provável | O que fazer |
|---|---|---|
| `Faltando no .env: SEL_RTAC_HOST, ...` | o `.env` não existe ou está incompleto | refaça o Passo 3 |
| Toda requisição volta `401` | conta com papel `File Transfer`, que não tem `API_Login` | refaça o Passo 1 |
| Painel abre mas mostra erro de conexão | RTAC inalcançável desta máquina | rode o `Test-NetConnection` do item 2 de "Antes de começar" |
| Equipamentos aparecem só com o código | `nomes_equipamento.json` não existe ou não tem aquele código | Passo 6 |
| Página sem logo e sem ícone de aba | não existe `logo.png` na pasta | Passo 7 |
| `localhost` funciona, outra máquina recusa | firewall ou perfil da placa | Passo 8 |
| `.exe` fecha sozinho reclamando do `.env` | os arquivos não foram copiados para junto do executável | veja "Gerar o executável" |
| `Unable to find acceptable character detection dependency` | build do PyInstaller sem o `charset_normalizer` compilado | o `.spec` já resolve isso, use ele e não o modo `--onefile` na mão |

---

## Rodar as checagens

```powershell
py test_pareamento.py
```

São 6 checagens da lógica de pareamento entre abertura e fechamento. Todas
precisam passar. Rode depois de qualquer alteração nessa parte do código.

---

## Por que o código é assim

Esta seção não tem passo a passo. Ela registra decisões que não são óbvias
lendo o código, para que ninguém as desfaça sem saber o que estava resolvendo.

**Pareamento de abertura e fechamento.** Uma abertura é o evento `Alarmed`
casado com o `Normalized` seguinte da mesma tag. `Acknowledged` não entra:
reconhecer um alarme gera um registro novo com a mesma `Message`, e usar a
`Message` contaria o reconhecimento como uma abertura nova. `Alarmed` repetido
enquanto o ponto já está aberto é ignorado, porque fisicamente o religador
precisa fechar para abrir de novo, então são o mesmo evento gravado duas vezes.
Sem esse tratamento, religador fechado aparecia como pendente de fechamento.

**Nome do alimentador não sai do `Category`.** O complemento do `Category` não
vira nome. `BC-02 CHAVE VÁCUO` viraria "BC-02 VÁCUO", como se existisse um
alimentador chamado Vácuo. Por isso o cadastro é um arquivo separado.

**Cores das séries de dados.** A cor `--lime` fica fora das séries de propósito,
e a regra vale para qualquer cor de marca que entre no lugar dela: contra o
verde da marca ela não alcança o piso de separação para visão normal, e contra
o laranja quebra no daltonismo deutan. Ela é cor de marca, para cabeçalho,
detalhe e status. As séries 1 a 3 passam o validador de daltonismo nos dois
modos.

**Nome do equipamento vai por `data-eq`, não por `onclick`.** Dentro de um
atributo `onclick` o nome vai escapado para HTML, e um equipamento com apóstrofo
ou `&` chegava ao handler como `O&#39;HIGGINS`. Nunca casava com a chave crua, a
linha não expandia, e não aparecia erro nenhum no console.

**Erro da API não vai para o cliente.** O `/data.json` manda só um booleano
dizendo que falhou. O `str(exc)` do `requests` carrega a URL completa, o que
entregaria o endereço do RTAC e a estrutura da API para qualquer visitante. O
detalhe fica no log do servidor, que é onde quem opera vai procurar.

**O `.env` é apontado explicitamente.** O `load_dotenv()` sem argumento resolve
o arquivo a partir do diretório atual, subindo. Rodando de outra pasta, ou como
serviço, onde o diretório atual é o `system32`, ele não acha nada, e o erro que
aparece é "Faltando no .env", que parece credencial errada e não caminho errado.

**O mock tem cadastro próprio.** Ele não herda o `nomes_equipamento.json` do
painel. Numa máquina sem esse arquivo, nenhum equipamento teria apelido e o caso
de renderização com nome sumiria do teste.

**O mock passa pela lógica real.** Ele gera 7 anos de histórico sintético com
semente fixa e manda pelo `_pair_open_close` de verdade, e não por um atalho que
devolve pares prontos. O pareamento é a parte com mais chance de errar, então
tem que estar no caminho do teste. Casos plantados de propósito: ano vazio no
meio da série, mês vazio, dia muito acima do topo da rampa de calor, duração de
vários dias, 29 de fevereiro, evento no primeiro e no último dia do mês,
pendência real de fechamento, `Alarmed` duplicado, equipamento sem cadastro,
equipamento sem palavra de classe no `Category`, e nome com apóstrofo e `&`.
