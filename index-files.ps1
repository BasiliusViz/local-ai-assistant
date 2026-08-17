# Indexer: local files -> Qdrant
# Reads .md/.txt from a folder, splits into chunks, gets embeddings from
# ollama-embed (bge-m3) and upserts into the "knowledge" collection.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\index-files.ps1
#   powershell -ExecutionPolicy Bypass -File .\index-files.ps1 -SourceDir "C:\path\to\docs"

param(
    [string]$SourceDir  = "",
    [string]$Qdrant     = "http://localhost:6333",
    [string]$Embedder   = "http://localhost:11434",
    [string]$Collection = "knowledge",
    [string]$EmbedModel = "bge-m3",
    [string]$Source     = "local",
    [int]$ChunkSize     = 1500,
    [int]$BatchSize     = 16
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if ($SourceDir -eq "") {
    $SourceDir = Join-Path $PSScriptRoot "testdocs"
}

# absolute path: relative one would break the Substring below
if (Test-Path $SourceDir) {
    $SourceDir = (Resolve-Path $SourceDir).Path
}

# ---------- helpers ----------

function New-ChunkId {
    param([string]$Text)
    $md5  = [System.Security.Cryptography.MD5]::Create()
    $hash = $md5.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Text))
    return ([guid]$hash).ToString()
}

# Splits markdown into chunks and tracks the heading path for each one.
#
# Two fixes for retrieval quality, both measured on testdocs:
#   1. A heading with no body of its own is NEVER a chunk. It used to become
#      one ("## Rollback" alone) and outrank real content in the results.
#   2. Every chunk carries its heading path. The caller prepends it before
#      embedding - a chunk stripped of its heading loses the very words that
#      made it findable. This is the cheap half of Anthropic's Contextual
#      Retrieval: no LLM pass, most of the benefit.
#
# Returns objects: .Text (original, goes to payload) and .Path (breadcrumb).
function Split-Text {
    param([string]$Text, [int]$MaxLen)

    $result = New-Object System.Collections.ArrayList

    # Split by headings, but NEVER inside a fenced code block: a .gitignore
    # example starts its comments with "#", and a regex split tears the
    # block apart into chunks like "build/" that answer nothing.
    $sections = New-Object System.Collections.ArrayList
    $current = New-Object System.Text.StringBuilder
    $fenced = $false

    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match '^\s*```') { $fenced = -not $fenced }

        if ((-not $fenced) -and ($line -match '^#{1,6}\s')) {
            if ($current.Length -gt 0) { [void]$sections.Add($current.ToString()) }
            $current = New-Object System.Text.StringBuilder
        }
        [void]$current.AppendLine($line)
    }
    if ($current.Length -gt 0) { [void]$sections.Add($current.ToString()) }

    # breadcrumb per heading level, so a deep section keeps its parents
    $trail = New-Object 'string[]' 7
    $pending = ""

    foreach ($sec in $sections) {
        $s = $sec.Trim()
        if ($s.Length -eq 0) { continue }

        $lines = $s -split "`r?`n", 2
        $head = ""
        $body = $s

        if ($lines[0] -match '^(#{1,6})\s+(.*)$') {
            $level = $Matches[1].Length
            $head = $Matches[2].Trim()
            $body = ""
            if ($lines.Count -gt 1) { $body = $lines[1].Trim() }

            $trail[$level] = $head
            for ($k = $level + 1; $k -lt 7; $k++) { $trail[$k] = "" }
        }

        # heading path: parents + own heading, joined
        $parts = @()
        foreach ($t in $trail) { if ($t) { $parts += $t } }
        $path = $parts -join " - "

        # a bare heading waits and glues itself onto the next section
        if ($body.Length -eq 0) {
            if ($head) { $pending = $path }
            continue
        }
        if ($pending -and -not $head) { $path = $pending }
        $pending = ""

        $pieces = New-Object System.Collections.ArrayList
        if ($body.Length -le $MaxLen) {
            [void]$pieces.Add($body)
        }
        else {
            $buf = ""
            $paras = [regex]::Split($body, '\r?\n\s*\r?\n')
            foreach ($para in $paras) {
                $p = $para.Trim()
                if ($p.Length -eq 0) { continue }
                if ((($buf.Length + $p.Length) -gt $MaxLen) -and ($buf.Length -gt 0)) {
                    [void]$pieces.Add($buf.Trim())
                    $buf = ""
                }
                $buf = $buf + "`n`n" + $p
            }
            if ($buf.Trim().Length -gt 0) { [void]$pieces.Add($buf.Trim()) }
        }

        foreach ($piece in $pieces) {
            [void]$result.Add([PSCustomObject]@{ Text = $piece; Path = $path })
        }
    }

    return $result.ToArray()
}

function Get-Embeddings {
    param([string[]]$Texts)
    $payload = @{ model = $EmbedModel; input = $Texts } | ConvertTo-Json -Depth 5
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
    $resp = Invoke-RestMethod -Method Post -Uri "$Embedder/api/embed" -ContentType "application/json" -Body $bytes
    # comma keeps the outer array intact: PowerShell would unwrap
    # a single-element result and break the [0] indexing
    return ,$resp.embeddings
}

# ---------- checks ----------

Write-Host "Checking services..." -ForegroundColor Cyan

try {
    $info = Invoke-RestMethod -Uri "$Qdrant/collections/$Collection"
    Write-Host "  Qdrant OK, points now: $($info.result.points_count)" -ForegroundColor Green
}
catch {
    Write-Host "  Qdrant not reachable or collection missing: $Collection" -ForegroundColor Red
    exit 1
}

try {
    $test = Get-Embeddings -Texts @("test")
    Write-Host "  Embedder OK, vector size: $($test[0].Count)" -ForegroundColor Green
}
catch {
    Write-Host "  Embedder not reachable: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $SourceDir)) {
    Write-Host "Folder not found: $SourceDir" -ForegroundColor Red
    exit 1
}

# ---------- collect files ----------

$files = Get-ChildItem -Path $SourceDir -Recurse -File | Where-Object { $_.Extension -in @(".md", ".markdown", ".txt") }
Write-Host "Files found: $($files.Count)" -ForegroundColor Cyan
if ($files.Count -eq 0) { exit 0 }

# ---------- index ----------

$totalChunks = 0
$fileNum = 0
$sep = [char]124   # pipe character, kept out of strings

foreach ($file in $files) {
    $fileNum = $fileNum + 1
    $text = Get-Content -Path $file.FullName -Raw -Encoding UTF8
    if ($null -eq $text -or $text.Trim().Length -eq 0) { continue }

    # @() is required: a single chunk would collapse to a string
    # and the range indexing below would slice it per character
    $chunks = @(Split-Text -Text $text -MaxLen $ChunkSize)
    if ($chunks.Count -eq 0) { continue }

    $relPath = $file.FullName.Substring($SourceDir.Length).TrimStart("\").TrimStart("/")
    Write-Host "[$fileNum/$($files.Count)] $relPath - chunks: $($chunks.Count)"

    # Drop this file's old points first. Chunk ids are derived from the chunk
    # number, so a document that now splits into fewer chunks would leave
    # orphans behind - stale text that still shows up in search results.
    $delFilter = @{
        filter = @{
            must = @(
                @{ key = "source";    match = @{ value = $Source } },
                @{ key = "source_id"; match = @{ value = $relPath } }
            )
        }
    } | ConvertTo-Json -Depth 10 -Compress
    try {
        $delBytes = [System.Text.Encoding]::UTF8.GetBytes($delFilter)
        Invoke-RestMethod -Method Post -Uri "$Qdrant/collections/$Collection/points/delete?wait=true" -ContentType "application/json" -Body $delBytes | Out-Null
    }
    catch {
        Write-Host "    Cleanup warning: $($_.Exception.Message)" -ForegroundColor Yellow
    }

    for ($i = 0; $i -lt $chunks.Count; $i = $i + $BatchSize) {
        $last = $i + $BatchSize - 1
        if ($last -ge $chunks.Count) { $last = $chunks.Count - 1 }
        $batch = @($chunks[$i..$last])

        # What we embed is not what we store. The vector is built from
        # "heading path + text" so the chunk carries its own context;
        # the payload keeps the clean original for the model to read.
        $toEmbed = @()
        foreach ($ch in $batch) {
            if ($ch.Path) { $toEmbed += ($ch.Path + "`n`n" + $ch.Text) }
            else          { $toEmbed += $ch.Text }
        }

        try {
            $vectors = Get-Embeddings -Texts $toEmbed
        }
        catch {
            Write-Host "    Embedding error: $($_.Exception.Message)" -ForegroundColor Red
            continue
        }

        $points = New-Object System.Collections.ArrayList
        for ($j = 0; $j -lt $batch.Count; $j = $j + 1) {
            $chunkIdx = $i + $j
            $idSeed = $Source + $sep + $relPath + $sep + $chunkIdx

            $payload = @{
                source     = $Source
                source_id  = $relPath
                space      = $file.Directory.Name
                title      = $file.BaseName
                url        = $file.FullName
                acl_groups = @("all")
                updated_at = $file.LastWriteTimeUtc.ToString("o")
                chunk_idx  = $chunkIdx
                heading    = $batch[$j].Path
                text       = $batch[$j].Text
            }

            $point = @{
                id      = New-ChunkId -Text $idSeed
                vector  = @{ dense = $vectors[$j] }
                payload = $payload
            }
            [void]$points.Add($point)
        }

        $upsert = @{ points = $points.ToArray() } | ConvertTo-Json -Depth 10 -Compress
        $upsertBytes = [System.Text.Encoding]::UTF8.GetBytes($upsert)

        try {
            Invoke-RestMethod -Method Put -Uri "$Qdrant/collections/$Collection/points?wait=true" -ContentType "application/json" -Body $upsertBytes | Out-Null
            $totalChunks = $totalChunks + $points.Count
        }
        catch {
            Write-Host "    Qdrant upsert error: $($_.Exception.Message)" -ForegroundColor Red
        }
    }
}

Write-Host ""
Write-Host "Chunks written: $totalChunks" -ForegroundColor Green
$final = Invoke-RestMethod -Uri "$Qdrant/collections/$Collection"
Write-Host "Total in collection: $($final.result.points_count)" -ForegroundColor Green
