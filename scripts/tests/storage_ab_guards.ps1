$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-AbTest {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "TEST FAILED: $Message" }
}

$scripts = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$corePath = Join-Path $scripts 'storage_ab_core.psm1'
$nativePath = Join-Path $scripts 'storage_ab_native.psm1'
$entryPath = Join-Path $scripts 'storage_ab.ps1'
foreach ($path in @($corePath, $nativePath, $entryPath)) {
    $tokens = $null
    $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    Assert-AbTest ($errors.Count -eq 0) "PowerShell parse errors in $path"
    $commands = @($ast.FindAll({
        param($node)
        $node -is [Management.Automation.Language.CommandAst]
    }, $true))
    foreach ($command in $commands) {
        $name = $command.GetCommandName()
        Assert-AbTest ($name -notin @(
            'Clear-Disk', 'Remove-Partition', 'Remove-Item', 'Set-Disk',
            'Set-MpPreference', 'Add-MpPreference', 'Remove-MpPreference',
            'diskpart', 'format', 'fsutil', 'Invoke-Expression'
        )) "Forbidden native command $name"
        if ($path -eq $corePath) {
            Assert-AbTest ($name -notin @(
                'Get-Disk', 'Get-Partition', 'Get-Volume', 'Initialize-Disk',
                'New-Partition', 'Format-Volume', 'New-Item', 'Move-Item',
                'Copy-Item', 'robocopy', 'Get-MpComputerStatus', 'Get-MpPreference'
            )) 'The mockable core must not contain native operations.'
        }
        if ($name -eq 'Format-Volume') {
            $parameters = @($command.CommandElements |
                Where-Object { $_ -is [Management.Automation.Language.CommandParameterAst] } |
                ForEach-Object ParameterName)
            Assert-AbTest ('Partition' -in $parameters) 'Format must target a partition object.'
            Assert-AbTest ('DevDrive' -in $parameters) 'Format must explicitly create a Dev Drive.'
            Assert-AbTest ('Force' -notin $parameters -and 'Full' -notin $parameters) 'Use default format settings.'
            Assert-AbTest ('DriveLetter' -notin $parameters) 'Never format by a drive-letter lookup.'
        }
        if ($name -eq 'New-Partition') {
            $parameters = @($command.CommandElements |
                Where-Object { $_ -is [Management.Automation.Language.CommandParameterAst] } |
                ForEach-Object ParameterName)
            Assert-AbTest ('AssignDriveLetter' -in $parameters -and 'DriveLetter' -notin $parameters) 'Discover an assigned free drive letter.'
        }
    }
    if ($path -eq $nativePath) {
        $modeReads = @($commands | Where-Object {
            $_.GetCommandName() -eq 'Get-AbNativeProperty' -and
            $_.CommandElements.Count -eq 3 -and
            $_.CommandElements[2].Extent.Text -eq "'PerformanceModeStatus'"
        })
        Assert-AbTest ($modeReads.Count -eq 1 -and
            $modeReads[0].CommandElements[1].Extent.Text -eq '$preferences') 'PerformanceModeStatus belongs to preferences, not computer status.'
        $preferenceReads = @($commands | Where-Object { $_.GetCommandName() -eq 'Get-MpPreference' })
        Assert-AbTest ($preferenceReads.Count -eq 1) 'Read the actual performance-mode preference without changing it.'
    }
}

$workflow = Get-Content -LiteralPath (Join-Path $scripts '..\.github\workflows\storage-ab.yml') -Raw
$storageStep = [regex]::Match($workflow,
    '(?ms)^      - name: Apply guarded experimental storage layout\r?\n.*?(?=^      -|\z)').Value
Assert-AbTest ($storageStep.Contains('working-directory: ${{ runner.temp }}')) 'Start the storage shell outside the checkout so inherited directory handles cannot lock its rename.'

# Only the pure core is imported. Native adapters above are parsed, never run.
Import-Module $corePath -Force

function New-AbFake {
    $state = @{
        calls = [System.Collections.Generic.List[string]]::new()
        context = @{
            github_actions = 'true'; runner_environment = 'github-hosted'
            runner_os = 'Windows'; runner_arch = 'ARM64'
            os_arch = 'Arm64'; process_arch = 'Arm64'; is_windows = $true
            repository = 'lei9444/fastapi'; run_id = '123'; run_attempt = '1'
            commit = ('a' * 40); image_os = 'win11-arm'; image_version = 'fixture'
        }
        python = @{ version = '3.13.17'; platform = 'win-arm64'; pointer_bits = 64 }
        workspace = @{
            logical = 'C:\work\repo'; physical = 'C:\work\repo'
            is_directory = $true; is_reparse = $false; safe = $true; has_venv = $false
            path_info = @{ logical = 'C:\work\repo'; physical = 'C:\work\repo'; filesystem = 'NTFS' }
        }
        inventory = @{
            complete = $true
            disks = @(
                @{
                    Number = 0; BusType = 'NVMe'; PartitionStyle = 'GPT'
                    IsBoot = $true; IsSystem = $true; IsOffline = $false; IsReadOnly = $false
                    Size = [uint64](100GB); NumberOfPartitions = 3
                    Identity = 'private-system-identity'; Native = 'never-native'
                },
                @{
                    Number = 1; BusType = 'NVMe'; PartitionStyle = 'RAW'
                    IsBoot = $false; IsSystem = $false; IsOffline = $false; IsReadOnly = $false
                    Size = [uint64](100GB); NumberOfPartitions = 0
                    Identity = 'private-scratch-identity'; Native = 'never-native'
                }
            )
            partition_disk_numbers = @(0); used_letters = @('C')
        }
        inventory_calls = 0
        protection_calls = 0
        change_on_recheck = $false
        fail = ''
        drive_letter = 'Q'
        filesystem = 'ReFS'
        is_dev_drive = $true
        original = @('hidden:secret.txt', 'readonly:code.py', 'empty:directory', 'symlink:../target')
        copied = $null
        backup = $null
        logical_kind = 'directory'
    }
    $operations = @{
        Context = { $state.calls.Add('context'); return $state.context }.GetNewClosure()
        Python = { $state.calls.Add('python'); return $state.python }.GetNewClosure()
        Workspace = { param($c, $l, $t) $state.calls.Add('workspace'); return $state.workspace }.GetNewClosure()
        Inventory = {
            $state.calls.Add('inventory')
            $state.inventory_calls++
            $copy = $state.inventory | ConvertTo-Json -Depth 10 | ConvertFrom-Json -AsHashtable
            if ($state.change_on_recheck -and $state.inventory_calls -gt 1) {
                $copy.disks[1].PartitionStyle = 'GPT'
                $copy.disks[1].NumberOfPartitions = 1
            }
            return $copy
        }.GetNewClosure()
        Protection = {
            $state.calls.Add('protection')
            $state.protection_calls++
            return @{
                available = $true
                realtime_protection_enabled = -not ($state.fail -eq 'protection' -and $state.protection_calls -gt 1)
                performance_mode_status = 1; performance_mode = 'Disabled'
            }
        }.GetNewClosure()
        Initialize = { param($d) $state.calls.Add('initialize'); if ($state.fail -eq 'initialize') { throw 'initialize failed' } }.GetNewClosure()
        Partition = {
            param($d)
            $state.calls.Add('partition')
            return @{ DiskNumber = $d.Number; PartitionNumber = 1; DriveLetter = $state.drive_letter }
        }.GetNewClosure()
        Format = {
            param($d, $p)
            $state.calls.Add('format')
            if ($state.fail -eq 'format') { throw 'format failed' }
            return @{ root = "$($state.drive_letter):\"; filesystem = $state.filesystem; is_dev_drive = $state.is_dev_drive }
        }.GetNewClosure()
        Plan = {
            param($c, $w, $v, $l, $t)
            $state.calls.Add('plan')
            $physical = if ($l -eq 'default') { 'C:\work\repo' } else { "$($state.drive_letter):\storage-ab\workspace" }
            return @{
                workspace_logical = 'C:\work\repo'; workspace_physical = $physical
                backup = $(if ($l -eq 'default') { $null } else { 'C:\work\repo.backup' })
            }
        }.GetNewClosure()
        Copy = {
            param($s, $t)
            $state.calls.Add('copy')
            if ($state.fail -eq 'copy') { throw 'copy failed' }
            $state.copied = @($state.original)
            return @{ verified = $true; files = 2; directories = 1; links = 1; bytes = 100; source_digest = 'same'; target_digest = 'same' }
        }.GetNewClosure()
        Rename = {
            param($s, $b)
            $state.calls.Add('rename')
            $state.backup = @($state.original)
            $state.original = $null
            $state.logical_kind = 'absent'
            if ($state.fail -eq 'rename-after') { throw 'rename failed after preserving backup' }
        }.GetNewClosure()
        Junction = {
            param($s, $t)
            $state.calls.Add('junction')
            if ($state.fail -in @('junction', 'restore')) { throw 'junction failed' }
            $state.logical_kind = 'junction'
        }.GetNewClosure()
        Restore = {
            param($p)
            $state.calls.Add('restore')
            if ($state.fail -eq 'restore') { throw 'restore copy failed' }
            if ($null -eq $state.backup) { return @{ status = 'not_needed'; backup_retained = $false } }
            $state.original = @($state.backup)
            $state.logical_kind = 'directory'
            return @{ status = 'restored'; backup_retained = $true }
        }.GetNewClosure()
        Configure = {
            param($p, $l)
            $state.calls.Add('configure')
            if ($state.fail -eq 'configure') { throw 'configure failed' }
            return @{
                paths = @{ workspace = @{ physical = $p.workspace_physical } }
                initial_cache_state = @{
                    uv_cache = @{ fresh = $true; entries = 0 }
                    pip_cache = @{ fresh = $true; entries = 0 }
                    temp = @{ fresh = $true; entries = 0 }
                    wheelhouse = @{ fresh = $true; entries = 0 }
                }
                environment = @{ UV_CACHE_DIR = 'physical-uv'; PIP_CACHE_DIR = 'physical-pip'; TEMP = 'physical-temp'; TMP = 'physical-temp' }
            }
        }.GetNewClosure()
        SafeError = { param($e) return [string]$e.Exception.Message }
    }
    return @{ state = $state; operations = $operations }
}

$tests = 0
foreach ($change in @(
    @('github_actions', 'false'), @('runner_environment', 'self-hosted'),
    @('runner_os', 'Linux'), @('runner_arch', 'X64'), @('os_arch', 'X64'),
    @('process_arch', 'X64'), @('repository', 'another/repo')
)) {
    $fake = New-AbFake
    $fake.state.context[$change[0]] = $change[1]
    $result = Invoke-AbStorage -Layout refs-devdrive -Trial 1 -Operations $fake.operations
    Assert-AbTest (-not $result.comparable -and $fake.state.calls.Count -eq 1) "Context guard $($change[0])"
    $tests++
}

foreach ($change in @(
    @('IsBoot', $true), @('IsSystem', $true), @('IsOffline', $true),
    @('IsReadOnly', $true), @('NumberOfPartitions', 1),
    @('Size', [uint64](49GB)), @('PartitionStyle', 'GPT'), @('BusType', 'SATA'),
    @('IsBoot', $null), @('IsReadOnly', 'false'), @('Size', '100GB'),
    @('Identity', $null)
)) {
    $fake = New-AbFake
    $fake.state.inventory.disks[1][$change[0]] = $change[1]
    $result = Invoke-AbStorage -Layout refs-devdrive -Trial 1 -Operations $fake.operations
    Assert-AbTest (-not $result.comparable -and 'initialize' -notin $fake.state.calls) "Disk guard $($change[0])"
    $tests++
}

foreach ($case in @('zero', 'multiple', 'partition-inventory', 'recheck', 'letter', 'reparse', 'unsafe-workspace', 'venv', 'python')) {
    $fake = New-AbFake
    switch ($case) {
        'zero' { $fake.state.inventory.disks = @($fake.state.inventory.disks[0]) }
        'multiple' {
            $extra = $fake.state.inventory.disks[1].Clone()
            $extra.Number = 2; $extra.Identity = 'private-extra-identity'
            $fake.state.inventory.disks += $extra
        }
        'partition-inventory' { $fake.state.inventory.partition_disk_numbers = @(0, 1) }
        'recheck' { $fake.state.change_on_recheck = $true }
        'letter' { $fake.state.drive_letter = 'C' }
        'reparse' { $fake.state.workspace.is_reparse = $true }
        'unsafe-workspace' { $fake.state.workspace.safe = $false }
        'venv' { $fake.state.workspace.has_venv = $true }
        'python' { $fake.state.python.platform = 'win-amd64' }
    }
    $result = Invoke-AbStorage -Layout refs-devdrive -Trial 1 -Operations $fake.operations
    Assert-AbTest (-not $result.comparable -and 'format' -notin $fake.state.calls) "Guard case $case"
    if ($case -ne 'letter') { Assert-AbTest ('initialize' -notin $fake.state.calls) "No initialization for $case" }
    Assert-AbTest ($null -ne $fake.state.original) "Original preserved for $case"
    $tests++
}

$fake = New-AbFake
$result = Invoke-AbStorage -Layout default -Trial 1 -Operations $fake.operations
Assert-AbTest ($result.comparable -and $result.applied_layout -eq 'default') 'Default layout is measurable.'
foreach ($name in @('initialize', 'partition', 'format', 'copy', 'rename', 'junction')) {
    Assert-AbTest ($name -notin $fake.state.calls) "Default does not mutate storage: $name"
}
$tests++

$fake = New-AbFake
$result = Invoke-AbStorage -Layout refs-devdrive -Trial 1 -Operations $fake.operations
Assert-AbTest ($result.comparable -and $result.backup_retained) 'Successful migration retains backup.'
Assert-AbTest ($result.paths.workspace.physical -eq 'Q:\storage-ab\workspace') 'Uses discovered drive, not D.'
Assert-AbTest (($fake.state.backup -join '|') -eq ($fake.state.copied -join '|')) 'Hidden/read-only/empty/link entries preserved.'
Assert-AbTest ($fake.state.calls.IndexOf('copy') -lt $fake.state.calls.IndexOf('rename')) 'Copy before rename.'
Assert-AbTest ($fake.state.calls.IndexOf('rename') -lt $fake.state.calls.IndexOf('junction')) 'Rename before junction.'
$json = $result | ConvertTo-Json -Depth 15
Assert-AbTest ($json -notmatch 'private-scratch-identity|private-system-identity|never-native') 'No private disk identity in report.'
$tests++

foreach ($failure in @('initialize', 'format', 'copy', 'junction', 'rename-after', 'configure', 'protection', 'restore')) {
    $fake = New-AbFake
    $original = $fake.state.original -join '|'
    $fake.state.fail = $failure
    $result = Invoke-AbStorage -Layout refs-devdrive -Trial 1 -Operations $fake.operations
    Assert-AbTest (-not $result.comparable -and -not $result.applied) "Failure is explicit: $failure"
    $surviving = if ($null -ne $fake.state.backup) { $fake.state.backup } else { $fake.state.original }
    Assert-AbTest (($surviving -join '|') -eq $original) "Original data survives $failure"
    if ($failure -in @('junction', 'rename-after', 'configure', 'protection', 'restore')) {
        Assert-AbTest ($null -ne $fake.state.backup) "Backup retained for $failure"
    }
    $tests++
}

foreach ($case in @('not-refs', 'not-devdrive')) {
    $fake = New-AbFake
    if ($case -eq 'not-refs') { $fake.state.filesystem = 'NTFS' } else { $fake.state.is_dev_drive = $false }
    $result = Invoke-AbStorage -Layout refs-devdrive -Trial 1 -Operations $fake.operations
    Assert-AbTest (-not $result.comparable -and 'copy' -notin $fake.state.calls) "Validate actual volume: $case"
    $tests++
}

$source = @{ digest = 'hash'; files = 2; directories = 1; links = 1; bytes = 10 }
Assert-AbManifestEqual $source $source.Clone()
foreach ($key in @('digest', 'files', 'directories', 'links', 'bytes')) {
    $target = $source.Clone()
    $target[$key] = 'different'
    $failed = $false
    try { Assert-AbManifestEqual $source $target } catch { $failed = $true }
    Assert-AbTest $failed "Manifest mismatch $key is rejected."
    $tests++
}
Write-Host "$tests mocked storage guard cases passed; native storage adapters were not imported or executed."
