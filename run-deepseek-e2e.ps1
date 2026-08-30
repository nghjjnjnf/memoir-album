$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectDir

Write-Host "请先在 DeepSeek 控制台撤销已经粘贴到聊天中的旧密钥，并生成新密钥。"
$SecureKey = Read-Host "请输入新密钥（输入不会显示，也不会写入文件）" -AsSecureString
$Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureKey)

try {
    $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    & .\.venv\Scripts\python.exe .\scripts\run_synthetic_e2e.py --provider deepseek
    if ($LASTEXITCODE -ne 0) {
        throw "DeepSeek 端到端测试未通过，退出码：$LASTEXITCODE"
    }
}
finally {
    Remove-Item Env:\DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
}

