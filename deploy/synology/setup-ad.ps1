<#
.SYNOPSIS
  Подготовка Active Directory для интеграции со SchoolHub.

.DESCRIPTION
  Выполняется НА КОНТРОЛЛЕРЕ ДОМЕНА (или на машине с RSAT) от имени администратора домена.
  Скрипт идемпотентен: повторный запуск не создаёт дубликаты.

  Что делает:
    1. Создаёт сервисную учётку svc_schoolhub (только чтение каталога, без права входа).
    2. Даёт ей право чтения нужных OU (или всего домена).
    3. Создаёт группы SchoolStaff / SchoolAdmins / SchoolHub_Owners.
    4. Проверяет доступность LDAPS (порт 636) и наличие сертификата на DC.
    5. Выводит готовые значения для .env SchoolHub.

.EXAMPLE
  .\setup-ad.ps1 -DomainName school.local -BaseDN "DC=school,DC=local" -ServiceAccountName svc_schoolhub

.NOTES
  Пароль сервисной учётки задаётся интерактивно (не хранится в скрипте).
  Учётка НЕ добавляется в Domain Admins — нужен только Read.
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $DomainName,          # school.local
    [Parameter(Mandatory = $true)] [string] $BaseDN,               # DC=school,DC=local
    [string] $ServiceAccountName = 'svc_schoolhub',
    [string] $ServiceAccountOU = 'OU=Service Accounts',           # будет создана, если нет
    [string[]] $GroupNames = @('SchoolStaff', 'SchoolAdmins', 'SchoolHub_Owners'),
    [int]    $LdapsPort = 636,
    [switch] $SkipCertificateCheck
)

$ErrorActionPreference = 'Stop'

function Write-Step($msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)    { Write-Host "    [OK] $msg" -ForegroundColor Green }
function Write-Warn($msg)  { Write-Host "    [!]  $msg" -ForegroundColor Yellow }
function Write-Err($msg)   { Write-Host "    [X]  $msg" -ForegroundColor Red }

# ------------------------------------------------------------------ 0. проверка окружения
Write-Step 'Проверка окружения'
try {
    Import-Module ActiveDirectory -ErrorAction Stop
    $dc = Get-ADDomainController -Discover -DomainName $DomainName -ErrorAction Stop
    Write-Ok "Модуль ActiveDirectory доступен, DC: $($dc.HostName[0])"
} catch {
    Write-Err "Не удалось импортировать модуль ActiveDirectory или найти DC: $($_.Exception.Message)"
    Write-Warn 'Запускайте скрипт на контроллере домена либо установите RSAT: Add-WindowsCapability -Online -Name "Rsat.ActiveDirectory.DS-LDS.Tools~~~~0.0.1.0"'
    exit 1
}

# ------------------------------------------------------------------ 1. OU для сервисных учёток
Write-Step "Проверка OU '$ServiceAccountOU'"
$ouPath = "$ServiceAccountOU,$BaseDN"
$ou = Get-ADOrganizationalUnit -Filter "DistinguishedName -eq '$ouPath'" -ErrorAction SilentlyContinue
if (-not $ou) {
    $parts = $ServiceAccountOU -split ','
    New-ADOrganizationalUnit -Name ($parts[0] -replace '^OU=', '') -Path $BaseDN -ProtectedFromAccidentalDeletion $true | Out-Null
    Write-Ok "OU создана: $ouPath"
} else {
    Write-Ok "OU существует: $ouPath"
}

# ------------------------------------------------------------------ 2. сервисная учётка
Write-Step "Сервисная учётка '$ServiceAccountName'"
$svc = Get-ADUser -Filter "SamAccountName -eq '$ServiceAccountName'" -ErrorAction SilentlyContinue
if (-not $svc) {
    $secure = Read-Host -Prompt "    Пароль для $ServiceAccountName (будет запрошен дважды)" -AsSecureString
    New-ADUser `
        -SamAccountName $ServiceAccountName `
        -UserPrincipalName "$ServiceAccountName@$DomainName" `
        -Name $ServiceAccountName `
        -DisplayName "SchoolHub Service Account" `
        -Description "Read-only account for SchoolHub LDAP authentication (bind + user/group lookup)" `
        -Path $ouPath `
        -AccountPassword $secure `
        -Enabled $true `
        -PasswordNeverExpires $true `
        -CannotChangePassword $true | Out-Null
    $svc = Get-ADUser -Filter "SamAccountName -eq '$ServiceAccountName'"
    Write-Ok "Создана: $($svc.DistinguishedName)"
} else {
    Write-Ok "Уже существует: $($svc.DistinguishedName)"
    Write-Warn 'Если пароль утерян — сбросьте его вручную и обновите LDAP_BIND_PASSWORD в .env'
}

# учётка не должна уметь входить интерактивно: убираем из группы Users право logon не требуется,
# но явно запрещаем вход на компьютеры домена (logonWorkstations = несуществующий хост)
try {
    Set-ADUser -Identity $svc -LogonWorkstations 'no-interactive-logon' -ErrorAction Stop
    Write-Ok 'Интерактивный вход сервисной учётке запрещён'
} catch { Write-Warn "Не удалось ограничить интерактивный вход: $($_.Exception.Message)" }

# ------------------------------------------------------------------ 3. право чтения каталога
Write-Step 'Право чтения каталога (Read permissions)'
try {
    $acl = Get-Acl -Path "AD:\$BaseDN"
    $rule = New-Object System.DirectoryServices.ActiveDirectoryAccessRule(
        $svc.SID, 'GenericRead', 'Allow'
    )
    $acl.AddAccessRule($rule)
    Set-Acl -Path "AD:\$BaseDN" -AclObject $acl
    Write-Ok "GenericRead выдан на $BaseDN"
    Write-Warn 'Если пользователи живут в отдельном OU — достаточно выдать право только на него (меньше поверхность доступа)'
} catch {
    Write-Warn "Не удалось назначить ACL автоматически: $($_.Exception.Message)"
    Write-Warn "Назначьте вручную: ADUC → свойства $BaseDN → Security → Add → $ServiceAccountName → Read"
}

# ------------------------------------------------------------------ 4. группы SchoolHub
Write-Step 'Группы SchoolHub'
foreach ($g in $GroupNames) {
    $exists = Get-ADGroup -Filter "SamAccountName -eq '$g'" -ErrorAction SilentlyContinue
    if (-not $exists) {
        New-ADGroup -Name $g -SamAccountName $g -GroupCategory Security -GroupScope Global `
            -Path $BaseDN -Description "SchoolHub access/role mapping" | Out-Null
        Write-Ok "Создана группа: $g"
    } else {
        Write-Ok "Группа существует: $g"
    }
}
Write-Warn 'Добавьте в SchoolStaff всех сотрудников школы (или используйте существующие группы в LDAP_ALLOWED_GROUPS)'

# ------------------------------------------------------------------ 5. LDAPS
Write-Step "Проверка LDAPS (порт $LdapsPort)"
$dcHost = $dc.HostName[0]
try {
    $tcp = New-Object System.Net.Sockets.TcpClient
    $iar = $tcp.BeginConnect($dcHost, $LdapsPort, $null, $null)
    if ($iar.AsyncWaitHandle.WaitOne(5000)) {
        $tcp.EndConnect($iar) | Out-Null
        Write-Ok "$dcHost`:$LdapsPort отвечает"
    } else {
        Write-Err "$dcHost`:$LdapsPort не ответил за 5 секунд"
    }
    $tcp.Close()
} catch {
    Write-Err "Порт $LdapsPort недоступен: $($_.Exception.Message)"
}

if (-not $SkipCertificateCheck) {
    Write-Step 'Сертификат DC для LDAPS'
    $cert = Get-ChildItem -Path Cert:\LocalMachine\My |
        Where-Object { $_.Subject -like "*$dcHost*" -and $_.NotAfter -gt (Get-Date) } |
        Select-Object -First 1
    if ($cert) {
        Write-Ok "Найден: $($cert.Subject), действует до $($cert.NotAfter)"
        Write-Warn 'Экспортируйте цепочку (корневой CA) в PEM и положите в /volume1/docker/schoolhub/certs/rootca.pem'
        Write-Host '    Пример экспорта корневого CA:' -ForegroundColor DarkGray
        Write-Host '    Get-ChildItem Cert:\LocalMachine\Root | Where-Object {$_.Subject -like "*school*"} | ' -ForegroundColor DarkGray
        Write-Host '      ForEach-Object { [IO.File]::WriteAllBytes("$env:TEMP\rootca.cer", $_.RawData) }' -ForegroundColor DarkGray
        Write-Host '    openssl x509 -inform der -in rootca.cer -out rootca.pem' -ForegroundColor DarkGray
    } else {
        Write-Warn "Сертификат для $dcHost не найден в LocalMachine\My — LDAPS может не работать."
        Write-Warn 'Проверьте наличие шаблона Domain Controller в ADCS или используйте LDAP+StartTLS (порт 389).'
    }
}

# ------------------------------------------------------------------ 6. готовый блок для .env
Write-Step 'Значения для /volume1/docker/schoolhub/.env'
$dnParts = $DomainName.Split('.') | ForEach- { "DC=$_" }
$computedBaseDN = $dnParts -join ','
Write-Host @"

LDAP_ENABLED=true
LDAP_SERVER=$dcHost`:$LdapsPort
LDAP_USE_SSL=true
LDAP_USE_STARTTLS=true
LDAP_TLS_VERIFY=required
LDAP_CA_CERT=/certs/rootca.pem
LDAP_DOMAIN=$DomainName
LDAP_BASE_DN=$BaseDN
LDAP_BIND_DN=$($svc.DistinguishedName)
LDAP_BIND_PASSWORD=<пароль сервисной учётки>
LDAP_LOGIN_FORMAT=upn
LDAP_PROVISIONING=auto
LDAP_DEFAULT_ROLE=TEACHER
LDAP_ALLOWED_GROUPS=["SchoolStaff"]
LDAP_ADMIN_GROUPS=["SchoolAdmins"]
LDAP_OWNER_GROUPS=["SchoolHub_Owners"]

# вычисленный BaseDN (для сверки): $computedBaseDN
"@ -ForegroundColor White

Write-Step 'Дальнейшие шаги'
Write-Host '  1. Скопируйте блок выше в .env и подставьте пароль сервисной учётки.' -ForegroundColor Gray
Write-Host '  2. Экспортируйте корневой CA в PEM → /volume1/docker/schoolhub/certs/rootca.pem' -ForegroundColor Gray
Write-Host '  3. Проверьте связность из контейнера: docker compose exec backend python -c "import socket;socket.create_connection(('"$dcHost"',$LdapsPort),5)"' -ForegroundColor Gray
Write-Host '  4. Откройте GET /api/v1/auth/ad/diagnostics под Владельцем — покажет проблемы конфигурации и ответ DC.' -ForegroundColor Gray
Write-Host ''
