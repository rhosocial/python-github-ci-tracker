# REST API verification against the locally running server.
#
# There is no host-side 8000 to probe: REST stays inside the container network.
# The webhook listener serves the same FastAPI app, and it terminates TLS, so
# the probe goes to 18080 with certificate checking off.
$ErrorActionPreference = 'Continue'
$base = 'https://127.0.0.1:18080'
$key  = $env:CITRACKER_API_KEY
if (-not $key) { $key = 'test-api-key-12345' }

function Try-Req($method, $url, $headers, $body) {
    try {
        $params = @{
            Uri             = $url
            Method          = $method
            Headers         = $headers
            TimeoutSec      = 30
            UseBasicParsing = $true
            SkipCertificateCheck = $true
        }
        if ($body) { $params.Body = $body; $params.ContentType = 'application/json' }
        $resp = Invoke-WebRequest @params
        return "OK $($resp.StatusCode): $($resp.Content)"
    } catch {
        $code = $null
        if ($_.Exception.Response) { $code = $_.Exception.Response.StatusCode.value__ }
        $detail = ''
        if ($_.Exception.Response) {
            try {
                $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $detail = $reader.ReadToEnd()
            } catch {}
        }
        return "ERR $code : $detail"
    }
}

"=== healthz ==="
Try-Req 'GET' "$base/healthz" $null $null
"`n=== latest-run (no auth, expect 401) ==="
Try-Req 'GET' "$base/api/v1/latest-run?repository=rhosocial/python-activerecord" $null $null
"`n=== latest-run (auth) ==="
Try-Req 'GET' "$base/api/v1/latest-run?repository=rhosocial/python-activerecord&branch=feature/typed-columns" @{ Authorization = "Bearer $key" } $null
"`n=== runs list (auth) ==="
Try-Req 'GET' "$base/api/v1/runs?repository=rhosocial/python-activerecord&per_page=2" @{ Authorization = "Bearer $key" } $null
"`n=== repositories (auth) ==="
Try-Req 'GET' "$base/api/v1/repositories" @{ Authorization = "Bearer $key" } $null
