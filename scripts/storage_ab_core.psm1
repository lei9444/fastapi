Set-StrictMode -Version Latest

# This module has no native storage or filesystem operations. Every effect must
# be supplied explicitly; local tests provide a completely mocked operation map.
function Get-AbValue {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return $Object[$Name] }
        return $null
    }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -ne $property) { return $property.Value }
    return $null
}

function Test-AbInteger {
    param($Value)
    return ($Value -is [byte] -or $Value -is [int16] -or $Value -is [int32] -or
        $Value -is [int64] -or $Value -is [uint16] -or $Value -is [uint32] -or
        $Value -is [uint64])
}

function Get-AbContextErrors {
    param($Context)
    $errors = [System.Collections.Generic.List[string]]::new()
    foreach ($pair in @(
        @('github_actions', 'true'),
        @('runner_environment', 'github-hosted'),
        @('runner_os', 'Windows'),
        @('runner_arch', 'ARM64'),
        @('os_arch', 'Arm64'),
        @('process_arch', 'Arm64'),
        @('repository', 'lei9444/fastapi')
    )) {
        $value = Get-AbValue $Context $pair[0]
        if ($value -isnot [string] -or $value -ine $pair[1]) {
            $errors.Add("invalid_$($pair[0])")
        }
    }
    $windows = Get-AbValue $Context 'is_windows'
    if ($windows -isnot [bool] -or -not $windows) { $errors.Add('not_windows') }
    foreach ($name in @('run_id', 'run_attempt')) {
        $value = Get-AbValue $Context $name
        if ($value -isnot [string] -or $value -notmatch '^[1-9][0-9]*$') {
            $errors.Add("invalid_$name")
        }
    }
    $sha = Get-AbValue $Context 'commit'
    if ($sha -isnot [string] -or $sha -notmatch '^[0-9a-f]{40}$') {
        $errors.Add('invalid_commit')
    }
    return $errors.ToArray()
}

function Get-AbDiskErrors {
    param($Disk, [object[]]$PartitionDiskNumbers)
    $errors = [System.Collections.Generic.List[string]]::new()
    foreach ($pair in @(@('BusType', 'NVMe'), @('PartitionStyle', 'RAW'))) {
        $value = Get-AbValue $Disk $pair[0]
        if ($value -isnot [string] -or $value -ine $pair[1]) {
            $errors.Add("invalid_$($pair[0])")
        }
    }
    foreach ($name in @('IsBoot', 'IsSystem', 'IsOffline', 'IsReadOnly')) {
        $value = Get-AbValue $Disk $name
        if ($value -isnot [bool] -or $value) { $errors.Add("unsafe_$name") }
    }
    $number = Get-AbValue $Disk 'Number'
    if (-not (Test-AbInteger $number) -or $number -lt 0) {
        $errors.Add('unknown_disk_number')
    } elseif ($number -in $PartitionDiskNumbers) {
        $errors.Add('present_in_partition_inventory')
    }
    $parts = Get-AbValue $Disk 'NumberOfPartitions'
    if (-not (Test-AbInteger $parts) -or $parts -ne 0) {
        $errors.Add('not_partition_free')
    }
    $size = Get-AbValue $Disk 'Size'
    if (-not (Test-AbInteger $size) -or $size -lt 50GB) {
        $errors.Add('unknown_or_small_capacity')
    }
    $identity = Get-AbValue $Disk 'Identity'
    if ($identity -isnot [string] -or [string]::IsNullOrWhiteSpace($identity)) {
        $errors.Add('identity_unavailable')
    }
    return $errors.ToArray()
}

function Get-AbPublicDisk {
    param($Disk, [string[]]$Errors)
    $public = [ordered]@{}
    foreach ($name in @(
        'Number', 'BusType', 'PartitionStyle', 'Size', 'NumberOfPartitions',
        'IsBoot', 'IsSystem', 'IsOffline', 'IsReadOnly'
    )) {
        $public[$name] = Get-AbValue $Disk $name
    }
    $public['eligible'] = $Errors.Count -eq 0
    $public['reasons'] = @($Errors)
    return $public
}

function Invoke-AbOperation {
    param(
        [System.Collections.IDictionary]$Operations,
        [string]$Name,
        [object[]]$Arguments = @()
    )
    if (-not $Operations.Contains($Name) -or $Operations[$Name] -isnot [scriptblock]) {
        throw "Missing explicit operation: $Name"
    }
    return & $Operations[$Name] @Arguments
}

function Assert-AbManifestEqual {
    param($Source, $Target)
    foreach ($name in @('digest', 'files', 'directories', 'links', 'bytes')) {
        if ((Get-AbValue $Source $name) -cne (Get-AbValue $Target $name)) {
            throw "AB_GUARD: workspace_copy_mismatch_$name"
        }
    }
}

function Invoke-AbStorage {
    param(
        [Parameter(Mandatory)][ValidateSet('default', 'refs-devdrive')][string]$Layout,
        [Parameter(Mandatory)][ValidateRange(1, 3)][int]$Trial,
        [Parameter(Mandatory)][System.Collections.IDictionary]$Operations
    )
    $total = [Diagnostics.Stopwatch]::StartNew()
    $phase = $null
    $phaseName = 'inspection_seconds'
    $plan = $null
    $result = [ordered]@{
        schema_version = 1
        requested_layout = $Layout
        trial = $Trial
        applied_layout = $null
        applied = $false
        comparable = $false
        status = 'not_applied'
        reason = ''
        context = $null
        python = $null
        disk_inventory = @()
        eligible_disk_count = $null
        selected_disk = $null
        mutation_attempted = $false
        volume_verified = $false
        workspace_migrated = $false
        backup_path = $null
        backup_retained = $false
        rollback = 'not_needed'
        copy = $null
        workspace_before = $null
        paths = $null
        initial_cache_state = $null
        protection_before = $null
        protection_after = $null
        environment = $null
        timings = [ordered]@{
            inspection_seconds = $null
            creation_seconds = $null
            migration_seconds = $null
            configuration_seconds = $null
            total_seconds = $null
        }
    }
    try {
        $phase = [Diagnostics.Stopwatch]::StartNew()
        $context = Invoke-AbOperation $Operations 'Context'
        $result.context = [ordered]@{}
        foreach ($name in @(
            'github_actions', 'runner_environment', 'runner_os', 'runner_arch',
            'os_arch', 'process_arch', 'is_windows', 'repository', 'run_id',
            'run_attempt', 'commit', 'image_os', 'image_version'
        )) {
            $result.context[$name] = Get-AbValue $context $name
        }
        $contextErrors = @(Get-AbContextErrors $context)
        if ($contextErrors.Count -gt 0) {
            $result.status = 'refused'
            $result.reason = $contextErrors -join ', '
            return $result
        }
        $python = Invoke-AbOperation $Operations 'Python'
        $result.python = $python
        if ($python.version -ne '3.13.17' -or
            $python.platform -ne 'win-arm64' -or $python.pointer_bits -ne 64) {
            throw 'AB_GUARD: native_python_mismatch'
        }
        $workspace = Invoke-AbOperation $Operations 'Workspace' @($context, $Layout, $Trial)
        if ($workspace.is_directory -isnot [bool] -or -not $workspace.is_directory -or
            $workspace.is_reparse -isnot [bool] -or $workspace.is_reparse -or
            $workspace.safe -isnot [bool] -or -not $workspace.safe -or
            $workspace.has_venv -isnot [bool] -or $workspace.has_venv) {
            throw 'AB_GUARD: workspace_is_not_a_fresh_ordinary_checkout'
        }
        $result.workspace_before = Get-AbValue $workspace 'path_info'
        $result.applied_layout = 'default'
        $inventory = Invoke-AbOperation $Operations 'Inventory'
        if ($inventory.complete -isnot [bool] -or -not $inventory.complete) {
            throw 'AB_GUARD: incomplete_storage_inventory'
        }
        $eligible = @()
        foreach ($disk in $inventory.disks) {
            $errors = @(Get-AbDiskErrors $disk @($inventory.partition_disk_numbers))
            $result.disk_inventory += Get-AbPublicDisk $disk $errors
            if ($errors.Count -eq 0) { $eligible += $disk }
        }
        $result.eligible_disk_count = $eligible.Count
        $result.protection_before = Invoke-AbOperation $Operations 'Protection'
        if ($result.protection_before.available -isnot [bool] -or
            -not $result.protection_before.available) {
            throw 'AB_GUARD: protection_state_unavailable'
        }
        $phase.Stop()
        $result.timings[$phaseName] = [math]::Round($phase.Elapsed.TotalSeconds, 6)
        $phase = $null
        $volume = $null
        if ($Layout -eq 'refs-devdrive') {
            if ($eligible.Count -ne 1) {
                $result.reason = "eligible_raw_nvme_count_$($eligible.Count)"
                $result.applied_layout = 'default'
                return $result
            }
            $selected = $eligible[0]
            $result.selected_disk = Get-AbPublicDisk $selected @()
            $phaseName = 'creation_seconds'
            $phase = [Diagnostics.Stopwatch]::StartNew()
            $fresh = Invoke-AbOperation $Operations 'Inventory'
            if ($fresh.complete -isnot [bool] -or -not $fresh.complete) {
                throw 'AB_GUARD: recheck_inventory_unavailable'
            }
            $again = @($fresh.disks | Where-Object {
                @(Get-AbDiskErrors $_ @($fresh.partition_disk_numbers)).Count -eq 0
            })
            if ($again.Count -ne 1 -or $again[0].Number -ne $selected.Number -or
                $again[0].Identity -cne $selected.Identity -or
                $again[0].Size -ne $selected.Size) {
                throw 'AB_GUARD: raw_disk_changed_before_initialization'
            }
            $selected = $again[0]
            $result.mutation_attempted = $true
            $null = Invoke-AbOperation $Operations 'Initialize' @($selected)
            $partition = Invoke-AbOperation $Operations 'Partition' @($selected)
            if (-not (Test-AbInteger $partition.DiskNumber) -or
                $partition.DiskNumber -ne $selected.Number -or
                -not (Test-AbInteger $partition.PartitionNumber) -or
                $partition.PartitionNumber -le 0 -or
                $partition.DriveLetter -notmatch '^[A-Za-z]$' -or
                $partition.DriveLetter -in @($fresh.used_letters)) {
                throw 'AB_GUARD: new_partition_or_drive_letter_conflict'
            }
            $volume = Invoke-AbOperation $Operations 'Format' @($selected, $partition)
            if ($volume.filesystem -ine 'ReFS' -or $volume.is_dev_drive -isnot [bool] -or
                -not $volume.is_dev_drive) {
                throw 'AB_GUARD: formatted_volume_is_not_verified_refs_dev_drive'
            }
            $result.volume_verified = $true
            $phase.Stop()
            $result.timings[$phaseName] = [math]::Round($phase.Elapsed.TotalSeconds, 6)
            $phase = $null
        }
        $plan = Invoke-AbOperation $Operations 'Plan' @($context, $workspace, $volume, $Layout, $Trial)
        if ($Layout -eq 'refs-devdrive') {
            $phaseName = 'migration_seconds'
            $phase = [Diagnostics.Stopwatch]::StartNew()
            $result.copy = Invoke-AbOperation $Operations 'Copy' @($workspace.logical, $plan.workspace_physical)
            if ($result.copy.verified -isnot [bool] -or -not $result.copy.verified) {
                throw 'AB_GUARD: workspace_copy_not_verified'
            }
            $result.backup_path = $plan.backup
            $result.applied_layout = 'partial'
            $null = Invoke-AbOperation $Operations 'Rename' @($workspace.logical, $plan.backup)
            $result.backup_retained = $true
            $null = Invoke-AbOperation $Operations 'Junction' @($workspace.logical, $plan.workspace_physical)
            $result.workspace_migrated = $true
            $result.applied_layout = 'refs-workspace-only'
            $phase.Stop()
            $result.timings[$phaseName] = [math]::Round($phase.Elapsed.TotalSeconds, 6)
            $phase = $null
        } else {
            $result.applied_layout = 'default'
        }
        $phaseName = 'configuration_seconds'
        $phase = [Diagnostics.Stopwatch]::StartNew()
        $configured = Invoke-AbOperation $Operations 'Configure' @($plan, $Layout)
        $result.paths = $configured.paths
        $result.initial_cache_state = $configured.initial_cache_state
        $result.environment = $configured.environment
        $result.protection_after = Invoke-AbOperation $Operations 'Protection'
        if ($result.protection_after.available -isnot [bool] -or
            -not $result.protection_after.available -or
            $result.protection_after.realtime_protection_enabled -ne
            $result.protection_before.realtime_protection_enabled) {
            throw 'AB_GUARD: protection_state_changed_or_unavailable'
        }
        $result.applied_layout = $Layout
        $result.applied = $true
        $result.comparable = $true
        $result.status = 'applied'
    } catch {
        $result.status = 'failed'
        $result.applied = $false
        $result.comparable = $false
        $result.reason = Invoke-AbOperation $Operations 'SafeError' @($_)
        if ($Layout -eq 'refs-devdrive' -and $null -ne $plan) {
            try {
                $recovery = Invoke-AbOperation $Operations 'Restore' @($plan)
                $result.rollback = $recovery.status
                $result.backup_retained = $recovery.backup_retained
                if ($recovery.status -eq 'restored') {
                    $result.applied_layout = 'default-restored'
                }
            } catch {
                $result.rollback = 'failed_backup_preserved'
                $result.reason += '; ' + (Invoke-AbOperation $Operations 'SafeError' @($_))
            }
        }
    } finally {
        if ($null -ne $phase) {
            $phase.Stop()
            $result.timings[$phaseName] = [math]::Round($phase.Elapsed.TotalSeconds, 6)
        }
        $total.Stop()
        $result.timings.total_seconds = [math]::Round($total.Elapsed.TotalSeconds, 6)
    }
    return $result
}

Export-ModuleMember -Function Get-AbContextErrors, Get-AbDiskErrors, Get-AbPublicDisk, Test-AbInteger, Assert-AbManifestEqual, Invoke-AbStorage
