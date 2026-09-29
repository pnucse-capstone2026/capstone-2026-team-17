[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$OpenBrowser,
    [switch]$ProductionLike,
    [switch]$BackendReload,
    [switch]$SkipFrontendBuild,
    [switch]$ForceFrontendBuild,
    [switch]$ResetDatabaseSchema,
    [switch]$SkipBootstrap,
    [switch]$ForceToolchainBuild,
    [switch]$ResetDatabase,
    [ValidateRange(1024, 65535)]
    [int]$Port = 8100,
    [ValidateRange(1024, 65535)]
    [int]$FrontendPort = 5600,
    [ValidateRange(1024, 65535)]
    [int]$DatabasePort = 33060,
    [string]$DatabaseImage = "mysql:8.4"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$frontendRoot = Join-Path $repoRoot "frontend"
$runRoot = Join-Path $repoRoot ".easydep\dev"
$pidPath = Join-Path $runRoot "server.json"
$stdoutPath = Join-Path $runRoot "server.stdout.log"
$stderrPath = Join-Path $runRoot "server.stderr.log"
$frontendStdoutPath = Join-Path $runRoot "frontend.stdout.log"
$frontendStderrPath = Join-Path $runRoot "frontend.stderr.log"
$backendStdinPath = Join-Path $runRoot "server.stdin"
$frontendStdinPath = Join-Path $runRoot "frontend.stdin"
$frontendBuildHashPath = Join-Path $runRoot "frontend-build.sha256"
$frontendDependenciesHashPath = Join-Path $runRoot "frontend-dependencies.sha256"
$requirementsHashPath = Join-Path $runRoot "requirements.sha256"
$toolchainHashPath = Join-Path $runRoot "toolchain-build.sha256"
$environmentPath = Join-Path $repoRoot ".env"
$environmentExamplePath = Join-Path $repoRoot ".env.example"
$toolchainDockerfile = Join-Path $repoRoot "docker\Dockerfile.toolchain"
$toolchainImage = "easydep-toolchain:local"
$toolchainBuildCacheLimit = "5GB"
$memberGradleCacheVolume = "easydep-member-gradle-cache"
$databaseContainer = "easydep-mysql-dev"
$databaseVolume = "easydep-mysql-dev-data"
$databasePassword = "easydep-local"

New-Item -ItemType Directory -Force -Path $runRoot | Out-Null

function Test-CommandAvailable {
    param([Parameter(Mandatory = $true)][string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Read-HashRecord {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8).Trim()
}

function Get-CombinedFileHash {
    param(
        [Parameter(Mandatory = $true)][System.IO.FileInfo[]]$Files,
        [string[]]$ExtraRecords = @()
    )
    $manifest = foreach ($file in @($Files | Sort-Object FullName -Unique)) {
        $relativePath = $file.FullName.Substring($repoRoot.Length).TrimStart("\", "/").Replace("\", "/")
        $fileHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        "$relativePath=$fileHash"
    }
    $manifest += $ExtraRecords
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes(($manifest -join "`n"))
        return ([System.BitConverter]::ToString($sha256.ComputeHash($bytes))).Replace("-", "")
    }
    finally {
        $sha256.Dispose()
    }
}

function Get-FrontendBuildHash {
    $files = @()
    foreach ($directoryName in @("src", "static")) {
        $directory = Join-Path $frontendRoot $directoryName
        if (Test-Path -LiteralPath $directory) {
            $files += Get-ChildItem -LiteralPath $directory -Recurse -File
        }
    }
    foreach ($fileName in @(
        "package.json",
        "package-lock.json",
        "svelte.config.js",
        "vite.config.ts",
        "tsconfig.json",
        "components.json"
    )) {
        $file = Join-Path $frontendRoot $fileName
        if (Test-Path -LiteralPath $file) {
            $files += Get-Item -LiteralPath $file
        }
    }
    $files += Get-ChildItem -LiteralPath $frontendRoot -File -Filter ".env*" -ErrorAction SilentlyContinue

    $manifest = foreach ($file in @($files | Sort-Object FullName -Unique)) {
        $relativePath = $file.FullName.Substring($frontendRoot.Length).TrimStart("\", "/").Replace("\", "/")
        $fileHash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
        "$relativePath=$fileHash"
    }
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes(($manifest -join "`n"))
        return ([System.BitConverter]::ToString($sha256.ComputeHash($bytes))).Replace("-", "")
    }
    finally {
        $sha256.Dispose()
    }
}

function Get-FrontendDependenciesHash {
    $files = @("package.json", "package-lock.json") | ForEach-Object {
        Get-Item -LiteralPath (Join-Path $frontendRoot $_)
    }
    return Get-CombinedFileHash -Files $files
}

function Read-ServerRecord {
    if (-not (Test-Path -LiteralPath $pidPath)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $pidPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        return $null
    }
}

function Get-MatchingProcess {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [Parameter(Mandatory = $true)][string]$StartedAt,
        [int]$ToleranceSeconds = 2
    )
    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    $expected = [datetime]::Parse($StartedAt).ToUniversalTime()
    $actual = $process.StartTime.ToUniversalTime()
    if ([math]::Abs(($actual - $expected).TotalSeconds) -gt $ToleranceSeconds) {
        return $null
    }
    return $process
}

function Get-OwnedServerProcesses {
    $record = Read-ServerRecord
    if ($null -eq $record -or -not $record.pid -or -not $record.startedAt) {
        return @()
    }
    $owned = @()
    $launcher = Get-MatchingProcess `
        -ProcessId ([int]$record.pid) `
        -StartedAt ([string]$record.startedAt)
    if ($null -ne $launcher) {
        $owned += $launcher
    }
    if ($record.frontendPid -and $record.frontendStartedAt) {
        $frontend = Get-MatchingProcess `
            -ProcessId ([int]$record.frontendPid) `
            -StartedAt ([string]$record.frontendStartedAt)
        if ($null -ne $frontend) {
            $owned += $frontend
        }
    }
    return @($owned | Sort-Object Id -Unique)
}

function Stop-OwnedServer {
    foreach ($process in @(Get-OwnedServerProcesses)) {
        Write-Host "[EasyDep] Stopping owned development process $($process.Id)."
        # 개발 모드의 Uvicorn과 Vite는 변경 감시용 자식 프로세스를 만든다.
        # 부모만 종료하면 자식이 포트를 계속 점유할 수 있으므로 Windows가 제공하는
        # taskkill의 트리 종료를 사용한다. 위에서 PID와 시작 시각을 함께 확인했기
        # 때문에 같은 PID를 나중에 사용한 다른 프로그램을 잘못 종료하지 않는다.
        $previousPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        & taskkill.exe /PID $process.Id /T /F *> $null
        $taskkillExitCode = $LASTEXITCODE
        $ErrorActionPreference = $previousPreference
        if ($taskkillExitCode -ne 0) {
            Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        }
        $process.WaitForExit(15000) | Out-Null
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
}

function Assert-LoopbackPortAvailable {
    param(
        [Parameter(Mandatory = $true)][int]$CandidatePort,
        [Parameter(Mandatory = $true)][string]$Purpose
    )
    # 포트를 공유하도록 완화하면 이전 개발 서버가 남아 있어도 새 서버가 겹쳐 뜰 수 있다.
    # 배타적으로 bind해, 실제 서버가 안전하게 단독 사용 가능한 포트인지 확인한다.
    $deadline = [datetime]::UtcNow.AddSeconds(3)
    $lastError = "unknown socket error"
    do {
        $socket = [System.Net.Sockets.Socket]::new(
            [System.Net.Sockets.AddressFamily]::InterNetwork,
            [System.Net.Sockets.SocketType]::Stream,
            [System.Net.Sockets.ProtocolType]::Tcp
        )
        try {
            $socket.ExclusiveAddressUse = $true
            $socket.Bind(
                [System.Net.IPEndPoint]::new(
                    [System.Net.IPAddress]::Loopback,
                    $CandidatePort
                )
            )
            $socket.Listen(1)
            return
        }
        catch {
            $lastError = $_.Exception.Message
        }
        finally {
            $socket.Dispose()
        }
        Start-Sleep -Milliseconds 250
    } while ([datetime]::UtcNow -lt $deadline)
    throw (
        "$Purpose port $CandidatePort cannot be opened on 127.0.0.1. " +
        "It may be in use or reserved by Windows. Choose another port. " +
        "Original error: $lastError"
    )
}

function Invoke-Docker {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed with exit code $LASTEXITCODE."
    }
}

function Test-DockerImage {
    param([Parameter(Mandatory = $true)][string]$Image)
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & docker image inspect $Image *> $null
    $available = $LASTEXITCODE -eq 0
    $ErrorActionPreference = $previousPreference
    return $available
}

function Initialize-PythonEnvironment {
    $python = Join-Path $repoRoot ".venv\Scripts\python.exe"
    $createdEnvironment = $false
    if (-not (Test-Path -LiteralPath $python)) {
        Write-Host "[EasyDep] Creating the Python virtual environment."
        if (Test-CommandAvailable "py") {
            & py -3 -m venv (Join-Path $repoRoot ".venv")
        }
        elseif (Test-CommandAvailable "python") {
            & python -m venv (Join-Path $repoRoot ".venv")
        }
        else {
            throw "Python 3.11 or newer is required."
        }
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) {
            throw "Python virtual environment creation failed."
        }
        $createdEnvironment = $true
    }

    & $python -c "import sys; raise SystemExit(sys.version_info < (3, 11))"
    if ($LASTEXITCODE -ne 0) {
        throw "EasyDep requires Python 3.11 or newer."
    }

    $requirementsPath = Join-Path $repoRoot "requirements.txt"
    $requirementsFiles = @(
        Get-Item -LiteralPath $requirementsPath
        Get-Item -LiteralPath (Join-Path $repoRoot "requirements-common.txt")
        Get-Item -LiteralPath (Join-Path $repoRoot "requirements-bert.txt")
    )
    $requirementsHash = Get-CombinedFileHash -Files $requirementsFiles
    $uv = Join-Path $repoRoot ".venv\Scripts\uv.exe"
    if (-not (Test-Path -LiteralPath $uv)) {
        Write-Host "[EasyDep] Installing the uv package installer."
        & $python -X utf8 -m pip install --disable-pip-version-check "uv==0.8.22"
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $uv)) {
            throw "uv installation failed."
        }
    }
    if ($createdEnvironment -or $requirementsHash -ne (Read-HashRecord -Path $requirementsHashPath)) {
        Write-Host "[EasyDep] Synchronizing Python packages with uv."
        & $uv pip install `
            --python $python `
            --index-strategy unsafe-best-match `
            --requirements $requirementsPath
        if ($LASTEXITCODE -ne 0) {
            throw "Python package installation failed."
        }
        Set-Content -LiteralPath $requirementsHashPath -Value $requirementsHash -Encoding UTF8
    }
    else {
        Write-Host "[EasyDep] Python dependencies are unchanged."
    }
}

function Initialize-FrontendEnvironment {
    $node = Get-Command "node" -ErrorAction SilentlyContinue
    $npm = Get-Command "npm.cmd" -ErrorAction SilentlyContinue
    if ($null -eq $npm) {
        $npm = Get-Command "npm" -ErrorAction SilentlyContinue
    }
    if ($null -eq $node -or $null -eq $npm) {
        throw (
            "Fast frontend development requires Node.js 22 and npm. " +
            "Install Node.js or use -ProductionLike."
        )
    }
    & $node.Source -e "process.exit(Number(process.versions.node.split('.')[0]) < 22 ? 1 : 0)"
    if ($LASTEXITCODE -ne 0) {
        throw "Fast frontend development requires Node.js 22 or newer."
    }

    $dependencyHash = Get-FrontendDependenciesHash
    $viteEntry = Join-Path $frontendRoot "node_modules\vite\bin\vite.js"
    if (
        -not (Test-Path -LiteralPath $viteEntry) -or
        $dependencyHash -ne (Read-HashRecord -Path $frontendDependenciesHashPath)
    ) {
        Write-Host "[EasyDep] Installing frontend packages from package-lock.json."
        & $npm.Source ci --no-audit --no-fund --prefix $frontendRoot
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend package installation failed."
        }
        Set-Content `
            -LiteralPath $frontendDependenciesHashPath `
            -Value $dependencyHash `
            -Encoding UTF8
    }
    else {
        Write-Host "[EasyDep] Frontend dependencies are unchanged."
    }
}

function Initialize-Toolchain {
    $toolchainHash = Get-ToolchainBuildHash
    $imageExists = Test-DockerImage -Image $toolchainImage
    $recordedHash = Read-HashRecord -Path $toolchainHashPath
    if (
        $ForceToolchainBuild -or
        -not $imageExists -or
        $toolchainHash -ne $recordedHash
    ) {
        Write-Host "[EasyDep] Refreshing the shared implementation and Testing toolchain."
        Invoke-Docker -Arguments @(
            "build", "--file", $toolchainDockerfile,
            "--target", "toolchain", "-t", $toolchainImage, $repoRoot
        )
        Invoke-Docker -Arguments @(
            "run", "--rm", "--entrypoint", "sh", $toolchainImage,
            "./scripts/bootstrap-implementation-tools.sh"
        )
        Invoke-Docker -Arguments @(
            "run", "--rm", "--entrypoint", "sh", $toolchainImage,
            "./scripts/bootstrap-testing-tools.sh"
        )
        Write-Host "[EasyDep] Keeping Docker build cache within $toolchainBuildCacheLimit."
        Invoke-Docker -Arguments @(
            "builder", "prune", "--force",
            "--max-used-space", $toolchainBuildCacheLimit
        )
        Set-Content -LiteralPath $toolchainHashPath -Value $toolchainHash -Encoding UTF8
    }
    else {
        Write-Host "[EasyDep] Toolchain inputs are unchanged; reusing $toolchainImage."
    }
}

function Repair-ToolchainCacheOwnership {
    # 예전 툴체인 컨테이너가 root로 만든 named volume을 새 appuser 컨테이너가 이어 쓰면
    # Gradle의 libnative-platform.so를 읽고 갱신하지 못한다. 서버를 시작할 때 쓰기 권한만
    # 가볍게 확인하고, 문제가 있는 기존 volume에 한해서 한 번 소유권을 바로잡는다.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    & docker run --rm --entrypoint sh `
        -v "$memberGradleCacheVolume`:/tmp/easydep-gradle-cache" `
        $toolchainImage `
        -c "test -w /tmp/easydep-gradle-cache && { test ! -d /tmp/easydep-gradle-cache/native || test -w /tmp/easydep-gradle-cache/native; }" `
        *> $null
    $needsRepair = $LASTEXITCODE -ne 0
    $ErrorActionPreference = $previousPreference
    if (-not $needsRepair) {
        return
    }

    Write-Host "[EasyDep] Repairing ownership of the shared Gradle cache volume."
    Invoke-Docker -Arguments @(
        "run", "--rm", "--user", "root", "--entrypoint", "chown",
        "-v", "$memberGradleCacheVolume`:/tmp/easydep-gradle-cache",
        $toolchainImage,
        "-R", "1000:1000", "/tmp/easydep-gradle-cache"
    )
}

function Build-Frontend {
    $npm = Get-Command "npm.cmd" -ErrorAction SilentlyContinue
    if ($null -eq $npm) {
        $npm = Get-Command "npm" -ErrorAction SilentlyContinue
    }
    if ($null -eq $npm) {
        throw "Production-like frontend build requires Node.js 22 and npm."
    }
    & $npm.Source run build --prefix $frontendRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend build failed."
    }
}

function Wait-ForDatabase {
    $deadline = [datetime]::UtcNow.AddSeconds(180)
    do {
        $health = & docker inspect --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}" $databaseContainer 2>$null
        if ($LASTEXITCODE -eq 0 -and ($health -eq "healthy" -or $health -eq "running")) {
            & docker exec -e "MYSQL_PWD=$databasePassword" $databaseContainer mysqladmin ping -h 127.0.0.1 -uroot --silent 2>$null | Out-Null
            if ($LASTEXITCODE -eq 0) {
                return
            }
        }
        Start-Sleep -Seconds 2
    } while ([datetime]::UtcNow -lt $deadline)
    throw "MySQL was not ready within 180 seconds. Run: docker logs $databaseContainer"
}

function Get-ToolchainBuildHash {
    # 로컬 runner target이 실제로 사용하는 파일만 추적한다. API·프런트엔드·BERT 변경은
    # host 개발 서버에 즉시 반영되며 도구 이미지를 다시 만들 이유가 없다.
    $files = @()
    foreach ($relativePath in @(
        ".dockerignore",
        "docker/Dockerfile.toolchain",
        "requirements-common.txt",
        "requirements-browser-testing.txt",
        "scripts/bootstrap-implementation-tools.sh",
        "scripts/bootstrap-testing-tools.sh",
        "toolchain/opentofu/providers.tf",
        "toolchain/opentofu/tofurc"
    )) {
        $path = Join-Path $repoRoot $relativePath
        if (Test-Path -LiteralPath $path) {
            $files += Get-Item -LiteralPath $path
        }
    }
    return Get-CombinedFileHash -Files $files
}

function Read-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return ""
    }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*)\s*$") {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return ""
}

function Get-DatabaseHostPort {
    # ``docker port``는 컨테이너 상태나 Docker CLI 버전에 따라 빈 결과를 돌려줄 수 있다.
    # 생성 시 저장된 HostConfig를 읽으면 중지된 컨테이너도 같은 방식으로 확인할 수 있다.
    $raw = & docker inspect $databaseContainer 2>$null
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace(($raw -join ""))) {
        return $null
    }
    try {
        $details = @($raw | ConvertFrom-Json)[0]
        $bindings = $details.HostConfig.PortBindings.'3306/tcp'
    }
    catch {
        return $null
    }
    if ($null -eq $bindings -or $bindings.Count -eq 0 -or -not $bindings[0].HostPort) {
        return $null
    }
    return [int]$bindings[0].HostPort
}

function Test-HttpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [ValidateRange(1, 120)][int]$TimeoutSec = 5
    )
    try {
        $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec $TimeoutSec
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    }
    catch {
        return $false
    }
}

if ($Stop) {
    Stop-OwnedServer
    if (Test-CommandAvailable "docker") {
        $existing = & docker ps -a --filter "name=^/$databaseContainer$" --format "{{.Names}}" 2>$null
        if ($existing -eq $databaseContainer) {
            Write-Host "[EasyDep] Stopping MySQL. Its data volume is retained."
            Invoke-Docker -Arguments @("stop", $databaseContainer)
        }
    }
    Write-Host "[EasyDep] Stopped."
    exit 0
}

if (-not (Test-CommandAvailable "docker")) {
    throw "Docker Desktop and the docker CLI are required."
}
$prevEAP = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& docker info *> $null
$ErrorActionPreference = $prevEAP
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is not running."
}

if (-not $ProductionLike -and ($SkipFrontendBuild -or $ForceFrontendBuild)) {
    throw "-SkipFrontendBuild and -ForceFrontendBuild require -ProductionLike."
}
if ($ProductionLike -and $BackendReload) {
    throw "-BackendReload cannot be combined with -ProductionLike."
}
if (-not $ProductionLike -and $Port -eq $FrontendPort) {
    throw "Backend and frontend development ports must be different."
}

# 예약된 8000번처럼 열 수 없는 포트는 이미지와 BERT를 준비하기 전에 바로 알려준다.
Stop-OwnedServer
Assert-LoopbackPortAvailable -CandidatePort $Port -Purpose "Backend"
if (-not $ProductionLike) {
    Assert-LoopbackPortAvailable -CandidatePort $FrontendPort -Purpose "Frontend"
}
# Start-Process로 띄운 서버가 실행한 PowerShell의 키 입력이나 Ctrl+C를 가져가지 않도록
# 각 자식 프로세스에 전용 빈 표준입력 파일을 연결한다. 기존 프로세스가 잡고 있던 파일을
# 먼저 닫을 수 있도록 Stop-OwnedServer보다 뒤에서 새로 만든다.
Set-Content -LiteralPath $backendStdinPath -Value "" -NoNewline -Encoding ASCII
Set-Content -LiteralPath $frontendStdinPath -Value "" -NoNewline -Encoding ASCII

if ($ResetDatabase) {
    # 개발 DB 구조가 현재 코드와 맞지 않을 때만 사용한다. 컨테이너와 그 전용 volume을
    # 함께 지우므로, 기존 앱과 checkpoint도 모두 삭제된다는 사실을 출력해 둔다.
    Stop-OwnedServer
    $existingDatabase = & docker ps -a --filter "name=^/$databaseContainer$" --format "{{.Names}}"
    if ($existingDatabase -eq $databaseContainer) {
        Write-Host "[EasyDep] Removing the development MySQL container and all saved app data." -ForegroundColor Yellow
        Invoke-Docker -Arguments @("rm", "-f", $databaseContainer)
    }
    $existingVolume = & docker volume ls --filter "name=^$databaseVolume$" --format "{{.Name}}"
    if ($existingVolume -eq $databaseVolume) {
        Invoke-Docker -Arguments @("volume", "rm", $databaseVolume)
    }
}

if (-not (Test-Path -LiteralPath $environmentPath)) {
    Copy-Item -LiteralPath $environmentExamplePath -Destination $environmentPath
    Write-Host (
        "[EasyDep] Created .env from .env.example. " +
        "Set LLM_PROVIDER, API_KEY, BASE_URL and MODEL before running an LLM stage."
    ) -ForegroundColor Yellow
}
$configuredToolchainImage = Read-DotEnvValue -Path $environmentPath -Name "EASYDEP_TOOLCHAIN_IMAGE"
if (-not [string]::IsNullOrWhiteSpace($configuredToolchainImage)) {
    $toolchainImage = $configuredToolchainImage
}
if ($SkipBootstrap) {
    Write-Host "[EasyDep] Skipping dependency and toolchain bootstrap by explicit request."
    if (-not (Test-DockerImage -Image $toolchainImage)) {
        throw "-SkipBootstrap requires the existing Docker image: $toolchainImage"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $frontendRoot "node_modules\vite\bin\vite.js"))) {
        throw "-SkipBootstrap requires existing frontend node_modules."
    }
}
else {
    Initialize-PythonEnvironment
    Initialize-FrontendEnvironment
    Initialize-Toolchain
}
Repair-ToolchainCacheOwnership

if ($ProductionLike) {
    if ($SkipFrontendBuild) {
        if (-not (Test-Path -LiteralPath (Join-Path $frontendRoot "build\index.html"))) {
            throw "-SkipFrontendBuild requires an existing frontend/build/index.html."
        }
        Write-Host "[EasyDep] Reusing the frontend build by explicit request."
    }
    else {
        $buildHash = Get-FrontendBuildHash
        $builtHash = Read-HashRecord -Path $frontendBuildHashPath
        $buildIndex = Join-Path $frontendRoot "build\index.html"
        if (-not $ForceFrontendBuild -and (Test-Path -LiteralPath $buildIndex) -and $buildHash -eq $builtHash) {
            Write-Host "[EasyDep] Frontend inputs are unchanged; reusing the existing build."
        }
        else {
            Write-Host "[EasyDep] Building the production-like SvelteKit frontend."
            Build-Frontend
            if (-not (Test-Path -LiteralPath $buildIndex)) {
                throw "Frontend build did not produce frontend/build/index.html."
            }
            Set-Content -LiteralPath $frontendBuildHashPath -Value $buildHash -Encoding UTF8
        }
    }
}

$existingDatabase = & docker ps -a --filter "name=^/$databaseContainer$" --format "{{.Names}}"
if ($existingDatabase -eq $databaseContainer) {
    $currentDatabasePort = Get-DatabaseHostPort
    if ($null -eq $currentDatabasePort) {
        throw (
            "The existing MySQL container port could not be read. " +
            "The container was left unchanged: $databaseContainer"
        )
    }
    if ($currentDatabasePort -ne $DatabasePort) {
        throw (
            "The existing MySQL container uses host port $currentDatabasePort, " +
            "not $DatabasePort. The container was left unchanged."
        )
    }
}

if ($existingDatabase -eq $databaseContainer) {
    $runningDatabase = & docker ps --filter "name=^/$databaseContainer$" --format "{{.Names}}"
    if ($runningDatabase -ne $databaseContainer) {
        Write-Host "[EasyDep] Restarting the existing MySQL container."
        Invoke-Docker -Arguments @("start", $databaseContainer)
    }
}
else {
    Write-Host "[EasyDep] Creating the development MySQL container."
    Invoke-Docker -Arguments @(
        "run", "-d",
        "--name", $databaseContainer,
        "--restart", "unless-stopped",
        "-e", "MYSQL_ROOT_PASSWORD=$databasePassword",
        "-e", "MYSQL_DATABASE=easydep",
        "-p", "127.0.0.1:$DatabasePort`:3306",
        "-v", "$databaseVolume`:/var/lib/mysql",
        "--health-cmd", "mysqladmin ping -h 127.0.0.1 -uroot -p$databasePassword --silent",
        "--health-interval", "5s",
        "--health-timeout", "5s",
        "--health-retries", "24",
        $DatabaseImage
    ) | Out-Null
}
Write-Host "[EasyDep] Waiting for MySQL."
Wait-ForDatabase

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw ".venv is missing. Create it and install requirements.txt first."
}

$env:DB_HOST = "127.0.0.1"
$env:DB_PORT = [string]$DatabasePort
$env:DB_USER = "root"
$env:DB_PASSWORD = $databasePassword
$env:DB_NAME = "easydep"
$env:DB_SCHEMA_RESET_ON_START = if ($ResetDatabaseSchema) { "true" } else { "false" }

Write-Host "[EasyDep] Starting the FastAPI backend. Logs: $runRoot"
$backendArguments = @(
    "-X", "utf8", "-m", "uvicorn", "server:app",
    "--host", "127.0.0.1", "--port", [string]$Port
)
if ($BackendReload) {
    $backendArguments += "--reload"
}
$server = Start-Process `
    -FilePath $python `
    -ArgumentList $backendArguments `
    -WorkingDirectory $repoRoot `
    -WindowStyle Hidden `
    -RedirectStandardInput $backendStdinPath `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

$serverRecord = @{
    pid = $server.Id
    startedAt = $server.StartTime.ToUniversalTime().ToString("O")
    repoRoot = $repoRoot
    port = $Port
    mode = if ($ProductionLike) { "production-like" } else { "development" }
    backendReload = [bool]$BackendReload
}
$serverRecord | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding UTF8

$healthUri = "http://127.0.0.1:$Port/api/health"
$deadline = [datetime]::UtcNow.AddSeconds(600)
do {
    $server.Refresh()
    if ($server.HasExited) {
        $tail = if (Test-Path -LiteralPath $stderrPath) {
            (Get-Content -LiteralPath $stderrPath -Tail 80 -Encoding UTF8) -join [Environment]::NewLine
        } else {
            "No backend error log is available."
        }
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        throw "The backend exited before becoming ready.`n$tail"
    }
    if (Test-HttpEndpoint $healthUri) {
        break
    }
    Start-Sleep -Seconds 2
} while ([datetime]::UtcNow -lt $deadline)

if (-not (Test-HttpEndpoint $healthUri)) {
    Stop-OwnedServer
    throw "The backend was not ready within 600 seconds. See $stderrPath"
}

$workspaceUri = "http://127.0.0.1:$Port/"
if (-not $ProductionLike) {
    $node = Get-Command "node" -ErrorAction Stop
    $viteEntry = Join-Path $frontendRoot "node_modules\vite\bin\vite.js"
    $env:EASYDEP_API_ORIGIN = "http://127.0.0.1:$Port"
    Write-Host "[EasyDep] Starting the Vite frontend with hot reload."
    $frontend = Start-Process `
        -FilePath $node.Source `
        -ArgumentList @(
            $viteEntry, "--host", "127.0.0.1", "--port", [string]$FrontendPort, "--strictPort"
        ) `
        -WorkingDirectory $frontendRoot `
        -WindowStyle Hidden `
        -RedirectStandardInput $frontendStdinPath `
        -RedirectStandardOutput $frontendStdoutPath `
        -RedirectStandardError $frontendStderrPath `
        -PassThru
    $serverRecord["frontendPid"] = $frontend.Id
    $serverRecord["frontendStartedAt"] = $frontend.StartTime.ToUniversalTime().ToString("O")
    $serverRecord["frontendPort"] = $FrontendPort
    $serverRecord | ConvertTo-Json | Set-Content -LiteralPath $pidPath -Encoding UTF8

    $frontendUri = "http://127.0.0.1:$FrontendPort/"
    $frontendDeadline = [datetime]::UtcNow.AddSeconds(120)
    do {
        $frontend.Refresh()
        if ($frontend.HasExited) {
            $tail = if (Test-Path -LiteralPath $frontendStderrPath) {
                (Get-Content -LiteralPath $frontendStderrPath -Tail 80 -Encoding UTF8) -join [Environment]::NewLine
            } else {
                "No frontend error log is available."
            }
            Stop-OwnedServer
            throw "The frontend exited before becoming ready.`n$tail"
        }
        if (Test-HttpEndpoint $frontendUri) {
            break
        }
        Start-Sleep -Seconds 1
    } while ([datetime]::UtcNow -lt $frontendDeadline)
    if (-not (Test-HttpEndpoint $frontendUri)) {
        Stop-OwnedServer
        throw "The frontend was not ready within 120 seconds. See $frontendStderrPath"
    }
    $workspaceUri = $frontendUri
}

$checks = @(
    "http://127.0.0.1:$Port/api/workspace/apps?limit=1"
)
if ($ProductionLike) {
    $checks += @(
        "http://127.0.0.1:$Port/",
        "http://127.0.0.1:$Port/workspace/"
    )
}
else {
    $checks += $workspaceUri
}
foreach ($uri in $checks) {
    $timeoutSec = if ($uri -eq "http://127.0.0.1:$Port/api/workspace/apps?limit=1") {
        30
    } else {
        5
    }
    if (-not (Test-HttpEndpoint -Uri $uri -TimeoutSec $timeoutSec)) {
        Stop-OwnedServer
        throw "End-to-end integration check failed: $uri"
    }
}

Write-Host ""
Write-Host "[EasyDep] Ready" -ForegroundColor Green
Write-Host "  UI:   $workspaceUri"
Write-Host "  API:  http://127.0.0.1:$Port/docs"
$modeLabel = if ($ProductionLike) {
    "production-like"
}
elseif ($BackendReload) {
    "development (frontend and backend hot reload)"
}
else {
    "development (frontend hot reload; stable backend)"
}
Write-Host "  Mode: $modeLabel"
Write-Host "  Logs: $runRoot"
Write-Host "  Stop: powershell -ExecutionPolicy Bypass -File scripts\run-easydep.ps1 -Stop"

if ($OpenBrowser) {
    Start-Process $workspaceUri
}
