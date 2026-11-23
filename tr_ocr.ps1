param(
    [Parameter(Mandatory = $true)]
    [string]$List,          # text file: one PNG path per line
    [Parameter(Mandatory = $true)]
    [string]$Output,        # JSON: one entry per image with lines and words in pixels
    [string]$Language = 'ko'
)

# Windows' own OCR engine (Windows.Media.Ocr): offline, no extra install. Korean needs the
# Korean language's OCR feature, which comes with the Korean language pack.
$ErrorActionPreference = 'Stop'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
$exitCode = 0
try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics, ContentType = WindowsRuntime]
    $null = [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime]
    $asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
    function Wait-Async($operation, [Type]$type) {
        $task = $asTask.MakeGenericMethod($type).Invoke($null, @($operation))
        $task.Wait()
        $task.Result
    }

    # Windows PowerShell builds the WinRT Language from a plain tag string itself.
    $engine = $null
    try { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($Language) } catch { }
    if ($null -eq $engine) {
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    }
    if ($null -eq $engine -or -not ([string]$engine.RecognizerLanguage.LanguageTag).StartsWith($Language)) {
        throw "NO_LANGUAGE"
    }
    $pages = @()
    foreach ($path in [IO.File]::ReadAllLines($List, $utf8)) {
        if (-not $path) { continue }
        $file = Wait-Async ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Wait-Async ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Wait-Async ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Wait-Async ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Wait-Async ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $lines = @()
        foreach ($line in $result.Lines) {
            $words = @()
            foreach ($word in $line.Words) {
                $box = $word.BoundingRect
                $words += [ordered]@{ text = $word.Text; x = $box.X; y = $box.Y; w = $box.Width; h = $box.Height }
            }
            $lines += [ordered]@{ text = $line.Text; words = $words }
        }
        $pages += [ordered]@{ image = $path; width = $bitmap.PixelWidth; height = $bitmap.PixelHeight;
                              angle = $result.TextAngle; lines = $lines }
        $bitmap.Dispose(); $stream.Dispose()
    }
    [IO.File]::WriteAllText($Output, (ConvertTo-Json -InputObject $pages -Depth 6 -Compress), $utf8)
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    $exitCode = 1
}
exit $exitCode
