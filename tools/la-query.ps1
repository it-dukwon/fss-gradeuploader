# Log Analytics KQL 쿼리 — 한글 안전 버전.
#
# 문제: `az monitor log-analytics query` 는 Windows에서 stdout을 콘솔 코드페이지(cp949)로
#       인코딩해 한글이 깨진다(복구 불가). chcp/Console.OutputEncoding 으로도 해결 안 됨.
# 해결: az CLI 텍스트 출력 대신 Log Analytics REST API를 직접 호출하고,
#       응답 raw 바이트를 UTF-8로 직접 디코딩한다(인코딩 단계 우회).
#
# 사용 예:
#   pwsh tools/la-query.ps1 -Workspace a8259b62-... -Query "ContainerAppConsoleLogs_CL | take 20" -OutFile out.txt
#   pwsh tools/la-query.ps1 -Workspace <customerId> -Query "<KQL>"
# (OutFile 생략 시 콘솔 출력 — 단, 콘솔도 cp949면 깨질 수 있으니 파일로 받아 Read 권장)

param(
    [Parameter(Mandatory = $true)][string]$Workspace,   # Log Analytics customerId (GUID)
    [Parameter(Mandatory = $true)][string]$Query,        # KQL
    [string]$OutFile
)

$ErrorActionPreference = "Stop"

$token = az account get-access-token --resource "https://api.loganalytics.io" --query accessToken -o tsv
$bodyBytes = [System.Text.Encoding]::UTF8.GetBytes((@{ query = $Query } | ConvertTo-Json -Compress))

$resp = Invoke-WebRequest -Method Post `
    -Uri "https://api.loganalytics.io/v1/workspaces/$Workspace/query" `
    -Headers @{ Authorization = "Bearer $token" } `
    -Body $bodyBytes -ContentType "application/json" -UseBasicParsing

$json = [System.Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
$data = $json | ConvertFrom-Json
$rows = $data.tables[0].rows

$lines = foreach ($row in $rows) { ($row -join "`t") }

if ($OutFile) {
    $lines | Out-File -FilePath $OutFile -Encoding utf8
    Write-Output "wrote $($rows.Count) rows -> $OutFile"
} else {
    $lines
}
