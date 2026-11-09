Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'storage_ab_core.psm1')

# Native operations are loaded only by the hosted-runner entry point.
# Local validation parses this module; it never imports or invokes this adapter.
$script:AbSettings = $null
$script:AbPrivateIds = $null

function Get-AbNativeProperty {
    param($Object, [string]$Name)
    $property = $Object.PSObject.Properties[$Name]
    if ($null -ne $property) { return $property.Value }
    return $null
}

function Get-AbNativeContext {
    return @{
        github_actions = $env:GITHUB_ACTIONS
        runner_environment = $env:STORAGE_RUNNER_ENVIRONMENT
        runner_os = $env:STORAGE_RUNNER_OS
        runner_arch = $env:STORAGE_RUNNER_ARCH
        os_arch = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        process_arch = [Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString()
        is_windows = [Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
            [Runtime.InteropServices.OSPlatform]::Windows)
        repository = $env:GITHUB_REPOSITORY
        run_id = $env:GITHUB_RUN_ID
        run_attempt = $env:GITHUB_RUN_ATTEMPT
        commit = $env:GITHUB_SHA
        image_os = $env:ImageOS
        image_version = $env:ImageVersion
        workspace = $env:GITHUB_WORKSPACE
        expected_workspace = $env:STORAGE_EXPECTED_WORKSPACE
        runner_temp = $env:RUNNER_TEMP
        tool_cache = $env:RUNNER_TOOL_CACHE
    }
}

function Invoke-AbNativePython {
    param([string]$Code, [string[]]$Arguments = @())
    $text = @(& $script:AbSettings.Python -c $Code @Arguments)
    if ($LASTEXITCODE -ne 0) { throw 'AB_GUARD: native_python_probe_failed' }
    return ($text -join "`n") | ConvertFrom-Json
}

function Get-AbNativePythonFacts {
    return Invoke-AbNativePython @'
import json, platform, struct, sysconfig
print(json.dumps({"version": platform.python_version(),
                  "platform": sysconfig.get_platform(),
                  "pointer_bits": struct.calcsize("P") * 8}))
'@
}

function Get-AbNativePath {
    param([string]$Path)
    $facts = Invoke-AbNativePython -Code @'
import json, os, sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.is_absolute():
    raise ValueError("Expected an absolute path")
physical = path.resolve(strict=False)
existing = physical
while not existing.exists():
    if existing.parent == existing:
        raise ValueError("No existing volume for path")
    existing = existing.parent
print(json.dumps({"physical": str(physical), "drive": physical.drive,
                  "exists": physical.exists(), "is_dev_drive": os.path.isdevdrive(existing)}))
'@ -Arguments @($Path)
    if ($facts.drive -notmatch '^[A-Za-z]:$') {
        throw 'AB_GUARD: path_is_not_on_a_local_drive'
    }
    $volumes = @(Get-Volume -DriveLetter $facts.drive[0] -ErrorAction Stop)
    if ($volumes.Count -ne 1 -or $volumes[0].FileSystem -isnot [string]) {
        throw 'AB_GUARD: filesystem_metadata_unavailable'
    }
    return [ordered]@{
        logical = $Path
        physical = $facts.physical
        drive = $facts.drive.ToUpperInvariant()
        filesystem = $volumes[0].FileSystem
        is_dev_drive = $facts.is_dev_drive
        exists = $facts.exists
    }
}

function Get-AbNativeEntry {
    param([string]$Path)
    $parent = [IO.Path]::GetDirectoryName($Path.TrimEnd('\'))
    $name = [IO.Path]::GetFileName($Path.TrimEnd('\'))
    try {
        $entries = @(Get-ChildItem -LiteralPath $parent -Force -ErrorAction Stop)
    } catch [System.Management.Automation.ItemNotFoundException] {
        return $null
    }
    $matches = @($entries | Where-Object { $_.Name -ieq $name })
    if ($matches.Count -gt 1) { throw 'AB_GUARD: ambiguous_directory_entry' }
    if ($matches.Count -eq 1) { return $matches[0] }
    return $null
}

function Assert-AbNativeAbsent {
    param([string]$Path)
    if ($null -ne (Get-AbNativeEntry $Path)) {
        throw 'AB_GUARD: destination_already_exists'
    }
}

function Test-AbNativeAncestor {
    param([string]$Parent, [string]$Child)
    $left = $Parent.TrimEnd('\')
    return $Child.TrimEnd('\').Equals($left, [StringComparison]::OrdinalIgnoreCase) -or
        $Child.StartsWith($left + '\', [StringComparison]::OrdinalIgnoreCase)
}

function Get-AbNativeWorkspace {
    param($Context, [string]$Layout, [int]$Trial)
    if ([string]::IsNullOrWhiteSpace($Context.workspace) -or
        [string]::IsNullOrWhiteSpace($Context.expected_workspace) -or
        -not [IO.Path]::IsPathFullyQualified($Context.workspace) -or
        -not [IO.Path]::GetFullPath($Context.workspace).Equals(
            [IO.Path]::GetFullPath($Context.expected_workspace),
            [StringComparison]::OrdinalIgnoreCase)) {
        throw 'AB_GUARD: unexpected_workspace'
    }
    $item = Get-Item -LiteralPath $Context.workspace -Force -ErrorAction Stop
    $reparse = [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
    if (-not $item.PSIsContainer -or $reparse) {
        return @{ is_directory = [bool]$item.PSIsContainer; is_reparse = $reparse; safe = $false; has_venv = $false }
    }
    $path = Get-AbNativePath $item.FullName
    if ($path.physical.TrimEnd('\') -ieq [IO.Path]::GetPathRoot($path.physical).TrimEnd('\') -or
        -not (Test-Path -LiteralPath (Join-Path $item.FullName '.git'))) {
        throw 'AB_GUARD: workspace_is_not_a_checkout_directory'
    }
    foreach ($protected in @($Context.runner_temp, $Context.tool_cache)) {
        if ([string]::IsNullOrWhiteSpace($protected)) { throw 'AB_GUARD: protected_path_unknown' }
        $resolved = Get-AbNativePath $protected
        if (Test-AbNativeAncestor $path.physical $resolved.physical) {
            throw 'AB_GUARD: workspace_contains_a_protected_runner_path'
        }
    }
    $backup = "$($item.FullName).storage-ab-backup-$($Context.run_id)-$($Context.run_attempt)-$Trial"
    if ($Layout -eq 'refs-devdrive') { Assert-AbNativeAbsent $backup }
    return @{
        logical = $item.FullName
        physical = $path.physical
        path_info = $path
        backup = $backup
        is_directory = $true
        is_reparse = $false
        safe = $true
        has_venv = [bool](Test-Path -LiteralPath (Join-Path $path.physical '.venv'))
    }
}

function ConvertTo-AbNativeDisk {
    param($Disk)
    $record = @{ Native = $Disk }
    foreach ($name in @(
        'Number', 'Size', 'NumberOfPartitions', 'IsBoot', 'IsSystem', 'IsOffline', 'IsReadOnly'
    )) {
        $record[$name] = Get-AbNativeProperty $Disk $name
    }
    foreach ($name in @('BusType', 'PartitionStyle')) {
        $value = Get-AbNativeProperty $Disk $name
        $record[$name] = if ($null -eq $value) { $null } else { $value.ToString() }
    }
    $record.Identity = Get-AbNativeProperty $Disk 'UniqueId'
    foreach ($name in @('UniqueId', 'SerialNumber', 'Path')) {
        $value = Get-AbNativeProperty $Disk $name
        if ($value -is [string] -and $value.Length -ge 8) {
            $script:AbPrivateIds.Add($value)
        }
    }
    return $record
}

function Get-AbNativeInventory {
    $partitions = @(Get-Partition -ErrorAction Stop)
    $volumes = @(Get-Volume -ErrorAction Stop)
    $psDrives = @(Get-PSDrive -PSProvider FileSystem -ErrorAction Stop)
    $disks = @(Get-Disk -ErrorAction Stop | ForEach-Object { ConvertTo-AbNativeDisk $_ })
    $letters = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($value in @($partitions.DriveLetter) + @($volumes.DriveLetter) + @($psDrives.Name)) {
        if ([string]$value -match '^[A-Za-z]$') { [void]$letters.Add([string]$value) }
    }
    foreach ($drive in [Environment]::GetLogicalDrives()) {
        if ($drive -match '^[A-Za-z]:\\$') { [void]$letters.Add($drive.Substring(0, 1)) }
    }
    return @{
        complete = $true
        disks = $disks
        partition_disk_numbers = @($partitions | Select-Object -ExpandProperty DiskNumber -Unique)
        used_letters = @($letters)
    }
}

function Get-AbNativeProtection {
    try {
        $status = Get-MpComputerStatus -ErrorAction Stop
        $realtime = Get-AbNativeProperty $status 'RealTimeProtectionEnabled'
        $mode = Get-AbNativeProperty $status 'PerformanceModeStatus'
        if ($mode -is [Enum]) { $mode = [int]$mode }
        if ($realtime -isnot [bool] -or -not (Test-AbInteger $mode) -or $mode -notin @(0, 1)) {
            $modeValue = if ($null -ne $mode) { Get-AbNativeProperty $mode 'value__' } else { $null }
            return @{
                available = $false
                realtime_protection_enabled = $(if ($realtime -is [bool]) { $realtime } else { $null })
                performance_mode_status = $null
                performance_mode = 'Unknown'
                error = 'Required protection properties are unavailable.'
                observed = @{
                    realtime_type = $(if ($null -ne $realtime) { $realtime.GetType().FullName } else { $null })
                    mode_type = $(if ($null -ne $mode) { $mode.GetType().FullName } else { $null })
                    mode_text = $(if ($null -ne $mode) { [string]$mode } else { $null })
                    mode_value_field = $(if (Test-AbInteger $modeValue) { $modeValue } else { $null })
                }
            }
        }
        return @{
            available = $true
            realtime_protection_enabled = $realtime
            performance_mode_status = [int]$mode
            performance_mode = $(if ($mode -eq 0) { 'Enabled' } else { 'Disabled' })
        }
    } catch {
        return @{ available = $false; realtime_protection_enabled = $null; performance_mode_status = $null; performance_mode = 'Unavailable'; error = (Get-AbNativeSafeError $_) }
    }
}

function Initialize-AbNativeDisk {
    param($Disk)
    $fresh = Get-AbNativeInventory
    $matches = @($fresh.disks | Where-Object {
        @(Get-AbDiskErrors $_ @($fresh.partition_disk_numbers)).Count -eq 0
    })
    if ($matches.Count -ne 1 -or $matches[0].Number -ne $Disk.Number -or
        $matches[0].Identity -cne $Disk.Identity -or $matches[0].Size -ne $Disk.Size) {
        throw 'AB_GUARD: final_raw_disk_recheck_failed'
    }
    $null = Initialize-Disk -InputObject $matches[0].Native -PartitionStyle GPT -ErrorAction Stop
}

function New-AbNativePartition {
    param($Disk)
    $current = ConvertTo-AbNativeDisk (Get-Disk -Number $Disk.Number -ErrorAction Stop)
    Assert-AbNativeInitializedDisk $current $Disk
    $partition = $current.Native | New-Partition -UseMaximumSize -AssignDriveLetter -ErrorAction Stop
    return @{
        Native = $partition
        DiskNumber = $partition.DiskNumber
        PartitionNumber = $partition.PartitionNumber
        DriveLetter = [string]$partition.DriveLetter
        Offset = $partition.Offset
        Size = $partition.Size
    }
}

function Assert-AbNativeInitializedDisk {
    param($Current, $Expected)
    if ($Current.Identity -cne $Expected.Identity -or
        $Current.Number -ne $Expected.Number -or
        $Current.Size -ne $Expected.Size -or $Current.BusType -ne 'NVMe' -or
        $Current.PartitionStyle -ne 'GPT' -or
        -not (Test-AbInteger $Current.NumberOfPartitions)) {
        throw 'AB_GUARD: initialized_disk_identity_changed'
    }
    foreach ($name in @('IsBoot', 'IsSystem', 'IsOffline', 'IsReadOnly')) {
        if ($Current[$name] -isnot [bool] -or $Current[$name]) {
            throw 'AB_GUARD: initialized_disk_safety_property_changed'
        }
    }
}

function Format-AbNativePartition {
    param($Disk, $Partition)
    $current = ConvertTo-AbNativeDisk (Get-Disk -Number $Disk.Number -ErrorAction Stop)
    Assert-AbNativeInitializedDisk $current $Disk
    $part = Get-Partition -DiskNumber $Disk.Number -PartitionNumber $Partition.PartitionNumber -ErrorAction Stop
    $byLetter = @(Get-Partition -DriveLetter $Partition.DriveLetter -ErrorAction Stop)
    if ($part.Offset -ne $Partition.Offset -or $part.Size -ne $Partition.Size -or
        [string]$part.DriveLetter -ine $Partition.DriveLetter -or
        $byLetter.Count -ne 1 -or $byLetter[0].DiskNumber -ne $Disk.Number -or
        $byLetter[0].PartitionNumber -ne $Partition.PartitionNumber) {
        throw 'AB_GUARD: partition_identity_or_letter_changed'
    }
    $before = @($part | Get-Volume -ErrorAction Stop)
    if ($before.Count -ne 1 -or $before[0].FileSystem -isnot [string] -or
        $before[0].FileSystem -notin @('', 'RAW')) {
        throw 'AB_GUARD: new_partition_is_already_formatted_or_unknown'
    }
    $null = Format-Volume -Partition $part -DevDrive -FileSystem ReFS -Confirm:$false -ErrorAction Stop
    $root = '{0}:\' -f $Partition.DriveLetter
    $facts = Get-AbNativePath $root
    return @{
        root = $root
        filesystem = $facts.filesystem
        is_dev_drive = $facts.is_dev_drive
    }
}

function New-AbNativePlan {
    param($Context, $Workspace, $Volume, [string]$Layout, [int]$Trial)
    if ($Layout -eq 'refs-devdrive') {
        $root = Join-Path $Volume.root 'storage-ab'
        Assert-AbNativeAbsent $root
        $workspacePhysical = Join-Path $root 'workspace'
        $data = Join-Path $root 'data'
    } else {
        $workspacePhysical = $Workspace.physical
        $parent = [IO.Path]::GetDirectoryName($workspacePhysical)
        $data = Join-Path $parent ".storage-ab-$($Context.run_id)-$($Context.run_attempt)-$Trial"
        Assert-AbNativeAbsent $data
    }
    return @{
        workspace_logical = $Workspace.logical
        original_workspace_physical = $Workspace.physical
        workspace_physical = $workspacePhysical
        backup = $(if ($Layout -eq 'refs-devdrive') { $Workspace.backup } else { $null })
        data_root = $data
        uv_cache = Join-Path $data 'uv-cache'
        pip_cache = Join-Path $data 'pip-cache'
        temp = Join-Path $data 'temp'
        wheelhouse = Join-Path $data 'wheelhouse'
    }
}

function Get-AbNativeManifest {
    param([string]$Root)
    $entries = [System.Collections.Generic.List[object]]::new()
    $mask = [IO.FileAttributes]::ReadOnly -bor [IO.FileAttributes]::Hidden -bor
        [IO.FileAttributes]::System -bor [IO.FileAttributes]::Archive
    $queue = [System.Collections.Generic.Queue[string]]::new()
    $queue.Enqueue($Root)
    $files = 0L
    $directories = 0L
    $links = 0L
    $bytes = 0L
    while ($queue.Count -gt 0) {
        $directory = $queue.Dequeue()
        $current = Get-Item -LiteralPath $directory -Force -ErrorAction Stop
        if (-not $current.PSIsContainer -or
            ($current.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'AB_GUARD: directory_changed_during_inventory'
        }
        foreach ($item in Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop) {
            $relative = [IO.Path]::GetRelativePath($Root, $item.FullName)
            $record = [ordered]@{
                path = $relative
                attributes = [int]($item.Attributes -band $mask)
                kind = ''
                length = $null
                hash = $null
                target = $null
            }
            $linkType = Get-AbNativeProperty $item 'LinkType'
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                $targets = @(Get-AbNativeProperty $item 'Target')
                if ($linkType -notin @('SymbolicLink', 'Junction') -or $targets.Count -ne 1) {
                    throw 'AB_GUARD: unsupported_workspace_reparse_point'
                }
                $record.kind = $linkType
                $record.target = [string]$targets[0]
                $links++
            } elseif ($item.PSIsContainer) {
                $record.kind = 'directory'
                $directories++
                $queue.Enqueue($item.FullName)
            } else {
                if ($linkType -eq 'HardLink') {
                    throw 'AB_GUARD: hardlink_topology_cannot_be_preserved'
                }
                $record.kind = 'file'
                $record.length = $item.Length
                $record.hash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256 -ErrorAction Stop).Hash
                $bytes += $item.Length
                $files++
            }
            $entries.Add($record)
        }
    }
    $canonical = @($entries | Sort-Object { $_.path }) | ConvertTo-Json -Depth 8 -Compress
    $digest = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData(
        [Text.Encoding]::UTF8.GetBytes([string]$canonical))).ToLowerInvariant()
    return @{ digest = $digest; files = $files; directories = $directories; links = $links; bytes = $bytes }
}

function Copy-AbNativeWorkspace {
    param([string]$Source, [string]$Target, [string]$ExpectedPhysicalTarget = $Target)
    Assert-AbNativeAbsent $Target
    $sourceItem = Get-Item -LiteralPath $Source -Force -ErrorAction Stop
    if (-not $sourceItem.PSIsContainer -or
        ($sourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'AB_GUARD: copy_source_is_not_ordinary'
    }
    if ((Get-AbNativePath $Target).physical -ine $ExpectedPhysicalTarget) {
        throw 'AB_GUARD: copy_target_parent_is_not_expected'
    }
    $before = Get-AbNativeManifest $Source
    $null = [IO.Directory]::CreateDirectory($Target)
    if ((Get-AbNativePath $Target).physical -ine $ExpectedPhysicalTarget) {
        throw 'AB_GUARD: copy_target_changed'
    }
    $copyOutput = @(& robocopy $Source $Target /E /COPY:DAT /DCOPY:DAT /SL /SJ /R:0 /W:0 /NP /NFL /NDL /NJH /NJS)
    $code = $LASTEXITCODE
    if ($code -ge 8) { throw "AB_GUARD: robocopy_failed_exit_$code" }
    $global:LASTEXITCODE = 0
    $afterSource = Get-AbNativeManifest $Source
    $afterTarget = Get-AbNativeManifest $Target
    Assert-AbManifestEqual $before $afterSource
    Assert-AbManifestEqual $before $afterTarget
    return @{
        verified = $true
        source_digest = $before.digest
        target_digest = $afterTarget.digest
        files = $before.files
        directories = $before.directories
        links = $before.links
        bytes = $before.bytes
        robocopy_exit_code = $code
    }
}

function Move-AbNativeOriginal {
    param([string]$Source, [string]$Backup)
    Assert-AbNativeAbsent $Backup
    if ([IO.Path]::GetDirectoryName($Source) -ine [IO.Path]::GetDirectoryName($Backup)) {
        throw 'AB_GUARD: backup_must_be_a_sibling'
    }
    $item = Get-Item -LiteralPath $Source -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'AB_GUARD: workspace_changed_before_rename'
    }
    Set-Location -LiteralPath $env:RUNNER_TEMP
    [Environment]::CurrentDirectory = $env:RUNNER_TEMP
    [IO.Directory]::Move($Source, $Backup)
}

function New-AbNativeJunction {
    param([string]$Logical, [string]$Physical)
    Assert-AbNativeAbsent $Logical
    $null = New-Item -ItemType Junction -Path $Logical -Target $Physical -ErrorAction Stop
    $entry = Get-AbNativeEntry $Logical
    $path = Get-AbNativePath $Logical
    if ($null -eq $entry -or $entry.LinkType -ne 'Junction' -or
        -not $path.physical.Equals($Physical, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'AB_GUARD: workspace_junction_verification_failed'
    }
}

function Restore-AbNativeOriginal {
    param($Plan)
    $backup = Get-AbNativeEntry $Plan.backup
    if ($null -eq $backup) {
        return @{ status = 'not_needed'; backup_retained = $false }
    }
    if (-not $backup.PSIsContainer -or
        ($backup.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'AB_GUARD: backup_changed_no_restore_attempted'
    }
    $original = Get-AbNativeEntry $Plan.workspace_logical
    if ($null -ne $original) {
        if ($original.LinkType -ne 'Junction' -or
            (Get-AbNativePath $Plan.workspace_logical).physical -ine $Plan.workspace_physical) {
            return @{ status = 'blocked_original_path_occupied'; backup_retained = $true }
        }
        $quarantine = $Plan.backup + '.failed-junction'
        Assert-AbNativeAbsent $quarantine
        [IO.Directory]::Move($Plan.workspace_logical, $quarantine)
    }
    # Copy back rather than deleting or consuming the retained original backup.
    $null = Copy-AbNativeWorkspace $Plan.backup $Plan.workspace_logical $Plan.original_workspace_physical
    return @{ status = 'restored'; backup_retained = $true }
}

function Set-AbNativePaths {
    param($Plan, [string]$Layout)
    Assert-AbNativeAbsent $Plan.data_root
    $null = New-Item -ItemType Directory -Path $Plan.data_root -ErrorAction Stop
    $paths = [ordered]@{
        workspace = Get-AbNativePath $Plan.workspace_logical
        venv = Get-AbNativePath (Join-Path $Plan.workspace_physical '.venv')
    }
    $initial = [ordered]@{}
    foreach ($name in @('uv_cache', 'pip_cache', 'temp', 'wheelhouse')) {
        $path = $Plan[$name]
        Assert-AbNativeAbsent $path
        $null = New-Item -ItemType Directory -Path $path -ErrorAction Stop
        $count = @(Get-ChildItem -LiteralPath $path -Force -ErrorAction Stop).Count
        if ($count -ne 0) { throw 'AB_GUARD: new_cache_or_temp_directory_not_empty' }
        $initial[$name] = @{ fresh = $true; entries = $count }
        $paths[$name] = Get-AbNativePath $path
        if ($paths[$name].drive -ne $paths.workspace.drive) {
            throw 'AB_GUARD: cache_and_workspace_are_on_different_volumes'
        }
    }
    if ($paths.venv.exists) { throw 'AB_GUARD: workload_venv_already_exists' }
    if ($Layout -eq 'refs-devdrive') {
        foreach ($info in $paths.Values) {
            if ($info.filesystem -ine 'ReFS' -or -not $info.is_dev_drive) {
                throw 'AB_GUARD: treatment_path_is_not_on_verified_dev_drive'
            }
        }
    }
    return @{
        paths = $paths
        initial_cache_state = $initial
        environment = [ordered]@{
            UV_CACHE_DIR = $paths.uv_cache.physical
            PIP_CACHE_DIR = $paths.pip_cache.physical
            TEMP = $paths.temp.physical
            TMP = $paths.temp.physical
        }
    }
}

function Get-AbNativeSafeError {
    param($Failure)
    $text = [string]$Failure.Exception.Message
    foreach ($identifier in @($script:AbPrivateIds | Sort-Object Length -Descending)) {
        $text = [regex]::Replace(
            $text, [regex]::Escape($identifier), '<redacted>',
            [Text.RegularExpressions.RegexOptions]::IgnoreCase)
    }
    $text = [regex]::Replace($text, '(?i)[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}', '<redacted-guid>')
    $text = [regex]::Replace($text, 'S-\d-\d+(?:-\d+){2,}', '<redacted-sid>')
    $text = $text.Replace("`r", ' ').Replace("`n", ' ')
    return $text.Substring(0, [math]::Min(1000, $text.Length))
}

function Get-AbNativeUvFacts {
    $command = Get-Command uv -CommandType Application -ErrorAction Stop
    $text = @(& $command.Source --version) -join "`n"
    if ($LASTEXITCODE -ne 0 -or $text -notmatch '^uv 0\.13\.5(?:\s|$)') {
        throw 'AB_GUARD: unexpected_uv_version'
    }
    $stream = [IO.File]::OpenRead($command.Source)
    $reader = [IO.BinaryReader]::new($stream)
    try {
        if ($reader.ReadUInt16() -ne 0x5A4D) { throw 'AB_GUARD: uv_is_not_a_pe_executable' }
        $stream.Position = 0x3C
        $offset = $reader.ReadUInt32()
        $stream.Position = $offset
        if ($reader.ReadUInt32() -ne 0x00004550 -or $reader.ReadUInt16() -ne 0xAA64) {
            throw 'AB_GUARD: uv_is_not_native_arm64'
        }
    } finally {
        $reader.Dispose()
    }
    return @{ version = $text.Trim(); architecture = 'ARM64' }
}

function Get-AbNativeInspection {
    param($Storage)
    $context = Get-AbNativeContext
    if (@(Get-AbContextErrors $context).Count -ne 0) {
        throw 'AB_GUARD: postcheck_runner_context_invalid'
    }
    if ($Storage.context.commit -ne $context.commit -or
        $Storage.context.run_id -ne $context.run_id -or
        $Storage.context.run_attempt -ne $context.run_attempt) {
        throw 'AB_GUARD: storage_record_identity_mismatch'
    }
    if ($context.workspace -ine $context.expected_workspace -or
        $context.workspace -ine $Storage.workspace_before.logical) {
        throw 'AB_GUARD: logical_workspace_environment_changed'
    }
    $uv = Get-AbNativeUvFacts
    $uvCache = (@(& uv cache dir) -join "`n").Trim()
    if ($LASTEXITCODE -ne 0) { throw 'AB_GUARD: uv_cache_probe_failed' }
    $python = Join-Path $Storage.paths.venv.physical 'Scripts\python.exe'
    $pipCache = (@(& $python -m pip cache dir) -join "`n").Trim()
    if ($LASTEXITCODE -ne 0) { throw 'AB_GUARD: pip_cache_probe_failed' }
    $paths = [ordered]@{
        workspace = Get-AbNativePath $context.workspace
        venv = Get-AbNativePath $Storage.paths.venv.physical
        uv_cache = Get-AbNativePath $uvCache
        pip_cache = Get-AbNativePath $pipCache
        temp = Get-AbNativePath $env:TEMP
        tmp = Get-AbNativePath $env:TMP
        wheelhouse = Get-AbNativePath $Storage.paths.wheelhouse.physical
    }
    foreach ($pair in @(
        @('uv_cache', 'UV_CACHE_DIR'), @('pip_cache', 'PIP_CACHE_DIR'),
        @('temp', 'TEMP'), @('tmp', 'TMP')
    )) {
        $expected = $Storage.environment[$pair[1]]
        $actual = [Environment]::GetEnvironmentVariable($pair[1], 'Process')
        if ([string]::IsNullOrWhiteSpace($actual) -or
            [IO.Path]::GetFullPath($actual).TrimEnd('\') -ine $expected.TrimEnd('\') -or
            $paths[$pair[0]].physical -ine $expected) {
            throw 'AB_GUARD: effective_cache_or_temp_path_mismatch'
        }
    }
    if ([IO.Path]::GetFullPath($uvCache).TrimEnd('\') -ine $Storage.paths.uv_cache.physical -or
        [IO.Path]::GetFullPath($pipCache).TrimEnd('\') -ine $Storage.paths.pip_cache.physical) {
        throw 'AB_GUARD: tools_did_not_report_physical_cache_paths'
    }
    foreach ($info in $paths.Values) {
        if (-not $info.exists -or $info.drive -ne $paths.workspace.drive) {
            throw 'AB_GUARD: workload_path_missing_or_on_another_volume'
        }
        if ($Storage.requested_layout -eq 'refs-devdrive' -and
            ($info.filesystem -ine 'ReFS' -or -not $info.is_dev_drive)) {
            throw 'AB_GUARD: effective_treatment_path_is_not_dev_drive'
        }
    }
    $protection = Get-AbNativeProtection
    if (-not $protection.available -or
        $protection.realtime_protection_enabled -ne
        $Storage.protection_before.realtime_protection_enabled) {
        throw 'AB_GUARD: protection_changed_before_measurement'
    }
    $backupRetained = $false
    if ($Storage.requested_layout -eq 'refs-devdrive') {
        $backup = Get-AbNativeEntry $Storage.backup_path
        $logical = Get-AbNativeEntry $context.workspace
        if ($null -eq $backup -or -not $backup.PSIsContainer -or
            ($backup.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            $logical.LinkType -ne 'Junction') {
            throw 'AB_GUARD: backup_or_workspace_junction_missing'
        }
        $backupRetained = $true
    }
    return @{
        paths = $paths
        uv = $uv
        protection = $protection
        backup_retained = $backupRetained
        logical_workspace_unchanged = $true
    }
}

function New-AbNativeOperations {
    param([Parameter(Mandatory)][string]$Python)
    $script:AbSettings = @{ Python = $Python }
    $script:AbPrivateIds = [System.Collections.Generic.List[string]]::new()
    return @{
        Context = { Get-AbNativeContext }
        Python = { Get-AbNativePythonFacts }
        Workspace = { param($Context, $Layout, $Trial) Get-AbNativeWorkspace $Context $Layout $Trial }
        Inventory = { Get-AbNativeInventory }
        Protection = { Get-AbNativeProtection }
        Initialize = { param($Disk) Initialize-AbNativeDisk $Disk }
        Partition = { param($Disk) New-AbNativePartition $Disk }
        Format = { param($Disk, $Partition) Format-AbNativePartition $Disk $Partition }
        Plan = { param($Context, $Workspace, $Volume, $Layout, $Trial) New-AbNativePlan $Context $Workspace $Volume $Layout $Trial }
        Copy = { param($Source, $Target) Copy-AbNativeWorkspace $Source $Target }
        Rename = { param($Source, $Backup) Move-AbNativeOriginal $Source $Backup }
        Junction = { param($Logical, $Physical) New-AbNativeJunction $Logical $Physical }
        Restore = { param($Plan) Restore-AbNativeOriginal $Plan }
        Configure = { param($Plan, $Layout) Set-AbNativePaths $Plan $Layout }
        Inspect = { param($Storage) Get-AbNativeInspection $Storage }
        SafeError = { param($Failure) Get-AbNativeSafeError $Failure }
    }
}

Export-ModuleMember -Function New-AbNativeOperations
