param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('export', 'build')]
    [string]$Mode,
    [Parameter(Mandatory = $true)]
    [string]$Source,
    [Parameter(Mandatory = $true)]
    [string]$Output,
    [string]$Pdf,
    [string]$VerifyXml
)

$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false, $true)

function Get-NewPath([string]$Path) {
    $fullPath = [IO.Path]::GetFullPath($Path)
    if (Test-Path -LiteralPath $fullPath) {
        throw "Output path already exists: $fullPath"
    }
    if (-not [IO.Directory]::Exists([IO.Path]::GetDirectoryName($fullPath))) {
        throw "Output directory does not exist: $fullPath"
    }
    return $fullPath
}

function Set-XmlEncoding([string]$Xml, [string]$Encoding) {
    $pattern = '(?i)\A(<\?xml\s+[^?]*?\bencoding\s*=\s*["''])[^"'']+(["''])'
    return $Xml -replace $pattern, ('${1}' + $Encoding + '${2}')
}

function Open-Hwp([object]$Hwp, [string]$Path) {
    # This loads the document from memory without opening or locking the source file.
    $data = [Convert]::ToBase64String([IO.File]::ReadAllBytes($Path))
    if (-not $Hwp.SetTextFile($data, 'HWP', '')) {
        throw "Hancom could not load HWP: $Path"
    }
}

function Save-Native([object]$Hwp, [string]$Path, [string]$Format) {
    $options = ''
    if ($Format -eq 'HWP') { $options = 'lock:false;fullsave:true' }
    if (-not $Hwp.SaveAs($Path, $Format, $options)) {
        throw "Hancom could not save $Format`: $Path"
    }
    if (-not [IO.File]::Exists($Path) -or ([IO.FileInfo]::new($Path)).Length -eq 0) {
        throw "Hancom did not produce a $Format file: $Path"
    }
}

function Write-Hml([object]$Hwp, [string]$Path) {
    $xml = [string]$Hwp.GetTextFile('HWPML2X', '')
    if ($xml -notmatch '<HWPML(?:\s|>)') {
        throw 'Hancom did not return HWPML document data.'
    }
    [IO.File]::WriteAllText($Path, (Set-XmlEncoding $xml 'UTF-8'), $utf8)
}

$hwp = $null
$mutex = $null
$locked = $false
$exitCode = 0
try {
    $Source = [IO.Path]::GetFullPath($Source)
    if (-not [IO.File]::Exists($Source)) { throw "Source not found: $Source" }
    $Output = Get-NewPath $Output
    if ($Pdf) { $Pdf = Get-NewPath $Pdf }
    if ($VerifyXml) { $VerifyXml = Get-NewPath $VerifyXml }
    $destinations = @($Output, $Pdf, $VerifyXml) | Where-Object { $_ }
    if (($destinations | Sort-Object -Unique).Count -ne $destinations.Count) {
        throw 'Output, PDF, and verification files must use separate paths.'
    }

    $sourceExtension = [IO.Path]::GetExtension($Source).ToLowerInvariant()
    $outputExtension = [IO.Path]::GetExtension($Output).ToLowerInvariant()
    if ($Mode -eq 'export' -and $sourceExtension -notin '.hwp', '.hwpx') {
        throw 'An HWP or HWPX source is required.'
    }
    if ($Mode -eq 'build' -and $outputExtension -notin '.hwp', '.hwpx') {
        throw 'The output filename must have an HWP or HWPX extension.'
    }
    if ($Pdf -and [IO.Path]::GetExtension($Pdf) -ne '.pdf') {
        throw 'The PDF filename must have a PDF extension.'
    }
    if ($VerifyXml -and $Mode -ne 'build') {
        throw 'VerifyXml is available in build mode.'
    }

    $mutex = [Threading.Mutex]::new($false, 'Local\TR.Hancom')
    try {
        $locked = $mutex.WaitOne([TimeSpan]::FromSeconds(30))
    } catch [Threading.AbandonedMutexException] {
        $locked = $true
    }
    if (-not $locked) { throw 'Hancom is busy with another TR conversion.' }

    $hwp = New-Object -ComObject 'HWPFrame.HwpObject'
    if ($Mode -eq 'export') {
        if ($sourceExtension -eq '.hwp') {
            Open-Hwp $hwp $Source
        } else {
            $hwp.XHwpWindows.Item(0).Visible = $true
            if (-not $hwp.Open($Source, 'HWPX', 'lock:false;forceopen:true;suspendpassword:true')) {
                throw "Hancom could not open HWPX: $Source"
            }
        }
        Write-Hml $hwp $Output
        if ($Pdf) { Save-Native $hwp $Pdf 'PDF' }
    } else {
        $xml = [IO.File]::ReadAllText($Source, $utf8)
        if ($xml -notmatch '<HWPML(?:\s|>)') { throw 'Build requires an HWPML document.' }
        if (-not $hwp.SetTextFile((Set-XmlEncoding $xml 'UTF-16'), 'HWPML2X', '')) {
            throw 'Hancom rejected the edited HWPML document.'
        }
        if ($Pdf) { Save-Native $hwp $Pdf 'PDF' }
        Save-Native $hwp $Output ($outputExtension.Substring(1).ToUpperInvariant())
        if ($VerifyXml) {
            if ($outputExtension -eq '.hwp') { Open-Hwp $hwp $Output }
            Write-Hml $hwp $VerifyXml
        }
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    $exitCode = 1
} finally {
    if ($null -ne $hwp) {
        try { $null = $hwp.Quit() } catch { }
        $null = [Runtime.InteropServices.Marshal]::ReleaseComObject($hwp)
    }
    if ($locked) { $mutex.ReleaseMutex() }
    if ($null -ne $mutex) { $mutex.Dispose() }
}
exit $exitCode
