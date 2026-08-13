#Requires -RunAsAdministrator
<#
Registra o painel de indicadores como tarefa agendada: sobe no boot, sem janela
de console, e reinicia sozinho se cair. Nao e um servico do Windows, nao
aparece em services.msc, mas entrega o mesmo comportamento sem depender de
wrapper (NSSM, WinSW, pywin32).

    .\instalar_servico.ps1
    .\instalar_servico.ps1 -Pasta "D:\Paineis\PainelAberturas"
    .\instalar_servico.ps1 -Mantenedor "DOMINIO\fulano"

A pasta de producao precisa existir antes, com o PainelAberturas.exe e o .env
dentro. Veja a secao "Colocar em producao" do README.

Depois de instalado:
    Stop-ScheduledTask       -TaskName PainelIndicadores   # para agora
    Start-ScheduledTask      -TaskName PainelIndicadores   # sobe de novo
    Disable-ScheduledTask    -TaskName PainelIndicadores   # nao sobe mais no boot
    Unregister-ScheduledTask -TaskName PainelIndicadores -Confirm:$false

Stop-ScheduledTask mata o exe. O painel fica fora do ar ate voce mandar subir.
#>
param(
    # Fora do perfil do usuario de proposito. Em Desktop ou Documentos, a pasta
    # vai junto se o perfil for recriado, e some se alguem limpar a area de
    # trabalho. A tarefa roda como SYSTEM, entao o lugar dela e no nivel da
    # maquina.
    [string]$Pasta = "C:\PainelAberturas",

    # Conta que vai poder editar o .env sem elevacao. Sem isso, so
    # Administradores e SYSTEM enxergam a pasta.
    [string]$Mantenedor
)

$TAREFA = "PainelIndicadores"
$exe = Join-Path $Pasta "PainelAberturas.exe"
$log = Join-Path $Pasta "painel_indicadores.log"

if (-not (Test-Path $exe)) {
    throw "Nao achei $exe. Gere o executavel com o PyInstaller e copie pra essa pasta, ou passe -Pasta."
}
if (-not (Test-Path (Join-Path $Pasta ".env"))) {
    throw "Nao achei o .env em $Pasta. Sem ele o painel sobe e morre com 'Faltando no .env', no log, sem console pra mostrar."
}

# Qualquer pasta criada em C:\ herda leitura pra BUILTIN\Usuarios, ou seja,
# qualquer conta local consegue abrir o .env e ler a senha do RTAC em texto
# puro. Dentro do perfil do usuario isso nao acontecia. Como a pasta saiu do
# perfil, a permissao tem que ser refeita na mao.
#
# SIDs em vez de nomes: "Administradores" e "Administrators" dependem do idioma
# do Windows, e o script precisa rodar igual nas duas.
$acl = @("*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F")   # SYSTEM, Administradores
if ($Mantenedor) { $acl += "${Mantenedor}:(OI)(CI)F" }

Write-Host "Restringindo o acesso a $Pasta..."
icacls $Pasta /inheritance:r /grant $acl | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls falhou em $Pasta com codigo $LASTEXITCODE." }

# Roda como SYSTEM: sobe no boot sem ninguem logado, e a sessao 0 nao tem
# desktop, entao nao ha janela pra aparecer. O cmd /c so existe pra redirecionar
# o log. Headless, ele e a unica forma de ver o detalhe de um erro da API, que
# de proposito nao vai pro navegador.
$action = New-ScheduledTaskAction -Execute "cmd.exe" `
    -Argument "/c `"`"$exe`" >> `"$log`" 2>&1`"" `
    -WorkingDirectory $Pasta

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TAREFA -Action $action -Principal $principal `
    -Settings $settings -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Description "Painel de historico de aberturas do RTAC SEL, porta 8422" -Force | Out-Null

Start-ScheduledTask -TaskName $TAREFA
Start-Sleep -Seconds 5

Get-ScheduledTask -TaskName $TAREFA | Get-ScheduledTaskInfo |
    Select-Object TaskName, LastRunTime, LastTaskResult, NumberOfMissedRuns

# LastTaskResult 267009 e "rodando agora", que e o esperado. Zero aqui
# significaria que o processo ja terminou, ou seja, que ele caiu.
Write-Host ""
Write-Host "Painel: http://localhost:8422"
Write-Host "Log:    $log"
