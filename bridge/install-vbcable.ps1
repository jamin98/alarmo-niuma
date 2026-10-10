# 安装 VB-CABLE 虚拟声卡（按住牛马说话要用）。
#
# 桥接把手机 / Core2 上录的话"播放"进 CABLE Input，Grok Bot 把 CABLE Output 当麦克风听写。
# 在跑 Grok Bot 和桥接的那台电脑上运行：双击 install-vbcable.bat，或
#     powershell -ExecutionPolicy Bypass -File install-vbcable.ps1             下载、校验签名、安装
#     powershell -ExecutionPolicy Bypass -File install-vbcable.ps1 -CheckOnly  只下载和校验签名，不安装
#
# 安装程序来自 VB-Audio 官网，运行前会检查它的数字签名有效、且属于 VB-Audio 的作者 Vincent Burel，不对就不运行。
# 安装时 Windows 会弹管理员确认，安装窗口里点 "Install Driver"；装完建议重启一次。
param(
    [switch]$CheckOnly,
    [switch]$Force
)
$ErrorActionPreference = 'Stop'
$Url = 'https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip'
$Signer = 'VB-Audio|Vincent Burel|BUREL VINCENT'   # 证书里写的是 "CN=BUREL VINCENT Entrepreneur individuel"（法国 Eymet）

function Test-Cable {
    [bool](Get-PnpDevice -Class AudioEndpoint -ErrorAction SilentlyContinue |
           Where-Object { $_.FriendlyName -like '*CABLE*' })
}

if ((Test-Cable) -and -not $Force -and -not $CheckOnly) {
    Write-Host '已经装过 VB-CABLE（系统里有 CABLE 设备），不用再装。要重装加 -Force。' -ForegroundColor Green
    exit 0
}

$dir = Join-Path $env:TEMP 'niuma-vbcable'
if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
New-Item -ItemType Directory -Path $dir | Out-Null
$zip = Join-Path $dir 'VBCABLE_Driver_Pack45.zip'

Write-Host "下载 $Url"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -Uri $Url -OutFile $zip -UseBasicParsing
Write-Host ("下载完成：{0:N0} 字节" -f (Get-Item $zip).Length)
Expand-Archive -Path $zip -DestinationPath (Join-Path $dir 'pack') -Force

# 按系统架构挑安装程序
$arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
$all = Get-ChildItem -Path (Join-Path $dir 'pack') -Recurse -Filter '*.exe'
Write-Host ('包里的安装程序：' + (($all | ForEach-Object Name) -join '、'))
$exe = switch ($arch) {
    'ARM64' { $all | Where-Object { $_.Name -match 'arm64' } | Select-Object -First 1 }
    'AMD64' { $all | Where-Object { $_.Name -match 'x64' } | Select-Object -First 1 }
    default { $all | Where-Object { $_.Name -notmatch 'x64|arm64' } | Select-Object -First 1 }
}
if (-not $exe) { throw "包里没找到适合 $arch 的安装程序" }

# 校验数字签名
$sig = Get-AuthenticodeSignature -FilePath $exe.FullName
$subject = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { '(无签名)' }
Write-Host "安装程序：$($exe.Name)  签名状态：$($sig.Status)  签名者：$subject"
if ($sig.Status -ne 'Valid' -or $subject -notmatch $Signer) {
    throw '签名校验不通过，不运行这个安装程序。'
}
Write-Host '签名校验通过（VB-Audio）。' -ForegroundColor Green

if ($CheckOnly) {
    Write-Host "只检查，没有安装。安装程序在 $($exe.FullName)"
    exit 0
}

Write-Host '启动安装程序：Windows 会弹管理员确认，点"是"；安装窗口里点 "Install Driver"。'
Start-Process -FilePath $exe.FullName -Verb RunAs -Wait

if (Test-Cable) {
    Write-Host '装好了：系统里已经有 CABLE Input / CABLE Output。建议重启一次电脑。' -ForegroundColor Green
    Write-Host '这台电脑没有别的麦克风时，CABLE Output 就是默认录音设备，Grok Bot 会直接用它。'
    Write-Host '然后测试（不会发送）：python bridge.py --voice-test 某个bot的完整名'
} else {
    Write-Host '还没检测到 CABLE 设备：如果安装窗口里已经点了 Install Driver，重启电脑后再看；否则重新运行本脚本。' -ForegroundColor Yellow
}
